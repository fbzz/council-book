"""M5-K: the operator's "why" screen (`council show <decision>` and the approval screen).

Per line with a leg it prints the public trail (`publish.trail`) over the SEALED, not yet revealed
cycle document in `state_dir/salts/`, then the ledger legs in percent and x. It reads public models
only, refuses a seal that does not open its commitment, flags a leg without a trail, and after the
reveal its trail blocks equal `council why` on the same document."""

from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from council.operator import approve as approve_mod
from council.operator import why
from council.publish import commit_reveal, journal, trail

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "site_journal" / "journal"
DONE = "2026-09-25T1440Z"


def _doc_bytes() -> bytes:
    return (FIXTURE.parent / journal.cycle_path(DONE)).read_bytes()


def _seal(state: Path, sealed: bytes) -> Path:
    salt = secrets.token_hex(32)
    record = commit_reveal.SealedCycle(salt=salt, sealed_hex=sealed.hex(),
                                       commitment_sha=commit_reveal.bytes_commitment_sha256(sealed, salt))
    return commit_reveal.save_sealed(record, state / "salts")


def _unrevealed_journal(tmp_path: Path) -> Path:
    """A copy of the fixture journal without the cycle document (and its reveal file)."""
    root = tmp_path / "public"
    shutil.copytree(FIXTURE, root / "journal")
    for p in (root / "journal").rglob(f"cycles/**/{DONE}*.json"):
        p.unlink()
    return root / "journal"


def _plan() -> SimpleNamespace:
    legs = [SimpleNamespace(seq=leg["seq"], kind=leg["kind"], line=leg["line"], symbol=f"V{leg['seq']}",
                            direction=leg["direction"], leverage=leg["leverage"],
                            weight_before=leg["weight_before_x"], weight_after=leg["weight_after_x"],
                            stop_distance=(leg["stop_distance_pct"] or 0) / 100 or None)
            for leg in json.loads(_doc_bytes())["plan"]["legs"]]
    return SimpleNamespace(legs=legs)


def _decision(cycle_id: str | None = DONE, kind: str = "rebalance") -> SimpleNamespace:
    return SimpleNamespace(cycle_id=cycle_id, kind=kind, decision_id=f"{DONE}-{kind}-abc123")


def _screen(state: Path, journal_dir: Path, **kw) -> tuple[list[str], list[str]]:
    out: list[str] = []
    missing = why.decision_why(_decision(**kw), _plan(), state_dir=state, journal_dir=journal_dir,
                               names={}, echo=out.append)
    return out, missing


def _blocks(text: list[str]) -> list[str]:
    """The trail lines of a screen (leg rows and headers removed)."""
    return [t for t in text if t and not t.startswith("   leg ") and "· source:" not in t
            and t != why.SEALED_NOTE]


@pytest.fixture
def sealed(tmp_path):
    state = tmp_path / "state"
    _seal(state, _doc_bytes())
    return state


def test_every_leg_line_gets_its_trail_then_its_legs_in_percent_and_x(sealed, tmp_path):
    out, missing = _screen(sealed, _unrevealed_journal(tmp_path))
    assert missing == []
    text = "\n".join(out)
    assert "source: sealed public document, not yet revealed" in text and why.SEALED_NOTE in text
    for leg in _plan().legs:
        assert f"   leg {leg.seq} {leg.kind} " in text
    assert "+13.4% → +6.7% of NAV (+0.134x → +0.067x)" in text          # SEMIS partial close
    assert "-6.2% of NAV" in text and "stop 6.0%" in text                # GBPUSD short open
    for key in {leg.line for leg in _plan().legs}:
        head = next(i for i, t in enumerate(out) if t.startswith(key + " ") or t.startswith(key))
        assert out[head + 1].strip(), f"{key} has an empty trail"


def test_after_the_reveal_the_trail_blocks_equal_council_why(sealed, tmp_path):
    before, _ = _screen(sealed, FIXTURE)                   # sealed file, same public outcome files
    revealed = why.journal_trails(FIXTURE, DONE, names={})
    assert revealed is not None
    lines = {leg.line for leg in _plan().legs}
    expected = [t for tr_ in revealed if tr_.line in lines for t in trail.render_trail(tr_)]
    assert _blocks(before) == expected
    why_text = trail.render_text(revealed)
    for block_line in expected:
        assert block_line in why_text


def test_without_a_seal_the_screen_reads_the_revealed_document(tmp_path):
    out, missing = _screen(tmp_path / "state", FIXTURE)
    assert missing == [] and "source: revealed public record" in "\n".join(out)
    assert why.SEALED_NOTE not in out


