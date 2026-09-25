"""reconcile(): drift limit, stop on every position, unknown positions, expected fills."""

from __future__ import annotations

from datetime import UTC, datetime

from council.execution.reconcile import ExpectedPosition, reconcile
from council.models.broker import ExposureSnapshot, Position

NOW = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)


def _pos(pid, symbol, *, is_buy=True, leverage=1, sl=90.0):
    return Position(position_id=pid, instrument_id=pid, symbol=symbol, is_buy=is_buy,
                    leverage=leverage, units=1.0, open_rate=100.0, amount=100.0, sl_rate=sl)


def _snap(signed_w, positions):
    return ExposureSnapshot(taken_at=NOW, equity_usd=1.0, credit_usd=1.0, positions=positions,
                            signed_w=signed_w, gross=0.0, net=0.0, margin_use=0.0)


def test_drift_at_limit_passes(policy):
    assert policy.risk["reconcile"]["drift_max"] == 0.03
    res = reconcile(_snap({"SPX500": 0.12, "NSDQ100": 0.30}, [_pos(1, "SPX500"), _pos(2, "NSDQ100")]),
                    {"SPX": 0.10, "NDX": 0.31}, [], policy)
    assert res.drift == 0.03 or abs(res.drift - 0.03) < 1e-12
    assert res.ok and res.achieved_w == {"SPX": 0.12, "NDX": 0.30}


def test_drift_above_limit_fails(policy):
    res = reconcile(_snap({"SPX500": 0.131}, [_pos(1, "SPX500")]), {"SPX": 0.10}, [], policy)
    assert not res.ok and not res.drift_ok and res.protected


def test_vehicles_aggregate_into_their_line(policy):
    res = reconcile(_snap({"SPX500": 0.05, "SPY": 0.05}, [_pos(1, "SPX500"), _pos(2, "SPY")]),
                    {"SPX": 0.10}, [], policy)
    assert res.ok and res.achieved_w["SPX"] == 0.1


def test_missing_stop_loss_fails(policy):
    res = reconcile(_snap({"GOLD": 0.1}, [_pos(1, "GOLD", sl=None)]), {"GOLD": 0.1}, [], policy)
    assert not res.ok and res.missing_sl == ["GOLD"]


def test_unknown_position_fails(policy):
    res = reconcile(_snap({"UNMAPPED_77": 0.02}, [_pos(1, "UNMAPPED_77")]), {}, [], policy)
    assert not res.ok and res.unknown_positions == ["UNMAPPED_77"]


def test_expected_position_checks(policy):
    snap = _snap({"SPX500": 0.1}, [_pos(1, "SPX500", sl=90.0)])
    within = ExpectedPosition(position_id=1, symbol="SPX500", direction="long", sl_rate=90.0 * 1.001)
    assert reconcile(snap, {"SPX": 0.1}, [within], policy).ok               # 0.1% <= 0.2%
    outside = within.model_copy(update={"sl_rate": 90.0 * 1.003})
    res = reconcile(snap, {"SPX": 0.1}, [outside], policy)                  # 0.3% > 0.2%
    assert not res.ok and "stop-loss rate" in res.issues[0]
    wrong = ExpectedPosition(position_id=1, symbol="SPX500", direction="short", leverage=2)
    issues = reconcile(snap, {"SPX": 0.1}, [wrong], policy).issues
    assert "SPX500: direction mismatch" in issues and "SPX500: leverage mismatch" in issues
    gone = ExpectedPosition(position_id=9, symbol="SPX500", direction="long")
    assert reconcile(snap, {"SPX": 0.1}, [gone], policy).issues == ["SPX500: expected position missing"]
