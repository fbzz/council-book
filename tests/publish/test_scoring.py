from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from council.scoring import controls, scoreboard, twr

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _t(i: int) -> pd.Timestamp:
    return pd.Timestamp(T0 + timedelta(days=i))


# ------------------------------------------------------------------------------ TWR
def test_twr_ignores_deposits():
    marks = [(T0, 100.0), (T0 + timedelta(days=1), 110.0), (T0 + timedelta(days=2), 160.0, 50.0)]
    idx = [v for _, v in twr.nav_index(marks)]
    assert idx == pytest.approx([100.0, 110.0, 110.0])


def test_twr_withdrawal_is_not_a_loss():
    marks = [(T0, 100.0), (T0 + timedelta(days=1), 60.0, -40.0)]
    assert twr.nav_index(marks)[-1][1] == pytest.approx(100.0)


def test_twr_rejects_bad_marks():
    with pytest.raises(ValueError):
        twr.nav_index([(T0, 100.0), (T0, 101.0)])
    with pytest.raises(ValueError):
        twr.nav_index([(T0, 0.0)])


def test_drawdown_and_peak_fraction():
    values = [100.0, 120.0, 90.0, 96.0]
    assert twr.drawdown(values) == pytest.approx([0.0, 0.0, -0.25, -0.2])
    assert twr.peak_fraction(values) == pytest.approx(0.8)
    assert twr.period_returns([100.0, 110.0]) == pytest.approx([0.1])


# ------------------------------------------------------------------------------ controls
def _returns():
    return pd.DataFrame({"A": [0.10, -0.05, 0.02], "B": [0.0, 0.10, -0.10]}, index=[_t(1), _t(2), _t(3)])


def test_book_index_no_lookahead_and_drift():
    w = pd.DataFrame({"A": [0.5], "B": [0.0]}, index=[_t(0)])
    idx = controls.book_index(w, _returns())
    # day 1: 0.5 * 10% = +5%; holding drifts to 0.55/1.05
    assert idx[_t(1)] == pytest.approx(105.0)
    h = 0.55 / 1.05
    assert idx[_t(2)] == pytest.approx(105.0 * (1 + h * -0.05))


def test_weights_decided_at_period_end_earn_the_next_period_only():
    w = pd.DataFrame({"A": [1.0]}, index=[_t(1)])       # decided at the END of day 1
    idx = controls.book_index(w, _returns())
    assert idx[_t(1)] == pytest.approx(100.0)            # the +10% of day 1 is not earned


def test_costs_are_charged_per_side_on_traded_weight():
    w = pd.DataFrame({"A": [0.5]}, index=[_t(0)])
    idx = controls.book_index(w, pd.DataFrame({"A": [0.0]}, index=[_t(1)]), cost_bps=100)
    assert idx[_t(0)] == pytest.approx(99.5)             # 0.5 x 1%


def test_deadband_skips_small_changes_but_not_closes():
    w = pd.DataFrame({"A": [0.50, 0.51, 0.0]}, index=[_t(0), _t(1), _t(2)])
    r = pd.DataFrame({"A": [0.0, 0.0, 0.0]}, index=[_t(1), _t(2), _t(3)])
    idx = controls.book_index(w, r, cost_bps=100, deadband_x=0.02)
    assert idx[_t(1)] == pytest.approx(99.5)             # 0.01 change skipped: no cost
    assert idx[_t(2)] == pytest.approx(99.5 * (1 - 0.005))   # close to 0 always trades


def test_c2x_scales_to_council_exposure():
    ref = pd.DataFrame({"A": [0.8]}, index=[_t(0)])
    council = pd.DataFrame({"A": [0.4]}, index=[_t(0)])
    assert controls.exposure_scale(council, ref) == pytest.approx(0.5)
    idx = controls.c2x_exposure_matched(ref, council, _returns())
    assert idx[_t(1)] == pytest.approx(104.0)            # 0.4 x 10%


def test_c3_hold_never_trades():
    idx = controls.c3_hold({"A": 0.5, "B": 0.5}, _returns())
    assert idx.iloc[0] == pytest.approx(105.0)
    assert len(idx) == 3


def test_c4_buy_and_hold():
    out = controls.c4_buy_and_hold(pd.DataFrame({"SPY": [0.01, 0.02], "BTCUSDT": [0.1, None]}, index=[_t(1), _t(2)]))
    assert out["SPY"].iloc[-1] == pytest.approx(100 * 1.01 * 1.02)
    assert out["BTCUSDT"].iloc[-1] == pytest.approx(110.0)


def test_c2_reference_equals_book_index():
    ref = pd.DataFrame({"A": [0.5]}, index=[_t(0)])
    pd.testing.assert_series_equal(controls.c2_reference(ref, _returns()), controls.book_index(ref, _returns()))


# ------------------------------------------------------------------------------ scoreboard
def _card(i, direction, ret, role="news", sigma=0.01):
    return scoreboard.CardOutcome(card_id=f"K:{role}:{i}", role=role, card_type="news_material", direction=direction,
                                  created_at=T0 + timedelta(days=i), forward={1: ret, 5: ret, 20: None}, sigma_daily=sigma)


def test_hits_and_normalised_scores():
    assert scoreboard.hit("risk_up", 0.01) and not scoreboard.hit("risk_up", 0.0)
    assert scoreboard.hit("risk_down", -0.01) and scoreboard.hit("neutral", 0.5) is None
    assert scoreboard.normalised_score("risk_down", -0.02, 0.01, 4) == pytest.approx(1.0)


def test_wilson_interval():
    lo, hi = scoreboard.wilson(55, 100)
    assert lo < 0.55 < hi and hi - lo == pytest.approx(0.19, abs=0.01)
    assert scoreboard.wilson(0, 0) is None


def test_not_yet_meaningful_below_60_cards():
    cards = [_card(i, "risk_up", 0.01) for i in range(59)] + [_card(200, "risk_up", 0.01)]
    s = scoreboard.score_role("news", cards[:59])
    assert not s.meaningful and s.label == scoreboard.NOT_YET


def test_not_yet_meaningful_below_26_weeks():
    cards = [_card(i % 100, "risk_up", 0.01) for i in range(80)]
    s = scoreboard.score_role("news", cards)
    assert s.directional == 80 and s.span_weeks < 26 and not s.meaningful


def test_meaningful_with_60_cards_over_26_weeks():
    cards = [_card(i * 4, "risk_up" if i % 2 else "risk_down", 0.01) for i in range(61)]
    s = scoreboard.score_role("news", cards)
    assert s.meaningful and s.label == ""
    assert s.hit_rate[1] == pytest.approx(30 / 61)       # risk_down with +1% is a miss
    assert s.resolved[20] == 0 and s.hit_rate[20] is None


def test_scoreboard_groups_by_role_and_counts_neutral():
    board = scoreboard.scoreboard([_card(1, "neutral", 0.01, role="macro"), _card(2, "risk_up", 0.01)])
    assert [s.role for s in board] == ["macro", "news"]
    assert board[0].cards == 1 and board[0].directional == 0
