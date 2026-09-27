"""`council why <cycle> [<line>]` and `council show <id> --why` (transparency-v2 T5a-core).

- On a revealed cycle (the public journal) anyone may run it: public models only.
- The private ledger source (richer: drops, the bands before the cards, fall-back lines) runs only
  in the operator's terminal; `--source ledger` under CLAUDECODE=1 is refused before anything is read.
- An unrevealed cycle is refused outside the operator's terminal (exit 2).
- The words printed are exactly the words `publish.trail` builds.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from council import cli
from council.ledger.db import LEDGER_FILE, Ledger
from council.operator import why
from council.publish import trail
from tests.fixtures import trail_records as tr

JOURNAL = Path(__file__).resolve().parents[1] / "fixtures" / "site_journal" / "journal"
DONE = "2026-09-25T1440Z"
runner = CliRunner()


def invoke(argv: list[str], **env: str):
    return runner.invoke(cli.app, argv, env=env or None)


def journal_text(cycle_id: str, line: str | None = None) -> str:
    found = why.journal_trails(JOURNAL, cycle_id, names=why.line_names())
    assert found is not None
    header = f"{cycle_id} · why each line moved · source: revealed public record"
    return trail.render_text(found, header=header, line=line)


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")


# ------------------------------------------------------------------------------ journal source
def test_why_on_a_revealed_cycle_prints_the_builders_words(agent):
    res = invoke(["why", DONE, "--journal", str(JOURNAL)])
    assert res.exit_code == 0, res.output
    assert res.output == journal_text(DONE)
    assert "SEMIS" in res.output and "outcome: changed" in res.output
    assert "NVDA" in res.output and "outcome: reference_trade" in res.output


def test_show_why_equals_why_and_takes_a_decision_id(agent):
    a = invoke(["show", DONE, "--why", "--journal", str(JOURNAL)])
    b = invoke(["show", f"{DONE}-rebalance-d4e5f6", "--why", "--journal", str(JOURNAL)])
    assert a.exit_code == b.exit_code == 0
    assert a.output == b.output == journal_text(DONE)


def test_one_line(agent):
    res = invoke(["why", DONE, "SEMIS", "--journal", str(JOURNAL)])
    assert res.exit_code == 0 and res.output == journal_text(DONE, "SEMIS")
    assert "GBPUSD" not in res.output
    held = invoke(["why", DONE, "NDX", "--journal", str(JOURNAL)])
    assert held.exit_code == 0 and "held the reference; nobody asked" in held.output
    missing = invoke(["show", DONE, "--why", "--line", "XYZ", "--journal", str(JOURNAL)])
    assert missing.exit_code == 1


def test_why_prints_a_trail_for_every_changed_line_of_every_fixture_cycle(agent):
    for path in sorted((JOURNAL / "cycles").rglob("*Z.json")):
        doc = json.loads(path.read_text())
        risk = doc["risk"]
        changed = [s for s in set(risk["base_x"]) | set(risk["final_x"])
                   if abs(risk["base_x"].get(s, 0.0) - risk["final_x"].get(s, 0.0)) > 1e-9]
        res = invoke(["why", path.stem, "--journal", str(JOURNAL)])
        assert res.exit_code == 0, res.output
        for line in changed:
            assert any(text.startswith(f"{line} ") and "outcome:" in text for text in res.output.splitlines()), \
                (path.stem, line)


def test_an_unrevealed_cycle_is_refused_in_an_agent_context_before_the_ledger_is_read(agent, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("the ledger must not be read in an agent context")

    monkeypatch.setattr(why, "ledger_trails", boom)
    for argv in (["why", "2026-09-26T1440Z", "--journal", str(JOURNAL)],
                 ["show", "2026-09-26T1440Z-rebalance-abc123", "--why", "--journal", str(JOURNAL)]):
        res = invoke(argv)
        assert res.exit_code == 2, res.output
        assert "refused" in res.output and "not revealed" in res.output


def test_ledger_source_is_operator_only(agent, monkeypatch):
    monkeypatch.setattr(why, "ledger_trails", lambda *a, **k: pytest.fail("read before the guard"))
    res = invoke(["why", DONE, "--source", "ledger", "--journal", str(JOURNAL)])
    assert res.exit_code == 2 and "CLAUDECODE is set" in res.output


def test_bad_arguments():
    assert invoke(["why", "yesterday"]).exit_code == 2
    assert invoke(["why", DONE, "--source", "site"]).exit_code == 2


def test_the_command_is_registered():
    assert "why" in {c.name for c in cli.app.registered_commands}


# ------------------------------------------------------------------------------ ledger source
def _ledger_with(record, *, reject: str | None = None, fill: bool = False) -> Ledger:
    from council.paths import state_dir

    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(root / LEDGER_FILE)
    ledger.migrate()
    ledger.record_cycle(record, now=tr.SLOT)
    if record.decision_id:
        ledger.create_decision(decision_id=record.decision_id, kind="rebalance", cycle_id=record.cycle_id,
                               valid_until=tr.SLOT + timedelta(hours=3), plan=record.plan,
                               state="awaiting_publication", now=tr.SLOT)
        ledger.insert_legs(record.decision_id, record.plan.legs)
        ledger.transition(record.decision_id, "proposed", "sealed", now=tr.SLOT)
        if reject:
            ledger.transition(record.decision_id, "rejected", reject, actor="operator", now=tr.SLOT)
        if fill:
            at = tr.SLOT + timedelta(minutes=20)
            ledger.transition(record.decision_id, "approved", "ok", actor="operator", now=at)
            ledger.transition(record.decision_id, "executing", "go", now=at)
            ledger.update_leg(record.decision_id, 1, state="submitting", now=at)
            ledger.update_leg(record.decision_id, 1, state="filled", now=at)
            ledger.transition(record.decision_id, "completed", "done", now=at)
            ledger.set_runtime(f"exec_report:{record.decision_id}",
                               {"report": {"reconcile": {"achieved_w": {"SEMIS": 0.0668}}}})
    return ledger


def test_operator_reads_the_ledger_with_the_current_decision(as_operator):
    _ledger_with(tr.cut_executed(), reject="Wait for a second reading.")
    res = invoke(["why", tr.CYCLE, "SEMIS"])
    assert res.exit_code == 0, res.output
    assert why.PRIVATE_NOTE in res.output and "source: ledger" in res.output
    assert "outcome: rejected" in res.output and "“Wait for a second reading.”" in res.output


def test_operator_sees_the_fill_and_the_achieved_weight(as_operator):
    _ledger_with(tr.cut_executed(), fill=True)
    res = invoke(["show", f"{tr.CYCLE}-rebalance-abc123", "--why", "--line", "SEMIS"])
    assert res.exit_code == 0, res.output
    assert "outcome: changed" in res.output
    assert "filled; achieved 6.7%" in res.output and "approved (slot 14:40 UTC)" in res.output


def test_operator_output_equals_the_builders_words(as_operator):
    ledger = _ledger_with(tr.bear_asked_pm_held())
    res = invoke(["why", tr.CYCLE])
    found = why.ledger_trails(ledger.path.parent, tr.CYCLE, names=why.line_names())
    expected = trail.render_text(found, header=f"{tr.CYCLE} · why each line moved · source: ledger\n"
                                               f"{why.PRIVATE_NOTE}")
    assert res.exit_code == 0 and res.output == expected
    assert "code would not have allowed a cut to 0.50" in res.output


def test_operator_falls_back_to_the_journal_without_a_ledger_record(as_operator):
    res = invoke(["why", DONE, "--journal", str(JOURNAL)])
    assert res.exit_code == 0 and res.output == journal_text(DONE)


def test_operator_journal_source_skips_the_ledger(as_operator, monkeypatch):
    monkeypatch.setattr(why, "ledger_trails", lambda *a, **k: pytest.fail("journal source read the ledger"))
    res = invoke(["why", DONE, "--source", "journal", "--journal", str(JOURNAL)])
    assert res.exit_code == 0 and res.output == journal_text(DONE)


def test_rehearsal_ledger(as_operator):
    from council.paths import state_dir

    root = state_dir() / "rehearsal"
    root.mkdir(parents=True)
    Ledger(root / LEDGER_FILE).record_cycle(tr.rehearsal(), now=tr.SLOT)
    res = invoke(["why", tr.CYCLE, "NDX", "--rehearsal"])
    assert res.exit_code == 0, res.output
    assert "outcome: not_ordered" in res.output and trail.REHEARSAL_PLAN in res.output
