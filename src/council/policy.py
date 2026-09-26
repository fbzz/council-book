"""Typed access to the frozen policy files. Every cycle records the policy SHA-256.

Files in the policy directory (top level only; `variants/` is research, never loaded here):
- `universe.yaml`: the core lines. It may not define stock lines or the stock sleeve.
- `stock-sleeve.yaml` (optional, one per quarter, tagged `stocks-<quarter>`): this quarter's stock
  lines and the retired registry. `Policy.load` expands each row into a real, long-only `LineSpec`
  and appends it AFTER the core lines. `include_sleeve=False` gives the core-only policy.
- `stock-rank.yaml` (optional; required when a sleeve is present): `Policy.stocks`.
- `calendar-*.yaml`: merged into `Policy.calendar` (lists concatenated in file-name order).
- `risk.yaml`, `reference.yaml`, `costs.yaml`, `council.yaml`.

Line identity is validated when the policy loads, never mid-cycle: every line id matches the public
`LINE_PATTERN` and does not start with `UNMAPPED`, and line ids, vehicle symbols and aliases form ONE
namespace in which every symbol belongs to exactly one line (a line may reuse its own id).
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)

from council.paths import POLICY_DIR
from council.publish.public_models import LINE_PATTERN  # the one definition of a line id

AssetClass = Literal["stock", "etf", "crypto", "index", "commodity", "fx"]
Sleeve = Literal["core", "crypto", "overlay", "satellite"]
HistorySource = Literal["etoro", "tiingo", "binance"]
StockRole = Literal["selected", "shortlist", "retiring"]

UNIVERSE_FILE = "universe.yaml"
SLEEVE_FILE = "stock-sleeve.yaml"
STOCK_RANK_FILE = "stock-rank.yaml"
CALENDAR_GLOB = "calendar-*.yaml"
# A broker position on an instrument no line owns is locked as `UNMAPPED_<id>`
# (`execution.planner.UNMAPPED_PREFIX`), so no line id, vehicle or alias may start with this.
RESERVED_LINE_PREFIX = "UNMAPPED"
WEIGHT_EPS = 1e-9
_LINE_ID = re.compile(LINE_PATTERN)

LineId = Annotated[StrictStr, Field(pattern=LINE_PATTERN)]
Cik = Annotated[StrictStr, Field(pattern=r"^\d{10}$")]
Quarter = Annotated[StrictStr, Field(pattern=r"^\d{4}Q[1-4]$")]
Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Sector = Annotated[StrictStr, Field(pattern=r"^[A-Za-z][A-Za-z0-9 &/-]{0,23}$")]
SignalTicker = Annotated[StrictStr, Field(pattern=r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")]
BrokerSymbol = Annotated[StrictStr, Field(pattern=r"^[A-Z0-9][A-Z0-9._\-]{0,19}$")]
Name = Annotated[StrictStr, Field(min_length=1, max_length=80)]


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: HistorySource
    ticker: str


class Vehicle(BaseModel):
    """A tradable instrument candidate for a line. Resolved against broker eligibility at onboarding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    settlement: Literal["real", "cfd"]


class Vehicles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    long: list[Vehicle]
    short: list[Vehicle] = Field(default_factory=list)


class StockMeta(BaseModel):
    """What a stock line carries beyond a core line. Only `stock-sleeve.yaml` sets it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: StockRole
    sector: Sector
    cik: Cik
    rank: StrictInt | None = Field(default=None, ge=1)
    credited: Literal["corporate_action"] | None = None
    aliases: tuple[LineId, ...] = ()
    eligibility_checked_at: AwareDatetime | None = None


class LineSpec(BaseModel):
    """An exposure line: what the council reasons about (e.g. Nasdaq-100), not a broker symbol."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    name: str
    asset_class: AssetClass
    sleeve: Sleeve
    in_reference: bool
    base_weight: float = Field(gt=0, le=1.0)
    council_deviations: bool = True
    signal: Signal
    vehicles: Vehicles
    stock: StockMeta | None = None

    @property
    def shortable(self) -> bool:
        return bool(self.vehicles.short)

    @property
    def session(self) -> str:
        """Trading session of the PREFERRED long vehicle: London-listed UCITS/ETCs ('.L') trade on
        London hours, crypto 24/7, index/commodity/FX CFDs 24/5, US ETF CFDs on US hours. The broker's
        eligibility is still the final word at execution."""
        if self.asset_class == "crypto":
            return "crypto"
        first = self.vehicles.long[0].symbol if self.vehicles.long else self.symbol
        if first.endswith(".L"):
            return "lse"
        if self.asset_class in ("index", "commodity", "fx"):
            return "fx24x5"
        return "us"


