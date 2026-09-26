"""WP-G end to end, offline (stub LLM, mock HTTP transports, fake broker, no publisher):

- a cycle with 40 stock lines whose stock source answers 429 finishes: the core is evaluated, every
  stock line is frozen no_data and held, and the history gather stays inside its time budget;
- the per-line material-change state and its one-off read of a legacy single fingerprint;
- the plan reads eligibility and rates only for current lines' vehicles and held positions.
Nothing is approved or executed; `invariants.STOCK_SLEEVE_LIVE` stays False (the sleeve policy is
handed to the context directly, never through `build_context`)."""

from __future__ import annotations

from collections import Counter

import httpx
import pytest

from council.cycle import _material, core_lines, evidence_lines, plan_instrument_ids, run_cycle
from council.data import alpaca
from council.facts.market import gather_history
from council.ledger.db import Ledger
from council.models.broker import ExposureSnapshot, Position
from council.runtime import (
    MATERIAL_GLOBAL,
    Sources,
    consume_fingerprints,
    material_fingerprint,
    material_fingerprints,
)
from tests.data.test_market_live_path import ALPACA, TIINGO, Clock, Router, sleeve_policy_with
from tests.integration.test_end_to_end import NOW, SLOT, _ctx, _history, _no_events


@pytest.fixture(scope="module")
def forty(tmp_path_factory):
    return sleeve_policy_with(tmp_path_factory, 32, 8)


def test_a_cycle_with_40_stock_lines_and_a_rate_limited_stock_source_finishes(tmp_path, forty):
    clock = Clock()
    router = Router(clock=clock, cost_s=20.0)
    router.status[ALPACA] = [429]
    client = httpx.Client(transport=httpx.MockTransport(router))
    keys = alpaca.AlpacaKeys(key_id="PKTESTKEYID0001", secret="sEcReTvAlUe-never-logged-1")

    def history(slot):
        return gather_history(forty, now=slot, tiingo_token="tok", alpaca_keys=keys, client=client,
                              monotonic=clock)

    ctx = _ctx(tmp_path)
    ctx.policy = forty
    ctx.sources = Sources(history=history, events=_no_events)
    out = run_cycle(ctx)
    client.close()
    assert out.status == "on_time" and out.decision_state == "reviewed_no_action"   # no broker yet
    assert router.count() == Counter({TIINGO: 7, "data-api.binance.vision": 2, ALPACA: 1})
    assert clock.t <= 300.0                                             # 10 requests of 20 s each
    stocks = [ln.symbol for ln in forty.universe.stock_lines()]
    assert sum(f.startswith("history_rate_limited:") for f in out.flags) == alpaca.SYMBOLS_PER_REQUEST
    assert sum(f.startswith("history_breaker:") for f in out.flags) == 40 - alpaca.SYMBOLS_PER_REQUEST
    record = ctx.ledger.get_cycle(out.cycle_id)
    final = record["risk"]["final_w"]
    assert all(final[s] == 0.0 for s in stocks)                         # the satellite is held (no_data)
    assert any(final[s] > 0 for s in core_lines(forty) if s in ("NDX", "SPX", "GOLD", "SEMIS", "BTC", "ETH"))
    assert record["material_fingerprint"] and len(record["material_fingerprint"]) == 64


def test_material_state_reads_a_legacy_fingerprint_once(tmp_path, policy):
    from council.facts.pack import build_fact_pack

    ledger = Ledger(tmp_path / "l.sqlite3")
    ledger.migrate()
    pack = build_fact_pack(cycle_id="c", slot=SLOT, now=NOW, policy=policy, states={})
    fps, changed = _material(ledger, pack, [], "NORMAL")
    assert set(changed.values()) == {True}                              # nothing stored: first cycle
    ledger.set_runtime("last_material_fingerprint", material_fingerprint(pack, [], "NORMAL"))
    _, changed = _material(ledger, pack, [], "NORMAL")
    assert set(changed.values()) == {False}                             # the legacy fingerprint matches
    ledger.set_material_fingerprints(consume_fingerprints(material_fingerprints(pack, [], "WARN"), None, []))
    _, changed = _material(ledger, pack, [], "NORMAL")
    assert set(changed.values()) == {True}                              # the map wins once stored


def test_evidence_lines_are_the_admitted_lines_not_held_by_a_blocker(sleeve_policy):
    from types import SimpleNamespace

    pack = SimpleNamespace(admitted=["NDX", "BTC", "TSTA"])
    assert evidence_lines(sleeve_policy, pack, []) == ["NDX", "BTC", "TSTA"]
    assert evidence_lines(sleeve_policy, pack, ["satellite:waiting_for_market"]) == ["NDX", "BTC"]
    assert evidence_lines(sleeve_policy, pack, ["execution_unknown:d1", "satellite:x"]) == []


def test_the_plan_reads_only_current_vehicles_and_held_instruments(tmp_path, policy):
    from council.broker.instruments import InstrumentMap

    found = {"EQQQ.L": 201, "SMH.L": 202, "BTC": 205, "OLDCO": 901, "GONE.L": 902}   # 901/902: retired names
    imap = InstrumentMap({}, path=tmp_path / "instruments.json").merged(found, NOW)
    held = Position(position_id=1, instrument_id=902, symbol="GONE.L", is_buy=True, units=1.0, open_rate=10.0,
                    amount=10.0, settlement="real")
    snap = ExposureSnapshot(taken_at=NOW, equity_usd=1000.0, credit_usd=0.0, positions=[held], signed_w={},
                            gross=0.0, net=0.0, margin_use=0.0)
    assert plan_instrument_ids(policy, imap, snap) == [201, 202, 205, 902]
    assert plan_instrument_ids(policy, imap, None) == [201, 202, 205]


