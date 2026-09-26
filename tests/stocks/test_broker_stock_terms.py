"""Broker parsing for single stocks (design §4.3, etoro-stocks #0, #7, #24): `requiresW8Ben`,
`allowedOrderQuantityType` and `tradeUnitType` read fail closed; stop-loss fields re-read fail closed
for the stock gate (core parsing unchanged); an instrument resolves only through EXACTLY ONE
eligibility row; renames are explicit aliases in instruments.json, never inferred. The broker is
reached only through the READ client over the FakeEtoro transport."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from council.broker.eligibility import (
    EligibilityTerms,
    parse_eligibility,
    select_config,
    trades_in_units,
    unit_orders_allowed,
    w8ben_required,
)
from council.broker.etoro_read import EtoroReadClient
from council.broker.fake import FakeEtoro, eligibility_row, leverage_config
from council.broker.instruments import (
    InstrumentIdentityChanged,
    InstrumentMap,
    found_from_rows,
    resolve,
    rows_by_symbol,
)
from council.models.broker import EligibilityRow
from council.paths import state_dir
from tests.stocks import cli_support as cs

NOW = cs.NOW
MISSING = object()


def parse_one(**raw_updates):
    raw = cs.stock_row("TSTA", 1)
    for k, v in raw_updates.items():
        if v is MISSING:
            raw.pop(k, None)
        else:
            raw[k] = v
    (row,) = parse_eligibility({"eligibilities": [raw]}, NOW)
    return row


# ------------------------------------------------------------------------------------ order terms


@pytest.mark.parametrize(("raw", "required"), [
    (MISSING, False), (None, False), (False, False), ("false", False), ("False", False), (0, False),
    (True, True), ("true", True), ("yes", True), (1, True), ("pending", True),   # unknown text: fail closed
])
def test_requires_w8ben_is_read_fail_closed(raw, required):
    row = parse_one(requiresW8Ben=raw)
    assert isinstance(row, EligibilityTerms)
    assert w8ben_required(row) is required


@pytest.mark.parametrize(("raw", "ok"), [
    ("Both", True), ("both", True), ("Units", True), ("UnitsOnly", True), ("Amount", False), ("", False),
    (None, False), (MISSING, False),
])
def test_orders_by_units_must_be_allowed(raw, ok):
    assert unit_orders_allowed(parse_one(allowedOrderQuantityType=raw)) is ok


@pytest.mark.parametrize(("raw", "ok"), [
    ("Units", True), ("units", True), ("Contracts", False), ("Lots", False), (None, False), (MISSING, False),
])
def test_the_instrument_must_trade_in_units(raw, ok):
    assert trades_in_units(parse_one(tradeUnitType=raw)) is ok


def test_a_plain_row_without_the_terms_fails_every_term_closed():
    plain = EligibilityRow(instrument_id=1, symbol="TSTA", fetched_at=NOW)
    assert not unit_orders_allowed(plain) and not trades_in_units(plain) and not w8ben_required(plain)


def test_stock_configs_read_stop_fields_fail_closed_and_core_parsing_is_unchanged():
    raw = cs.stock_row("TSTA", 1)
    config = raw["leverageConfigs"][0]
    for key in ("allowStopLossTakeProfit", "isPotential", "minStopLossPercentage", "maxStopLossPercentage"):
        config.pop(key)
    (row,) = parse_eligibility({"eligibilities": [raw]}, NOW)
    (stock,) = row.stock_configs
    assert stock.allow_sl_tp is False and stock.is_potential is True
    assert stock.min_sl_pct == 100.0 and stock.max_sl_pct == 0.0           # an empty range: no distance fits
    (core,) = row.leverage_configs                                        # the permissive core reading
    assert core.allow_sl_tp is True and core.is_potential is False and core.max_sl_pct == 100.0
    assert select_config(row, "long", 1) is not None                     # core resolution unchanged
    full = parse_one()
    assert full.stock_configs[0].allow_sl_tp and full.stock_configs[0].allow_edit_stop_loss


def test_the_read_client_returns_the_terms():
    b = cs.broker(["TSTA"], TSTA={"requiresW8Ben": True, "allowedOrderQuantityType": "Amount",
                                  "tradeUnitType": "Contracts"})
    (row,) = b.read.eligibility(symbols=["TSTA"])
    assert isinstance(row, EligibilityTerms) and row.requires_w8ben is True
    assert (row.allowed_order_quantity_type, row.trade_unit_type) == ("Amount", "Contracts")
    assert b.eligibility_posts() == 1 and b.writes() == 0


# ------------------------------------------------------------------------------------ exactly one row


class DuplicatingEtoro(FakeEtoro):
    """The broker answering TWO rows for one symbol (two listings with the same ticker)."""

    def __init__(self, duplicate: dict[str, int], **kw) -> None:
        super().__init__(**kw)
        self.duplicate = duplicate

    def _route_eligibility(self, req, match, body, headers):
        resp = super()._route_eligibility(req, match, body, headers)
        payload = resp.json()
        for row in list(payload["eligibilities"]):
            if row["symbol"] in self.duplicate:
                payload["eligibilities"].append({**row, "instrumentId": self.duplicate[row["symbol"]]})
        return type(resp)(200, json=payload)


@pytest.fixture
def dup_read():
    fake = DuplicatingEtoro({"DUP": 902}, clock=lambda: NOW)
    fake.add_instrument("DUP", 901, bid=10.0, ask=10.01)
    fake.add_instrument("ONE", 903, bid=10.0, ask=10.01)
    return fake, EtoroReadClient("test-app-key", "test-read-key", transport=fake.transport(), sleep=lambda _s: None)


def test_a_symbol_resolves_only_through_exactly_one_row(dup_read):
    _fake, read = dup_read
    imap = resolve(read, ["DUP", "one", "NOPE"], now=NOW)
    assert imap.ids() == {"one": 903}                                     # case-insensitive, exact symbol
    assert imap.ambiguous == ("DUP",) and set(imap.unresolved) == {"DUP", "NOPE"}
    assert InstrumentMap.load().get("DUP") is None                         # never "the first row"
    rows = read.eligibility(symbols=["DUP", "ONE"])
    single, many = rows_by_symbol(rows)
    assert set(single) == {"ONE"} and many == {"DUP"}
    found, missing, ambiguous = found_from_rows(rows, ["DUP", "ONE", "X"])
    assert found == {"ONE": 903} and missing == ["X"] and ambiguous == ["DUP"]


# ------------------------------------------------------------------------------------ explicit aliases


def test_a_rename_is_an_explicit_alias_and_keeps_the_old_symbol_readable():
    imap = InstrumentMap({}, path=state_dir() / "instruments.json").merged({"OLDN": 5, "OTHR": 6}, NOW)
    with pytest.raises(InstrumentIdentityChanged):
        imap.merged({"NEWN": 5}, NOW)                                    # inferred rename: refused
    renamed = imap.with_alias("OLDN", "NEWN", 5, NOW + timedelta(days=1))
    assert renamed.get("NEWN") == 5 and renamed.get("OLDN") == 5          # ledger rows keep reading OLDN
    assert renamed.symbol_for(5) == "NEWN" and renamed.symbols_by_id()[5] == "NEWN"
    assert renamed.current_symbol("OLDN") == "NEWN" and renamed.aliases["OLDN"].instrument_id == 5
    again = renamed.merged({"NEWN": 5, "OLDN": 5}, NOW + timedelta(days=2))   # re-verification: no raise
    assert again.symbol_for(5) == "NEWN" and "OLDN" in again.aliases
    with pytest.raises(InstrumentIdentityChanged):
        again.merged({"THIRD": 5}, NOW)                                  # a stranger still cannot share the id
    renamed.save()
    loaded = InstrumentMap.load()
    assert loaded.aliases == renamed.aliases and loaded.symbol_for(5) == "NEWN"
    assert json.loads(loaded.path.read_text())["aliases"]["OLDN"]["new"] == "NEWN"


def test_aliases_are_refused_when_they_do_not_fit_the_map():
    imap = InstrumentMap({}, path=state_dir() / "instruments.json").merged({"OLDN": 5, "OTHR": 6}, NOW)
    with pytest.raises(InstrumentIdentityChanged):
        imap.with_alias("OLDN", "NEWN", 7, NOW)                          # OLDN is not that instrument
    with pytest.raises(InstrumentIdentityChanged):
        imap.with_alias("OLDN", "OTHR", 5, NOW)                          # OTHR belongs to another instrument
    with pytest.raises(ValueError):
        imap.with_alias("OLDN", "OLDN", 5, NOW)
    renamed = imap.with_alias("OLDN", "NEWN", 5, NOW)
    with pytest.raises(InstrumentIdentityChanged):
        renamed.with_alias("OLDN", "FORK", 5, NOW)                       # one rename per old symbol
    with pytest.raises(InstrumentIdentityChanged):
        renamed.with_alias("NEWN", "OLDN", 5, NOW)                       # no cycles
    assert renamed.with_alias("NEWN", "THIRD", 5, NOW).symbol_for(5) == "THIRD"   # a chain is fine


def test_a_tampered_alias_file_does_not_load(tmp_path):
    path = tmp_path / "instruments.json"
    InstrumentMap({}, path=path).merged({"OLDN": 5, "OTHR": 6}, NOW).with_alias("OLDN", "NEWN", 5, NOW).save()
    data = json.loads(path.read_text())
    data["aliases"]["OLDN"]["instrument_id"] = 6
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="alias"):
        InstrumentMap.load(path)


def test_resolve_after_a_recorded_rename_keeps_the_identity(tmp_path):
    fake = FakeEtoro(clock=lambda: NOW)
    fake.add_instrument("OLDN", 5, bid=10.0, ask=10.01, row=eligibility_row("OLDN", 5, configs=[leverage_config()]))
    read = EtoroReadClient("test-app-key", "test-read-key", transport=fake.transport(), sleep=lambda _s: None)
    resolve(read, ["OLDN"], now=NOW)
    inst = fake.instruments[5]                                            # the broker renames the listing
    inst.symbol, inst.row = "NEWN", eligibility_row("NEWN", 5, configs=[leverage_config()])
    with pytest.raises(InstrumentIdentityChanged):
        resolve(read, ["NEWN"], now=NOW)
    InstrumentMap.load().with_alias("OLDN", "NEWN", 5, NOW).save()
    assert resolve(read, ["NEWN"], now=NOW).symbol_for(5) == "NEWN"
