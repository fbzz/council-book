"""The council's call log: every call that ran is recorded, even when the caller cancels the
council (the cycle's overall timeout), so the public per-agent log is complete."""

from __future__ import annotations

import asyncio

from council.deliberation.council import run_council
from council.llm.stub import StubGateway
from council.models.cycle import RoleCall

from .factories import (
    REF_LEVELS,
    SLOT,
    build_bands,
    build_pack,
    build_ref,
    clip_enforce,
    stub_responses,
)


class _Instant:
    async def __call__(self, s: float) -> None:
        return None


class _HangingPM(StubGateway):
    """The PM never answers: the caller's timeout cancels the council mid-stage."""

    async def complete(self, **kw):
        if kw["role"] == "pm":
            await asyncio.sleep(60)
        return await super().complete(**kw)


def _run(gw, reg, policy, call_log: list[RoleCall], timeout: float | None = None):
    lines = list(policy.universe.lines)
    hints = {ln.symbol: {"per_side_bps": 5.0, "carry_bps_day": 0.0} for ln in lines}
    coro = run_council(
        gw=gw, reg=reg, pack=build_pack(), ref=build_ref(), bands=build_bands(),
        current_levels=dict(REF_LEVELS), cost_hints=hints, lines=lines, policy=policy,
        enforce=clip_enforce, now=SLOT, sleep=_Instant(), run_macro=True, call_log=call_log,
    )
    return asyncio.run(asyncio.wait_for(coro, timeout) if timeout else coro)


def test_the_call_log_holds_every_call_of_a_finished_council(reg, policy):
    log: list[RoleCall] = []
    res = _run(StubGateway(stub_responses()), reg, policy, log)
    assert [c.role for c in log] == [c.role for c in res.calls]
    roles = [c.role for c in log]
    for role in ("news", "macro", "bull_open", "bear", "bull_rebuttal"):
        assert roles.count(role) == 1, role
    assert roles.count("pm") == 3 and roles.count("single_agent") == 3


def test_a_cancelled_council_keeps_the_calls_that_ran(reg, policy):
    log: list[RoleCall] = []
    try:
        _run(_HangingPM(stub_responses()), reg, policy, log, timeout=0.5)
    except TimeoutError:
        pass
    else:  # pragma: no cover - the PM never answers
        raise AssertionError("the council should have been cancelled")
    assert [c.role for c in log] == ["news", "macro", "bull_open", "bear", "bull_rebuttal"]
    assert all(c.status == "ok" for c in log)
