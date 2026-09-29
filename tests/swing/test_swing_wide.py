"""`council cycle --paper --ideas N`: a WIDE paper swing slot (stubs only, no LLM, no broker)."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from council.cycle import SwingWideRefused, swing_wide_of
from council.invariants import SWING_MAX_LLM_CALLS_PER_SLOT
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.swing import council as sc
from tests.swing import stubs as s

MAIN = "deepseek-v4.1-flash:cloud"
OTHER = "glm-5.3-flash:cloud"
TICKERS = [f"W{chr(65 + k // 26)}{chr(65 + k % 26)}" for k in range(20)]


def wide_responses(n: int):
    reading = [s.news(f"P:{k + 1:08x}", symbols=[t]) for k, t in enumerate(TICKERS[:n])]
    ideas = [s.idea(t, catalysts=[f"P:{k + 1:08x}"]) for k, t in enumerate(TICKERS[:n])]

    def skeptic(user: str, rep: int):
        k = int(re.search(r"IDEA TO REVIEW: idea:(\d+)\n", user).group(1))
        return s.verdict(f"idea:{k}", line=TICKERS[k - 1])

    def pm(user: str, rep: int):
        return s.pm(("idea:1", "enter"), line=TICKERS[0])

    main = {"scout": s.scout(*ideas), "swing_bull": s.case(line=TICKERS[0]),
            "swing_bear": s.case(line=TICKERS[0], bear=True), "swing_pm": pm}
    return reading, main, {"skeptic": skeptic}


def run_wide(policy, n: int, wide: int | None):
    reading, main, sk = wide_responses(n)
    gw, skg = StubGateway(responses=main, model=MAIN), StubGateway(responses=sk, model=OTHER)
    res = asyncio.run(sc.run_swing_stage(gw, PromptRegistry(), policy, s.inputs(reading=reading),
                                         gate=s.gate_with({}), skeptic_gw=skg, wide=wide))
    return res, gw, skg


def test_wide_20_accepts_20_ideas_and_the_skeptic_reviews_all(policy):
    res, gw, skg = run_wide(policy, 20, 20)
    assert not [f for f in res.flags if f.startswith(("swing_error", "scout_failed", "budget_"))], res.flags
    assert [c.role for c in skg.log].count("skeptic") == 20
    assert len(res.verdicts()) == 20 and all(v.status == "pass" for v in res.verdicts())
    assert [a.ref for a in res.entries()] == ["idea:1"]
    lim = sc.swing_limits(policy, 20)
    assert res.calls_used == 1 + 20 + 2 + policy.swing.llm.pm_replicates <= lim.max_calls
    assert lim.deadline_s == policy.swing.llm.deadline_s + 45 * 20


def test_wide_prompt_says_n(policy):
    from council.deliberation.common import prompt_context

    ctx = sc.swing_prompt_context(policy, sc.swing_limits(policy, 20))
    assert ctx["max_ideas"] == 20
    text = PromptRegistry().render("scout", **prompt_context(policy), **ctx)
    assert "at most 20 swing ideas" in text and "0 to 20 objects" in text
    default = PromptRegistry().render("scout", **prompt_context(policy), **sc.swing_prompt_context(policy))
    assert "at most 5 swing ideas" in default


def test_default_stays_5_3_9_and_rejects_a_sixth_idea(policy):
    lim = sc.swing_limits(policy)
    assert (lim.max_ideas, lim.max_skeptic, lim.max_calls, lim.deadline_s) == (
        5, policy.swing.llm.max_skeptic_calls, policy.swing.llm.max_calls_per_slot, policy.swing.llm.deadline_s)
    assert lim.max_calls <= SWING_MAX_LLM_CALLS_PER_SLOT
    assert sc.swing_prompt_context(policy)["max_ideas"] == 5
    assert sc.scout_schema(lim) is sc.ScoutOutput
    res, _gw, skg = run_wide(policy, 6, None)           # 6 ideas: the default schema refuses the reply
    assert any(f.startswith("scout_failed") for f in res.flags) and not skg.log
    res, _gw, skg = run_wide(policy, 5, None)
    assert [c.role for c in skg.log].count("skeptic") == policy.swing.llm.max_skeptic_calls
    assert res.calls_used <= SWING_MAX_LLM_CALLS_PER_SLOT


@pytest.mark.parametrize("bad", [0, 21, -1, True, 2.5, "5"])
def test_wide_out_of_range_is_refused(policy, bad):
    with pytest.raises(ValueError):
        sc.swing_limits(policy, bad)


def _ctx(tmp_path, *, mode="dry_run", publisher=None, broker=None, state="paper", wide=20):
    return SimpleNamespace(swing_wide=wide, settings=SimpleNamespace(mode=mode), publisher=publisher,
                           notifier=None, sources=SimpleNamespace(broker=broker), state_dir=tmp_path / state)


def test_only_a_paper_context_can_be_wide(tmp_path):
    assert swing_wide_of(_ctx(tmp_path)) == 20
    assert swing_wide_of(_ctx(tmp_path, wide=None)) is None
    assert swing_wide_of(_ctx(tmp_path, mode="live", wide=None)) is None
    for bad in (_ctx(tmp_path, mode="live"), _ctx(tmp_path, publisher=object()), _ctx(tmp_path, broker=object()),
                _ctx(tmp_path, state="rehearsal")):
        with pytest.raises(SwingWideRefused):
            swing_wide_of(bad)
    with pytest.raises(SwingWideRefused):
        swing_wide_of(_ctx(tmp_path), live=True)


def test_cli_ideas_needs_paper_and_a_range(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from council.cli import app

    monkeypatch.setenv("COUNCIL_MODE", "stub")
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path))
    for args in (["cycle", "--stub-llm", "--ideas", "20"], ["cycle", "--rehearsal", "--ideas", "3"],
                 ["cycle", "--dry-run", "--ideas", "3"]):
        res = CliRunner().invoke(app, args)
        assert res.exit_code == 2 and "--paper runs only" in res.output, (args, res.output)
    for n in ("0", "21"):
        res = CliRunner().invoke(app, ["cycle", "--paper", "--stub-llm", "--ideas", n])
        assert res.exit_code == 2 and "1..20" in res.output


def test_paper_context_is_the_one_setter(monkeypatch, tmp_path):
    import council.context as C
    from council import cli
    from council.settings import Settings

    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(C, "read_broker", lambda _s: (_ for _ in ()).throw(AssertionError("broker")))
    settings = Settings(role="dev", mode="stub")
    ctx = cli.paper_context(settings, stub_llm=True, ideas=20)
    assert ctx.swing_wide == 20 and swing_wide_of(ctx) == 20
    assert cli.paper_context(settings, stub_llm=True).swing_wide is None