class RecordingBroker:
    """The READ client, recording the instrument ids of eligibility and rates requests."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.eligibility_ids: list[list[int]] = []
        self.rate_ids: list[list[int]] = []

    def eligibility(self, symbols=None, instrument_ids=None):
        self.eligibility_ids.append(list(instrument_ids or []))
        return self.inner.eligibility(symbols=symbols, instrument_ids=instrument_ids)

    def rates(self, ids):
        self.rate_ids.append(list(ids))
        return self.inner.rates(ids)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_connected_cycle_does_not_ask_for_retired_instruments(tmp_path):
    from council.broker.etoro_read import EtoroReadClient
    from council.broker.fake import FakeClock, FakeEtoro, eligibility_row, leverage_config
    from council.broker.instruments import InstrumentMap
    from tests.integration.test_end_to_end import API_KEY, READ_KEY, VEHICLES, WRITE_KEY

    fclock = FakeClock(start=NOW)
    fake = FakeEtoro(clock=fclock.now, credit=10_000.0, write_user_keys={WRITE_KEY})
    for sym, (iid, px, settlement) in VEHICLES.items():
        configs = ([leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
                   if settlement == "real" else [leverage_config(direction="LONG"), leverage_config(direction="SHORT")])
        fake.add_instrument(sym, iid, bid=px * 0.9995, ask=px * 1.0005, row=eligibility_row(sym, iid, configs=configs))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    InstrumentMap({}, path=state / "instruments.json").merged(
        {**{s: v[0] for s, v in VEHICLES.items()}, "OLDCO": 990}, NOW).save()   # a retired stock
    broker = RecordingBroker(EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep))
    ctx = _ctx(tmp_path, broker=broker)
    out = run_cycle(ctx)
    assert out.decision_id, out.flags
    assert broker.eligibility_ids and all(990 not in ids for ids in broker.eligibility_ids)
    assert broker.rate_ids and all(990 not in ids for ids in broker.rate_ids)
    assert sorted(broker.rate_ids[-1]) == sorted(v[0] for v in VEHICLES.values())


def test_a_proposal_does_not_consume_the_evidence_of_a_line_that_could_not_act(tmp_path):
    """A connected cycle issues a proposal while OIL has no data (frozen, not admitted): the stored
    material key of OIL is kept, the admitted lines' keys are replaced (MC per line, design §11.4)."""
    from council.broker.etoro_read import EtoroReadClient
    from council.broker.fake import FakeClock, FakeEtoro, eligibility_row, leverage_config
    from council.broker.instruments import InstrumentMap
    from tests.integration.test_end_to_end import API_KEY, READ_KEY, VEHICLES, WRITE_KEY

    fclock = FakeClock(start=NOW)
    fake = FakeEtoro(clock=fclock.now, credit=10_000.0, write_user_keys={WRITE_KEY})
    for sym, (iid, px, settlement) in VEHICLES.items():
        configs = ([leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,))]
                   if settlement == "real" else [leverage_config(direction="LONG"), leverage_config(direction="SHORT")])
        fake.add_instrument(sym, iid, bid=px * 0.9995, ask=px * 1.0005, row=eligibility_row(sym, iid, configs=configs))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    InstrumentMap({}, path=state / "instruments.json").merged({s: v[0] for s, v in VEHICLES.items()}, NOW).save()
    broker = EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)

    def history_without_oil(slot):
        bars, flags = _history(slot)
        return {s: b for s, b in bars.items() if s != "OIL"}, flags

    from council.publish.gitops import Publisher

    publisher = Publisher(state / "publisher-clone", push=False, dry_run_dir=tmp_path / "preview")
    ctx = _ctx(tmp_path, broker=broker, publisher=publisher)
    ctx.sources = Sources(history=history_without_oil, events=_no_events, broker=broker)
    sentinel = "0" * 64
    ctx.ledger.set_material_fingerprints({MATERIAL_GLOBAL: sentinel, "OIL": sentinel, "NDX": sentinel})
    out = run_cycle(ctx)
    assert out.decision_id and out.decision_state == "proposed", out.flags
    stored = ctx.ledger.get_material_fingerprints()
    assert stored["OIL"] == sentinel                                   # frozen no_data: not consumed
    assert stored["NDX"] != sentinel and stored[MATERIAL_GLOBAL] != sentinel
    assert set(stored) == {MATERIAL_GLOBAL} | {ln.symbol for ln in ctx.policy.universe.lines}


def test_a_failing_fundamentals_source_never_stops_the_cycle(tmp_path, sleeve_policy):
    def broken(slot):
        raise RuntimeError("sec parser bug")

    ctx = _ctx(tmp_path)
    ctx.policy = sleeve_policy
    ctx.sources = Sources(history=_history, events=_no_events, fundamentals=broken)
    out = run_cycle(ctx)
    assert out.status == "on_time" and "fundamentals_error:RuntimeError" in out.flags
