"""The strict single-stock onboarding gate (design §4.3). Read-only: eligibility and rates through the
READ client, nothing else.

A name passes only when ALL of these hold (every missing field fails closed):
- the broker returned EXACTLY ONE eligibility row for the exact symbol asked for (case-insensitive);
  its returned symbol and instrument id are what the sleeve file records;
- `allowOpenPosition`, `allowClosePosition` and `allowPartialClosePosition` (the rule trims drift
  and sells retiring names in parts), each an explicit true (the core parser's permissive default
  for an absent close permission does not apply here);
- a long / leverage 1 / real / non-potential leverage config that allows setting and editing a
  stop-loss (`allowStopLossTakeProfit`, `allowEditStopLoss`), whose SL bounds admit EVERY stop
  distance in [stock floor, stop cap] after the policy's margin buffer (real 1x: SL% = distance x
  100). The floor is `risk.catastrophe_stop.floors.stock`, 0.15 (design §5.5) until policy sets it;
- `requiresW8Ben` false or absent (any other value fails; the user is asked whether to file it);
- orders by units allowed (`allowedOrderQuantityType`) and a unit (share) instrument
  (`tradeUnitType`);
- fractional units (`unitsQuantityType` naming fractions; absent or unknown = whole units), or a
  whole-unit price of at most 0.5 x one unit's virtual notional;
- a `/market-data/rates` quote with a positive bid and ask;
- a returned symbol the sleeve file can record (`council.policy.BrokerSymbol`).

Reasons are fixed codes (`REASONS`). The target notional is PRIVATE (it encodes the NAV): it lives
only in `GateConfig` (hidden from repr) and never in a reason, a log line or a public file.
`preflight_errors` lists the stock lines of a policy whose eligibility was never checked (a rank
run with `--no-eligibility`): live runs must refuse them. `preflight_blockers` gives the same as
satellite-scoped R20 blockers (the stock sleeve is held, the core runs); `council doctor` reports
them as a failed check.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from council.broker.eligibility import (
    close_allowed,
    fractional_units,
    trades_in_units,
    unit_orders_allowed,
    w8ben_required,
)
from council.broker.instruments import rows_by_symbol
from council.broker.parsing import parse_rates
from council.ledger.states import SATELLITE_BLOCKER_PREFIX
from council.models.broker import EligibilityRow, LeverageConfig, Quote
from council.policy import BrokerSymbol, Policy

STOCK_STOP_FLOOR = 0.15          # design §5.5; risk.yaml catastrophe_stop.floors.stock wins once set
WHOLE_UNIT_MAX_SHARE = 0.5       # a whole-unit price at most half of one unit's virtual notional
RESERVES = 15                    # names checked beyond selected + shortlist (one bounded call)
UNCHECKED = "stock_eligibility_unchecked"
REASONS = (
    "not_found", "ambiguous", "open_not_allowed", "close_not_allowed", "partial_close_not_allowed",
    "no_real_long_1x", "sl_bounds", "requires_w8ben", "units_not_allowed", "trade_unit_type",
    "whole_unit_price", "no_quote", "bad_symbol",
)
_BROKER_SYMBOL = TypeAdapter(BrokerSymbol)          # the sleeve file's own field type


def recordable(symbol: str) -> bool:
    """The sleeve file can record this broker symbol (`council.policy.BrokerSymbol`)."""
    try:
        _BROKER_SYMBOL.validate_python(symbol)
    except ValidationError:
        return False
    return True


@dataclass(frozen=True)
class GateConfig:
    stop_floor: float
    stop_cap: float
    sl_buffer_pp: float
    target_notional_usd: float = field(repr=False)     # PRIVATE: one unit of the virtual NAV
    whole_unit_max_share: float = WHOLE_UNIT_MAX_SHARE


def gate_config(policy: Policy, *, unit_share: float, virtual_nav_usd: float | None = None) -> GateConfig:
    """The gate's thresholds from policy: the stock stop floor and cap, the SL margin buffer, and one
    unit's virtual notional (`unit_share` x the virtual NAV; the policy's assumed NAV when the
    snapshot gives none)."""
    from council.risk.costs import trade_economics

    stops = policy.risk["catastrophe_stop"]
    floor = float((stops.get("floors") or {}).get("stock", STOCK_STOP_FLOOR))
    nav = trade_economics(policy, virtual_nav_usd=virtual_nav_usd).virtual_nav_usd
    if not 0 < unit_share <= 1:
        raise ValueError("unit_share must be in (0, 1]")
    return GateConfig(stop_floor=floor, stop_cap=float(stops["cap"]),
                      sl_buffer_pp=float(stops.get("margin_pct_buffer_pp", 0.5)),
                      target_notional_usd=float(unit_share) * float(nav))


@dataclass(frozen=True)
class Verdict:
    """The gate's answer for one requested symbol (or instrument id)."""

    requested: str
    ok: bool
    reasons: tuple[str, ...] = ()
    symbol: str | None = None             # exactly as eligibility returned it
    instrument_id: int | None = None
    checked_at: datetime | None = None
    whole_units: bool = False

    @property
    def reason(self) -> str:
        return ",".join(self.reasons) or "ok"


