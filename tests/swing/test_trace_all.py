"""`council cycle --paper --trace-all`: every idea with a fact card goes through every swing stage;
the real outcome is recorded beside the traced one (stubs only, no LLM, no broker, no network)."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from council.cycle import SwingWideRefused, swing_trace_of
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.swing import council as sc
from tests.swing import stubs as s

MAIN = "deepseek-v4.1-flash:cloud"
OTHER = "glm-5.3-flash:cloud"
TICKERS = ["TAAA", "TBBB", "TCCC", "TDDD", "TEEE", "TFFF", "TGGG"]


def _responses(n: int, *, enter: tuple[str, ...]):
    reading = [s.news(f"P:{k + 1:08x}", symbols=[t]) for k, t in enumerate(TICKERS[:n])]
    ideas = [s.idea(t, catalysts=[f"P:{k + 1:08x}"]) for k, t in enumerate(TICKERS[:n])]

    def skeptic(user: str, rep: int):
        k = int(re.search(r"IDEA TO REVIEW: idea:(\d+)\n", user).group(1))
        return s.verdict(f"idea:{k}", line=TICKERS[k - 1], verdict="fail" if k == 3 else "pass",
                         priced_in="yes" if k == 3 else "partly")

    def pm(user: str, rep: int):
        refs = [r for r in re.findall(r"IDEA (idea:\d+):", user)]
        acts = [(r, "enter" if r in enter else "pass") for r in refs]
        out = s.pm(*acts, line=TICKERS[int(refs[0].split(":")[1]) - 1])
        for a in out["actions"]:                          # cite each idea's own card id
            a["evidence_ids"] = [f"X:{TICKERS[int(a['ref'].split(':')[1]) - 1]}:rev_yoy"]
        return out

    def bull(user: str, rep: int):
        ref = re.findall(r"IDEA (idea:\d+):", user)[0]
        return s.case(ref, line=TICKERS[int(ref.split(":")[1]) - 1])

    def bear(user: str, rep: int):
        ref = re.findall(r"IDEA (idea:\d+):", user)[0]
        return s.case(ref, line=TICKERS[int(ref.split(":")[1]) - 1], bear=True)

    main = {"scout": s.scout(*ideas), "swing_bull": bull, "swing_bear": bear, "swing_pm": pm}
    return reading, main, {"skeptic": skeptic}


def run_trace(policy, n=7, *, wide=7, trace=True, enter=("idea:1", "idea:2", "idea:3", "idea:6")):
    reading, main, sk = _responses(n, enter=enter)
    gw, skg = StubGateway(responses=main, model=MAIN), StubGateway(responses=sk, model=OTHER)
    cards = {"TBBB": s.card("TBBB", sigma=9.0)}                   # idea:2 is chased at the real gate
    gate = s.gate_with(cards, fail={"TDDD": "no_facts"})          # idea:4 has no fact card
    res = asyncio.run(sc.run_swing_stage(gw, PromptRegistry(), policy, s.inputs(reading=reading),
                                         gate=gate, skeptic_gw=skg, wide=wide, trace_all=trace))
    return res, gw, skg


def test_trace_all_runs_every_stage_for_every_carded_idea(policy):
    res, gw, skg = run_trace(policy)
    assert not [f for f in res.flags if f.startswith(("swing_error", "scout_failed"))], res.flags
    # 6 ideas with a card: every one gets a Skeptic call (the no-facts idea none)
    assert [c.role for c in skg.log].count("skeptic") == 6
    roles = [c.role for c in gw.log]
    reps = policy.swing.llm.pm_replicates
    assert roles.count("swing_bull") == 2 and roles.count("swing_bear") == 2      # 6 ideas -> 2 batches
    assert roles.count("swing_pm") == 2 * reps
    t = res.trace
    assert [b["refs"] for b in t["batches"]] == [["idea:1", "idea:2", "idea:3", "idea:5", "idea:6"], ["idea:7"]]
    ideas = t["ideas"]
    assert ideas["idea:4"]["real"]["code"] == "no_facts" and ideas["idea:4"]["traced"]["stage"] == "gate"
    assert ideas["idea:2"]["real"] == {"stage": "gate", "code": "chased", "note": ""}
    assert ideas["idea:2"]["traced"]["code"] == "enter"            # the Skeptic and the gate did not block
    assert ideas["idea:3"]["real"]["stage"] == "skeptic" and ideas["idea:3"]["traced"]["code"] == "enter"
    assert ideas["idea:7"]["traced"]["code"] == "pm_pass" and ideas["idea:7"]["batch"] == 1
    # the REAL aggregate: only ideas the real pipeline would have shown the PM
    assert {a.ref for a in res.entries()} <= {"idea:1", "idea:5", "idea:6"}
    assert "idea:2" not in {a.ref for a in res.entries()}
    assert {a.ref for a in res.trace_aggregate.entries()} == {"idea:1", "idea:2", "idea:3", "idea:6"}
    assert all(i.verdict is not None for r, i in res.ideas.items() if r != "idea:4")
    for o in res.outcomes:                                        # outcomes stay the REAL pipeline
        assert ideas[o.ref]["real"]["code"] in (o.code, "enter", "pm_pass")


def test_trace_limits_scale_and_default_is_untouched(policy):
    base = sc.swing_limits(policy)
    lim = sc.swing_limits(policy, None, True)
    assert lim.trace_all and lim.max_ideas == base.max_ideas == 5
    assert lim.max_calls == 1 + sc.trace_calls(5 + sc.TRACE_CARRIED_ROOM, policy.swing.llm.pm_replicates)
    assert lim.deadline_s > base.deadline_s
    assert sc.trace_batches(5) == 1 and sc.trace_batches(6) == 2 and sc.trace_batches(0) == 0
    res, gw, skg = run_trace(policy, 5, wide=None)
    assert [c.role for c in skg.log].count("skeptic") == 4        # 5 ideas, one without facts
    res, gw, skg = run_trace(policy, 5, wide=None, trace=False)    # the real slot: unchanged behaviour
    assert res.trace is None and res.trace_aggregate is None


def _ctx(tmp_path, *, mode="dry_run", publisher=None, broker=None, state="paper", trace=True):
    return SimpleNamespace(swing_trace_all=trace, settings=SimpleNamespace(mode=mode), publisher=publisher,
                           notifier=None, sources=SimpleNamespace(broker=broker), state_dir=tmp_path / state)


def test_only_a_paper_context_can_trace(tmp_path):
    assert swing_trace_of(_ctx(tmp_path)) is True
    assert swing_trace_of(_ctx(tmp_path, trace=False)) is False
    for bad in (_ctx(tmp_path, mode="live"), _ctx(tmp_path, publisher=object()), _ctx(tmp_path, broker=object()),
                _ctx(tmp_path, state="rehearsal")):
        with pytest.raises(SwingWideRefused):
            swing_trace_of(bad)
    with pytest.raises(SwingWideRefused):
        swing_trace_of(_ctx(tmp_path), live=True)


def test_cli_trace_all_needs_paper(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from council.cli import app

    monkeypatch.setenv("COUNCIL_MODE", "stub")
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path))
    for args in (["cycle", "--stub-llm", "--trace-all"], ["cycle", "--rehearsal", "--trace-all"],
                 ["cycle", "--dry-run", "--trace-all"]):
        res = CliRunner().invoke(app, args)
        assert res.exit_code == 2 and "--paper runs only" in res.output, (args, res.output)


# ------------------------------------------------------------------ a whole paper cycle + the report
@pytest.fixture(scope="module")
def traced_cycle(tmp_path_factory):
    """One stubbed PAPER cycle at a swing slot with --trace-all (no broker, no publisher, state dir
    named `paper`), then its report."""
    import json

    from council.cycle import run_cycle
    from council.ledger.db import Ledger
    from council.llm.stub import StubGateway as SG
    from council.policy import Policy
    from council.runtime import CycleContext, Sources
    from council.settings import Settings
    from council.swing import sources as ss
    from council.swing.report import write_report
    from tests.integration import test_end_to_end as e2e

    tmp = tmp_path_factory.mktemp("trace")
    state = tmp / "paper"
    state.mkdir()
    ledger = Ledger(state / "ledger.sqlite3", clock=lambda: e2e.NOW)
    ctx = CycleContext(policy=Policy.load(include_sleeve=False), settings=Settings(role="dev", mode="stub"),
                       ledger=ledger, gateway=SG(e2e.hold_reference_stub()), registry=PromptRegistry(),
                       sources=Sources(history=e2e._history, events=e2e._no_events, broker=None),
                       publisher=None, clock=lambda: e2e.NOW, state_dir=state)
    (state / "account").mkdir(exist_ok=True)
    (state / "account" / "swing.json").write_text(json.dumps({"funded_real_nav_usd": 2000.0}))
    ctx.sources.swing = ss.fixture_swing_sources(skeptic_gateway=ss.fixture_skeptic_gateway(ctx.policy))
    ctx.swing_trace_all = True
    out = run_cycle(ctx)
    path = write_report(state, out.cycle_id)
    return ctx, out, path


def test_the_paper_cycle_records_the_trace(traced_cycle):
    ctx, out, _ = traced_cycle
    rec = ctx.ledger.get_cycle(out.cycle_id)
    assert "swing_trace_all" in rec["flags"], rec["flags"]
    assert not [f for f in rec["flags"] if f.startswith(("swing_error", "swing_trace_error"))], rec["flags"]
    tr = rec["extras"]["swing"]["trace"]
    assert tr["ideas"] and all(i["real_gate_outcome"]["stage"] and i["traced_outcome"]["stage"] for i in tr["ideas"])
    assert tr["inputs"]["reading"] and tr["budget"]["swing_pct"] + tr["budget"]["core_pct"] == 100
    licensed = [r for r in tr["inputs"]["reading"] if r["licensed"]]
    assert licensed and all("title" not in r and "summary" not in r for r in licensed)   # never in the ledger
    assert rec.get("pm") is not None and rec.get("risk") is not None                      # the core decision


def test_the_report_renders_every_section_offline(traced_cycle):
    import re as _re
    import stat

    ctx, out, path = traced_cycle
    assert path == ctx.state_dir / "reports" / f"{out.cycle_id}.html"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    html = path.read_text()
    for sid in ("summary", "inputs", "scout", "gate", "skeptic", "debate", "pm", "rules", "core"):
        assert f'id="{sid}"' in html, sid
    for word in ("What it chose", "real gate outcome", "traced outcome", "Reading list", "Movers screen",
                 "Bull", "Bear", "Swing budget", "Core target weights", "Risk engine", "PRIVATE"):
        assert word in html, word
    for col in ("#3fc7a0", "#f07a5f", "#a68bfa", "#7fa7c9", "#e3b341", "#54a31b", "#3fb8e6", "#e05fb0"):
        assert col in html
    refs = _re.findall(r'(?:src|href)\s*=\s*["\']([^"\']*)', html, flags=_re.I)
    assert all(r.startswith("#") for r in refs), refs
    assert "<script" not in html.lower() and "@import" not in html and "url(" not in html


def test_the_report_cli_refuses_a_non_paper_state_dir(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from council.cli import app

    monkeypatch.setenv("COUNCIL_MODE", "stub")
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path))
    live = tmp_path / "live"
    live.mkdir()
    res = CliRunner().invoke(app, ["paper", "report", "c1", "--state-dir", str(live)])
    assert res.exit_code == 2 and "paper state dir" in res.output, res.output


def test_the_report_cli_writes_the_file_and_warns(traced_cycle, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from council.cli import app

    ctx, out, _ = traced_cycle
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    target = tmp_path / "r.html"
    res = CliRunner().invoke(app, ["paper", "report", out.cycle_id, "--state-dir", str(ctx.state_dir),
                                   "--out", str(target)])
    assert res.exit_code == 0, res.output
    assert target.exists() and "never publish" in res.output
