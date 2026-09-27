"""Vehicle symbol → broker instrument id, persisted privately at `<state_dir>/instruments.json`.

Rules:
- Resolution is by EXACT symbol through the eligibility route (search entitlement may be blocked).
  A symbol resolves only when the broker returns EXACTLY ONE row for it; two or more rows for the
  same symbol are ambiguous and the symbol stays unresolved (fail closed, never "the first row").
- The map is immutable: re-verification may refresh `verified_at`, but a symbol whose instrument id
  changes — or a new symbol claiming an id another symbol owns — raises InstrumentIdentityChanged
  and nothing is written (the caller freezes that instrument).
- Explicit aliases (design §3.6, a ticker rename): `with_alias(old, new, id)` records that the
  broker renamed instrument `id` from `old` to `new`. Only then may `new` share `old`'s id: `new`
  becomes the instrument's current symbol (`symbol_for`), `old` stays readable (`get(old)`, for
  ledger rows and old records). An alias is never inferred from a broker answer.
- The file is written atomically (temp file + rename), mode 0600, and never inside the repo.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from council.models.broker import EligibilityRow
from council.models.common import Frozen
from council.paths import assert_outside_repo, state_dir

FILE_NAME = "instruments.json"
FORMAT_VERSION = 1            # "aliases" is an optional key: files without it read as before


class InstrumentIdentityChanged(RuntimeError):
    def __init__(self, symbol: str, old_id: int | None, new_id: int, detail: str = "") -> None:
        super().__init__(detail or f"instrument identity changed for {symbol}")
        self.symbol = symbol
        self.old_id = old_id
        self.new_id = new_id


class InstrumentEntry(Frozen):
    instrument_id: int
    resolved_at: datetime
    verified_at: datetime


class InstrumentAlias(Frozen):
    """An explicit rename of one instrument: `old` was its symbol, `new` is its current one."""

    old: str
    new: str
    instrument_id: int
    recorded_at: datetime


class EligibilityReader(Protocol):
    def eligibility(
        self, symbols: list[str] | None = None, instrument_ids: list[int] | None = None
    ) -> list[EligibilityRow]: ...


def default_path() -> Path:
    return state_dir() / FILE_NAME


def rows_by_symbol(rows: Iterable[EligibilityRow]) -> tuple[dict[str, EligibilityRow], set[str]]:
    """({upper symbol: its one row}, {upper symbols with more than one row}). An ambiguous symbol
    is left out of the first map: a symbol resolves only through exactly one row."""
    groups: dict[str, list[EligibilityRow]] = {}
    for row in rows:
        groups.setdefault(row.symbol.upper(), []).append(row)
    single = {sym: group[0] for sym, group in groups.items() if len(group) == 1}
    return single, {sym for sym, group in groups.items() if len(group) > 1}


class InstrumentMap:
    """Read-only mapping; `merged` / `with_alias` return a new map, `save` persists it."""

    def __init__(
        self,
        entries: Mapping[str, InstrumentEntry] | None = None,
        *,
        path: Path | None = None,
        unresolved: Iterable[str] = (),
        aliases: Mapping[str, InstrumentAlias] | None = None,
        ambiguous: Iterable[str] = (),
    ) -> None:
        self._entries = MappingProxyType(dict(entries or {}))
        self._aliases = MappingProxyType(dict(aliases or {}))
        self.path = path or default_path()
        self.unresolved: tuple[str, ...] = tuple(unresolved)
        self.ambiguous: tuple[str, ...] = tuple(ambiguous)

    # --------------------------------------------------------------------------- queries
    @property
    def entries(self) -> Mapping[str, InstrumentEntry]:
        return self._entries

    @property
    def aliases(self) -> Mapping[str, InstrumentAlias]:
        """Explicit renames keyed by the OLD symbol."""
        return self._aliases

    def get(self, symbol: str) -> int | None:
        entry = self._entries.get(symbol)
        return entry.instrument_id if entry else None

    def current_symbol(self, symbol: str) -> str:
        """The symbol an (old) symbol was renamed to, following recorded aliases; itself otherwise."""
        seen = {symbol}
        while symbol in self._aliases:
            symbol = self._aliases[symbol].new
            if symbol in seen:                       # a cycle cannot be recorded; stop defensively
                break
            seen.add(symbol)
        return symbol

    def symbol_for(self, instrument_id: int) -> str | None:
        """The CURRENT symbol of an instrument: after a recorded rename, the new symbol."""
        found = [s for s, e in self._entries.items() if e.instrument_id == instrument_id]
        if not found:
            return None
        current = [s for s in found if s not in self._aliases]
        return (current or found)[0]

    def ids(self) -> dict[str, int]:
        """Every symbol (renamed-away ones included) → its instrument id."""
        return {s: e.instrument_id for s, e in self._entries.items()}

    def symbols_by_id(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for entry in self._entries.values():
            symbol = self.symbol_for(entry.instrument_id)
            if symbol is not None:
                out[entry.instrument_id] = symbol
        return out

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def _linked(self, a: str, b: str) -> bool:
        """`a` and `b` are the same instrument through recorded renames (either direction)."""
        return self.current_symbol(a) == self.current_symbol(b)

    # --------------------------------------------------------------------------- updates
    def merged(
        self,
        found: Mapping[str, int],
        now: datetime,
        *,
        unresolved: Iterable[str] = (),
        ambiguous: Iterable[str] = (),
    ) -> InstrumentMap:
        """A new map with `found` added/verified. Identity changes raise; nothing is mutated. A
        symbol may share an id only with symbols it is linked to by a recorded alias."""
        entries = dict(self._entries)
        for symbol, instrument_id in found.items():
            existing = entries.get(symbol)
            if existing is not None and existing.instrument_id != instrument_id:
                raise InstrumentIdentityChanged(symbol, existing.instrument_id, instrument_id)
            others = [s for s, e in entries.items() if e.instrument_id == instrument_id and s != symbol]
            strangers = [s for s in others if not self._linked(s, symbol)]
            if strangers:
                raise InstrumentIdentityChanged(
                    symbol, None, instrument_id,
                    f"instrument id already mapped to {strangers[0]}; refusing {symbol}",
                )
            entries[symbol] = InstrumentEntry(
                instrument_id=instrument_id,
                resolved_at=existing.resolved_at if existing else now,
                verified_at=now,
            )
        return InstrumentMap(entries, path=self.path, unresolved=unresolved, aliases=self._aliases,
                             ambiguous=ambiguous)

    def with_alias(self, old: str, new: str, instrument_id: int, now: datetime) -> InstrumentMap:
        """A new map recording that instrument `instrument_id` was renamed from `old` to `new`.

        Refused (InstrumentIdentityChanged, nothing mutated) unless `old` is mapped to that id,
        `new` is not mapped to another id, and the rename neither forks nor undoes an earlier one."""
        if old == new:
            raise ValueError("an alias needs two different symbols")
        entries = dict(self._entries)
        known = entries.get(old)
        if known is None or known.instrument_id != instrument_id:
            raise InstrumentIdentityChanged(
                old, known.instrument_id if known else None, instrument_id,
                f"{old} is not mapped to the renamed instrument; refusing the alias to {new}",
            )
        target = entries.get(new)
        if target is not None and target.instrument_id != instrument_id:
            raise InstrumentIdentityChanged(new, target.instrument_id, instrument_id)
        if old in self._aliases and self._aliases[old].new != new:
            raise InstrumentIdentityChanged(old, instrument_id, instrument_id,
                                            f"{old} was already renamed to {self._aliases[old].new}")
        if self.current_symbol(new) == old:
            raise InstrumentIdentityChanged(new, instrument_id, instrument_id,
                                            f"{new} was renamed to {old}; refusing a cycle")
        entries[new] = InstrumentEntry(
            instrument_id=instrument_id,
            resolved_at=target.resolved_at if target else now,
            verified_at=now,
        )
        aliases = dict(self._aliases)
        aliases[old] = InstrumentAlias(old=old, new=new, instrument_id=instrument_id, recorded_at=now)
        return InstrumentMap(entries, path=self.path, aliases=aliases)

    # --------------------------------------------------------------------------- persistence
    @classmethod
    def load(cls, path: Path | None = None) -> InstrumentMap:
        path = path or default_path()
        if not path.exists():
            return cls({}, path=path)
        data = json.loads(path.read_text())
        if data.get("version") != FORMAT_VERSION:
            raise ValueError(f"unsupported instruments.json version {data.get('version')!r}")
        entries = {
            symbol: InstrumentEntry.model_validate(raw)
            for symbol, raw in (data.get("instruments") or {}).items()
        }
        aliases = {
            old: InstrumentAlias.model_validate(raw)
            for old, raw in (data.get("aliases") or {}).items()
        }
        for old, alias in aliases.items():
            ends = (entries.get(old), entries.get(alias.new))
            if alias.old != old or any(e is None or e.instrument_id != alias.instrument_id for e in ends):
                raise ValueError(f"instruments.json: alias {old!r} does not match its entries")
        return cls(entries, path=path, aliases=aliases)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": FORMAT_VERSION,
            "instruments": {
                s: e.model_dump(mode="json") for s, e in sorted(self._entries.items())
            },
        }
        if self._aliases:
            out["aliases"] = {o: a.model_dump(mode="json") for o, a in sorted(self._aliases.items())}
        return out

    def save(self) -> Path:
        assert_outside_repo(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".instruments.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.to_json(), fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.path


def found_from_rows(rows: Iterable[EligibilityRow],
                    wanted: Iterable[str]) -> tuple[dict[str, int], list[str], list[str]]:
    """({symbol: instrument id}, missing, ambiguous) for the wanted symbols: a symbol maps only
    through exactly one returned row with that symbol (case-insensitive)."""
    single, many = rows_by_symbol(rows)
    found: dict[str, int] = {}
    missing: list[str] = []
    ambiguous: list[str] = []
    for symbol in dict.fromkeys(wanted):
        key = symbol.upper()
        if key in many:
            ambiguous.append(symbol)
        elif key in single:
            found[symbol] = single[key].instrument_id
        else:
            missing.append(symbol)
    return found, missing, ambiguous


def resolve(
    read_client: EligibilityReader,
    symbols: Iterable[str],
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> InstrumentMap:
    """Resolve exact symbols through eligibility, verify against the stored map, persist.

    Returns the new map; symbols the broker did not return, or returned more than once, are listed
    in `.unresolved` (the latter also in `.ambiguous`) and are not mapped."""
    wanted = list(dict.fromkeys(symbols))
    current = InstrumentMap.load(path)
    if not wanted:
        return current
    rows = read_client.eligibility(symbols=wanted)
    found, missing, ambiguous = found_from_rows(rows, wanted)
    updated = current.merged(found, now or datetime.now(UTC), unresolved=[*missing, *ambiguous],
                             ambiguous=ambiguous)
    updated.save()
    return updated


# ------------------------------------------------------------------------ resolution report (M5-C)
# `council instruments resolve` (m5-readiness §4 K3/K8/K15, gate K19): per line, the vehicle chosen
# for a long at 1x, with its currency, price unit, whole-unit flag, SL bounds and minimum.
#
# The eligibility route documents no currency field. The keys below are the candidates the parser
# accepts; until the token-day fixture (`doctor --record-fixtures`) confirms one, an absent currency
# is `unit_unknown` and the vehicle is not planned (fail closed, flag `instrument_unit_unknown`).
CURRENCY_KEYS = ("currency", "instrumentCurrency", "denominationCurrency")
PRICE_UNIT_KEYS = ("priceUnit", "quoteUnit", "priceCurrency")
# price unit -> (currency, factor from one price unit to one currency unit)
PRICE_UNITS: dict[str, tuple[str, float]] = {
    "USD": ("USD", 1.0), "EUR": ("EUR", 1.0), "GBP": ("GBP", 1.0), "CHF": ("CHF", 1.0),
    "GBX": ("GBP", 0.01), "GBP_PENCE": ("GBP", 0.01),
}
_UNIT_ALIASES = {"GBP": "GBP", "GBX": "GBX", "GBPENCE": "GBX", "PENCE": "GBX", "GBp": "GBX"}


class UnknownPriceUnit(ValueError):
    """A price unit the scaling table does not know: the vehicle is not planned."""


def normalise_unit(value: Any) -> str | None:
    """'GBp' / 'GBX' / 'pence' -> 'GBX'; other ISO codes upper-cased; blank or non-text -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text in _UNIT_ALIASES:
        return _UNIT_ALIASES[text]
    upper = text.upper().replace(" ", "").replace("-", "")
    return _UNIT_ALIASES.get(upper, upper)