# --------------------------------------------------------------------------- the stock sleeve file
class _SleeveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class SleeveLine(_SleeveModel):
    """One row of `stock-sleeve.yaml`. Strings are strict: an unquoted `ON`, `YES` or `NO` (a YAML
    boolean) fails instead of becoming a ticker called True."""

    symbol: LineId                              # line id: the ticker, class separator "_"
    name: Name
    role: StockRole
    sector: Sector
    cik: Cik
    rank: StrictInt | None = Field(default=None, ge=1)
    signal_ticker: SignalTicker                  # history-source format (BRK-B)
    etoro_symbol: BrokerSymbol                   # the exact symbol the eligibility check returned
    eligibility_checked_at: AwareDatetime | None = None
    credited: Literal["corporate_action"] | None = None
    aliases: tuple[LineId, ...] = ()


class RetiredStock(_SleeveModel):
    """A row of the append-only retired registry, keyed by CIK. Never a `LineSpec`."""

    cik: Cik
    symbol: LineId
    name: Name
    sector: Sector
    from_: Quarter = Field(alias="from")
    to: Quarter
    vehicles: tuple[BrokerSymbol, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> RetiredStock:
        if self.to < self.from_:
            raise ValueError(f"retired {self.symbol}: 'to' {self.to} is before 'from' {self.from_}")
        return self


class StockSleeveInfo(_SleeveModel):
    """The sleeve file's header and retired registry, kept on the merged `Universe`."""

    version: StrictInt = Field(ge=1, le=1)
    quarter: Quarter
    rank_asof: date
    rank_config_sha256: Sha256Hex
    sleeve_weight: float = Field(gt=0, le=1.0)
    names_target: StrictInt = Field(ge=1, le=40)
    retired: tuple[RetiredStock, ...] = ()


class StockSleeveFile(StockSleeveInfo):
    lines: tuple[SleeveLine, ...]

    def info(self) -> StockSleeveInfo:
        return StockSleeveInfo.model_validate(self.model_dump(by_alias=True, exclude={"lines"}))


def expand_sleeve(sleeve: StockSleeveFile, *, history_source: HistorySource = "tiingo") -> list[LineSpec]:
    """Each sleeve row → a real, long-only, unlevered satellite `LineSpec` (the file cannot express a
    CFD, a short vehicle or leverage). Only `selected` rows are in the reference book."""
    unit = sleeve.sleeve_weight / sleeve.names_target
    return [
        LineSpec(
            symbol=row.symbol, name=row.name, asset_class="stock", sleeve="satellite",
            in_reference=row.role == "selected", base_weight=unit, council_deviations=True,
            signal=Signal(source=history_source, ticker=row.signal_ticker),
            vehicles=Vehicles(long=[Vehicle(symbol=row.etoro_symbol, settlement="real")], short=[]),
            stock=StockMeta(role=row.role, sector=row.sector, cik=row.cik, rank=row.rank,
                            credited=row.credited, aliases=row.aliases,
                            eligibility_checked_at=row.eligibility_checked_at),
        )
        for row in sleeve.lines
    ]


# ------------------------------------------------------------------------------ identity rules
def symbol_owners(lines: Iterable[LineSpec], *, aliases: bool = False) -> tuple[dict[str, str], list[str]]:
    """Every line id and vehicle symbol (with `aliases`, every former line id too) → its line, plus
    one message per symbol two lines claim. A line may reuse its own id as a vehicle."""
    owners: dict[str, str] = {}
    collisions: list[str] = []
    for line in lines:
        symbols = [line.symbol, *(v.symbol for v in line.vehicles.long), *(v.symbol for v in line.vehicles.short)]
        if aliases and line.stock is not None:
            symbols += list(line.stock.aliases)
        for symbol in dict.fromkeys(symbols):
            owner = owners.setdefault(symbol, line.symbol)
            if owner != line.symbol:
                collisions.append(f"symbol {symbol} belongs to lines {owner} and {line.symbol}")
    return owners, collisions


def _identity_errors(lines: Sequence[LineSpec]) -> list[str]:
    errors = [f"line id {sym} is defined {n} times"
              for sym, n in Counter(line.symbol for line in lines).items() if n > 1]
    for line in lines:
        if not _LINE_ID.fullmatch(line.symbol):
            errors.append(f"line id {line.symbol!r} does not match {LINE_PATTERN}")
        vehicles = [v.symbol for v in (*line.vehicles.long, *line.vehicles.short)]
        aliases = list(line.stock.aliases) if line.stock is not None else []
        for sym in (line.symbol, *vehicles, *aliases):
            if sym.upper().startswith(RESERVED_LINE_PREFIX):
                errors.append(f"line {line.symbol}: {sym!r} uses the reserved prefix {RESERVED_LINE_PREFIX}")
            if not sym or any(c.isspace() for c in sym):
                errors.append(f"line {line.symbol}: symbol {sym!r} is empty or contains whitespace")
    return errors + symbol_owners(lines, aliases=True)[1]


def _stock_errors(lines: Sequence[LineSpec], sleeve: StockSleeveInfo | None) -> list[str]:
    errors: list[str] = []
    stocks = [ln for ln in lines if ln.asset_class == "stock" or ln.stock is not None]
    for line in stocks:
        if line.asset_class != "stock":
            errors.append(f"line {line.symbol}: stock metadata on a {line.asset_class} line")
        if line.stock is None:
            errors.append(f"stock line {line.symbol} has no stock metadata (stock lines come only from {SLEEVE_FILE})")
        if line.sleeve != "satellite":
            errors.append(f"stock line {line.symbol} must be in the satellite sleeve, not {line.sleeve}")
        if not line.vehicles.long or any(v.settlement != "real" for v in line.vehicles.long):
            errors.append(f"stock line {line.symbol}: every long vehicle must be real shares")
        if line.vehicles.short:
            errors.append(f"stock line {line.symbol}: stock lines are long-only (no short vehicle)")
    if stocks and sleeve is None:
        errors.append(f"stock lines without a stock sleeve header ({SLEEVE_FILE})")
    if sleeve is None:
        return errors

    meta = [(ln.symbol, ln.stock) for ln in stocks if ln.stock is not None]
    selected = sum(1 for _, m in meta if m.role == "selected")
    if selected > sleeve.names_target:
        errors.append(f"{selected} selected stock lines exceed names_target {sleeve.names_target}")
    # One live line per company (CIK). A company is live or retired, never both: when a retired
    # company comes back, the rank drops its registry row. A recycled ticker (a registry row whose
    # ticker a live line of ANOTHER company now uses) is therefore allowed; the same company is not.
    for cik, n in Counter(m.cik for _, m in meta).items():
        if n > 1:
            holders = ", ".join(sorted(sym for sym, m in meta if m.cik == cik))
            errors.append(f"CIK {cik} is held by {n} live lines ({holders})")
    for cik, n in Counter(r.cik for r in sleeve.retired).items():
        if n > 1:
            errors.append(f"CIK {cik} appears {n} times in the retired registry")
    live_by_cik = {m.cik: sym for sym, m in meta}
    for row in sleeve.retired:
        if row.cik in live_by_cik:
            errors.append(f"CIK {row.cik} is live (line {live_by_cik[row.cik]}) and in the retired registry "
                          f"(as {row.symbol}); drop the registry row of a returning company")
    return errors


def _weight_errors(universe: Universe) -> list[str]:
    core = math.fsum(ln.base_weight for ln in universe.lines if ln.in_reference and ln.asset_class != "stock")
    sleeve = universe.stock_sleeve.sleeve_weight if universe.stock_sleeve is not None else 0.0
    if core + sleeve > universe.reference_gross_max + WEIGHT_EPS:
        return [f"in-reference core weight {core:.6f} + sleeve weight {sleeve:.6f} exceeds "
                f"reference_gross_max {universe.reference_gross_max}"]
    return []


class Universe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    reference_gross_max: float = Field(gt=0, le=1.0)
    lines: list[LineSpec]
    controls: dict[str, list[str]] = Field(default_factory=dict)
    stock_sleeve: StockSleeveInfo | None = None

    @model_validator(mode="after")
    def _validate_lines(self) -> Universe:
        errors = [*_identity_errors(self.lines), *_stock_errors(self.lines, self.stock_sleeve),
                  *_weight_errors(self)]
        if errors:
            raise ValueError("invalid universe: " + "; ".join(errors))
        return self

    def by_symbol(self) -> dict[str, LineSpec]:
        return {line.symbol: line for line in self.lines}

    def symbols(self) -> list[str]:
        return [line.symbol for line in self.lines]

    def vehicle_map(self) -> dict[str, str]:
        """Every vehicle symbol (and each line id itself) → its line. A loaded universe cannot
        collide (the validator above); one built with `model_copy`, which skips validation, still
        raises here rather than mapping a symbol to the wrong line."""
        owners, collisions = symbol_owners(self.lines)
        if collisions:
            raise ValueError(collisions[0])
        return owners

    def stock_lines(self) -> list[LineSpec]:
        return [line for line in self.lines if line.asset_class == "stock"]


# ---------------------------------------------------------------------------------------- files
def _load(name: str, directory: Path | None = None) -> dict[str, Any]:
    path = (directory or POLICY_DIR) / name
    with path.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def _merge_calendar(into: dict[str, Any], new: dict[str, Any], where: str) -> None:
    def kind(v: Any) -> str:
        return "list" if isinstance(v, list) else "mapping" if isinstance(v, dict) else "value"

    for key, value in new.items():
        if key not in into:
            into[key] = copy.deepcopy(value)
        elif kind(into[key]) != kind(value):
            raise ValueError(f"{where}: calendar key {key!r} changes type between files")
        elif isinstance(value, list):
            into[key] += [copy.deepcopy(v) for v in value if v not in into[key]]
        elif isinstance(value, dict):
            _merge_calendar(into[key], value, where)
        else:
            into[key] = value


def _load_calendars(directory: Path) -> dict[str, Any]:
    """Merge every `calendar-*.yaml` in file-name (year) order, recursively: lists are concatenated
    (exact duplicates dropped), mappings merged key by key, and any other value comes from the
    latest file that sets it. A key whose kind (list, mapping, value) differs between files fails."""
    files = sorted(directory.glob(CALENDAR_GLOB))
    if not files:
        raise FileNotFoundError(f"no {CALENDAR_GLOB} in {directory}")
    merged: dict[str, Any] = {}
    for path in files:
        _merge_calendar(merged, _load(path.name, directory), path.name)
    return merged


def _universe(directory: Path, stocks: dict[str, Any], *, include_sleeve: bool) -> Universe:
    data = _load(UNIVERSE_FILE, directory)
    if "stock_sleeve" in data:
        raise ValueError(f"{UNIVERSE_FILE} may not define the stock sleeve; it lives in {SLEEVE_FILE}")
    for raw in data.get("lines") or []:
        if isinstance(raw, dict) and (raw.get("asset_class") == "stock" or "stock" in raw):
            raise ValueError(f"{UNIVERSE_FILE}: stock line {raw.get('symbol')!r}; stock lines come only "
                             f"from {SLEEVE_FILE}")
    if not include_sleeve or not (directory / SLEEVE_FILE).exists():
        return Universe.model_validate(data)
    sleeve = StockSleeveFile.model_validate(_load(SLEEVE_FILE, directory))
    if not stocks:
        raise ValueError(f"{SLEEVE_FILE} requires {STOCK_RANK_FILE} (shortlist size, history symbol cap)")
    extra = expand_sleeve(sleeve, history_source=stocks.get("history_source", "tiingo"))
    core = [LineSpec.model_validate(raw) for raw in data.get("lines") or []]
    return Universe.model_validate({**data, "lines": [*core, *extra], "stock_sleeve": sleeve.info()})


class Policy(BaseModel):
    """All policy files, parsed. `risk`, `reference`, `costs`, `council` and `stocks` stay dicts on
    purpose: the modules that own them validate the parts they use, and tests assert every number."""

    model_config = ConfigDict(frozen=True)

    universe: Universe
    risk: dict[str, Any]
    reference: dict[str, Any]
    costs: dict[str, Any]
    council: dict[str, Any]
    calendar: dict[str, Any]
    sha256: str
    stocks: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_sleeve_config(self) -> Policy:
        stock = self.universe.stock_lines()
        if not stock:
            return self
        errors: list[str] = []
        caps: dict[str, int] = {}
        for key in ("shortlist_size", "tiingo_symbol_cap"):
            value = self.stocks.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                errors.append(f"{STOCK_RANK_FILE}: {key} must be a positive integer")
            else:
                caps[key] = value
        shortlist = sum(1 for ln in stock if ln.stock is not None and ln.stock.role == "shortlist")
        if "shortlist_size" in caps and shortlist > caps["shortlist_size"]:
            errors.append(f"{shortlist} shortlist lines exceed shortlist_size {caps['shortlist_size']}")
        tickers = {ln.signal.ticker for ln in stock}
        if "tiingo_symbol_cap" in caps and len(tickers) > caps["tiingo_symbol_cap"]:
            errors.append(f"{len(tickers)} distinct stock history tickers exceed tiingo_symbol_cap "
                          f"{caps['tiingo_symbol_cap']}")
        sleeve, ref = self.universe.stock_sleeve, self.reference.get("sleeve")
        if isinstance(ref, dict) and sleeve is not None:
            if "weight" in ref and not math.isclose(float(ref["weight"]), sleeve.sleeve_weight, abs_tol=WEIGHT_EPS):
                errors.append(f"sleeve_weight {sleeve.sleeve_weight} differs from reference.yaml sleeve.weight "
                              f"{ref['weight']}")
            if "names" in ref and int(ref["names"]) != sleeve.names_target:
                errors.append(f"names_target {sleeve.names_target} differs from reference.yaml sleeve.names "
                              f"{ref['names']}")
        if errors:
            raise ValueError("invalid stock sleeve: " + "; ".join(errors))
        return self

    @classmethod
    def load(cls, directory: Path | None = None, *, include_sleeve: bool = True) -> Policy:
        """Parse and validate the policy in `directory` (default: the repository's `policy/`).
        `include_sleeve=False` ignores `stock-sleeve.yaml`, for its lines and for the hash."""
        directory = directory or POLICY_DIR
        stocks = _load(STOCK_RANK_FILE, directory) if (directory / STOCK_RANK_FILE).exists() else {}
        return cls(
            universe=_universe(directory, stocks, include_sleeve=include_sleeve),
            risk=_load("risk.yaml", directory),
            reference=_load("reference.yaml", directory),
            costs=_load("costs.yaml", directory),
            council=_load("council.yaml", directory),
            calendar=_load_calendars(directory),
            stocks=stocks,
            sha256=policy_sha256(directory, exclude=() if include_sleeve else (SLEEVE_FILE,)),
        )


def policy_sha256(directory: Path | None = None, exclude: Iterable[str] = ()) -> str:
    """Hash of every policy file (sorted by name) — changes whenever any number changes. `exclude`
    names files left out (the core-only policy leaves out the sleeve file)."""
    directory = directory or POLICY_DIR
    skip = set(exclude)
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.yaml")):
        if path.name in skip:
            continue
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# ------------------------------------------------------------------------------ process default
_installed: Policy | None = None


@lru_cache(maxsize=1)
def _loaded_default() -> Policy:
    from council import invariants  # lazy: invariants imports this module

    return Policy.load(include_sleeve=invariants.STOCK_SLEEVE_LIVE)


def install_default_policy(policy: Policy | None) -> None:
    """Make `default_policy()` return `policy` in this process (a live context installs its HEAD
    snapshot, so no module falls back to the working tree); `None` restores the repository file."""
    global _installed
    _installed = policy


def default_policy() -> Policy:
    """The process's policy: the installed live snapshot, else the working-tree `policy/` loaded
    once. A committed sleeve becomes stock lines only once `invariants.STOCK_SLEEVE_LIVE` is True
    (the go-live commit); until then this is the core-only policy."""
    return _installed if _installed is not None else _loaded_default()


default_policy.cache_clear = _loaded_default.cache_clear  # type: ignore[attr-defined]
