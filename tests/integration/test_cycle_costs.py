"""WP-E end to end, offline (stub LLM, synthetic history, fake broker, no publisher): a connected
cycle prices the $1 fee privately from the snapshot equity and the mirror ratio, flags a missing
ratio, stamps legs with origin, reference level and fee, stores the held-level migration, and
withholds the fee-bearing R14 value. Nothing is approved or executed."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from council.broker.etoro_read import EtoroReadClient
from council.cycle import gap_references, run_cycle
from council.operator.mirror import set_mirror
from council.risk.held_levels import MIGRATION_KEY
from tests.integration.test_end_to_end import API_KEY, NOW, READ_KEY, VEHICLES, WRITE_KEY, _ctx
from tests.risk.helpers import state


@pytest.fixture
def broker(tmp_path):
    from council.broker.fake import FakeClock, FakeEtoro, eligibility_row, leverage_config
    from council.broker.instruments import InstrumentMap

    fclock = FakeClock(start=NOW)
    fake = FakeEtoro(clock=fclock.now, credit=10_000.0, write_user_keys={WRITE_KEY})
    for sym, (iid, px, settlement) in VEHICLES.items():
        if settlement == "real":
            configs = [leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
        else:
            configs = [leverage_config(direction="LONG"), leverage_config(direction="SHORT")]
        fake.add_instrument(sym, iid, bid=px * 0.9995, ask=px * 1.0005,
                            row=eligibility_row(sym, iid, configs=configs))
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    InstrumentMap({}, path=state_dir / "instruments.json").merged(
        {s: v[0] for s, v in VEHICLES.items()}, NOW).save()
    return EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)


def _checks(ctx, cycle_id):
    record = ctx.ledger.get_cycle(cycle_id)
    return {(c["rule_id"], c["name"]): c for c in record["risk"]["checks"]}


def test_connected_cycle_prices_the_fee_privately(tmp_path, broker):
    ctx = _ctx(tmp_path, broker=broker)
    out = run_cycle(ctx)
    assert out.decision_id, out.flags
    assert "mirror_ratio_missing" in out.flags
    legs = ctx.ledger.legs(out.decision_id)
    assert legs
    for row in legs:
        assert row.detail["origin"] == "reference"                    # a flat book built toward the reference
        assert row.detail["ref_level"] is not None
        fee = row.detail["fee_bps_nav"]
        if row.settlement == "real" and row.line not in ("BTC", "ETH"):
            # $10k equity, assumed mirror 0.1: 1e4 x (1/10,000 + 1/1,000) = 11 bps
            assert fee == pytest.approx(11.0) and row.detail["fee_drag"] > 0
        else:
            assert fee == 0.0
    assert any(r.detail["fee_bps_nav"] > 0 for r in legs)
    assert _checks(ctx, out.cycle_id)[("R14", "cycle_cost_bps")]["value"] == "R14_fee"
    assert ctx.ledger.get_runtime(MIGRATION_KEY)["levels"]
    peak = ctx.ledger.get_runtime("real_adjusted_peak")                 # D19: stored, private
    assert isinstance(peak, float) and peak > 0
    decision = ctx.ledger.get_decision(out.decision_id)
    assert decision.plan["fee_bps_nav"] == pytest.approx(sum(r.detail["fee_bps_nav"] for r in legs))
    assert decision.plan["cost_bps_nav"] == pytest.approx(sum(r.detail["cost_bps_nav"] for r in legs))

    set_mirror(ctx.state_dir, ratio=0.25, now=NOW)                    # the operator stores the ratio
    again = run_cycle(ctx, force=True)
    assert again.decision_id and "mirror_ratio_missing" not in again.flags
    fees = {r.detail["fee_bps_nav"] for r in ctx.ledger.legs(again.decision_id)} - {0.0}
    assert fees and all(f == pytest.approx(5.0) for f in fees)        # 1e4 x (1/10,000 + 1/2,500)


def test_gap_references_cover_stock_lines_only(sleeve_policy):
    idx = pd.bdate_range(end=pd.Timestamp(NOW.date()) - timedelta(days=1), periods=5)
    frame = pd.DataFrame({"close": [100.0, 101.0, 102.0, 103.0, 104.0]}, index=idx)
    states = {"TSTA": state("TSTA", "stock", sigma_ann=0.3176), "NDX": state("NDX", "index")}
    out = gap_references(sleeve_policy, ["TSTA", "TSTB", "NDX"], states, {"TSTA": frame, "NDX": frame})
    assert set(out) == {"TSTA", "TSTB"}                               # core lines are never guarded
    assert out["TSTA"][0] == 104.0 and out["TSTA"][1] == pytest.approx(0.3176 / 252 ** 0.5)
    assert out["TSTB"] is None                                        # no history: the open is skipped
