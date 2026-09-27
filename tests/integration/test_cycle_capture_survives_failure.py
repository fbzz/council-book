"""What each agent saw is written as soon as the council returns: a failure later in the cycle (the
risk engine, the plan, the publish) must not lose the capture, which is exactly when the operator
needs it (review of stage 2)."""

from __future__ import annotations

import contextlib

from council import clock
from council import cycle as cycle_mod
from council.cycle import run_cycle
from council.deliberation.capture import calls_path, load_inputs
from council.runtime import Sources
from tests.integration.test_end_to_end import NOW, _ctx, _history, _no_events


def test_a_risk_engine_crash_keeps_the_capture(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, publisher=None, clock=lambda: NOW)
    ctx.sources = Sources(history=_history, events=_no_events, news=None)

    def boom(*_a, **_k):
        raise RuntimeError("engine failure after the council")

    monkeypatch.setattr(cycle_mod, "_evaluate", boom)
    with contextlib.suppress(RuntimeError):
        run_cycle(ctx)
    cycle_id = clock.classify(NOW).cycle_id
    assert calls_path(ctx.state_dir, cycle_id).is_file(), "no capture was written before the failure"
    inputs = load_inputs(ctx.state_dir, cycle_id)
    assert {c.role for c in inputs.calls} >= {"bull_open", "bear", "pm"}