def test_neither_sealed_nor_revealed_flags_every_leg(tmp_path):
    out, missing = _screen(tmp_path / "state", _unrevealed_journal(tmp_path))
    assert missing == sorted({leg.line for leg in _plan().legs})
    assert "trail unavailable" in "\n".join(out)


def test_a_seal_that_does_not_open_its_commitment_is_never_shown(sealed, tmp_path):
    path = sealed / "salts" / f"{DONE}.json"
    record = json.loads(path.read_text())
    record["salt"] = secrets.token_hex(32)
    path.write_text(json.dumps(record))
    out, missing = _screen(sealed, _unrevealed_journal(tmp_path))
    assert missing and "does not open its commitment" in "\n".join(out)
    assert not any(t.startswith("   leg ") for t in out)


def test_a_decision_without_a_cycle_has_no_council_trail(sealed, tmp_path):
    out, missing = _screen(sealed, FIXTURE, cycle_id=None, kind="smoke")
    assert missing == [] and out == ["why: no council trail; this smoke comes from code, not from a council cycle"]


def test_a_leg_whose_line_has_no_trail_is_flagged(sealed, tmp_path):
    plan = _plan()
    plan.legs.append(SimpleNamespace(seq=9, kind="open", line="NOPE", symbol="X", direction="long", leverage=1,
                                     weight_before=0.0, weight_after=0.01, stop_distance=None))
    out: list[str] = []
    missing = why.decision_why(_decision(), plan, state_dir=sealed, journal_dir=FIXTURE, names={},
                               echo=out.append)
    assert missing == ["NOPE"] and why.NO_TRAIL in "\n".join(out)


def test_the_approval_screen_warns_and_never_raises(sealed, tmp_path, monkeypatch):
    printed: list[str] = []
    deps = approve_mod.ApprovalDeps(ledger=None, policy=None, read=None, write_factory=lambda: None,
                                    state_dir=sealed, print_fn=printed.append, journal_dir=FIXTURE)
    assert approve_mod.why_screen(_decision(), _plan(), deps) == []
    assert any(t.startswith("   leg 1 ") for t in printed)

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(why, "decision_why", boom)
    printed.clear()
    assert approve_mod.why_screen(_decision(), _plan(), deps) == []
    assert printed == ["WARNING: why trail unavailable (RuntimeError); reject unless you know why each line moves"]


def test_the_screen_reads_only_the_seal_and_public_files(sealed, tmp_path, monkeypatch):
    from council.ledger import db

    monkeypatch.setattr(why, "ledger_trails", lambda *a, **k: pytest.fail("the screen read the private record"))
    monkeypatch.setattr(db.Ledger, "get_cycle", lambda *a, **k: pytest.fail("the screen read the ledger record"))
    (sealed / "calls").mkdir()
    (sealed / "licensed").mkdir()
    (sealed / "calls").chmod(0)
    (sealed / "licensed").chmod(0)                  # private captures and licensed text: unreadable
    try:
        out, missing = _screen(sealed, _unrevealed_journal(tmp_path))
    finally:
        (sealed / "calls").chmod(0o700)
        (sealed / "licensed").chmod(0o700)
    assert missing == [] and out


def test_a_line_trimmed_by_r14_names_r14_on_the_screen(tmp_path):
    from council.policy import Policy
    from council.publish import redact
    from council.publish.commit_reveal import canonical_json
    from tests.fixtures import trail_records as tr

    universe = Policy.load(include_sleeve=False).universe
    base = {**tr.BOOK, "GOLD": 0.0}
    rec = tr.record(risk_decision=tr.risk(raw=dict(tr.REF_LEVELS), base=base, final={**base, "GOLD": 0.045},
                                          notes=["GOLD: R14 cycle cost budget"]),
                    the_plan=tr.plan({"GOLD": (0.0, 0.045)}), decision_state="proposed")
    lm = redact.LineMap(universe)
    vehicle = next(s for s in lm._map if lm._map[s] == "GOLD")
    rec.plan.legs[0].symbol = vehicle
    doc = redact.public_cycle(rec, None, lines=universe)
    assert doc.plan and doc.plan.legs
    state = tmp_path / "state"
    _seal(state, canonical_json(doc.model_dump(mode="json")))
    out: list[str] = []
    missing = why.decision_why(SimpleNamespace(cycle_id=rec.cycle_id, kind="rebalance"), rec.plan,
                               state_dir=state, journal_dir=tmp_path / "public" / "journal", names={},
                               echo=out.append)
    text = "\n".join(out)
    assert missing == [] and "(R14)" in text and "Risk engine" in text
    assert f"   leg 1 open {vehicle} " in text and "+0.0% → +4.5% of NAV" in text
