"""`council cycle --paper --publish`: the paper run's PUBLIC record (journal/paper/...) through the
public pipeline. Stubs only: no LLM, no broker, no network; fixtures are synthetic."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from council.models.cycle import CycleRecord
from council.publish import commit_reveal, leakscan
from council.publish import paper as P

SYNTH_RSS = "Acme Corp. (NASDAQ: ACME) schedules an investor day"     # the fixture's synthetic N: headline


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    """One stubbed PAPER --trace-all cycle with --publish into a temporary repo dir."""
    from council.cycle import run_cycle
    from council.ledger.db import Ledger
    from council.llm.prompts import PromptRegistry
    from council.llm.stub import StubGateway as SG
    from council.policy import Policy
    from council.runtime import CycleContext, Sources
    from council.settings import Settings
    from council.swing import sources as ss
    from tests.integration import test_end_to_end as e2e

    tmp = tmp_path_factory.mktemp("paperpub")
    state, repo = tmp / "paper", tmp / "repo"
    state.mkdir()
    repo.mkdir()
    ledger = Ledger(state / "ledger.sqlite3", clock=lambda: e2e.NOW)
    ctx = CycleContext(policy=Policy.load(include_sleeve=False), settings=Settings(role="dev", mode="stub"),
                       ledger=ledger, gateway=SG(e2e.hold_reference_stub()), registry=PromptRegistry(),
                       sources=Sources(history=e2e._history, events=e2e._no_events, broker=None),
                       publisher=None, clock=lambda: e2e.NOW, state_dir=state)
    (state / "account").mkdir(exist_ok=True)
    (state / "account" / "swing.json").write_text(json.dumps({"funded_real_nav_usd": 2000.0}))
    ctx.sources.swing = ss.fixture_swing_sources(skeptic_gateway=ss.fixture_skeptic_gateway(ctx.policy))
    ctx.swing_trace_all = True
    ctx.paper_publish_dir = repo
    out = run_cycle(ctx)
    return ctx, out, repo


def _files(repo: Path) -> dict[str, bytes]:
    return {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


def _record(ctx, cycle_id) -> CycleRecord:
    return CycleRecord.model_validate(ctx.ledger.get_cycle(cycle_id))


def test_the_paper_cycle_is_published_sealed_and_revealed(published):
    ctx, out, repo = published
    assert "paper_published:1" in out.flags, out.flags
    files = _files(repo)
    cyc = P.cycle_file(out.cycle_id)
    assert {cyc, P.reveal_file(out.cycle_id), P.DECISIONS_PATH, P.LATEST_PATH} <= set(files)
    sealed = files[cyc]
    doc = P.PublicPaperCycle.model_validate_json(sealed)
    assert doc.paper is True and doc.decision_no == 1 and doc.swing is not None and doc.swing.trace_all
    rev = json.loads(files[P.reveal_file(out.cycle_id)])
    assert commit_reveal.verify_bytes(sealed, rev["salt"], rev["commitment_sha256"])
    row = json.loads(files[P.DECISIONS_PATH].decode().splitlines()[0])
    assert row["commitment_sha256"] == rev["commitment_sha256"] and row["mode"] == "paper"
    # the trace-all data: real vs traced outcome, Skeptic verdicts, debate, every PM replicate, budget
    assert all(i.real_outcome.stage != "unknown" and i.traced_outcome.stage != "unknown" for i in doc.swing.ideas)
    assert any(i.idea.verdict is not None for i in doc.swing.ideas)
    assert doc.swing.batches and doc.swing.batches[0].replicates and doc.swing.batches[0].bull is not None
    assert doc.swing.budget is not None and doc.swing.inputs.reading
    assert doc.core.pm is not None and doc.core.risk is not None          # the core council's public decision
    latest = P.PublicPaperLatest.model_validate_json(files[P.LATEST_PATH])
    assert latest.decision_no == 1 and latest.core and latest.swing_pct + latest.core_pct == 100


def test_no_licensed_text_price_unit_or_path_in_any_published_paper_file(published):
    ctx, out, repo = published
    for name, data in _files(repo).items():
        assert leakscan.scan_bytes(name, data, licensed_texts=[SYNTH_RSS]) == [], name
        text = data.decode()
        assert SYNTH_RSS not in text and "investor day" not in text
        for word in ("open_rate", "units", "position_id", "instrument_id", "amount", "nav_usd", "funded_real",
                     "cost_rt", "/Users/", "$"):
            assert word not in text, (name, word)
    doc = P.PublicPaperCycle.model_validate_json(_files(repo)[P.cycle_file(out.cycle_id)])
    n_items = [r for r in doc.swing.inputs.reading if r.item.id.startswith("N:")]
    assert n_items and all(r.item.title is None and r.item.link is None for r in n_items)
    assert all(r.item.kind == "licensed_news" and r.item.source for r in n_items)     # id + feed label only


def test_agent_text_quoting_licensed_text_is_withheld(published):
    ctx, out, _ = published
    rec = _record(ctx, out.cycle_id)
    sw = copy.deepcopy(rec.extras["swing"])
    sw["ideas"][0]["thesis"] = "As the release said, " + SYNTH_RSS.lower() + " next month in town"
    part = P.paper_swing(sw, cycle_id=out.cycle_id, licensed_texts=[], own_texts=[SYNTH_RSS])
    blob = part.model_dump_json()
    assert "investor day" not in blob and "withheld" in blob
    blocked = P.paper_swing(sw, cycle_id=out.cycle_id, licensed_texts=[], own_texts=None)
    assert "investor day" not in blocked.model_dump_json()


def test_three_nav_invariance_of_the_paper_public_json(published):
    """$1.5k (below the fee reference), $2k and $20k paper NAVs: the private NAV-dependent fields
    (actual round-trip cost, the funded NAV) change; the public JSON does not."""
    ctx, out, _ = published
    rec = _record(ctx, out.cycle_id)
    docs = []
    for nav in (1500.0, 2000.0, 20000.0):
        extras = copy.deepcopy(rec.extras)
        for i in extras["swing"]["trace"]["ideas"]:
            for k in ("paper_leg", "real_leg"):
                if i.get(k) and i[k].get("ok"):
                    i[k]["cost_rt_pct"] = round(2.5 + 300.0 / nav, 3)
                    i[k]["nav_usd"] = nav
        extras["funded_real_nav_usd"] = nav
        r = rec.model_copy(update={"extras": extras})
        doc = P.build_paper_cycle(r, None, lines=ctx.policy.universe, decision_no=1, install_key=b"k" * 32,
                                  own_texts=[SYNTH_RSS])
        docs.append(commit_reveal.canonical_json(doc))
    assert docs[0] == docs[1] == docs[2]
    for nav in ("1500", "20000"):
        assert nav not in docs[0].decode()


def test_decision_numbering_is_stable_and_append_only(published, tmp_path):
    ctx, out, _ = published
    rec = _record(ctx, out.cycle_id)
    repo = tmp_path / "repo"
    slot = rec.slot

    def publish(k: int) -> tuple[int, bytes]:
        cid = (slot + timedelta(hours=4 * k)).strftime("%Y-%m-%dT%H%MZ")
        r = rec.model_copy(update={"cycle_id": cid, "slot": slot + timedelta(hours=4 * k)})
        files, doc = P.paper_files(lambda no: P.build_paper_cycle(r, None, lines=ctx.policy.universe, decision_no=no,
                                                                  own_texts=[]), repo, cid,
                                   sealed_at=datetime(2026, 10, 1, tzinfo=UTC))
        P.write(repo, files)
        return doc.decision_no, files[P.DECISIONS_PATH]

    n1, d1 = publish(0)
    n2, d2 = publish(1)
    n3, d3 = publish(2)
    assert (n1, n2, n3) == (1, 2, 3)
    assert d2.startswith(d1) and d3.startswith(d2)                     # earlier rows kept byte for byte
    again, d2b = publish(1)                                            # a forced re-run keeps its number
    assert again == 2 and len(d2b.decode().splitlines()) == 3
    assert d2b.decode().splitlines()[0] == d3.decode().splitlines()[0]
    assert d2b.decode().splitlines()[2] == d3.decode().splitlines()[2]
    assert P.number_for(P.read_rows(repo), "2030-01-01T0000Z") == 4
    with pytest.raises(P.PaperPublishError):                           # a number is never reassigned
        P.decisions_bytes(d3, P.PublicPaperDecisionRow.model_validate(
            {**json.loads(d3.decode().splitlines()[0]), "cycle_id": "2030-01-01T0000Z"}))


def test_paper_portfolio_counts_open_legs_and_closed_paper_pnl():
    rows = [{"decision_no": 1, "cycle_id": "2026-09-29T1840Z", "slot": "2026-09-29T18:40:00Z",
             "chosen": [{"ticker": "ACME", "side": "long", "size_nav_pct": 8.0, "time_stop_date": "2026-10-20"},
                        {"ticker": "WIDG", "side": "short", "size_nav_pct": 6.0, "time_stop_date": "2026-10-20"}]}]
    paper_rows = [{"origin_cycle": "2026-09-29T1840Z", "ticker": "ACME", "side": "long", "status": "closed",
                   "ret_pct": 10.0}]
    from council.publish.public_models import PublicCycleV1

    core = PublicCycleV1.model_validate({"cycle_id": "2026-10-01T1840Z", "slot": "2026-10-01T18:40:00Z",
                                         "status": "dry_run", "late_by_min": 0, "policy_sha": "", "model": "m"})
    doc = P.PublicPaperCycle(decision_no=2, cycle_id="2026-10-01T1840Z", slot=core.slot, core=core)
    latest = P.paper_latest(doc, rows, paper_rows, today=date(2026, 10, 1))
    assert [h.ticker for h in latest.swing_open] == ["WIDG"] and latest.swing_pct == 6.0
    assert latest.performance.swing_closed_legs == 1 and latest.performance.swing_return_pct == 0.8
    assert latest.performance.since == date(2026, 9, 29)


def test_publish_is_refused_off_paper(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from council.cli import app
    from council.cycle import SwingWideRefused, paper_publish_of

    ok = SimpleNamespace(paper_publish_dir=tmp_path, settings=SimpleNamespace(mode="dry_run"), publisher=None,
                         notifier=None, sources=SimpleNamespace(broker=None), state_dir=tmp_path / "paper")
    assert paper_publish_of(ok) == tmp_path
    for bad in ({"publisher": object()}, {"settings": SimpleNamespace(mode="live")}, {"state_dir": tmp_path / "x"},
                {"sources": SimpleNamespace(broker=object())}):
        with pytest.raises(SwingWideRefused):
            paper_publish_of(SimpleNamespace(**{**vars(ok), **bad}))
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path))
    for args in (["cycle", "--stub-llm", "--publish"], ["cycle", "--rehearsal", "--publish"],
                 ["cycle", "--paper", "--stub-llm", "--publish-dir", str(tmp_path)]):
        res = CliRunner().invoke(app, args)
        assert res.exit_code == 2, (args, res.output)
