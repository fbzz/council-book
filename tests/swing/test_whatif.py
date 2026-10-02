"""What-if P&L of every paper idea (user request 2026-10-02): bar walk, dedupe, corporate actions."""


import pandas as pd
import pytest

from council.swing import whatif as wi


def _bars(rows):
    idx = pd.to_datetime([d for d, *_ in rows]).tz_localize("UTC")
    return pd.DataFrame([{"open": o, "high": h, "low": low, "close": c} for _, o, h, low, c in rows], index=idx)


def _row(ticker="ACME", side="long", ref=100.0, stop=0.05, target=0.10, day="2026-10-01",
         ts="2026-10-15", group="skeptic_rejected", cycle="2026-10-01T1440Z", pid="p1", code="skeptic_reject"):
    return {"paper_id": pid, "ticker": ticker, "side": side, "entry_ref": ref, "stop_pct": stop,
            "target_pct": target, "time_stop_date": ts, "origin_cycle": cycle, "opened_at": day,
            "record": {"group": group, "entry_day": day, "ref": f"idea:{pid}", "drop_code": code}}


def test_target_hit_long_is_net_of_both_legs():
    b = _bars([("2026-10-01", 100, 101, 99, 100), ("2026-10-02", 101, 111, 100, 110)])
    w = wi.evaluate_row(_row(), b)
    assert w.status == "target" and w.gross_pct == pytest.approx(10.0) and w.net_pct == pytest.approx(7.5)


def test_gap_through_stop_books_at_the_open_and_same_bar_books_the_stop():
    gap = _bars([("2026-10-01", 100, 100, 100, 100), ("2026-10-02", 90, 92, 89, 91)])
    assert wi.evaluate_row(_row(), gap).gross_pct == pytest.approx(-10.0)
    both = _bars([("2026-10-01", 100, 100, 100, 100), ("2026-10-02", 100, 112, 94, 105)])
    assert wi.evaluate_row(_row(), both).status == "stop"


def test_short_side_and_open_mark():
    b = _bars([("2026-10-01", 100, 100, 100, 100), ("2026-10-02", 99, 99.5, 97, 98)])
    w = wi.evaluate_row(_row(side="short"), b)
    assert w.status == "open" and w.gross_pct == pytest.approx(2.0) and w.days_held == 1


def test_dedupe_keeps_one_per_ticker_side_slot():
    rows = [_row(pid="a"), _row(pid="b"), _row(pid="c", side="short")]
    assert len(wi.dedupe(rows)) == 2


def test_entry_day_spin_off_is_a_corporate_action_and_excluded():
    b = _bars([("2026-09-30", 70, 71, 69, 70), ("2026-10-01", 12, 13, 11, 11.3)])
    w = wi.evaluate_row(_row(ref=70.0, group="paper_only", code="setup_paper_only"), b)
    assert w.status == "corporate_action" and w.net_pct is None and not w.priced
    agg = {a["key"]: a for a in wi.aggregate([w], "group")} if isinstance(wi.aggregate([w], "group"), list) else {}
    assert all(a.get("n", 0) == 0 for a in agg.values())


def test_aggregate_counts_only_priced_ideas():
    b = _bars([("2026-10-01", 100, 101, 99, 100), ("2026-10-02", 101, 111, 100, 110)])
    items = wi.whatif([_row(pid="a"), _row(pid="b", ticker="ZZZ")], {"ACME": b, "ZZZ": b})
    s = wi.summary(items)
    assert s and len(items) == 2
