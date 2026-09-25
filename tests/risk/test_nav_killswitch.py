"""NAV (lifetime peak) and the R3 soft kill switch: warn_at 0.80, halt_at 0.75,
confirm_reads 2, confirm_gap_s 60."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council.risk.killswitch import allows_increase, confirmed_breach_reads, evaluate, resume
from council.risk.nav import nav_index, start_nav, update_nav
from tests.risk.helpers import override

# ----------------------------------------------------------------------------- NAV


def test_nav_index_and_lifetime_peak(now):
    s = update_nav(None, 100.0, now)
    s = update_nav(s, 120.0, now + timedelta(hours=4))
    s = update_nav(s, 90.0, now + timedelta(hours=8))
    assert s.peak == 120.0 and s.last == 90.0
    assert s.nav_index == pytest.approx(90.0) and s.peak_index == pytest.approx(120.0)
    assert s.drawdown == pytest.approx(0.25)
    assert nav_index(s, 110.0) == pytest.approx(110.0)


def test_nav_out_of_order_read_counts_toward_peak_only(now):
    s = start_nav(100.0, now)
    s = update_nav(s, 130.0, now - timedelta(minutes=5))
    assert s.peak == 130.0 and s.last == 100.0 and s.updated_at == now


def test_nav_rejects_bad_reads(now):
    with pytest.raises(ValueError):
        start_nav(0.0, now)
    with pytest.raises(ValueError):
        update_nav(start_nav(100.0, now), -1.0, now)
    with pytest.raises(ValueError):
        start_nav(100.0, now.replace(tzinfo=None))


# ----------------------------------------------------------------------------- kill switch


def _nav(now, peak=100.0, last=100.0):
    return start_nav(100.0, now).model_copy(update={"peak": peak, "last": last})


def _eval(policy, now, reads, prev="NORMAL", positions=True, peak=100.0):
    return evaluate(nav=_nav(now, peak=peak), equity_reads=reads, prev_state=prev,
                    has_positions=positions, policy=policy)


def test_warn_at_080_pass_and_fail(policy, now):
    assert _eval(policy, now, [(now, 80.01)]).state == "NORMAL"
    d = _eval(policy, now, [(now, 80.0)])
    assert d.state == "WARN" and d.drawdown == pytest.approx(0.20)
    assert not allows_increase("WARN") and allows_increase("NORMAL")


def test_halt_at_075_needs_two_reads_60s_apart(policy, now):
    one = _eval(policy, now, [(now, 75.0)])
    assert one.state == "WARN" and "awaiting confirmation" in one.reason
    too_close = _eval(policy, now, [(now - timedelta(seconds=59), 74.0), (now, 75.0)])
    assert too_close.state == "WARN"
    confirmed = _eval(policy, now, [(now - timedelta(seconds=60), 74.0), (now, 75.0)])
    assert confirmed.state == "HALTED" and confirmed.confirmed_reads == 2
    above = _eval(policy, now, [(now - timedelta(seconds=60), 75.01), (now, 75.01)])
    assert above.state == "WARN"


def test_a_read_above_the_line_resets_confirmation(policy, now):
    reads = [(now - timedelta(seconds=120), 70.0), (now - timedelta(seconds=60), 76.0),
             (now, 74.0)]
    assert _eval(policy, now, reads).state == "WARN"
    assert confirmed_breach_reads(reads, 75.0, 60) == 1


def test_confirm_reads_comes_from_policy(policy, now):
    strict = override(policy, "risk", {"killswitch.confirm_reads": 3})
    reads = [(now - timedelta(seconds=60), 74.0), (now, 74.0)]
    assert _eval(strict, now, reads).state == "WARN"
    reads.insert(0, (now - timedelta(seconds=120), 74.0))
    assert _eval(strict, now, reads).state == "HALTED"


def test_halt_without_positions_is_flat_and_latches(policy, now):
    reads = [(now - timedelta(seconds=60), 70.0), (now, 70.0)]
    assert _eval(policy, now, reads, positions=False).state == "FLAT"
    # latched: equity recovering above every line does not un-halt
    assert _eval(policy, now, [(now, 99.0)], prev="HALTED").state == "HALTED"
    assert _eval(policy, now, [(now, 99.0)], prev="HALTED", positions=False).state == "FLAT"


def test_peak_is_lifetime_max_of_reads(policy, now):
    d = _eval(policy, now, [(now - timedelta(hours=1), 130.0), (now, 104.0)], peak=100.0)
    assert d.peak == 130.0 and d.state == "WARN"


def test_resume_requires_reason_and_keeps_peak(policy, now):
    with pytest.raises(ValueError):
        resume("HALTED", "  ")
    with pytest.raises(ValueError):
        resume("NORMAL", "why")
    r = resume("FLAT", "operator reviewed the drawdown")
    assert r.state == "NORMAL" and "operator reviewed" in r.reason
    reads = [(now - timedelta(seconds=60), 74.0), (now, 74.0)]
    assert _eval(policy, now, reads, prev=r.state).state == "HALTED"  # same lifetime peak


def test_evaluate_without_reads_uses_nav(policy, now):
    nav = start_nav(100.0, now).model_copy(update={"last": 79.0})
    d = evaluate(nav=nav, equity_reads=[], prev_state="NORMAL", has_positions=True, policy=policy)
    assert d.state == "WARN"
