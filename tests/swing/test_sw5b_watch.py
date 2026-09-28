"""SW-5b: the watch's swing part (swing-book.md rev 2, §1.8, §1.9): flags only, bid/ask by side,
the take-profit rate, broker closes classified from the closed-trade record (never a guessed stop)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from council import watch
from council.cycle import swing_code_exits
from council.ledger.db import Ledger
from council.models.broker import Position

NVDA_ID = 201


@pytest.fixture
def ledger(tmp_path, fclock):
    return Ledger(tmp_path / "ledger.sqlite3", clock=fclock.now)


@pytest.fixture
def fclock():
    from council.broker.fake import FakeClock

    return FakeClock()


def pos(pid: int, *, is_buy: bool = True, close: float = 100.0, sl: float = 94.0, tp: float | None = 110.0) -> Position:
    return Position(position_id=pid, instrument_id=NVDA_ID, symbol="NVDA", is_buy=is_buy, units=8.0, open_rate=100.0,
                    amount=800.0, sl_rate=sl, tp_rate=tp, settlement="real" if is_buy else "cfd",
                    exposure_usd=800.0, close_rate=close)


def trade(ledger, tid: str, pid: int, *, side: str = "long", sl: float = 94.0, tp: float | None = 110.0,
          time_stop: str = "2026-10-15", detail: dict | None = None):
    ledger.create_swing_trade(tid, ticker="NVDA", side=side, instrument_id=NVDA_ID, sl_rate=sl, tp_rate=tp,
                              time_stop_date=time_stop, origin_cycle="2026-09-29T1840Z",
                              detail={"size_nav": 0.08, "stop_pct": 0.06, **(detail or {})})
    ledger.transition_swing_trade(tid, "entry_executing")
    ledger.transition_swing_trade(tid, "open")
    ledger.update_swing_trade(tid, position_ids=[pid], open_rate=100.0, units=8.0)


class Read:
    def __init__(self, records: dict | None = None):
        self.records = records or {}

    def closed_trade(self, pid):
        return self.records.get(pid)


def ctx(tmp_path, ledger, policy, read=None):
    return SimpleNamespace(policy=policy, ledger=ledger, sources=SimpleNamespace(broker=read or Read()),
                           state_dir=tmp_path)


def test_observations_keep_the_take_profit_and_the_quote_by_side(tmp_path, ledger, policy, fclock):
    trade(ledger, "trade:l", 1)
    trade(ledger, "trade:s", 2, side="short", sl=106.0, tp=90.0)
    snap = SimpleNamespace(positions=[pos(1, close=101.0), pos(2, is_buy=False, close=99.5, sl=106.0, tp=90.0)])
    watch._stop_hits(ctx(tmp_path, ledger, policy), snap, fclock.now())
    stored = ledger.get_runtime("watch_positions")
    assert stored["1"] == {"symbol": "NVDA", "sl_rate": 94.0, "bid": 101.0, "tp_rate": 110.0}
    assert stored["2"]["ask"] == 99.5 and stored["2"]["bid"] is None and stored["2"]["tp_rate"] == 90.0


def test_a_tp_close_on_a_long_is_closed_target_with_no_urgent_alert(tmp_path, ledger, policy, fclock, monkeypatch):
    monkeypatch.setattr(watch, "closed_trade_route_ok", lambda state_dir: True)
    trade(ledger, "trade:l", 1)
    c = ctx(tmp_path, ledger, policy, Read({1: {"positionId": 1, "closeRate": 110.0, "closeReason": "take_profit"}}))
    now = fclock.now()
    watch._stop_hits(c, SimpleNamespace(positions=[pos(1, close=109.0)]), now)
    alerts = watch._stop_hits(c, SimpleNamespace(positions=[]), now + timedelta(minutes=15))
    assert alerts == ["swing trade closed at its take-profit: SW_NVDA"]
    assert not any(a.startswith("URGENT") for a in alerts)
    t = ledger.swing_trade("trade:l")
    assert t.state == "closed_target" and t.close_rate == pytest.approx(110.0)
    assert t.detail["exit_kind"] == "target" and t.detail["net_ret"] == pytest.approx(0.10 - 0.025)
    assert t.detail["r_declared"] == pytest.approx((0.10 - 0.025) / 0.06)
    assert not ledger.stop_hits_since(now - timedelta(days=1), universe=policy.universe)


def test_a_vanished_short_with_no_data_is_unclassified_not_a_stop_hit(tmp_path, ledger, policy, fclock):
    trade(ledger, "trade:s", 2, side="short", sl=106.0, tp=90.0)
    c = ctx(tmp_path, ledger, policy)                       # the closed-trade route is not proven
    now = fclock.now()
    watch._stop_hits(c, SimpleNamespace(positions=[pos(2, is_buy=False, close=105.9, sl=106.0, tp=90.0)]), now)
    alerts = watch._stop_hits(c, SimpleNamespace(positions=[]), now + timedelta(minutes=15))
    assert ledger.swing_trade("trade:s").state == "closed_unclassified"
    assert not any("stop" in a and "hit" in a for a in alerts)
    assert any(a.startswith("URGENT swing_close_unclassified:SW_NVDA") for a in alerts)
    assert not ledger.stop_hits_since(now - timedelta(days=1), universe=policy.universe)


def test_a_close_through_the_stop_is_closed_stop_and_elsewhere_is_external():
    assert watch.classify_swing_close("long", 94.0, 110.0, {"closeRate": 93.0}) == ("closed_stop", 93.0)
    assert watch.classify_swing_close("short", 106.0, 90.0, {"closeRate": 89.9}) == ("closed_target", 89.9)
    assert watch.classify_swing_close("long", 94.0, 110.0, {"closeRate": 101.0})[0] == "closed_external"
    assert watch.classify_swing_close("long", 94.0, 110.0, None) == ("closed_unclassified", None)
    assert watch.classify_swing_close("long", 94.0, 110.0, {"closeRate": 0})[0] == "closed_unclassified"


def test_the_watch_only_flags_and_the_cycle_creates_the_time_stop_exit(tmp_path, ledger, policy, fclock):
    trade(ledger, "trade:l", 1, time_stop="2026-10-01")               # due today
    snap = SimpleNamespace(positions=[pos(1)])
    alerts = watch.swing_flags(ledger, policy, snap, fclock.now())
    assert alerts == ["swing time_stop_due: trade:l"]
    assert watch.swing_flags(ledger, policy, snap, fclock.now()) == []       # once a day
    assert ledger.decisions() == []                                          # the watch made no exit
    assert ledger.swing_trade("trade:l").state == "open"
    slot = datetime(2026, 10, 1, 18, 40, tzinfo=fclock.now().tzinfo)
    assert swing_code_exits(ledger, policy, slot) == {"trade:l": "time"}       # the cycle's exit


def test_a_target_that_never_reached_the_broker_is_flagged_when_the_price_is_through_it(
        tmp_path, ledger, policy, fclock):
    trade(ledger, "trade:l", 1, tp=110.0, detail={"tp_mode": "none"})
    assert watch.swing_flags(ledger, policy, SimpleNamespace(positions=[pos(1, close=109.0, tp=None)]), fclock.now()) == []
    alerts = watch.swing_flags(ledger, policy, SimpleNamespace(positions=[pos(1, close=110.5, tp=None)]), fclock.now())
    assert alerts == ["swing target_reached_unplaced: trade:l"]
    slot = datetime(2026, 10, 1, 18, 40, tzinfo=fclock.now().tzinfo)
    assert swing_code_exits(ledger, policy, slot) == {"trade:l": "target"}


def test_a_confirmed_earnings_date_flags_the_exit(tmp_path, ledger, policy, fclock):
    trade(ledger, "trade:l", 1, time_stop="2026-10-20",
          detail={"earnings_next": "2026-10-05", "earnings_confirmed": True})
    alerts = watch.swing_flags(ledger, policy, SimpleNamespace(positions=[pos(1)]), fclock.now())
    assert alerts == ["swing earnings_exit_due: trade:l"]
    assert date.fromisoformat("2026-10-05") > fclock.now().date()
