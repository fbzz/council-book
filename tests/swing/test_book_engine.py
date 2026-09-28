"""SW-4: swing lines in the risk engine (all-or-nothing, entries dropped before any core line
shrinks), the vehicle assertion and the runtime vehicle -> line map (swing-book.md §1.9, §3.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest

from council.execution.reconcile import line_weights, reconcile
from council.models.facts import EventItem
from council.swing import book as B
from tests.risk.helpers import NOW, loose, override, run, snapshot


@pytest.fixture
def lp(policy):
    return loose(policy)


def entry(ticker: str = "ACME", side: str = "long", size: float = 0.08, sigma: float = 0.025, **kw):
    return B.entry_line(f"idea:{ticker.lower()}", ticker, side, size, stop_pct=kw.pop("stop", 0.05),
                        sigma_daily=sigma, **kw)


def core_of(d, swing=("SW_",)):
    return {s: w for s, w in d.final_w.items() if not s.startswith(swing)}


def test_entry_is_pinned_and_core_untouched(lp):
    d0 = run(lp)
    d1 = run(lp, extra_lines=[entry(), entry("WIDG", "short", 0.04, stop=0.06)])
    assert d1.final_w["SW_ACME"] == pytest.approx(0.08)
    assert d1.final_w["SW_WIDG"] == pytest.approx(-0.04)
    assert core_of(d1) == pytest.approx(core_of(d0)) and not d1.swing_dropped


def test_r8_binding_drops_the_last_swing_entry_core_untouched(lp):
    d0 = run(lp)
    one = 0.08 * 0.025 * 252 ** 0.5
    tight = override(lp, "risk", {"ex_ante_vol_hard": d0.ex_ante_vol + one + 0.005})
    base = run(tight)
    assert core_of(base) == pytest.approx(core_of(d0))           # the core alone fits
    d = run(tight, extra_lines=[entry("ACME"), entry("WIDG")])
    assert d.final_w.get("SW_ACME") == pytest.approx(0.08)
    assert "SW_WIDG" not in d.final_w
    assert d.swing_dropped == {"SW_WIDG": "swing_book_limit:R8"}
    assert core_of(d) == pytest.approx(core_of(base))


def test_holds_and_exits_are_never_removed(lp):
    d0 = run(lp)
    tight = override(lp, "risk", {"ex_ante_vol_hard": d0.ex_ante_vol + 0.001})
    hold = B.open_line("trade:1", "HOLD", "long", 0.08, sigma_daily=0.03)
    ex = B.open_line("trade:2", "GONE", "long", 0.05, exit_=True, sigma_daily=0.03)
    d = run(tight, current={"SW_HOLD": 0.08, "SW_GONE": 0.05}, extra_lines=[hold, ex, entry()])
    assert d.final_w["SW_HOLD"] == pytest.approx(0.08) and d.final_w["SW_GONE"] == 0.0
    assert d.swing_dropped == {"SW_ACME": "swing_book_limit:R8"}


def test_warn_refuses_entries_and_halt_flattens_all(lp):
    d = run(lp, kill_state="WARN", extra_lines=[entry()])
    assert d.swing_dropped == {"SW_ACME": "swing_book_limit:R3"} and "SW_ACME" not in d.final_w
    hold = B.open_line("trade:1", "HOLD", "short", -0.04, stop_pct=0.06, sigma_daily=0.02)
    d = run(lp, kill_state="HALTED", current={"SW_HOLD": -0.04, "NDX": 0.2}, extra_lines=[hold, entry()])
    assert all(w == 0.0 for w in d.final_w.values()) and d.final_w["SW_HOLD"] == 0.0
    assert d.swing_dropped == {"SW_ACME": "swing_book_limit:R3"}


def test_swing_blocker_and_missing_vol_drop_entries(lp):
    d0 = run(lp)
    d = run(lp, blockers=["swing:trade:x"], extra_lines=[entry()])
    assert d.swing_dropped == {"SW_ACME": "swing_book_limit:R20"}
    assert core_of(d) == pytest.approx(core_of(d0))
    d = run(lp, extra_lines=[entry(sigma=None)])
    assert d.swing_dropped == {"SW_ACME": "swing_book_limit:R8"}


def test_macro_event_window_drops_entries(lp):
    ev = EventItem(id="E:1", kind="fomc", at_utc=NOW + timedelta(hours=1), severity=3, source="fed")
    d = run(lp, events=[ev], extra_lines=[entry()])
    assert d.swing_dropped.get("SW_ACME") == "swing_book_limit:R16"


def test_gross_limit_drops_entries_before_shrinking_core(lp):
    d0 = run(lp)
    tight = override(lp, "risk", {"gross.proposal_max": d0.gross + 0.1})
    d = run(tight, extra_lines=[entry("A1"), entry("A2")])
    assert d.final_w.get("SW_A1") == pytest.approx(0.08) and "SW_A2" not in d.final_w
    assert d.swing_dropped["SW_A2"].startswith("swing_book_limit:R")
    assert core_of(d) == pytest.approx(core_of(d0))


def test_equity_beta_cluster_counts_swing_longs(lp):
    d0 = run(lp)
    members = lp.risk["caps"]["equity_beta_cluster"]["members"]
    cluster = sum(abs(d0.final_w.get(s, 0.0)) for s in members)
    tight = override(lp, "risk", {"caps.equity_beta_cluster.max": cluster + 0.1})
    d = run(tight, extra_lines=[entry("A1", beta_60d=1.2), entry("A2", beta_60d=1.2)])
    assert "SW_A1" in d.final_w and d.swing_dropped == {"SW_A2": "swing_book_limit:R5"}


# ------------------------------------------------------------------------------ vehicles
def test_stock_cfd_long_and_leverage_raise():
    with pytest.raises(B.SwingVehicleError):
        B.SwingLine(line_id="SW_ACME", ref="idea:a", ticker="ACME", side="long", action="enter",
                    pinned_w=0.08, settlement="cfd", stop_pct=0.05, sigma_ann=0.4)
    with pytest.raises(B.SwingVehicleError):
        B.assert_vehicle("long", "real", 2, 0.05)
    with pytest.raises(B.SwingVehicleError):
        B.assert_vehicle("short", "real", 1, 0.05)
    with pytest.raises(B.SwingVehicleError):
        B.assert_vehicle("short", "cfd", 1, 0.09)
    with pytest.raises(B.SwingVehicleError):
        B.open_line("trade:1", "X", "short", -0.04)                  # a short without its stop
    B.assert_vehicle("short", "cfd", 1, 0.08)


# ------------------------------------------------------------------------------ runtime map
@dataclass
class Row:
    ticker: str
    instrument_id: int | None
    state: str


def test_vehicle_map_and_reconcile_sees_swing_positions(policy):
    rows = [Row("ACME", 9001, "open"), Row("OLD", 9002, "closed_stop"), Row("BRK.B", 9003, "entry_unknown")]
    m = B.build_vehicle_map(rows)
    assert m.symbols_by_id == {9001: "ACME", 9003: "BRK_B"}
    assert m.owner("ACME") == "SW_ACME" and m.owner("OLD") is None
    snap = snapshot({"ACME": 0.08})
    assert line_weights(snap, policy)[1] == ["ACME"]                  # without the map: unknown
    achieved, unknown = line_weights(snap, policy, swing_map=m)
    assert unknown == [] and achieved == {"SW_ACME": pytest.approx(0.08)}
    res = reconcile(snap, {"SW_ACME": 0.08}, [], policy, swing_map=m)
    assert res.unknown_positions == [] and res.drift == pytest.approx(0.0)


def test_vehicle_map_leaves_core_vehicles_alone(policy):
    from council.execution.planner import vehicle_to_line

    core = next(iter(vehicle_to_line(policy.universe)))
    m = B.build_vehicle_map([Row(core, 1, "open")], core_vehicles=[core])
    assert not m
    assert m.merged_lines({"X": "NDX"}) == {"X": "NDX"}


def test_swing_vehicle_map_from_ledger(tmp_path):
    from council.ledger.db import Ledger

    class Broken:
        def swing_trades(self, **kw):
            raise RuntimeError("no table")

    assert not B.swing_vehicle_map(Broken())
    led = Ledger(tmp_path / "l.sqlite")
    assert not B.swing_vehicle_map(led)


def test_line_ids():
    assert B.line_id("BRK.B") == "SW_BRK_B"
    with pytest.raises(ValueError):
        B.line_id("not a ticker!")


def test_vanished_swing_position_is_not_a_core_stop_hit(policy):
    from council.stocks import corporate

    m = B.build_vehicle_map([Row("ACME", 9001, "open")])
    seen = {7: corporate.Observation("ACME", sl_rate=95.0, bid=120.0)}      # a long closed at its TP
    [v] = corporate.classify_vanished_positions(seen, [], [], policy, sigma_4h={}, swing_map=m)
    assert v.outcome == corporate.SWING_CLOSED and v.line == "SW_ACME"
    assert corporate.vanished_alerts([v]) == []                              # no vanished_not_stop URGENT
    [v] = corporate.classify_vanished_positions(seen, [], [], policy, sigma_4h={})
    assert v.outcome != corporate.SWING_CLOSED                              # without the map: today's rule


def test_swing_notes_are_public_codes_only(lp):
    from council.publish.trace_rules import listed, public_hold_reason
    from council.swing.rules import public_code

    d0 = run(lp)
    tight = override(lp, "risk", {"ex_ante_vol_hard": d0.ex_ante_vol + 0.001})
    d = run(tight, extra_lines=[entry()])
    assert "SW_ACME: swing_book_limit:R8" in d.hold_reasons
    for note in d.hold_reasons:
        if note.startswith("SW_"):
            assert public_hold_reason(note) == note and listed(note.split(": ", 1)[1])
    assert public_hold_reason(f"SW_ACME: {public_code('weekly_cap')}") == "SW_ACME: S3:weekly_cap"
    assert public_hold_reason("SW_ACME: swing_book_limit:R8 0.31") == "SW_ACME: held"


def test_approval_drift_skips_swing_lines_closed_at_the_broker():
    live = B.build_vehicle_map([Row("KEEP", 1, "open")])
    base = {"NDX": 0.30, "SW_GONE": 0.08, "SW_KEEP": 0.08}
    current = {"NDX": 0.30, "SW_KEEP": 0.08}
    assert B.approval_drift(current, base, live) == pytest.approx(0.0)
    assert B.approval_drift({"NDX": 0.30}, base, live) == pytest.approx(0.08)       # KEEP still live: counts
    assert B.approval_drift(current, base, B.build_vehicle_map([Row("GONE", 2, "open"), Row("KEEP", 1, "open")])) \
        == pytest.approx(0.08)
