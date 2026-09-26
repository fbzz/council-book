"""The quarterly stock-sleeve file, its merge into the Universe, and the load-time identity rules
(one namespace of line ids and vehicle symbols, the line-id charset, stock lines real/long/1x,
weights, caps, CIKs). Today's 9-line policy must load exactly as before."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from council import policy as policy_module
from council.context import _first_fact_id
from council.execution import planner
from council.invariants import InvariantViolation, check_policy
from council.paths import POLICY_DIR
from council.policy import (
    SLEEVE_FILE,
    STOCK_RANK_FILE,
    LineSpec,
    Policy,
    Universe,
    default_policy,
    policy_sha256,
)
from council.publish.public_models import LINE_PATTERN
from tests.conftest import SLEEVE_FIXTURE, make_sleeve_policy_dir

CORE = ["NDX", "SEMIS", "SPX", "GOLD", "BTC", "ETH", "OIL", "EURUSD", "GBPUSD"]
STOCKS = ["TSTA", "TSTB", "TSTC_B", "F", "TSTD", "TSTE"]


def _sleeve_dir(tmp_path: Path, *, sleeve=None, rank=None, reference=None, raw_sleeve: str | None = None,
                rebased: bool = True) -> Path:
    """A policy directory = policy/ + the fixture overlay, with optional edits of the parsed files."""
    d = make_sleeve_policy_dir(tmp_path / "policy")
    if not rebased:
        shutil.copyfile(POLICY_DIR / "universe.yaml", d / "universe.yaml")
    for name, edit in ((SLEEVE_FILE, sleeve), (STOCK_RANK_FILE, rank), ("reference.yaml", reference)):
        if edit is not None:
            data = yaml.safe_load((d / name).read_text())
            edit(data)
            (d / name).write_text(yaml.safe_dump(data, sort_keys=False))
    if raw_sleeve is not None:
        (d / SLEEVE_FILE).write_text(raw_sleeve)
    return d


def _row(data: dict, symbol: str) -> dict:
    return next(r for r in data["lines"] if r["symbol"] == symbol)


def _fails(d: Path, match: str) -> None:
    with pytest.raises((ValueError, ValidationError), match=match):
        Policy.load(d)


# ------------------------------------------------------------------------ today's policy: no change
def test_the_repository_policy_is_the_core_pin_until_a_sleeve_is_committed(policy):
    full = Policy.load()
    if (POLICY_DIR / SLEEVE_FILE).exists():
        pytest.skip("a stock sleeve is committed: Policy.load() is no longer the core pin")
    assert full.sha256 == policy.sha256 == policy_sha256(POLICY_DIR)
    assert full.universe == policy.universe and full.universe.symbols() == CORE
    assert full.universe.stock_sleeve is None and full.universe.stock_lines() == []
    if not (POLICY_DIR / STOCK_RANK_FILE).exists():
        assert full.stocks == {}


def test_the_repository_policy_loads_through_every_validator():
    """CI loads the real policy/ (with any committed sleeve) on every commit, so a bad file fails CI,
    not a live cycle."""
    full = Policy.load()
    check_policy(full)
    assert Universe.model_validate(full.universe.model_dump(by_alias=True)) == full.universe
    assert all(re.fullmatch(LINE_PATTERN, s) for s in full.universe.symbols())


def test_tests_run_on_the_core_pin(policy):
    assert default_policy() is policy
    assert policy.universe.stock_lines() == [] and policy.universe.stock_sleeve is None


def test_no_test_outside_tests_policy_loads_the_unpinned_policy():
    """Once a sleeve is committed, a bare `Policy.load()` in a test would silently run on stock lines;
    tests use the `policy` fixture or `Policy.load(include_sleeve=False)`."""
    tests = Path(__file__).resolve().parents[1]
    bare = re.compile(r"Policy\.load\(\s*\)")
    offenders = [f"{path.relative_to(tests)}:{n}"
                 for path in sorted(tests.rglob("*.py")) if path.relative_to(tests).parts[0] != "policy"
                 for n, text in enumerate(path.read_text().splitlines(), 1) if bare.search(text)]
    assert offenders == []


def test_the_calendar_merges_every_calendar_file(tmp_path):
    y2026 = yaml.safe_load((POLICY_DIR / "calendar-2026.yaml").read_text())
    if [p.name for p in POLICY_DIR.glob("calendar-*.yaml")] == ["calendar-2026.yaml"]:
        assert Policy.load().calendar == y2026                     # one file: exactly its content
    d = make_sleeve_policy_dir(tmp_path / "p", overlay=tmp_path / "none")
    for extra in d.glob("calendar-*.yaml"):
        if extra.name != "calendar-2026.yaml":
            extra.unlink()
    assert Policy.load(d).calendar == y2026
    (d / "calendar-2098.yaml").write_text(
        'version: 1\nfomc_decisions_utc:\n  - "2098-01-27T19:00:00Z"\n  - "2026-12-09T19:00:00Z"\nnotes: "2098"\n'
        'exchange_calendar:\n  nyse_holidays: ["2098-01-01"]\n  source: a\n')
    (d / "calendar-2099.yaml").write_text('exchange_calendar:\n  nyse_holidays: ["2099-01-01"]\n  source: b\n')
    cal = Policy.load(d).calendar
    assert cal["fomc_decisions_utc"] == [*y2026["fomc_decisions_utc"], "2098-01-27T19:00:00Z"]
    assert cal["notes"] == "2098"
    assert cal["exchange_calendar"] == {"nyse_holidays": ["2098-01-01", "2099-01-01"], "source": "b"}
    (d / "calendar-2100.yaml").write_text("fomc_decisions_utc: none\n")
    with pytest.raises(ValueError, match="changes type"):
        Policy.load(d)


# ------------------------------------------------------------------------------------------ merge
def test_stock_lines_are_appended_after_the_core_lines_in_file_order(sleeve_policy, policy):
    assert sleeve_policy.universe.symbols() == [*CORE, *STOCKS]
    assert [ln.symbol for ln in sleeve_policy.universe.stock_lines()] == STOCKS
    core = {ln.symbol: ln for ln in policy.universe.lines}
    for line in sleeve_policy.universe.lines[:9]:          # the fixture only re-bases base_weight
        assert line.model_copy(update={"base_weight": core[line.symbol].base_weight}) == core[line.symbol]


def test_a_sleeve_row_expands_into_a_real_long_unlevered_satellite_line(sleeve_policy):
    by = sleeve_policy.universe.by_symbol()
    tstc = by["TSTC_B"]
    assert (tstc.asset_class, tstc.sleeve, tstc.council_deviations, tstc.session) == ("stock", "satellite", True, "us")
    assert tstc.base_weight == pytest.approx(0.50 / 8)
    assert [(v.symbol, v.settlement) for v in tstc.vehicles.long] == [("TSTC.B", "real")]
    assert tstc.vehicles.short == [] and not tstc.shortable
    assert (tstc.signal.source, tstc.signal.ticker) == ("tiingo", "TSTC-B")
    assert (tstc.stock.role, tstc.stock.sector, tstc.stock.cik, tstc.stock.rank) == ("selected", "Manuf", "0000900003", 3)
    assert [s for s in STOCKS if by[s].in_reference] == ["TSTA", "TSTB", "TSTC_B", "F"]    # role == selected
    assert by["TSTE"].stock.role == "shortlist" and by["TSTE"].stock.eligibility_checked_at is None
    sleeve = sleeve_policy.universe.stock_sleeve
    assert (sleeve.quarter, sleeve.names_target, sleeve.sleeve_weight) == ("2026Q4", 8, 0.5)
    assert [(r.symbol, r.cik, r.from_, r.to) for r in sleeve.retired] == [("TSTZ", "0000900099", "2026Q3", "2026Q4")]
    check_policy(sleeve_policy)


def test_the_core_only_policy_ignores_the_sleeve_for_lines_and_hash(sleeve_policy_dir, sleeve_policy, tmp_path):
    core = Policy.load(sleeve_policy_dir, include_sleeve=False)
    assert core.universe.symbols() == CORE and core.universe.stock_sleeve is None
    without = tmp_path / "without"
    shutil.copytree(sleeve_policy_dir, without)
    (without / SLEEVE_FILE).unlink()
    assert core.sha256 == policy_sha256(without) == Policy.load(without).sha256
    assert core.sha256 != sleeve_policy.sha256 == policy_sha256(sleeve_policy_dir)
    assert core.stocks == sleeve_policy.stocks and core.stocks["shortlist_size"] == 8


def test_the_fixture_universe_is_the_repository_universe_re_based():
    """The fixture copies policy/universe.yaml with the core re-base (in-reference weights × 0.45/0.95,
    rounded down to 6 decimals); it must follow every other change to the real file."""
    real = yaml.safe_load((POLICY_DIR / "universe.yaml").read_text())
    fixture = yaml.safe_load((SLEEVE_FIXTURE / "universe.yaml").read_text())
    rebased = {"NDX": 0.165789, "SEMIS": 0.071052, "SPX": 0.071052, "GOLD": 0.056842, "BTC": 0.061578, "ETH": 0.023684}
    for line in real["lines"]:
        if line["symbol"] in rebased:
            line["base_weight"] = rebased[line["symbol"]]
    assert fixture == real
    assert sum(rebased.values()) == pytest.approx(0.449997)


def test_a_sleeve_needs_the_stock_rank_file(tmp_path):
    d = _sleeve_dir(tmp_path)
    (d / STOCK_RANK_FILE).unlink()
    _fails(d, "requires stock-rank.yaml")


def test_universe_yaml_cannot_carry_stock_lines_or_the_sleeve(tmp_path):
    d = make_sleeve_policy_dir(tmp_path / "p", overlay=tmp_path / "none")
    data = yaml.safe_load((d / "universe.yaml").read_text())
    data["lines"].append({"symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock", "sleeve": "satellite",
                          "in_reference": False, "base_weight": 0.05, "signal": {"source": "tiingo", "ticker": "NVDA"},
                          "vehicles": {"long": [{"symbol": "NVDA", "settlement": "real"}]}})
    (d / "universe.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    _fails(d, "stock lines come only from stock-sleeve.yaml")
    data["lines"].pop()
    data["stock_sleeve"] = {}
    (d / "universe.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    _fails(d, "may not define the stock sleeve")


# ------------------------------------------------------------------------- identity: one namespace
def test_a_stock_line_id_colliding_with_a_core_vehicle_fails(tmp_path):
    def spy(data):
        row = _row(data, "TSTA")
        row.update(symbol="SPY", signal_ticker="SPY", etoro_symbol="SPY")
    _fails(_sleeve_dir(tmp_path, sleeve=spy), "symbol SPY belongs to lines SPX and SPY")


def test_a_vehicle_shared_across_lines_fails(tmp_path):
    _fails(_sleeve_dir(tmp_path / "a", sleeve=lambda d: _row(d, "TSTA").update(etoro_symbol="QQQ")),
           "symbol QQQ belongs to lines NDX and TSTA")
    _fails(_sleeve_dir(tmp_path / "b", sleeve=lambda d: _row(d, "TSTB").update(etoro_symbol="TSTA")),
           "symbol TSTA belongs to lines TSTA and TSTB")


def test_an_alias_is_part_of_the_namespace(tmp_path):
    _fails(_sleeve_dir(tmp_path / "a", sleeve=lambda d: _row(d, "TSTA").update(aliases=["TSTB"])),
           "symbol TSTB belongs to lines TSTA and TSTB")
    ok = Policy.load(_sleeve_dir(tmp_path / "b", sleeve=lambda d: _row(d, "TSTA").update(aliases=["OLDA"])))
    assert ok.universe.by_symbol()["TSTA"].stock.aliases == ("OLDA",)


def test_duplicate_line_ids_fail(tmp_path):
    def dup(data):
        data["lines"].append({**_row(data, "TSTA"), "cik": "0000900050", "etoro_symbol": "TSTA2"})
    _fails(_sleeve_dir(tmp_path, sleeve=dup), "line id TSTA is defined 2 times")


@pytest.mark.parametrize("bad", ["brk_b", "BRK_", "_B", "BRK.B", "ABCDEFGHIJKLM", "STK:NVDA", ""])
def test_a_line_id_outside_the_public_pattern_fails(tmp_path, bad):
    """Each bad id fails on exactly its own field, against the public pattern, not on something else."""
    d = _sleeve_dir(tmp_path, sleeve=lambda data: _row(data, "TSTA").update(symbol=bad))
    row = [r["symbol"] for r in yaml.safe_load((d / SLEEVE_FILE).read_text())["lines"]].index(bad)
    with pytest.raises(ValidationError) as caught:
        Policy.load(d)
    errors = caught.value.errors()
    assert [(e["loc"], e["type"], e["input"]) for e in errors] == [
        (("lines", row, "symbol"), "string_pattern_mismatch", bad)]
    assert errors[0]["ctx"]["pattern"] == LINE_PATTERN


def test_a_core_line_id_outside_the_public_pattern_fails(tmp_path):
    d = make_sleeve_policy_dir(tmp_path / "p", overlay=tmp_path / "none")
    text = (d / "universe.yaml").read_text().replace("symbol: EURUSD\n", "symbol: EUR-USD\n", 1)
    (d / "universe.yaml").write_text(text)
    _fails(d, "line id 'EUR-USD' does not match")


def test_the_unmapped_prefix_is_reserved(tmp_path):
    assert planner.UNMAPPED_PREFIX.startswith(policy_module.RESERVED_LINE_PREFIX)
    _fails(_sleeve_dir(tmp_path / "a", sleeve=lambda d: _row(d, "TSTA").update(symbol="UNMAPPED1")),
           "reserved prefix UNMAPPED")
    d = make_sleeve_policy_dir(tmp_path / "b", overlay=tmp_path / "none")
    text = (d / "universe.yaml").read_text().replace("symbol: GBPUSD\n", "symbol: UNMAPPED_7\n", 1)
    (d / "universe.yaml").write_text(text)
    _fails(d, "reserved prefix UNMAPPED")


def test_the_unmapped_prefix_is_reserved_for_vehicles_and_aliases_too(tmp_path):
    _fails(_sleeve_dir(tmp_path / "a", sleeve=lambda d: _row(d, "TSTA").update(etoro_symbol="UNMAPPED_5")),
           r"line TSTA: 'UNMAPPED_5' uses the reserved prefix UNMAPPED")
    _fails(_sleeve_dir(tmp_path / "b", sleeve=lambda d: _row(d, "TSTB").update(aliases=["UNMAPPED9"])),
           r"line TSTB: 'UNMAPPED9' uses the reserved prefix UNMAPPED")
    d = make_sleeve_policy_dir(tmp_path / "c", overlay=tmp_path / "none")
    text = (d / "universe.yaml").read_text().replace("{symbol: SOXX, settlement: cfd}, {symbol: SMH,",
                                                     "{symbol: unmapped_soxx, settlement: cfd}, {symbol: SMH,", 1)
    assert "unmapped_soxx" in text
    (d / "universe.yaml").write_text(text)
    _fails(d, r"line SEMIS: 'unmapped_soxx' uses the reserved prefix UNMAPPED")


def test_class_share_and_one_letter_ids_are_valid(sleeve_policy):
    assert {"TSTC_B", "F"} <= set(sleeve_policy.universe.symbols())
    assert _first_fact_id("see F:TSTC_B:trend") == "F:TSTC_B:trend"          # the stub's evidence id
    assert _first_fact_id("cites F:F:dd52 only") == "F:F:dd52"


def test_an_unquoted_yaml_boolean_ticker_fails(tmp_path):
    raw = (SLEEVE_FIXTURE / SLEEVE_FILE).read_text().replace('symbol: "TSTA"', "symbol: ON", 1)
    assert "symbol: ON\n" in raw
    _fails(_sleeve_dir(tmp_path / "a", raw_sleeve=raw), "valid string")
    raw = (SLEEVE_FIXTURE / SLEEVE_FILE).read_text().replace('etoro_symbol: "TSTA"', "etoro_symbol: NO", 1)
    _fails(_sleeve_dir(tmp_path / "b", raw_sleeve=raw), "valid string")
    quoted = (SLEEVE_FIXTURE / SLEEVE_FILE).read_text().replace('"TSTA"', '"ON"')
    assert "ON" in Policy.load(_sleeve_dir(tmp_path / "c", raw_sleeve=quoted)).universe.symbols()


def test_the_sleeve_file_schema_is_closed(tmp_path):
    _fails(_sleeve_dir(tmp_path / "a", sleeve=lambda d: _row(d, "TSTA").update(settlement="cfd")), "Extra inputs")
    _fails(_sleeve_dir(tmp_path / "b", sleeve=lambda d: _row(d, "TSTA").update(leverage=2)), "Extra inputs")
    _fails(_sleeve_dir(tmp_path / "c", sleeve=lambda d: _row(d, "TSTA").update(role="core")), "role")
    _fails(_sleeve_dir(tmp_path / "d", sleeve=lambda d: _row(d, "TSTA").update(cik="1067983")), "cik")
    _fails(_sleeve_dir(tmp_path / "e", sleeve=lambda d: d.update(quarter="2026Q5")), "quarter")


# -------------------------------------------------------------------------- stock lines: real, long, 1x
def _stock_spec(**update) -> LineSpec:
    base = {"symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock", "sleeve": "satellite", "in_reference": True,
            "base_weight": 0.05, "signal": {"source": "tiingo", "ticker": "NVDA"},
            "vehicles": {"long": [{"symbol": "NVDA", "settlement": "real"}], "short": []},
            "stock": {"role": "selected", "sector": "BusEq", "cik": "0001045810"}}
    return LineSpec.model_validate({**base, **update})


def _with_stock(core: Policy, spec: LineSpec, info: dict | None = None) -> dict:
    data = core.universe.model_dump(by_alias=True)
    rebased = {ln["symbol"]: ln for ln in data["lines"]}
    for ln in rebased.values():
        if ln["in_reference"]:
            ln["base_weight"] = ln["base_weight"] * 0.45 / 0.95 - 1e-6
    sleeve = {"version": 1, "quarter": "2026Q4", "rank_asof": "2026-11-20", "rank_config_sha256": "0" * 64,
              "sleeve_weight": 0.5, "names_target": 10}
    return {**data, "lines": [*rebased.values(), spec.model_dump(by_alias=True)],
            "stock_sleeve": info if info is not None else sleeve}


def test_a_stock_with_a_cfd_or_short_vehicle_fails(policy):
    assert Universe.model_validate(_with_stock(policy, _stock_spec())).stock_lines()[0].symbol == "NVDA"
    cfd = _stock_spec(vehicles={"long": [{"symbol": "NVDA", "settlement": "cfd"}]})
    with pytest.raises(ValidationError, match="every long vehicle must be real"):
        Universe.model_validate(_with_stock(policy, cfd))
    short = _stock_spec(vehicles={"long": [{"symbol": "NVDA", "settlement": "real"}],
                                  "short": [{"symbol": "NVDA", "settlement": "cfd"}]})
    with pytest.raises(ValidationError, match="long-only"):
        Universe.model_validate(_with_stock(policy, short))
    core_sleeve = _stock_spec(sleeve="core")
    with pytest.raises(ValidationError, match="satellite sleeve"):
        Universe.model_validate(_with_stock(policy, core_sleeve))


def test_a_stock_line_needs_its_metadata_and_the_sleeve_header(policy):
    bare = _stock_spec(stock=None)
    with pytest.raises(ValidationError, match="no stock metadata"):
        Universe.model_validate(_with_stock(policy, bare))
    data = _with_stock(policy, _stock_spec())
    data["stock_sleeve"] = None
    with pytest.raises(ValidationError, match="without a stock sleeve header"):
        Universe.model_validate(data)
    etf = _stock_spec(asset_class="etf")
    with pytest.raises(ValidationError, match="stock metadata on a etf line"):
        Universe.model_validate(_with_stock(policy, etf))


def test_the_invariant_repeats_the_stock_rule_where_validators_are_skipped(sleeve_policy):
    risk = {**sleeve_policy.risk, "leverage_caps": {**sleeve_policy.risk["leverage_caps"], "stock": 2}}
    with pytest.raises(InvariantViolation, match="leverage_caps.stock"):
        check_policy(sleeve_policy.model_copy(update={"risk": risk}))
    lines = [ln.model_copy(update={"vehicles": ln.vehicles.model_copy(update={"short": list(ln.vehicles.long)})})
             if ln.symbol == "F" else ln for ln in sleeve_policy.universe.lines]
    bad = sleeve_policy.model_copy(update={"universe": sleeve_policy.universe.model_copy(update={"lines": lines})})
    with pytest.raises(InvariantViolation, match="short vehicle"):
        check_policy(bad)


# ------------------------------------------------------------------------------ weights and caps
def test_the_sleeve_over_the_un_rebased_core_breaks_the_weight_sum(tmp_path):
    _fails(_sleeve_dir(tmp_path, rebased=False), r"core weight 0.950000 \+ sleeve weight 0.500000 exceeds")


def test_selected_names_are_capped_by_names_target(tmp_path):
    _fails(_sleeve_dir(tmp_path, sleeve=lambda d: d.update(names_target=3)), "4 selected stock lines exceed names_target 3")


def test_the_shortlist_is_capped(tmp_path):
    _fails(_sleeve_dir(tmp_path, rank=lambda d: d.update(shortlist_size=1)), "2 shortlist lines exceed shortlist_size 1")


def test_distinct_history_tickers_are_capped(tmp_path):
    _fails(_sleeve_dir(tmp_path / "a", rank=lambda d: d.update(tiingo_symbol_cap=5)),
           "6 distinct stock history tickers exceed tiingo_symbol_cap 5")
    _fails(_sleeve_dir(tmp_path / "b", rank=lambda d: d.pop("tiingo_symbol_cap")),
           "tiingo_symbol_cap must be a positive integer")


def test_the_sleeve_must_match_the_reference_sleeve_section_when_present(tmp_path):
    ok = _sleeve_dir(tmp_path / "a", reference=lambda d: d.update(sleeve={"weight": 0.5, "names": 8}))
    assert Policy.load(ok).universe.stock_sleeve.names_target == 8
    _fails(_sleeve_dir(tmp_path / "b", reference=lambda d: d.update(sleeve={"weight": 0.4, "names": 8})),
           "differs from reference.yaml sleeve.weight")
    _fails(_sleeve_dir(tmp_path / "c", reference=lambda d: d.update(sleeve={"weight": 0.5, "names": 10})),
           "differs from reference.yaml sleeve.names")


# -------------------------------------------------------------------------------------------- CIKs
def test_one_live_line_per_company(tmp_path):
    _fails(_sleeve_dir(tmp_path, sleeve=lambda d: _row(d, "TSTB").update(cik="0000900001")),
           r"CIK 0000900001 is held by 2 live lines \(TSTA, TSTB\)")


def test_a_company_is_live_or_retired_never_both(tmp_path):
    def back(data):
        data["retired"][0]["cik"] = "0000900002"
    _fails(_sleeve_dir(tmp_path / "a", sleeve=back), "CIK 0000900002 is live .line TSTB. and in the retired registry")

    def twice(data):
        data["retired"].append({**data["retired"][0], "symbol": "TSTY"})
    _fails(_sleeve_dir(tmp_path / "b", sleeve=twice), "appears 2 times in the retired registry")

    def backwards(data):
        data["retired"][0].update({"from": "2027Q1", "to": "2026Q4"})
    _fails(_sleeve_dir(tmp_path / "c", sleeve=backwards), "is before 'from'")


def test_a_recycled_ticker_of_another_company_is_allowed(tmp_path):
    def recycle(data):
        data["lines"].append({**_row(data, "TSTE"), "symbol": "TSTZ", "cik": "0000900077",
                              "signal_ticker": "TSTZ", "etoro_symbol": "TSTZ"})
    pol = Policy.load(_sleeve_dir(tmp_path, sleeve=recycle))
    assert pol.universe.by_symbol()["TSTZ"].stock.cik == "0000900077"
    assert pol.universe.stock_sleeve.retired[0].cik == "0000900099"


# ------------------------------------------------------------------------------ vehicle_to_line
def test_vehicle_to_line_maps_stock_vehicles(sleeve_policy):
    mapping = planner.vehicle_to_line(sleeve_policy.universe)
    assert mapping["TSTC.B"] == "TSTC_B" and mapping["TSTC_B"] == "TSTC_B" and mapping["F"] == "F"
    assert mapping["SPY"] == "SPX" and "TSTZ" not in mapping           # the registry is never a line


def test_vehicle_to_line_still_refuses_an_unvalidated_collision(policy):
    """`model_copy` skips the load-time validator; the shared rule still refuses the ambiguity."""
    spx = policy.universe.by_symbol()["SPX"]
    clash = spx.model_copy(update={"symbol": "SPY2", "vehicles": spx.vehicles})
    universe = policy.universe.model_copy(update={"lines": [*policy.universe.lines, clash]})
    with pytest.raises(ValueError, match="belongs to lines SPX and SPY2"):
        planner.vehicle_to_line(universe)
    with pytest.raises(ValidationError, match="belongs to lines SPX and SPY2"):
        Universe.model_validate(universe.model_dump(by_alias=True))