def scale_price(price: float, unit: str) -> tuple[float, str]:
    """(price in its currency, currency): the ONE place a GBX (pence) quote becomes GBP (x 0.01).
    An unknown unit raises UnknownPriceUnit (never a silent 1:1)."""
    norm = normalise_unit(unit)
    if norm is None or norm not in PRICE_UNITS:
        raise UnknownPriceUnit(f"unknown price unit {unit!r}")
    currency, factor = PRICE_UNITS[norm]
    return float(price) * factor, currency


def unit_of(raw_row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(currency, price unit) of a raw eligibility row; (None, None) when the row names neither.
    A pence currency ('GBX', 'GBp') means currency GBP quoted in GBX; a named currency without an
    explicit unit is quoted in itself. A unit outside PRICE_UNITS reads as unknown."""
    currency = next((normalise_unit(raw_row.get(k)) for k in CURRENCY_KEYS if raw_row.get(k)), None)
    unit = next((normalise_unit(raw_row.get(k)) for k in PRICE_UNIT_KEYS if raw_row.get(k)), None)
    if currency == "GBX":
        currency, unit = "GBP", unit or "GBX"
    unit = unit or currency
    if unit is None or unit not in PRICE_UNITS:
        return currency, None
    if currency is None:
        currency = PRICE_UNITS[unit][0]
    if PRICE_UNITS[unit][0] != currency:
        return currency, None                     # a unit of another currency: unknown, fail closed
    return currency, unit


class LineResolution(Frozen):
    """One line's long-1x vehicle. PRIVATE: `min_position_usd` is a broker amount (never printed);
    the report prints codes, symbols and NAV shares only."""

    line: str
    vehicle: str | None = None
    settlement: str | None = None
    currency: str | None = None
    price_unit: str | None = None
    whole_units: bool = False
    sl_min_pct: float | None = None
    sl_max_pct: float | None = None
    min_position_usd: float | None = None
    held_until: str | None = None          # a capability the vehicle waits for (e.g. "cfd_long")
    code: str = "resolved"                 # resolved | no_vehicle | unit_unknown | held:<capability>


def resolution_report(
    lines: Iterable[Any],
    rows: Mapping[str, EligibilityRow],
    raw_rows: Mapping[str, Mapping[str, Any]],
    expected_cost: Any,
    capabilities: Mapping[str, bool] | None = None,
) -> list[LineResolution]:
    """Per line: the eligible long-1x candidate with the lowest expected cost (`eligibility.
    resolve_vehicle`), its unit facts, and whether it waits for a capability (a CFD vehicle waits
    for `cfd_long`, a real one for nothing here). `rows` / `raw_rows` are keyed by upper symbol and
    hold only unambiguous symbols."""
    from council.broker.eligibility import resolve_vehicle, whole_units_only

    caps = dict(capabilities or {})
    out: list[LineResolution] = []
    for line in lines:
        choice = resolve_vehicle(line, "long", 1, rows, expected_cost)
        if choice is None:
            out.append(LineResolution(line=line.symbol, code="no_vehicle"))
            continue
        key = choice.symbol.upper()
        row = rows.get(key) or rows.get(choice.symbol)
        currency, unit = unit_of(raw_rows.get(key) or {})
        held = "cfd_long" if choice.settlement == "cfd" and not caps.get("cfd_long") else None
        code = "unit_unknown" if unit is None else (f"held:{held}" if held else "resolved")
        out.append(LineResolution(
            line=line.symbol, vehicle=choice.symbol, settlement=choice.settlement,
            currency=currency, price_unit=unit,
            whole_units=bool(row is not None and whole_units_only(row)),
            sl_min_pct=choice.config.min_sl_pct, sl_max_pct=choice.config.max_sl_pct,
            min_position_usd=(max(float(row.min_position_exposure), float(choice.config.min_position_amount))
                              if row is not None else None),
            held_until=held, code=code,
        ))
    return out
