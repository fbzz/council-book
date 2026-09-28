"""SW-6: `council swing status` logic (design swing-book.md rev 2, §7.1), read-only over the ledger."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from council.benchmark import sq8
from council.swing import status


class FakeLedger:
    def __init__(self, trades, ideas=(), blockers=(), paper=(), days=()):
        self._t, self._i, self._b, self._p, self._d = list(trades), list(ideas), list(blockers), list(paper), list(days)

    def swing_trades(self, *, states=None):
        return self._t

    def swing_ideas(self, *, status=None):
        return self._i

    def swing_blockers(self):
        return self._b

    def paper_trades(self, *, status=None):
        return self._p

    def benchmark_days(self):
        return self._d


def _trade(tid, state, side="long", opened=datetime(2026, 10, 5, 19, tzinfo=UTC), detail=None):
    return SimpleNamespace(trade_id=tid, ticker="XYZ", side=side, state=state, opened_at=opened, open_rate=100.0,
                           sl_rate=95.0, tp_rate=110.0, time_stop_date="2026-10-16", detail=detail or {})


def test_status_lines_and_clean():
    led = FakeLedger([_trade("trade:a", "open")],
                     ideas=[{"idea_id": "idea:w", "ticker": "ABC", "side": "short", "status": "wait"}],
                     days=[{"sq8_ret": 0.01, "matched_idx_ret": None, "idx_hold_ret": -0.0125}])
    st = status.swing_status(led, today=date(2026, 10, 7), marks={"XYZ": 102.0}, resamples=500)
    t = st.open_trades[0]
    assert t.sessions_held == 2 and round(t.pnl_actual_pct, 6) == 2.0 and round(t.pnl_declared_pct, 6) == -0.5
    assert st.clean and st.entries_7d == 1
    text = "\n".join(st.lines())
    assert "CLEAN" in text and "idea:w" in text and sq8.LABEL in text and "benchmark sq8_cumulative: 1.0%" in text


def test_status_flags_tp_missing_blockers_and_pause():
    closed = [_trade(f"trade:c{i}", "closed_stop", detail={"r_declared": -0.5, "net_ret": -0.025, "size_nav": 0.08,
                                                            "beta": 1.0, "sector_etf_ret": 0.0})
              for i in range(20)]
    led = FakeLedger([_trade("trade:m", "open_tp_missing"), *closed], blockers=["swing:trade:m"])
    st = status.swing_status(led, today=date(2026, 10, 7), resamples=500)
    assert not st.clean and st.tp_missing == ["trade:m"] and st.pause.pause
    assert "mean_r_declared_le_0" in "\n".join(st.lines())


def test_status_skeptic_test_from_paper_rows():
    rows = [{"status": "closed", "ret_pct": 2.5, "stop_pct": 0.05,
             "record": {"group": "skeptic_rejected", "skeptic_verdict": "reject"}}] * 20
    rows += [{"status": "closed", "ret_pct": -2.5, "stop_pct": 0.05, "record": {"group": "executed"}}] * 20
    st = status.swing_status(FakeLedger([], paper=rows), today=date(2026, 10, 7), resamples=500)
    assert st.skeptic.status == "advisory"
    assert st.funnel["skeptic_rejected"].mean == 0.5
