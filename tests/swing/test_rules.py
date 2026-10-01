"""SW-4: one test per swing S-rule at its boundary (design swing-book.md rev 2, §3 and §9 SW-4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from council.swing import costs as sc
from council.swing import rules as R

TODAY = date(2026, 9, 29)            # a Tuesday
NOW = datetime(2026, 9, 29, 18, 40, tzinfo=UTC)


@pytest.fixture
def sp(policy):
    return policy.swing


def flat_cost(pct: float = 2.0):
    return lambda side, size, days: pct


def cand(**kw) -> R.Candidate:
    base = dict(ref="idea:a", ticker="ACME", side="long", setup="news_continuation", stop_pct=0.05,
                target_pct=0.12, time_stop_days=15, sigma_daily=0.025, atr_pct=0.03, adv_usd=1e9,
                price=100.0, beta_60d=1.0, sector="Tech", short_interest_pct_float=5.0, listing_days=900)
    base.update(kw)
    return R.Candidate(**base)


def book(**kw) -> R.BookState:
    kw.setdefault("drawdown_from_peak", 0.0)          # SW-5b: an unknown drawdown blocks entries
    return R.BookState(today=TODAY, now=NOW, **kw)


def trade(ref: str, side: str = "long", size: float = 0.08, stop: float = 0.05, sector: str | None = None,
          beta: float = 1.0) -> R.BookTrade:
    sec = sector or f"S_{ref}"
    return R.BookTrade(ref=ref, ticker=ref.upper(), side=side, size_nav=size, stop_pct=stop, sector=sec,
                       beta_60d=beta, buckets=frozenset({f"sector:{sec}"}))


def run(c, b=None, sp=None, cost=None):
    return R.screen_entry(c, b or book(), sp, cost or flat_cost())


# ------------------------------------------------------------------------------ S1 size
def test_s1_long_size_shrinks_to_loss_cap(sp):
    v = run(cand(stop_pct=0.10, atr_pct=0.05, target_pct=0.20, sigma_daily=0.035), sp=sp)
    assert v.ok and v.size_nav == pytest.approx(0.08)              # 0.008 / 0.10 = 0.08
    v = run(cand(stop_pct=0.12, atr_pct=0.05, target_pct=0.24, sigma_daily=0.04), sp=sp)
    assert v.ok and v.size_nav == pytest.approx(0.008 / 0.12)
    assert v.size_nav * v.stop_pct <= 0.008 + 1e-12


def test_s1_short_smaller_and_unknown_si_halves(sp):
    short = dict(side="short", adv_usd=5e8, move_since_news_sigma=-1.0)
    v = run(cand(stop_pct=0.08, atr_pct=0.03, target_pct=0.20, sigma_daily=0.035, **short), sp=sp)
    assert v.ok and v.size_nav == pytest.approx(0.005 / 0.08)
    v = run(cand(stop_pct=0.05, short_interest_pct_float=None, **short), sp=sp)
    assert v.ok and v.size_nav == pytest.approx(0.04) and "short_si_unknown" in v.flags
    v = run(cand(stop_pct=0.07, short_interest_pct_float=None, target_pct=0.14, **short), sp=sp)
    assert not v.ok and v.code == "stop_too_wide_for_size"         # 0.0714 x 0.5 = 3.6% < 4%


# ------------------------------------------------------------------------------ S2 / S3 / S4
def test_s2_max_open_and_third_short_refused(sp):
    full = book(trades=[trade(f"t{i}", size=0.04, stop=0.03) for i in range(6)])
    assert run(cand(), full, sp).code == "max_open"
    shorts = book(trades=[trade("s1", "short", beta=0.5), trade("s2", "short", beta=0.5)])
    c = cand(side="short", adv_usd=5e8, stop_pct=0.05)
    assert run(c, shorts, sp).code == "max_short"
    assert run(c, book(trades=shorts.trades[:1]), sp).ok


def test_s3_weekly_cap_policy_and_stricter(sp):
    assert run(cand(), book(entries_7d=5), sp).ok                  # 6th of 6
    assert run(cand(), book(entries_7d=6), sp).code == "weekly_cap"
    strict = sp.model_copy(update={"capacity": sp.capacity.model_copy(update={"max_new_7d": 3})})
    assert run(cand(), book(entries_7d=2), strict).ok
    assert run(cand(), book(entries_7d=3), strict).code == "weekly_cap"   # the 4th entry in 7 days


def test_s4_open_risk_counts_the_gap_multiplier(sp):
    open4 = [trade("a", stop=0.075), trade("b", stop=0.075), trade("c", "short", stop=0.075),
             trade("d", "short", stop=0.075)]                      # 4 x 0.08 x 0.075 x 1.5 = 0.036
    b = book(trades=open4)
    c = cand(stop_pct=0.04 / 1.2, atr_pct=0.02, target_pct=0.10)    # 0.08 x 0.0333 x 1.5 = 0.004
    assert run(c, b, sp).ok
    assert run(replace(c, stop_pct=0.034), b, sp).code == "open_risk"   # only 0.0027 without the 1.5x


# ------------------------------------------------------------------------------ S5 stops
def test_s5_stop_range_and_atr(sp):
    assert run(cand(stop_pct=None), sp=sp).code == "stop_missing"
    assert run(cand(stop_pct=0.019), sp=sp).code == "stop_out_of_range"
    assert run(cand(stop_pct=0.13), sp=sp).code == "stop_out_of_range"
    assert run(cand(side="short", adv_usd=5e8, stop_pct=0.081), sp=sp).code == "stop_out_of_range"
    v = run(cand(stop_pct=0.02, atr_pct=0.03), sp=sp)
    assert v.ok and v.stop_pct == pytest.approx(0.03) and "stop_widened_to_atr" in v.flags
    wide = cand(stop_pct=0.07, atr_pct=0.10, sigma_daily=0.035, target_pct=0.18)
    assert run(wide, sp=sp).ok
    assert run(replace(wide, target_pct=0.12), sp=sp).code == "stop_inside_atr"   # widened fails S6
    assert run(replace(wide, atr_pct=0.13), sp=sp).code == "stop_inside_atr"      # beyond the 12% max


# ------------------------------------------------------------------------------ S6 targets
def _ref_cost():
    cfg = sc.CostConfig(fee_usd=1.0, charged_on=("real", "virtual"), spread_floor_bps=10.0,
                        short_carry_bps_day_floor=0.0, weekend_multiplier=3.0)
    acct = sc.SwingAccount(2000.0)

    def fn(side, size, days):
        return sc.round_trip(side, size_nav=size, real_nav_usd=2000.0, virtual_nav_usd=10_000.0, account=acct,
                             cfg=cfg, entry_day=TODAY, time_stop_sessions=days).total_pct
    return fn


def test_s6_reference_boundary_at_1500(sp):
    # §3.3 worked example: 2.12% round trip at the $1.5k fee reference; 5% stop -> target >= 10.66%.
    fn = _ref_cost()
    c = cand(stop_pct=0.05, atr_pct=0.04, sigma_daily=0.02)
    assert run(replace(c, target_pct=0.107), sp=sp, cost=fn).ok
    assert run(replace(c, target_pct=0.106), sp=sp, cost=fn).code == "target_too_small"


def test_s6_ceiling_clips_and_vol_limits(sp):
    v = run(cand(target_pct=0.15, sigma_daily=0.02, atr_pct=0.02), sp=sp)
    assert v.ok and "target_clipped_to_vol" in v.flags
    assert v.target_pct == pytest.approx(1.5 * 0.02 * 15 ** 0.5)
    assert run(cand(target_pct=0.15, sigma_daily=0.012, atr_pct=0.02), sp=sp).code == "target_beyond_vol"
    assert run(cand(sigma_daily=0.041), sp=sp).code == "vol_too_high"
    assert run(cand(sigma_daily=0.04, target_pct=0.2, atr_pct=0.03), sp=sp).ok
    assert run(cand(sigma_daily=None), sp=sp).code == "vol_unknown"
    assert run(cand(target_pct=None), sp=sp).code == "target_missing"
    assert run(cand(), sp=sp, cost=lambda *a: None).code == "cost_unavailable"


def test_s6_min_cost_multiple(sp):
    # target 3x cost floor binds before the R/R floor with a tiny stop and a big cost
    assert run(cand(stop_pct=0.02, atr_pct=0.01, target_pct=0.089), sp=sp, cost=flat_cost(3.0)).code \
        == "target_too_small"
    assert run(cand(stop_pct=0.02, atr_pct=0.01, target_pct=0.091), sp=sp, cost=flat_cost(3.0)).ok


# ------------------------------------------------------------------------------ S7 / S8
def test_s7_time_stop_bounds(sp):
    assert run(cand(time_stop_days=2), sp=sp).code == "time_stop_out_of_range"
    assert run(cand(time_stop_days=16), sp=sp).code == "time_stop_out_of_range"
    v = run(cand(time_stop_days=None), sp=sp)
    assert v.ok and v.time_stop_days == 15 and v.time_stop_date == R.add_sessions(TODAY, 15)


def test_s8_liquidity(sp):
    assert run(cand(adv_usd=49_999_999), sp=sp).code == "illiquid"
    assert run(cand(adv_usd=50_000_000), sp=sp).ok
    assert run(cand(side="short", adv_usd=199e6), sp=sp).code == "illiquid"
    assert run(cand(price=9.99), sp=sp).code == "price_too_low"


# ------------------------------------------------------------------------------ S9 earnings
def test_s9_confirmed_estimated_and_post_report(sp):
    near = R.add_sessions(TODAY, 3)
    assert run(cand(earnings_next=near, earnings_confirmed=True), sp=sp).code == "earnings_window"
    later = R.add_sessions(TODAY, 10)
    v = run(cand(earnings_next=later, earnings_confirmed=True, target_pct=0.11), sp=sp)
    assert v.ok and v.time_stop_days == 9 and "time_stop_cut_earnings" in v.flags
    est = TODAY + timedelta(days=40)
    assert run(cand(earnings_next=est), sp=sp).time_stop_days == 15              # window far away
    est = TODAY + timedelta(days=7)                                              # window starts in 2 days
    assert run(cand(earnings_next=est), sp=sp).code == "earnings_window_estimated"
    assert run(cand(last_report_at=NOW - timedelta(hours=29)), sp=sp).code == "post_earnings_wait"
    assert run(cand(last_report_at=NOW - timedelta(hours=31)), sp=sp).ok


# ------------------------------------------------------------------------------ S10 correlation
def test_s10_sector_and_same_bet_buckets(sp):
    b = book(trades=[trade("x", sector="Tech"), trade("y", sector="Tech")])
    assert run(cand(sector="Tech"), b, sp).code == "bucket_full"
    b = book(trades=[trade("x", sector="Energy")])
    assert run(cand(sector="Tech", corr_open={"x": 0.61}), b, sp).ok             # 2 in x's bucket
    b2 = book(trades=[*b.trades, replace(trade("z", sector="Util"), buckets=frozenset({"sector:Util", "trade:x"}))])
    assert run(cand(sector="Tech", corr_open={"x": 0.61}), b2, sp).code == "bucket_full"
    assert run(cand(sector="Tech", corr_open={"x": 0.59}), b2, sp).ok


def test_s10_core_overweight_semis_bucket(sp):
    b = book(core_overweight=frozenset({"SEMIS"}))
    first = cand(ref="idea:a", sector="Tech", corr_core={"SEMIS": 0.65})
    v = run(first, b, sp)
    assert v.ok and "core:SEMIS" in v.buckets                   # core SEMIS + this long fill the bucket
    b = replace(b, trades=[v.book_trade(first)])
    second = cand(ref="idea:b", ticker="WIDG", sector="Energy", corr_core={"SEMIS": 0.65})
    assert run(second, b, sp).code == "bucket_full"
    assert run(second, replace(b, core_overweight=frozenset()), sp).ok


def test_s10_swing_net_beta(sp):
    b = book(trades=[trade("a", beta=1.5), trade("b", beta=1.375)])             # 0.12 + 0.11 = 0.23
    assert run(cand(beta_60d=1.0), b, sp).code == "swing_net_beta"               # 0.31
    assert run(cand(beta_60d=0.875), b, sp).ok                                   # 0.30


# ------------------------------------------------------------------------------ S11 / S13
def test_s11_chase(sp):
    assert run(cand(move_since_news_sigma=4.01), sp=sp).code == "chased"          # hard 4 sigma (2026-10-01)
    assert run(cand(move_since_news_sigma=4.1), sp=sp).code == "chased"
    v = run(cand(move_since_news_sigma=3.5), sp=sp)
    assert v.ok and "chase_prior_wait" in v.flags
    v = run(cand(move_since_news_sigma=2.5), sp=sp)
    assert v.ok and "chase_prior_wait" in v.flags
    assert run(cand(move_since_news_sigma=-4.5), sp=sp).ok                       # against a long
    assert run(cand(side="short", adv_usd=5e8, move_since_news_sigma=-3.5), sp=sp).ok
    assert run(cand(side="short", adv_usd=5e8, move_since_news_sigma=-4.1), sp=sp).code == "chased"


def test_s13_short_rules(sp):
    s = dict(side="short", adv_usd=5e8)
    assert run(cand(listing_days=179, **s), sp=sp).code == "short_new_listing"
    assert run(cand(short_interest_pct_float=20.1, **s), sp=sp).code == "short_crowded"
    assert run(cand(takeover_target=True, **s), sp=sp).code == "short_takeover_target"
    assert run(cand(last_session_sigma=-3.0, **s), sp=sp).code == "short_into_flush"
    assert run(cand(ret_20d=0.21, **s), sp=sp).code == "short_squeeze_risk"
    assert run(cand(from_52w_high=0.02, vol_ratio_last=3.5, **s), sp=sp).code == "short_squeeze_risk"
    assert run(cand(from_52w_high=0.02, vol_ratio_last=2.5, **s), sp=sp).ok


# ------------------------------------------------------------------------------ S12 / S14 / S15
def test_s12_reported_not_enforced(sp):
    v = run(cand(), book(fee_30d_nav_bps=99.0), sp)
    assert v.ok and "s12_over_budget" in v.flags
    enforce = sp.model_copy(update={"fees": sp.fees.model_copy(update={"mode": "enforce"})})
    assert run(cand(), book(fee_30d_nav_bps=99.0), enforce).code == "fee_budget"


def test_s14_cooloff(sp):
    stop = R.RecentExit("ACME", TODAY - timedelta(days=6), by_stop=True)        # 4 sessions ago
    assert run(cand(), book(recent_exits=[stop]), sp).code == "cooloff"
    stop = R.RecentExit("ACME", TODAY - timedelta(days=7), by_stop=True)        # 5 sessions ago
    assert run(cand(), book(recent_exits=[stop]), sp).ok
    ex = R.RecentExit("ACME", TODAY - timedelta(days=1), by_stop=False)
    assert run(cand(), book(recent_exits=[ex]), sp).code == "cooloff"


def test_s15_brake_blocker_setup(sp):
    assert run(cand(), book(brake_on=True), sp).code == "brake_on"
    assert R.public_code("brake_on") == "S15:brake_on"
    assert run(cand(), book(canary_pause=True), sp).code == "brake_engaged"
    assert run(cand(), book(brake_unknown=True), sp).code == "brake_unknown"
    assert run(cand(), book(blockers=["swing:trade:x"]), sp).code == "swing_blocker"
    assert run(cand(setup="gap_fade"), sp=sp).code == "setup_paper_only"
    assert run(cand(vehicle_owned_by_core=True), sp=sp).code == "vehicle_owned_by_core"


# ------------------------------------------------------------------------------ S16 / S17
def test_s16_entry_guard(sp):
    kw = dict(side="long", planned_rate=100.0, stop_rate=95.0, proposed_at=NOW, sp=sp)
    assert R.entry_guard(live_rate=101.5, now=NOW, **kw) is None                # 1.5% = min(2.5%, 1.5%)
    assert R.entry_guard(live_rate=101.6, now=NOW, **kw) == "swing_entry_ran"
    assert R.entry_guard(live_rate=95.0, now=NOW, **kw) == "swing_entry_stopped"
    assert R.entry_guard(live_rate=100.0, now=NOW + timedelta(minutes=61), **kw) == "expired"
    short = dict(kw, side="short", stop_rate=104.0)
    assert R.entry_guard(live_rate=98.5, now=NOW, **short) is None              # min(2%, 1.5%)
    assert R.entry_guard(live_rate=98.4, now=NOW, **short) == "swing_entry_ran"


def test_s17_drawdown_scaling_and_kill_states(sp):
    b = book(drawdown_from_peak=-0.10, trades=[trade("a"), trade("b", "short")])
    v = run(cand(), b, sp)
    assert v.ok and v.size_nav == pytest.approx(0.04) and "drawdown_scaled" in v.flags
    b3 = replace(b, trades=[*b.trades, trade("c", "short")])
    assert run(cand(), b3, sp).code == "max_open"                                # 3 open at -10%
    assert run(cand(), replace(b3, drawdown_from_peak=-0.09), sp).ok
    for state in ("WARN", "HALTED", "FLAT"):
        assert run(cand(), book(kill_state=state), sp).code == "kill_state"


# ------------------------------------------------------------------------------ final pass, exits
def test_final_pass_accepted_entries_join_the_book(sp):
    s = dict(side="short", adv_usd=5e8)
    cs = [cand(ref="idea:a", ticker="A", sector="A", **s), cand(ref="idea:b", ticker="B", sector="B", **s),
          cand(ref="idea:c", ticker="C", sector="C", **s)]
    ok, dropped = R.final_pass(cs, book(), sp, flat_cost())
    assert [v.ref for v in ok] == ["idea:a", "idea:b"]
    assert [(v.ref, v.code) for v in dropped] == [("idea:c", "max_short")]
    ok, dropped = R.final_pass([cand(ref=f"idea:{i}", ticker=f"T{i}", sector=f"S{i}", beta_60d=0.1)
                                for i in range(4)], book(entries_7d=4), sp, flat_cost())
    assert len(ok) == 2 and [d.code for d in dropped] == ["weekly_cap", "weekly_cap"]


def test_exit_due_time_stop_and_earnings():
    ts = R.add_sessions(TODAY, 2)
    assert R.exit_due(ts, TODAY, slots_per_session=1, lead_slots=2)[0]
    assert not R.exit_due(R.add_sessions(TODAY, 3), TODAY, slots_per_session=1, lead_slots=2)[0]
    assert not R.exit_due(ts, TODAY, slots_per_session=2, lead_slots=2)[0]
    far = R.add_sessions(TODAY, 10)
    due, when = R.exit_due(far, TODAY, slots_per_session=1, lead_slots=2,
                           earnings_next=R.add_sessions(TODAY, 3), earnings_confirmed=True)
    assert due and when == R.add_sessions(TODAY, 2)


def test_public_codes_cover_every_drop():
    assert R.public_code("weekly_cap") == "S3:weekly_cap"
    assert all(R.RULE_OF[c] for c in R.SWING_DROP_CODES)


def test_candidate_from_card_converts_percent_fields(sp):
    from council.swing.facts import FactCard

    fields = {"sigma_daily": 2.5, "atr14_pct": 3.0, "adv_usd_20d": 1e9, "px_ge_10": True, "beta_60d": 1.1,
              "short_interest_pct_float": 4.0, "dist_52w_high_pct": -2.0, "ret_20d": 5.0,
              "earnings_next": "2026-12-01", "earnings_confirmed": True, "move_since_news_live_sigma": 1.0}
    card = FactCard(line_id="ACME", side="long", slot=NOW.isoformat(), ok=True, fields=fields)
    c = R.candidate_from_card(card, ref="idea:a", ticker="ACME", setup="news_continuation", stop_pct=0.05,
                              target_pct=0.12, time_stop_days=15, sector="Tech")
    assert c.sigma_daily == pytest.approx(0.025) and c.atr_pct == pytest.approx(0.03)
    assert c.from_52w_high == pytest.approx(0.02) and c.ret_20d == pytest.approx(0.05)
    assert c.earnings_next == date(2026, 12, 1) and c.earnings_confirmed
    assert run(c, sp=sp).ok
    assert run(replace(c, px_ge_10=False), sp=sp).code == "price_too_low"
    assert run(replace(c, px_ge_10=None), sp=sp).code == "price_too_low"


# ------------------------------------------------------------------------------ review (SW-4 hunt)
def test_s2_one_swing_trade_per_ticker(sp):
    b = book(trades=[trade("brk", sector="Fin")])
    b = replace(b, trades=[replace(b.trades[0], ticker="BRK.B")])
    assert run(cand(ticker="BRK_B", sector="Other"), b, sp).code == "already_open"   # BRK.B == BRK_B
    ok, dropped = R.final_pass([cand(ref="idea:a", sector="A"), cand(ref="idea:b", sector="B")],
                               book(), sp, flat_cost())
    assert [v.ref for v in ok] == ["idea:a"] and [d.code for d in dropped] == ["already_open"]
    assert R.public_code("already_open") == "S2:already_open"


def test_unknown_kill_state_fails_closed(sp):
    assert run(cand(), book(kill_state="UNKNOWN"), sp).code == "kill_state"


def test_no_accepted_entry_breaks_a_hard_cap(sp):
    """Every accepted entry, over a grid of stops / ATR / SI / drawdown: size <= 8%, planned loss at
    the stop <= 0.8% (long) / 0.5% (short) NAV, stop within the side's range."""
    for side in ("long", "short"):
        for stop in (0.02, 0.03, 0.05, 0.07, 0.08, 0.10, 0.12):
            for atr in (0.01, 0.04, 0.09):
                for si in (5.0, None):
                    for dd in (None, -0.12):
                        c = cand(side=side, adv_usd=5e8, stop_pct=stop, atr_pct=atr, sigma_daily=0.04,
                                 target_pct=0.30, short_interest_pct_float=si)
                        v = run(c, book(drawdown_from_peak=dd), sp)
                        if not v.ok:
                            continue
                        cap = 0.008 if side == "long" else 0.005
                        assert v.size_nav <= 0.08 + 1e-12
                        assert v.size_nav * v.stop_pct <= cap + 1e-12
                        assert v.stop_pct <= (0.12 if side == "long" else 0.08) + 1e-12
                        if dd is not None:
                            assert v.size_nav <= 0.04 + 1e-12
