"""SW-6: the §8.2 metrics and the §8.3 pre-declared rules (design swing-book.md rev 2).

Accept: the Skeptic-test consequence fires on a fixture (rejected beat passed by 0.3 R over 40 ideas
-> advisory flag); the pause rule fires on `r <= 0` alone and on "trails matched index" alone."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from council.swing import metrics as M

FAST = 2000


def trade(r, net, *, size=0.08, side="long", beta=1.0, etf=0.0, exit_kind="target", days=5):
    return M.ClosedTrade(r_declared=r, net_ret=net, size_nav=size, side=side, beta=beta, sector_etf_ret=etf,
                         exit_kind=exit_kind, days_held=days)


def test_skeptic_consequence_fires_advisory():
    outcomes = [{"verdict": "reject", "r_declared": 0.5}] * 20 + [{"verdict": "pass", "r_declared": 0.2}] * 20
    t = M.skeptic_test(outcomes)
    assert t.status == "advisory" and t.advisory and t.n == 40


def test_skeptic_test_needs_40_and_ignores_wait():
    few = [{"verdict": "reject", "r_declared": 1.0}] * 19 + [{"verdict": "pass", "r_declared": 0.0}] * 20
    waits = [{"verdict": "wait", "r_declared": 5.0}] * 30
    t = M.skeptic_test(few + waits)
    assert t.status == "insufficient" and t.n == 39 and t.wait_mean == 5.0
    value = [{"verdict": "reject", "r_declared": -0.2}] * 20 + [{"verdict": "pass", "r_declared": 0.1}] * 20
    assert M.skeptic_test(value).status == "skeptic_value"
    close = [{"verdict": "reject", "r_declared": 0.2}] * 20 + [{"verdict": "pass", "r_declared": 0.1}] * 20
    assert M.skeptic_test(close).status == "inconclusive"


def test_pause_fires_on_r_le_0_alone():
    # mean R negative, but the swing contribution beats its matched index (the ETF fell for longs)
    trades = [trade(-0.1, -0.005, etf=-0.05)] * 20
    d = M.pause_rule(trades)
    assert d.applies and d.pause and d.reasons == ("mean_r_declared_le_0",)
    assert M.pause_rule([trade(0.0, 0.0, etf=-0.5)] * 20).reasons == ("mean_r_declared_le_0",)


def test_pause_fires_on_trailing_the_matched_index_alone():
    # positive R, but beta x sector ETF made more after the same declared cost
    trades = [trade(0.4, 0.02, beta=1.2, etf=0.08)] * 20
    d = M.pause_rule(trades)
    assert d.pause and d.reasons == ("trails_matched_index",)


def test_pause_passes_and_waits_for_20():
    good = [trade(0.4, 0.02, etf=0.0)] * 20
    assert not M.pause_rule(good).pause
    assert M.pause_rule(good[:19]) == M.PauseDecision(applies=False, pause=False, n=19)
    unmatched = [trade(0.4, 0.02, beta=None, etf=None)] * 20
    assert M.pause_rule(unmatched).reasons == ("matched_index_unavailable",)


def test_bootstrap_and_summary():
    rs = list(np.linspace(-1, 2, 30))
    iv = M.bootstrap_mean(rs, resamples=FAST)
    assert iv.low < iv.mean < iv.high and iv.n == 30
    assert M.bootstrap_mean(rs, resamples=FAST) == iv                    # seeded
    assert M.bootstrap_mean([]).mean is None
    trades = [trade(1.5, 0.075, exit_kind="target"), trade(-1.0, -0.05, exit_kind="stop"),
              trade(-0.5, -0.025, exit_kind="time", days=10)]
    s = M.summarize(trades, resamples=FAST)
    assert s.hit_rate == pytest.approx(1 / 3) and s.payoff == pytest.approx(1.5 / 0.75)
    assert s.contribution_bps == pytest.approx(0.08 * 0.0 * 1e4)
    assert s.matched_contribution_bps == pytest.approx(3 * 0.08 * -0.025 * 1e4)
    assert s.vs_matched == pytest.approx(0.025)
    assert s.exit_mix["stop"] == pytest.approx(1 / 3) and s.avg_days_held == pytest.approx(20 / 3)


def test_funnel_by_group():
    f = M.funnel([{"group": "executed", "r_declared": 1.0}, {"group": "executed", "r_declared": -1.0},
                  {"group": "skeptic_wait", "r_declared": 0.5}], resamples=FAST)
    assert f["executed"].mean == 0.0 and f["executed"].n == 2 and f["skeptic_wait"].n == 1


def test_review_due_and_promotion():
    g = date(2026, 11, 1)
    assert not M.review_due(29, g, date(2027, 1, 29))
    assert M.review_due(30, g, date(2026, 12, 1))
    assert M.review_due(5, g, date(2027, 1, 30))                         # 90 days first
    assert not M.review_due(5, g, date(2027, 2, 5), reviewed_day90=True)
    assert M.review_due(60, g, date(2027, 6, 1), last_review_n=30)
    assert not M.review_due(59, g, date(2027, 6, 1), last_review_n=30)
    assert M.setup_promotion([0.3, 0.1] * 15, resamples=FAST).eligible
    assert not M.setup_promotion([0.3, 0.1] * 14, resamples=FAST).eligible
    assert not M.setup_promotion([2.0, -1.8] * 15, resamples=FAST).eligible   # lower bound <= -0.1
    assert M.standard_error([1.0, -1.0, 1.0, -1.0]) == pytest.approx(np.std([1, -1, 1, -1], ddof=1) / 2)
