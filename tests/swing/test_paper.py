"""SW-6: one paper convention for every idea group (design swing-book.md rev 2, §8.2).

Accept: a stop gapped through books at the open; a stop-and-target bar books the stop; the declared
cost is charged on both legs; the entry session's bar never exits; every group uses the same rule."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from council.ledger.db import Ledger
from council.swing import paper

D0 = date(2026, 10, 5)                                   # Monday, the slot's session


def bars(rows):
    idx = pd.DatetimeIndex([pd.Timestamp(d).tz_localize("UTC") for d, *_ in rows])
    return pd.DataFrame([r[1:] for r in rows], index=idx, columns=["open", "high", "low", "close"])


def idea(side="long", group="executed", stop=0.05, target=0.10, time_stop=date(2026, 10, 16), ref="idea:a1"):
    return paper.PaperIdea(ref=ref, ticker="XYZ", side=side, group=group, entry_day=D0, entry_ref=100.0,
                           stop_pct=stop, target_pct=target, time_stop_day=time_stop)


def test_gap_through_the_stop_books_at_the_open():
    b = bars([("2026-10-05", 100, 101, 80, 100),          # entry session: ignored (pre-entry low)
              ("2026-10-06", 90, 92, 88, 91)])            # opens below the 95 stop
    out = paper.evaluate(idea(), b)
    assert out.exit_reason == "stop_gap" and out.gross_ret == pytest.approx(-0.10)
    assert out.net_ret == pytest.approx(-0.10 - 0.025)
    assert out.r_declared == pytest.approx(-0.125 / 0.05)
    assert out.days_held == 1


def test_short_gap_through_the_stop_books_at_the_open():
    b = bars([("2026-10-06", 108, 110, 107, 109)])        # short stop 105
    out = paper.evaluate(idea(side="short"), b)
    assert out.exit_reason == "stop_gap" and out.gross_ret == pytest.approx(-0.08)


def test_stop_and_target_in_the_same_bar_books_the_stop():
    b = bars([("2026-10-06", 100, 111, 94, 105)])
    out = paper.evaluate(idea(), b)
    assert out.exit_reason == "stop" and out.gross_ret == pytest.approx(-0.05)
    s = paper.evaluate(idea(side="short"), bars([("2026-10-06", 100, 106, 89, 95)]))
    assert s.exit_reason == "stop" and s.gross_ret == pytest.approx(-0.05)


def test_target_time_and_open():
    tgt = paper.evaluate(idea(), bars([("2026-10-06", 101, 103, 99, 102), ("2026-10-07", 104, 111, 103, 108)]))
    assert tgt.exit_reason == "target" and tgt.gross_ret == pytest.approx(0.10) and tgt.days_held == 2
    gap = paper.evaluate(idea(), bars([("2026-10-06", 112, 115, 111, 113)]))
    assert gap.exit_reason == "target_gap" and gap.gross_ret == pytest.approx(0.12)
    quiet = [(f"2026-10-{d:02d}", 100, 101, 99, 100.5) for d in (6, 7, 8, 9, 12, 13, 14, 15, 16)]
    tm = paper.evaluate(idea(), bars(quiet))
    assert tm.exit_reason == "time" and tm.exit_day == date(2026, 10, 16)
    assert tm.net_ret == pytest.approx(0.005 - 0.025)
    assert paper.evaluate(idea(), bars(quiet[:3])) is None                # still open
    assert paper.evaluate(idea(), bars([("2026-10-05", 100, 120, 80, 100)])) is None


@pytest.mark.parametrize("group", paper.GROUPS)
def test_every_group_uses_the_same_convention(group):
    b = bars([("2026-10-06", 90, 92, 88, 91)])
    base = paper.evaluate(idea(), b)
    out = paper.evaluate(idea(group=group), b)
    assert (out.exit_reason, out.r_declared) == (base.exit_reason, base.r_declared)
    assert "entry_ref" not in out.public() and out.public()["group"] == group


def test_bad_ideas_are_refused():
    with pytest.raises(paper.PaperError):
        idea(group="vibes")
    with pytest.raises(paper.PaperError):
        paper.PaperIdea(ref="idea:x", ticker="X", side="long", group="executed", entry_day=D0,
                        entry_ref=float("nan"), stop_pct=0.05, target_pct=0.1, time_stop_day=D0)


def test_slippage_and_mark():
    s = paper.slippage(101.0, 100.0, "long", 0.05)
    assert s["pct"] == pytest.approx(1.0) and s["r"] == pytest.approx(0.2)
    assert paper.slippage(99.0, 100.0, "short", 0.05)["pct"] == pytest.approx(1.0)
    assert paper.slippage(99.0, 100.0, "long", 0.05, leg="exit")["pct"] == pytest.approx(1.0)
    m = paper.mark(idea(), 102.0)
    assert m["net_pct"] == pytest.approx(2.0 - 2.5)
    assert m["to_stop_pct"] == pytest.approx(100 * (102 / 95 - 1))


def test_track_and_settle_through_the_ledger(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3", clock=lambda: datetime(2026, 10, 5, 19, tzinfo=UTC))
    for g, ref in (("skeptic_rejected", "idea:r1"), ("executed", "trade:t1")):
        paper.track(ledger, idea(group=g, ref=ref), origin_cycle="c1",
                    opened_at=datetime(2026, 10, 5, 18, 40, tzinfo=UTC), skeptic_verdict="reject" if g != "executed" else "pass")
    closed = paper.settle(ledger, {"XYZ": bars([("2026-10-06", 100, 111, 94, 105)])})
    assert {c.group for c in closed} == {"skeptic_rejected", "executed"}
    rows = paper.closed_outcomes(ledger.paper_trades())
    assert all(r["r_declared"] == pytest.approx((-0.05 - 0.025) / 0.05) for r in rows)
    assert {r["skeptic_verdict"] for r in rows} == {"reject", "pass"}
    assert ledger.paper_trades(status="open") == []


def test_drop_codes_on_ledger_rows_are_code_tokens_only():
    from council.swing.record import safe_code

    assert safe_code(None) is None
    assert safe_code("not_best_3") == "not_best_3" and safe_code("S6:max_new_7d") == "S6:max_new_7d"
    assert safe_code("chased 4.2% since news") == "unknown" and safe_code("") == "unknown"