def real_long_1x(row: EligibilityRow) -> LeverageConfig | None:
    """The row's long / 1x / real / non-potential config that lets us set and edit a stop, or None.
    Read from the fail-closed `stock_configs` (a row without them, or a config missing a stop-loss
    field, has none)."""
    for c in getattr(row, "stock_configs", None) or []:
        if (c.direction == "long" and 1 in c.leverage_values and c.settlement == "real"
                and not c.is_potential and c.allow_sl_tp and c.allow_edit_stop_loss):
            return c
    return None


def sl_bounds_ok(config: LeverageConfig, cfg: GateConfig) -> bool:
    """Every stop distance in [floor, cap] fits the config's SL% bounds after the buffer (1x: SL% =
    distance x 100), so no stop is ever widened or refused for this name."""
    lo = config.min_sl_pct + cfg.sl_buffer_pp
    hi = config.max_sl_pct - cfg.sl_buffer_pp
    return lo <= cfg.stop_floor * 100.0 + 1e-9 and cfg.stop_cap * 100.0 <= hi + 1e-9


def check_row(requested: str, row: EligibilityRow, quote: Quote | None, cfg: GateConfig, *,
              now: datetime, closing_only: bool = False) -> Verdict:
    """Every condition on one row. `closing_only` (a credited or retiring line that is only ever
    sold) checks only that the position can be closed."""
    reasons: list[str] = []
    whole = not fractional_units(row)          # an absent or unknown quantity type counts as whole units
    if not recordable(row.symbol):
        reasons.append("bad_symbol")
    if not close_allowed(row):                  # explicit JSON true only (absent fails closed)
        reasons.append("close_not_allowed")
    if not closing_only:
        if not row.allow_open:
            reasons.append("open_not_allowed")
        if not row.allow_partial_close:
            reasons.append("partial_close_not_allowed")
        config = real_long_1x(row)
        if config is None:
            reasons.append("no_real_long_1x")
        elif not sl_bounds_ok(config, cfg):
            reasons.append("sl_bounds")
        if w8ben_required(row):
            reasons.append("requires_w8ben")
        if not unit_orders_allowed(row):
            reasons.append("units_not_allowed")
        if not trades_in_units(row):
            reasons.append("trade_unit_type")
        if quote is None or quote.bid <= 0 or quote.ask <= 0:
            reasons.append("no_quote")
        elif whole and quote.ask > cfg.whole_unit_max_share * cfg.target_notional_usd:
            reasons.append("whole_unit_price")
    return Verdict(requested=requested, ok=not reasons, reasons=tuple(reasons), symbol=row.symbol,
                   instrument_id=row.instrument_id, checked_at=now, whole_units=whole)


def _quotes(read: Any, rows: Iterable[EligibilityRow], now: datetime) -> dict[int, Quote]:
    ids = sorted({r.instrument_id for r in rows})
    if not ids:
        return {}
    by_id = {r.instrument_id: r.symbol for r in rows}
    try:
        quotes = parse_rates(read.rates(ids), lambda i: by_id.get(i), at=now)
    except Exception:          # no quote is a gate failure for the name, never a crash
        return {}
    return {q.instrument_id: q for q in quotes.values()}


