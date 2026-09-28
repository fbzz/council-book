"""Closing a swing trade whose own exit leg filled, and its matched sector-ETF return (SW-5c).

- `close_filled_exit`: an `exit_pending` trade (or an open one a flatten closed) whose every swing
  close leg of the exit decision is `filled` becomes `closed_time` / `closed_target` /
  `closed_halt` / `closed_exit` with its percent-only outcome detail. Called by the executor at the
  finish of the run that filled the leg and, as a fallback, by the cycle's `settle_swing`.
  Idempotent: a trade already terminal, or whose exit legs are not all filled, is left alone.
- The close rate comes from the leg's recorded fill price when there is one, else from the
  broker's closed-trade record once `closed_trade_route` is proven; otherwise None (the metrics
  then leave the trade out; never a guessed rate).
- `sector_etf_return`: the trade's sector ETF (the fact card's `sector_etf`, kept in the trade's
  detail) from the last completed close before the entry day to the last completed close on or
  before the close day, from completed daily bars (`SwingSources.daily_bars`). No bars -> None.
Percent and codes only; no amount, unit or position id goes into the detail.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

EXIT_STATES = {"time": "closed_time", "halt": "closed_halt", "target": "closed_target"}
SectorBars = Callable[[list[str], date], Mapping[str, Any] | None]


def exit_state(kind: str) -> str:
    return EXIT_STATES.get(kind, "closed_exit")


def sector_etf_return(detail: Mapping[str, Any], opened_at: datetime | None, closed_at: datetime,
                      bars_fn: SectorBars | None) -> float | None:
    """The matched sector ETF's return over the trade's window, or None (module rule)."""
    etf = detail.get("sector_etf")
    if not isinstance(etf, str) or not etf or opened_at is None or bars_fn is None:
        return None
    try:
        from council.clock import NEW_YORK

        start = opened_at.astimezone(NEW_YORK).date()
        end = closed_at.astimezone(NEW_YORK).date()
        frame = (bars_fn([etf], end) or {}).get(etf)
        if frame is None or len(frame) == 0:
            return None
        days = [ts.date() for ts in frame.index]
        closes = frame["close"].tolist()
        before = [i for i, d in enumerate(days) if d < start]
        upto = [i for i, d in enumerate(days) if d <= end]
        if not before or not upto or upto[-1] <= before[-1]:
            return None
        a, b = float(closes[before[-1]]), float(closes[upto[-1]])
        if not (a > 0 and b > 0):
            return None
        return round(b / a - 1.0, 6)
    except Exception:  # noqa: BLE001 - a measurement input; never a crash of the close
        return None


def _leg_rate(row: Any) -> float | None:
    d = row.detail or {}
    for key in ("fill_price", "fill_rate", "avg_price", "close_rate"):
        v = d.get(key)
        if isinstance(v, int | float) and not isinstance(v, bool) and v > 0:
            return float(v)
    return None


def close_filled_exit(ledger: Any, trade_id: str, decision_id: str, now: datetime, *, read: Any = None,
                      route_ok: bool = False, kind: str | None = None,
                      sector_bars: SectorBars | None = None, actor: str | None = None) -> str | None:
    """Close one trade whose exit legs of `decision_id` all filled. Returns the new state, or None
    when nothing changed (module rules)."""
    from council import watch
    from council.swing.models import TERMINAL_STATES

    t = ledger.swing_trade(trade_id)
    if t is None or t.state in TERMINAL_STATES or t.state in ("proposed", "entry_executing"):
        return None
    detail = t.detail or {}
    if t.state == "exit_pending" and detail.get("exit_decision") not in (None, decision_id):
        return None
    pids = set(t.position_ids or ())
    legs = [r for r in ledger.legs(decision_id)
            if r.kind == "close" and ((r.detail or {}).get("swing_trade_id") == trade_id
                                      or (r.position_id is not None and r.position_id in pids))]
    if not legs or any(r.state != "filled" for r in legs):
        return None
    if t.state != "exit_pending":
        # an open trade closed by a decision the cycle did not record as its exit (a flatten):
        # it passes through exit_pending so the close is one of our own exit closes
        ledger.update_swing_trade(trade_id, detail={"exit_decision": decision_id, "pre_exit_state": t.state,
                                                    "exit_kind": kind or "exit"}, now=now)
        ledger.transition_swing_trade(trade_id, "exit_pending", reason=f"exit_filled:{kind or 'exit'}",
                                      now=now, **({"actor": actor} if actor else {}))
        t = ledger.swing_trade(trade_id)
        detail = t.detail or {}
    exit_kind = str(kind or detail.get("exit_kind") or "exit")
    rate = next((r for r in (_leg_rate(row) for row in legs) if r is not None), None)
    if rate is None and route_ok and read is not None:
        from council.operator.smoke import closed_trade_record

        pid = next((row.position_id for row in legs if row.position_id is not None), None)
        rec = closed_trade_record(read, pid)
        v = rec.get("closeRate") if rec else None
        rate = float(v) if isinstance(v, int | float) and not isinstance(v, bool) and v > 0 else None
    sector = sector_etf_return(detail, t.opened_at, now, sector_bars)
    d = ledger.get_decision(decision_id)
    closed_cycle = getattr(d, "cycle_id", None) or watch.last_cycle_id(ledger)
    ledger.update_swing_trade(trade_id, detail=watch.trade_outcome_detail(t, rate, exit_kind, now,
                                                                          sector_etf_ret=sector,
                                                                          closed_cycle=closed_cycle), now=now)
    state = exit_state(exit_kind)
    ledger.transition_swing_trade(trade_id, state, close_rate=rate, reason=f"exit_filled:{exit_kind}",
                                  cycle_id=getattr(d, "cycle_id", None), now=now,
                                  **({"actor": actor} if actor else {}))
    return state
