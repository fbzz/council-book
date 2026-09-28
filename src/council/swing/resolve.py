"""Ticker resolver for Scout ideas (design swing-book.md rev 2, §1.4 step 1; SW-1).

For every idea of a slot, BEFORE any other role sees it:
1. Normalise the exact US symbol the Scout wrote (`council.stocks.universe.normalise_id`:
   `BRK.B` / `BRK-B` / `BRK/B` -> line id `BRK_B`). Unusable text -> `unresolved_symbol`.
2. SEC ticker -> CIK (`company_tickers.json` rows keyed by line id). A symbol SEC does not list
   (`NVDAA`) -> `unresolved_symbol`. There is no fuzzy match, ever.
3. ONE bounded eToro eligibility request by exact symbols for all ideas of the slot
   (`council.stocks.eligibility.check_symbols` with the idea's `side`, at most MAX_SYMBOLS_PER_SLOT
   symbols): exactly one row whose returned symbol equals the request; `allowOpenPosition`,
   `allowStopLossTakeProfit`, a `real/long/1` config for a long or a `cfd/short/1` config for a short,
   SL bounds covering the swing stop range, plus the gate's other checks. Any failure ->
   `not_eligible_<side>` (the gate's own codes are kept in `detail`, private).
4. The capability gate: `stock_real_long` (long) / `stock_cfd_short` (short) must be verified
   (`Capabilities.allows_vehicle`); until SW-5 proves them this is `capability_not_proven:<cap>`.
5. A resolved instrument (id, returned symbol, SL% bounds of the side's config) is saved into
   `InstrumentMap` under the line id, so approval, the price guard and reconcile can map it.

Fail closed: without a broker read client every idea is `eligibility_unavailable`, unless a
RECORDED eligibility fixture is given (rehearsal): then the gate runs on the fixture, nothing is
saved, and a passing idea carries the label `rehearsal_unverified` (never tradeable).

Read-only: this module never imports the broker writer and makes no write call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from council.broker.eligibility import parse_eligibility
from council.broker.instruments import InstrumentMap, rows_by_symbol
from council.stocks import eligibility as gate
from council.stocks.sec import TickerRow
from council.stocks.universe import ticker_map, try_normalise_id

Side = Literal["long", "short"]
MAX_SYMBOLS_PER_SLOT = 8
SIDE_CAPABILITIES: Mapping[str, tuple[str, ...]] = {
    "long": ("stock_real_long",),
    "short": ("stock_cfd_short",),
}
REHEARSAL_LABEL = "rehearsal_unverified"


@dataclass(frozen=True)
class IdeaRef:
    ticker: str                      # exactly as the Scout wrote it
    side: Side


@dataclass(frozen=True)
class Resolution:
    ticker: str
    side: str
    ok: bool
    reason: str | None = None                    # public drop code
    line_id: str | None = None
    cik: int | None = None
    instrument_id: int | None = None
    broker_symbol: str | None = None
    min_sl_pct: float | None = None
    max_sl_pct: float | None = None
    label: str | None = None                     # REHEARSAL_LABEL on a fixture run
    detail: tuple[str, ...] = field(default=(), repr=False)   # the gate's codes (private audit)

    @property
    def tradeable(self) -> bool:
        return self.ok and self.label is None


class RecordedRead:
    """A READ-client stand-in over a recorded eligibility payload and a recorded rates payload (the
    rehearsal fixture). Only the two calls the gate makes exist."""

    def __init__(self, eligibility_payload: Any, rates_payload: Any, *, fetched_at: datetime) -> None:
        self._rows = parse_eligibility(eligibility_payload, fetched_at)
        self._rates = rates_payload

    def eligibility(self, symbols: Sequence[str] | None = None, instrument_ids: Sequence[int] | None = None,
                    **_: Any) -> list[Any]:
        wanted = {s.upper() for s in symbols or ()}
        ids = {int(i) for i in instrument_ids or ()}
        return [r for r in self._rows if r.symbol.upper() in wanted or r.instrument_id in ids]

    def rates(self, ids: Iterable[int]) -> Any:
        return self._rates


def broker_request_symbol(line_id: str) -> str:
    """The exact symbol asked of the broker: the line id with the class separator as `.`."""
    return line_id.replace("_", ".")


def _swing_gate_config(base: gate.GateConfig, side: str, *, stop_min: float, stop_max: float) -> gate.GateConfig:
    return gate.GateConfig(stop_floor=stop_min, stop_cap=stop_max, sl_buffer_pp=base.sl_buffer_pp,
                           target_notional_usd=base.target_notional_usd,
                           whole_unit_max_share=base.whole_unit_max_share)


def resolve_ideas(
    ideas: Sequence[IdeaRef],
    *,
    sec_tickers: Iterable[TickerRow],
    cfg: gate.GateConfig,
    stop_min: float,
    stop_max_long: float,
    stop_max_short: float,
    now: datetime,
    read: Any | None = None,
    recorded: RecordedRead | None = None,
    capabilities: Any | None = None,
    instruments: InstrumentMap | None = None,
) -> tuple[list[Resolution], InstrumentMap | None]:
    """Resolutions in the ideas' order and the updated instrument map (saved by the caller; None
    when nothing new was resolved or no map was given). At most one eligibility request."""
    by_line = ticker_map(sec_tickers)
    out: list[Resolution | None] = [None] * len(ideas)
    pending: dict[str, list[int]] = {}
    sides: dict[str, str] = {}
    for i, idea in enumerate(ideas):
        line_id = try_normalise_id(idea.ticker)
        row = by_line.get(line_id) if line_id else None
        if line_id is None or row is None:
            out[i] = Resolution(idea.ticker, idea.side, False, "unresolved_symbol", line_id=line_id)
            continue
        symbol = broker_request_symbol(line_id)
        if symbol in sides and sides[symbol] != idea.side:
            out[i] = Resolution(idea.ticker, idea.side, False, "conflicting_sides", line_id, row.cik)
            continue
        sides[symbol] = idea.side
        pending.setdefault(symbol, []).append(i)
    if len(pending) > MAX_SYMBOLS_PER_SLOT:
        for symbol in list(pending)[MAX_SYMBOLS_PER_SLOT:]:
            for i in pending.pop(symbol):
                idea = ideas[i]
                out[i] = Resolution(idea.ticker, idea.side, False, "resolve_quota",
                                    try_normalise_id(idea.ticker))
    source = read if read is not None else recorded
    verdicts: dict[str, gate.Verdict] = {}
    configs: dict[str, Any] = {}
    if pending and source is not None:
        try:
            verdicts, configs = _judge(source, list(pending), sides, cfg, now=now, stop_min=stop_min,
                                       stop_max={"long": stop_max_long, "short": stop_max_short})
        except Exception as exc:     # a failed read is `eligibility_unavailable`, never a crash or a pass
            failure = type(exc).__name__
            for indices in pending.values():
                for i in indices:
                    idea = ideas[i]
                    line_id = try_normalise_id(idea.ticker)
                    out[i] = Resolution(idea.ticker, idea.side, False, "eligibility_unavailable", line_id,
                                        by_line[line_id].cik if line_id else None, detail=(failure,))
            return [r for r in out if r is not None], None
    found: dict[str, int] = {}
    for symbol, indices in pending.items():
        for i in indices:
            idea = ideas[i]
            line_id = try_normalise_id(idea.ticker)
            cik = by_line[line_id].cik if line_id else None
            if source is None:
                out[i] = Resolution(idea.ticker, idea.side, False, "eligibility_unavailable", line_id, cik)
                continue
            v = verdicts.get(symbol)
            if v is None or not v.ok or v.symbol is None or v.symbol.upper() != symbol.upper():
                detail = v.reasons if v is not None else ("not_found",)
                out[i] = Resolution(idea.ticker, idea.side, False, f"not_eligible_{idea.side}", line_id, cik,
                                    detail=tuple(detail))
                continue
            config = configs.get(symbol)
            label = REHEARSAL_LABEL if read is None else None
            if read is not None and capabilities is not None:
                caps = SIDE_CAPABILITIES[idea.side]
                if not capabilities.allows_vehicle(caps):
                    missing = next(c for c in caps if c not in getattr(capabilities, "verified", ()))
                    out[i] = Resolution(idea.ticker, idea.side, False, f"capability_not_proven:{missing}",
                                        line_id, cik, v.instrument_id, v.symbol)
                    continue
            elif read is not None:
                cap = SIDE_CAPABILITIES[idea.side][0]
                out[i] = Resolution(idea.ticker, idea.side, False, f"capability_not_proven:{cap}",
                                    line_id, cik, v.instrument_id, v.symbol)
                continue
            out[i] = Resolution(idea.ticker, idea.side, True, None, line_id, cik, v.instrument_id, v.symbol,
                                config.min_sl_pct if config else None, config.max_sl_pct if config else None,
                                label=label)
            if read is not None and line_id is not None and v.instrument_id is not None:
                found[line_id] = int(v.instrument_id)
    new_map = instruments.merged(found, now) if instruments is not None and found else None
    return [r for r in out if r is not None], new_map


def _judge(source: Any, symbols: list[str], sides: Mapping[str, str], cfg: gate.GateConfig, *,
           now: datetime, stop_min: float, stop_max: Mapping[str, float]) -> tuple[dict[str, gate.Verdict],
                                                                              dict[str, Any]]:
    """ONE eligibility request for every symbol of the slot and one rates request for the names
    with exactly one row; each row judged by its idea's side with that side's stop range."""
    single, many = rows_by_symbol(source.eligibility(symbols=symbols))
    quotes = gate._quotes(source, [single[s.upper()] for s in symbols if s.upper() in single], now)
    verdicts: dict[str, gate.Verdict] = {}
    configs: dict[str, Any] = {}
    for symbol in symbols:
        key, side = symbol.upper(), sides[symbol]
        if key in many:
            verdicts[symbol] = gate.Verdict(symbol, False, ("ambiguous",), checked_at=now)
        elif key not in single:
            verdicts[symbol] = gate.Verdict(symbol, False, ("not_found",), checked_at=now)
        else:
            row = single[key]
            side_cfg = _swing_gate_config(cfg, side, stop_min=stop_min, stop_max=stop_max[side])
            verdicts[symbol] = gate.check_row(symbol, row, quotes.get(row.instrument_id), side_cfg,
                                              now=now, side=side)
            configs[symbol] = gate.side_config(row, side)
    return verdicts, configs