def check_symbols(read: Any, symbols: Sequence[str], cfg: GateConfig, *, now: datetime,
                  closing_only: Iterable[str] = ()) -> dict[str, Verdict]:
    """The gate for exact symbols: ONE eligibility request (the READ client batches at most 100 per
    POST) and one rates request for the names with exactly one row."""
    wanted = list(dict.fromkeys(symbols))
    if not wanted:
        return {}
    sell_only = {s.upper() for s in closing_only}
    single, many = rows_by_symbol(read.eligibility(symbols=wanted))
    quotes = _quotes(read, [single[s.upper()] for s in wanted if s.upper() in single], now)
    out: dict[str, Verdict] = {}
    for symbol in wanted:
        key = symbol.upper()
        if key in many:
            out[symbol] = Verdict(symbol, False, ("ambiguous",), checked_at=now)
        elif key not in single:
            out[symbol] = Verdict(symbol, False, ("not_found",), checked_at=now)
        else:
            row = single[key]
            out[symbol] = check_row(symbol, row, quotes.get(row.instrument_id), cfg, now=now,
                                    closing_only=key in sell_only)
    return out


@dataclass(frozen=True)
class InstrumentVerdicts:
    """The gate on one instrument id (the corporate-action path): the full gate (a line that may be
    bought) and the closing-only gate (a credited or retiring line that is only ever sold)."""

    row: EligibilityRow | None
    full: Verdict
    closing: Verdict


def instrument_verdicts(read: Any, instrument_id: int, cfg: GateConfig, *, now: datetime) -> InstrumentVerdicts:
    """ONE eligibility request by instrument id and one rates request: exactly one row with that id,
    judged by the full and by the closing-only gate."""
    rows = [r for r in read.eligibility(instrument_ids=[int(instrument_id)])
            if r.instrument_id == int(instrument_id)]
    label = str(int(instrument_id))
    if len(rows) != 1:
        failed = Verdict(label, False, ("not_found" if not rows else "ambiguous",), checked_at=now)
        return InstrumentVerdicts(None, failed, failed)
    row = rows[0]
    quote = _quotes(read, rows, now).get(row.instrument_id)
    return InstrumentVerdicts(row, check_row(label, row, quote, cfg, now=now),
                              check_row(label, row, quote, cfg, now=now, closing_only=True))


def check_instrument(read: Any, instrument_id: int, cfg: GateConfig, *, now: datetime,
                     closing_only: bool = False) -> tuple[Verdict, EligibilityRow | None]:
    """The gate for one instrument id: exactly one row with that id."""
    found = instrument_verdicts(read, instrument_id, cfg, now=now)
    return (found.closing if closing_only else found.full), found.row


def replace_failures(names: Sequence[str], order: Sequence[str], sector: Mapping[str, Any],
                     ok: Callable[[str], bool], used: Iterable[str] = ()) -> tuple[list[str], list[tuple[str, str]]]:
    """Keep each passing name that is not in `used`; replace every other one with the next passing,
    unused name in `order`, preferring the failed name's sector (the slot the frozen rule's sector
    constraint gave it), else any sector. A slot with no candidate left stays empty.
    Returns (final names, [(replaced name, replacement)])."""
    blocked = set(used)
    keep = [n for n in names if ok(n) and n not in blocked]
    taken = blocked | set(keep)
    final: list[str] = []
    replaced: list[tuple[str, str]] = []
    for name in names:
        if name in keep:
            final.append(name)
            continue
        pick = next((c for c in order if sector.get(c) == sector.get(name) and c not in taken and ok(c)), None)
        if pick is None:
            pick = next((c for c in order if c not in taken and ok(c)), None)
        if pick is not None:
            final.append(pick)
            taken.add(pick)
            replaced.append((name, pick))
    return final, replaced


def preflight_errors(policy: Policy) -> list[str]:
    """`stock_eligibility_unchecked:<line>` for every stock line whose eligibility was never checked
    (`eligibility_checked_at` null). A live run refuses these lines."""
    return [f"{UNCHECKED}:{line.symbol}" for line in policy.universe.stock_lines()
            if line.stock is not None and line.stock.eligibility_checked_at is None]


def preflight_blockers(policy: Policy) -> list[str]:
    """The live refusal as R20 input: one satellite-scoped blocker `satellite:<UNCHECKED>` when any
    stock line is unchecked (the sleeve is held; the core keeps running). Line ids stay out of the
    blocker string (blocker strings reach the public record); `preflight_errors` names them."""
    return [f"{SATELLITE_BLOCKER_PREFIX}{UNCHECKED}"] if preflight_errors(policy) else []


def doctor_sample(read: Any, symbols: Sequence[str], cfg: GateConfig, *, now: datetime) -> list[Verdict]:
    """`council doctor --live-read`: the full gate on a few stock symbols (reasons only, no numbers)."""
    verdicts = check_symbols(read, symbols, cfg, now=now)
    return [verdicts[s] for s in dict.fromkeys(symbols)]
