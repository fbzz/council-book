"""select_config / resolve_vehicle rules, and the immutable instrument map."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from council.broker.eligibility import parse_eligibility, resolve_vehicle, select_config
from council.broker.etoro_read import EtoroReadClient
from council.broker.fake import FakeEtoro, eligibility_row, leverage_config
from council.broker.instruments import InstrumentIdentityChanged, InstrumentMap, resolve
from council.paths import REPO_ROOT, state_dir

NOW = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)


def _row(symbol, iid, configs, **kw):
    (row,) = parse_eligibility({"eligibilities": [eligibility_row(symbol, iid, configs=configs, **kw)]}, NOW)
    return row


# ------------------------------------------------------------------------------ select_config
def test_select_config_rules():
    row = _row("X", 1, [
        leverage_config(settlement="CFD", direction="LONG", leverage_values=[1, 2]),
        leverage_config(settlement="REAL", direction="LONG", leverage_values=[1]),
        leverage_config(settlement="CFD", direction="SHORT", leverage_values=[1, 2], is_potential=True),
    ])
    assert select_config(row, "long", 1).settlement == "real"            # 1x long prefers real
    assert select_config(row, "long", 1, settlement="cfd").settlement == "cfd"
    assert select_config(row, "long", 2).settlement == "cfd"
    assert select_config(row, "long", 5) is None                          # leverage not offered
    assert select_config(row, "short", 1) is None                         # isPotential = unavailable


def test_select_config_needs_open_permission_and_stop_support():
    closed = _row("X", 1, [leverage_config()], allow_open=False)
    assert select_config(closed, "long", 1) is None
    no_sl = _row("X", 1, [leverage_config(allow_sl_tp=False)])
    assert select_config(no_sl, "long", 1) is None
    fixed_sl = _row("X", 1, [leverage_config(allow_edit_stop_loss=False)])
    assert select_config(fixed_sl, "long", 1) is None                     # our stop cannot be set
    assert select_config(_row("X", 1, [leverage_config()]), "long", 1) is not None


# ------------------------------------------------------------------------------ resolve_vehicle
def _rows(*rows):
    return {r.symbol: r for r in rows}


def test_resolve_vehicle_lowest_cost_then_order(policy):
    ndx = policy.universe.by_symbol()["NDX"]       # long: EQQQ.L real, CNDX.L real, QQQ cfd, NSDQ100 cfd
    rows = _rows(
        _row("QQQ", 11, [leverage_config()]),
        _row("NSDQ100", 12, [leverage_config()]),
    )
    costs = {"QQQ": 20.0, "NSDQ100": 10.0}
    choice = resolve_vehicle(ndx, "long", 1, rows, lambda v, r, c: costs[v.symbol])
    assert (choice.symbol, choice.instrument_id, choice.settlement, choice.direction) == ("NSDQ100", 12, "cfd", "long")
    assert choice.expected_cost_bps == 10.0
    tie = resolve_vehicle(ndx, "long", 1, rows, lambda v, r, c: 10.0)
    assert tie.symbol == "QQQ"                                              # earlier candidate wins ties


def test_resolve_vehicle_candidate_settlement_must_be_offered(policy):
    ndx = policy.universe.by_symbol()["NDX"]
    rows = _rows(
        _row("EQQQ.L", 21, [leverage_config(settlement="CFD")]),           # listed as real: not offered
        _row("NSDQ100", 12, [leverage_config()]),
    )
    assert resolve_vehicle(ndx, "long", 1, rows, lambda v, r, c: 1.0).symbol == "NSDQ100"
    rows["EQQQ.L"] = _row("EQQQ.L", 21, [leverage_config(settlement="Real", leverage_values=[1])])
    assert resolve_vehicle(ndx, "long", 1, rows, lambda v, r, c: 1.0).symbol == "EQQQ.L"
    assert resolve_vehicle(ndx, "long", 2, rows, lambda v, r, c: 1.0).symbol == "NSDQ100"   # real is 1x only


def test_resolve_vehicle_excludes_unpriced_and_handles_no_short(policy):
    lines = policy.universe.by_symbol()
    rows = _rows(_row("NSDQ100", 12, [leverage_config(direction="SHORT")]), _row("BTC", 5, [leverage_config()]))
    assert resolve_vehicle(lines["NDX"], "short", 1, rows, lambda v, r, c: None) is None
    assert resolve_vehicle(lines["NDX"], "short", 1, rows, lambda v, r, c: math.inf) is None
    assert resolve_vehicle(lines["NDX"], "short", 1, rows, lambda v, r, c: 3.0).symbol == "NSDQ100"
    assert resolve_vehicle(lines["BTC"], "short", 1, rows, lambda v, r, c: 1.0) is None     # no short vehicles
    assert resolve_vehicle(lines["GOLD"], "long", 1, rows, lambda v, r, c: 1.0) is None     # nothing eligible


# ------------------------------------------------------------------------------ instrument map
@pytest.fixture
def fake_read():
    fake = FakeEtoro()
    fake.add_instrument("SPX500", 101, bid=100.0, ask=100.1)
    fake.add_instrument("NSDQ100", 102, bid=200.0, ask=200.2)
    return fake, EtoroReadClient("test-app-key", "test-read-key", transport=fake.transport(), sleep=lambda _s: None)


def test_resolve_persists_immutable_map(fake_read):
    _fake, read = fake_read
    imap = resolve(read, ["SPX500", "NSDQ100", "NOPE"], now=NOW)
    assert imap.ids() == {"SPX500": 101, "NSDQ100": 102}
    assert imap.unresolved == ("NOPE",)
    path = state_dir() / "instruments.json"
    assert imap.path == path and path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    reloaded = InstrumentMap.load()
    assert reloaded.ids() == imap.ids() and reloaded.symbol_for(102) == "NSDQ100"
    with pytest.raises(TypeError):
        imap.entries["X"] = None  # type: ignore[index]


def test_reverification_keeps_resolved_at(fake_read):
    _fake, read = fake_read
    resolve(read, ["SPX500"], now=NOW)
    later = resolve(read, ["SPX500"], now=NOW + timedelta(days=7))
    entry = later.entries["SPX500"]
    assert entry.resolved_at == NOW and entry.verified_at == NOW + timedelta(days=7)


def test_identity_change_raises_and_writes_nothing(fake_read):
    fake, read = fake_read
    resolve(read, ["SPX500"], now=NOW)
    before = (state_dir() / "instruments.json").read_text()
    fake.instruments.pop(101)
    fake.add_instrument("SPX500", 999, bid=100.0, ask=100.1)
    with pytest.raises(InstrumentIdentityChanged) as err:
        resolve(read, ["SPX500"], now=NOW)
    assert (err.value.symbol, err.value.old_id, err.value.new_id) == ("SPX500", 101, 999)
    assert (state_dir() / "instruments.json").read_text() == before


def test_two_symbols_cannot_share_an_instrument_id():
    imap = InstrumentMap({}).merged({"A": 1}, NOW)
    with pytest.raises(InstrumentIdentityChanged):
        imap.merged({"B": 1}, NOW)


def test_map_refuses_repo_path_and_bad_versions(tmp_path):
    with pytest.raises(RuntimeError):
        InstrumentMap({}, path=REPO_ROOT / "instruments.json").save()
    bad = tmp_path / "instruments.json"
    bad.write_text(json.dumps({"version": 99, "instruments": {}}))
    with pytest.raises(ValueError):
        InstrumentMap.load(bad)
    assert len(InstrumentMap.load(tmp_path / "missing.json")) == 0
