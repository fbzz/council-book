"""WP-H end to end, offline (stub LLM, no broker, no publisher): an earnings event from the cycle's
event source reaches the fact pack, the council's bands (no adds on that stock) and the engine's
R16 box, on that stock only. `invariants.STOCK_SLEEVE_LIVE` stays False (the sleeve policy is handed
to the context directly, never through `build_context`)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from council.cycle import run_cycle
from council.models.facts import EventItem
from council.runtime import Sources
from tests.integration.test_end_to_end import SLOT, _ctx


def _history_for(policy):
    def history(slot):
        rng = np.random.default_rng(11)
        out = {}
        for line in policy.universe.lines:
            crypto = line.asset_class == "crypto"
            end = pd.Timestamp((slot - timedelta(days=1)).date())
            idx = pd.date_range(end=end, periods=500, freq="D") if crypto else pd.bdate_range(end=end, periods=500)
            idx = pd.DatetimeIndex(idx).tz_localize("UTC")
            r = 0.0012 + rng.normal(0, 0.03 if crypto else 0.009, len(idx))
            close = 100 * np.exp(np.cumsum(r))
            openp = np.concatenate([[close[0]], close[:-1]])
            out[line.symbol] = pd.DataFrame({"open": openp, "high": np.maximum(openp, close) * 1.002,
                                             "low": np.minimum(openp, close) * 0.998, "close": close,
                                             "volume": 1000.0}, index=idx)
        return out, []
    return history


def test_an_earnings_event_blocks_adds_on_its_stock_only(tmp_path, sleeve_policy):
    report = SLOT + timedelta(hours=20)                                  # confirmed for tonight
    event = EventItem(id=f"E:earnings:TSTA@{report.date().isoformat()}", kind="earnings", at_utc=report,
                      symbols=["TSTA"], severity=2, source="etoro_feed", known_at=SLOT - timedelta(days=3))

    def events(start, end):
        return [event], []

    ctx = _ctx(tmp_path)
    ctx.policy = sleeve_policy
    ctx.sources = Sources(history=_history_for(sleeve_policy), events=events)
    out = run_cycle(ctx)
    assert out.status == "on_time"
    record = ctx.ledger.get_cycle(out.cycle_id)
    assert "event window: no adds" in record["bands"]["TSTA"]["reasons"]
    assert all("event window" not in " ".join(record["bands"][s]["reasons"]) for s in ("TSTB", "TSTC_B", "F", "NDX"))
    risk = record["risk"]
    assert record["reference"]["entries"]["TSTA"]["level_ref"] == 1.0    # the rule wants it ...
    assert risk["banded_levels"]["TSTA"] == 0.0 and risk["final_w"]["TSTA"] == 0.0   # ... R16 keeps it out
    assert not any("R16" in r for r in risk["hold_reasons"])
    assert any(risk["final_w"][s] > 0 for s in ("TSTB", "TSTC_B", "F"))  # the other stocks are bought
    assert any(event.id in c["evidence_ids"] and c["scope"] == ["TSTA"] for c in record["cards"])   # in the pack
