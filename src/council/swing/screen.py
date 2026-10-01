"""The after-close movers screen (design swing-book.md rev 2, §1.2; SW-1). Facts, not picks.

Built ONCE per US trading day after the close, for the screen universe (S&P 500 + Nasdaq-100 +
S&P MidCap 400 + S&P SmallCap 600 + the AI list, ~1,520 names, plus the FF12 sector ETFs and the
market-context ETFs SPY / QQQ / IWM / VIXY), from COMPLETED daily bars only. The ETFs' D moves are kept in `Screen.context` (the Scout's MARKET
CONTEXT). A screen for D is available to a slot from D 20:30 New York (`ready_at`), whenever its
cache file was written (the cycle writes it at its own clock, which is at or after the slot):

- Readiness: the screen is for the latest session D whose D 20:30 New York has passed, when
  Alpaca's completed SIP bar for D exists (the bar-availability rule is D 20:00 New York; the extra
  30 minutes cover the free plan's delay). Before today's 20:30 that is the previous session (the
  caller keeps one screen per D in the cache and does not rebuild it).
- Data: Alpaca daily bars, `feed=sip` always (the IEX feed carries a fraction of the volume, so a
  ratio of an IEX bar to a SIP median would be meaningless - red-team pitfall), adjusted, completed
  days only (`available_only`), never the live-rate route, never a broker call. A name whose last
  bar is not D is left out (stale), never filled.
- Its OWN Alpaca pacing and breaker: at most one request per PACE_S seconds; its own request budget
  file (`state_dir/alpaca_screen_budget.json`, provider `alpaca_screen`), so a 429 during the screen
  trips only the screen's breaker and never the stock-line client's (`alpaca_budget.json`).
  A 429 stops the screen at once (partial screen, flag `screen_rate_limited`).
- Time budget: DEADLINE_S; request cap: MAX_REQUESTS.

Four lists, at most LIST_MAX names each (ids `M:<LINE_ID>:<list>`):
  (a) `movers`   |1-day move| / sigma_daily (20d) >= MOVER_MIN_SIGMA, largest first;
  (b) `volume`   volume ratio (SIP day volume / SIP 20-day median of the prior sessions) >= VOLUME_MIN_RATIO;
  (c) `unmoved`  an admitted 8-K/6-K accepted in D's window (after D-1's close, up to D's close)
                 and |move| < 1 sigma, most liquid first;
  (d) `laggard`  in each FF12 sector whose ETF, or >= 3 members, moved >= 2 sigma, the members that
                 moved < 0.5 sigma, strongest sector first, most liquid first.
Available to the next swing session. Values are percentages and ratios only (private cache;
the public record carries at most the list membership).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from council import paths
from council.clock import NEW_YORK, session_hours
from council.data import alpaca
from council.data.http import DataError, RateLimited
from council.facts.market import BudgetLimits, RequestBudget
from council.stocks.universe import try_normalise_id
from council.swing import series

PROVIDER = "alpaca_screen"
# The screen's own ceilings, far below Alpaca's free plan (200 requests a minute, no symbol quota:
# `facts.market.BUDGET_LIMITS`). One multi-symbol request counts ONCE against the hour and day
# limits, and `symbols_per_month` counts DISTINCT symbols (`RequestBudget.reserve`), so re-pulling
# the same names every session costs nothing more. The universe is ~1,520 names (S&P 500 +
# Nasdaq-100 + S&P 400 + S&P 600 + the AI list, user decision 2026-10-01) + 15 ETFs: one screen =
# ceil(1,535 / 100) = 16 requests, built once per session (`sources.prepare` caches it); a month of
# 23 sessions x 2 slots, even rebuilt at both, is 736 requests and ~1,535 distinct symbols plus
# index churn (test_screen_fits_a_month_of_two_slots).
LIMITS = BudgetLimits(requests_per_hour=40, requests_per_day=80, symbols_per_month=2_500)
PACE_S = 2.0
DEADLINE_S = 300.0
MAX_REQUESTS = 24
SYMBOLS_PER_REQUEST = 100          # ~100 x 50 sessions = 5,000 bars: one page
LOOKBACK_DAYS = 75                 # calendar days: >= 21 sessions for sigma and the volume median
READY_AFTER = (20, 30)             # New York wall time on D
UNIVERSE_MAX = 1_700
LIST_MAX = 10
MOVER_MIN_SIGMA = 2.0
VOLUME_MIN_RATIO = 2.0
UNMOVED_MAX_SIGMA = 1.0
SECTOR_MOVE_SIGMA = 2.0
SECTOR_MIN_MEMBERS = 3
LAGGARD_MAX_SIGMA = 0.5
LISTS = ("movers", "volume", "unmoved", "laggard")
SECTOR_ETF_LABEL: Mapping[str, str] = {
    "XLP": "consumer staples", "XLY": "consumer discretionary", "XLI": "industrials", "XLE": "energy",
    "XLB": "materials", "XLK": "technology", "XLC": "communication services", "XLU": "utilities",
    "XLV": "health care", "XLF": "financials",
}
# Broad-market ETFs whose D move is kept as the Scout's market context (VIXY: a VIX-futures ETF,
# the volatility proxy; no index level is ever read).
CONTEXT_ETFS: Mapping[str, str] = {
    "SPY": "S&P 500 ETF", "QQQ": "Nasdaq-100 ETF", "IWM": "Russell 2000 ETF",
    "VIXY": "VIX short-term futures ETF, volatility proxy",
}
# FF12 sector -> SPDR sector ETF ("Other" has none).
SECTOR_ETF: Mapping[str, str] = {
    "NoDur": "XLP", "Durbl": "XLY", "Manuf": "XLI", "Enrgy": "XLE", "Chems": "XLB", "BusEq": "XLK",
    "Telcm": "XLC", "Utils": "XLU", "Shops": "XLY", "Hlth": "XLV", "Money": "XLF",
}


@dataclass(frozen=True)
class ScreenName:
    line_id: str
    sector: str | None = None


@dataclass(frozen=True)
class Metric:
    line_id: str
    move_pct: float
    move_sigma: float
    vol_ratio: float | None
    dollar_volume_20d: float = field(repr=False)     # private (ranking only; never listed)
    sector: str | None = None


@dataclass
class Screen:
    session: str                                   # D, ISO date
    built_at: str
    lists: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {k: [] for k in LISTS})
    flags: list[str] = field(default_factory=list)
    requests: int = 0
    names_scored: int = 0
    ready: bool = True
    context: dict[str, dict[str, float]] = field(default_factory=dict)   # ETF: {move_pct, move_sigma}

    def fact_ids(self) -> list[str]:
        return [row["id"] for k in LISTS for row in self.lists.get(k, [])]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def content_hash(self) -> str:
        body = {"session": self.session, "lists": self.lists}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def fact_id(line_id: str, list_name: str) -> str:
    if list_name not in LISTS:
        raise ValueError(f"unknown screen list {list_name!r}")
    return f"M:{line_id}:{list_name}"


# --------------------------------------------------------------------------------- timing


def screen_session(now: datetime) -> date | None:
    """The session D the screen may be built for at `now`: the latest US session day whose
    READY_AFTER New York time has passed, if that is today's (New York) session or the latest
    session before it; None when no session is in the calendar lookback."""
    local = now.astimezone(NEW_YORK)
    day = local.date()
    for _ in range(10):
        if session_hours("us", day) is not None:
            ready = datetime(day.year, day.month, day.day, *READY_AFTER, tzinfo=NEW_YORK)
            if now >= ready:
                return day
        day -= timedelta(days=1)
    return None


def ready_at(session: date) -> datetime:
    """When D's screen facts become available (D READY_AFTER New York, as UTC): its completed
    bars and D's filing window are both known then, whenever the cache file is written."""
    return datetime(session.year, session.month, session.day, *READY_AFTER, tzinfo=NEW_YORK).astimezone(UTC)


def is_ready(now: datetime, session: date) -> bool:
    return now >= ready_at(session) and session_hours("us", session) is not None


# --------------------------------------------------------------------------------- universe


def build_universe(tickers: Iterable[str], sectors: Mapping[str, str | None] | None = None) -> list[ScreenName]:
    """Normalised, de-duplicated screen names (line ids), capped at UNIVERSE_MAX; sectors keyed by
    line id. Unusable tickers are skipped."""
    out: dict[str, ScreenName] = {}
    for t in tickers:
        lid = try_normalise_id(t)
        if lid is not None and lid not in out:
            out[lid] = ScreenName(lid, (sectors or {}).get(lid))
        if len(out) >= UNIVERSE_MAX:
            break
    return list(out.values())


# --------------------------------------------------------------------------------- metrics


def day_metric(line_id: str, bars: pd.DataFrame, session: date, sector: str | None = None) -> Metric | None:
    """D's move (% and sigma) and volume ratio from completed bars; None when D's bar is missing
    or the history is too short."""
    if bars is None or bars.empty:
        return None
    d = pd.Timestamp(session).tz_localize("UTC")
    bars = bars[bars.index <= d]          # a later session's bar (after D+1 20:00 NY) never shifts D
    if bars.empty or bars.index[-1] != d:
        return None
    close = series.closes(bars)
    volume = bars["volume"].to_numpy(dtype=float)
    end = len(close) - 1
    sigma = series.sigma_daily(close, end=end - 1)           # prior sessions only
    if sigma is None or close[end - 1] <= 0:
        return None
    move = series.pct(close[end], close[end - 1])
    med = series.volume_median(volume, end=end - 1)
    ratio = float(volume[end] / med) if med and volume[end] > 0 else None
    dv = float(np.median((close * volume)[max(0, end - 19):end + 1]))
    return Metric(line_id, move, series.sigma_move(move, sigma, 1), ratio, dv, sector)


def _row(m: Metric, list_name: str, **extra: Any) -> dict[str, Any]:
    return {"id": fact_id(m.line_id, list_name), "line_id": m.line_id,
            "move_pct": round(m.move_pct, 2), "move_sigma": round(m.move_sigma, 2),
            "vol_ratio": None if m.vol_ratio is None else round(m.vol_ratio, 2), "sector": m.sector, **extra}


def build_lists(metrics: Sequence[Metric], etf_metrics: Mapping[str, Metric],
                catalysts: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """The four lists from D's metrics (pure). `catalysts`: line ids with an admitted 8-K/6-K in
    D's window."""
    movers = sorted((m for m in metrics if abs(m.move_sigma) >= MOVER_MIN_SIGMA),
                    key=lambda m: (-abs(m.move_sigma), m.line_id))[:LIST_MAX]
    volume = sorted((m for m in metrics if m.vol_ratio is not None and m.vol_ratio >= VOLUME_MIN_RATIO),
                    key=lambda m: (-(m.vol_ratio or 0.0), m.line_id))[:LIST_MAX]
    cat = set(catalysts)
    unmoved = sorted((m for m in metrics if m.line_id in cat and abs(m.move_sigma) < UNMOVED_MAX_SIGMA),
                     key=lambda m: (-m.dollar_volume_20d, m.line_id))[:LIST_MAX]
    by_sector: dict[str, list[Metric]] = {}
    for m in metrics:
        if m.sector:
            by_sector.setdefault(m.sector, []).append(m)
    moving: list[tuple[float, str]] = []
    for sector, members in by_sector.items():
        etf = etf_metrics.get(SECTOR_ETF.get(sector, ""))
        strong = [m for m in members if abs(m.move_sigma) >= SECTOR_MOVE_SIGMA]
        if (etf is not None and abs(etf.move_sigma) >= SECTOR_MOVE_SIGMA) or len(strong) >= SECTOR_MIN_MEMBERS:
            strength = abs(etf.move_sigma) if etf is not None else float(np.median([abs(m.move_sigma) for m in strong]))
            moving.append((strength, sector))
    laggard: list[dict[str, Any]] = []
    for _strength, sector in sorted(moving, key=lambda t: (-t[0], t[1])):
        etf = etf_metrics.get(SECTOR_ETF.get(sector, ""))
        lag = sorted((m for m in by_sector[sector] if abs(m.move_sigma) < LAGGARD_MAX_SIGMA),
                     key=lambda m: (-m.dollar_volume_20d, m.line_id))
        for m in lag:
            if len(laggard) >= LIST_MAX:
                break
            laggard.append(_row(m, "laggard", sector_etf=SECTOR_ETF.get(sector),
                                sector_move_sigma=None if etf is None else round(etf.move_sigma, 2)))
    return {"movers": [_row(m, "movers") for m in movers], "volume": [_row(m, "volume") for m in volume],
            "unmoved": [_row(m, "unmoved") for m in unmoved], "laggard": laggard}


def catalysts_in_window(filings: Iterable[tuple[str, datetime]], session: date) -> set[str]:
    """Line ids with a filing accepted after D-1's close and up to D's close."""
    hours = session_hours("us", session)
    if hours is None:
        return set()
    prev = session - timedelta(days=1)
    for _ in range(10):
        if session_hours("us", prev) is not None:
            break
        prev -= timedelta(days=1)
    prev_hours = session_hours("us", prev)
    start = prev_hours[1] if prev_hours else hours[0] - timedelta(hours=18)
    out: set[str] = set()
    for ticker, at in filings:
        lid = try_normalise_id(ticker)
        if lid and start < at.astimezone(UTC) <= hours[1]:
            out.add(lid)
    return out


def context_of(etf_metrics: Mapping[str, Metric]) -> dict[str, dict[str, float]]:
    """{ETF: {move_pct, move_sigma}} of D for the context and sector ETFs (percent and sigma only)."""
    return {e: {"move_pct": round(m.move_pct, 2), "move_sigma": round(m.move_sigma, 2)}
            for e, m in sorted(etf_metrics.items())}


def market_context(context: Mapping[str, Mapping[str, float]], session: str) -> list[tuple[str, str]]:
    """(evidence id, line) market-context rows: the broad ETFs, then the sector ETFs by |sigma|.
    Ids `F:<ETF>:ret1d_sigma` (the market-fact vocabulary); facts only, never an index level."""
    rows: list[tuple[str, str]] = []

    def line(etf: str, label: str) -> None:
        m = context.get(etf)
        if m is None:
            return
        rows.append((f"F:{etf}:ret1d_sigma", f"{etf} ({label}) moved {m['move_pct']:+g}% "
                     f"({m['move_sigma']:+g} sigma of 20 sessions) in the {session} session"))

    for etf, label in CONTEXT_ETFS.items():
        line(etf, label)
    sectors = sorted((e for e in set(SECTOR_ETF.values()) if e in context),
                     key=lambda e: (-abs(context[e]["move_sigma"]), e))
    for etf in sectors:
        line(etf, f"{SECTOR_ETF_LABEL.get(etf, 'sector')} sector ETF")
    return rows


# --------------------------------------------------------------------------------- fetch


class _Stop(Exception):
    def __init__(self, flag: str) -> None:
        super().__init__(flag)
        self.flag = flag


def run_screen(
    universe: Sequence[ScreenName],
    *,
    now: datetime,
    keys: alpaca.AlpacaKeys | None,
    state_dir: Path,
    filings: Iterable[tuple[str, datetime]] = (),
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    budget_now: Callable[[], datetime] | None = None,
) -> Screen:
    """Build D's screen (module rules). Never raises on a data problem: flags say what happened."""
    session = screen_session(now)
    built = now.astimezone(UTC).isoformat()
    if session is None or not is_ready(now, session):
        return Screen(session="", built_at=built, flags=["screen_not_ready"], ready=False)
    screen = Screen(session=session.isoformat(), built_at=built)
    if keys is None:
        screen.flags.append("screen_no_alpaca_keys")
        screen.ready = False
        return screen
    assert alpaca.FEED == "sip", "the screen's volume ratios are SIP over SIP only"
    budget = RequestBudget(PROVIDER, state_dir / f"{PROVIDER}_budget.json", LIMITS,
                           **({"now_fn": budget_now} if budget_now else {}))
    if budget.breaker_open():
        screen.flags.append("screen_breaker_open")
        screen.ready = False
        return screen
    etfs = sorted(set(SECTOR_ETF.values()) | set(CONTEXT_ETFS))
    names = [n.line_id for n in universe]
    tickers = list(dict.fromkeys([*etfs, *names]))
    started = monotonic()
    last_sent: list[float] = []
    start_day = session - timedelta(days=LOOKBACK_DAYS)
    bars: dict[str, pd.DataFrame] = {}

    def reserve_for(chunk: Sequence[str]) -> Callable[[], None]:
        def reserve() -> None:
            if screen.requests >= MAX_REQUESTS:
                raise _Stop("screen_request_cap")
            if monotonic() - started >= DEADLINE_S:
                raise _Stop("screen_time_budget")
            if last_sent:
                wait = PACE_S - (monotonic() - last_sent[-1])
                if wait > 0:
                    sleep(wait)
            if not budget.reserve(chunk):
                raise _Stop("screen_budget")
            screen.requests += 1
            last_sent.append(monotonic())
        return reserve

    for i in range(0, len(tickers), SYMBOLS_PER_REQUEST):
        chunk = [t.replace("_", ".") for t in tickers[i:i + SYMBOLS_PER_REQUEST]]
        try:
            got = alpaca.fetch_daily(chunk, start_day, keys=keys, client=client, now=now,
                                     reserve=reserve_for(chunk), max_symbols=SYMBOLS_PER_REQUEST, retries=1)
        except _Stop as stop:
            screen.flags.append(stop.flag)
            break
        except RateLimited:
            budget.trip()
            screen.flags.append("screen_rate_limited")
            break
        except (DataError, httpx.HTTPError, ValueError) as exc:
            screen.flags.append(f"screen_fetch_error:{type(exc).__name__}")
            continue
        for sym, df in got.items():
            bars[sym.replace(".", "_")] = df
    etf_metrics = {e: m for e in etfs if (m := day_metric(e, bars.get(e), session)) is not None}
    screen.context = context_of(etf_metrics)
    metrics = [m for n in universe if (m := day_metric(n.line_id, bars.get(n.line_id), session, n.sector))]
    screen.names_scored = len(metrics)
    missing = len(universe) - len(metrics)
    if missing:
        screen.flags.append("screen_names_missing")
    screen.lists = build_lists(metrics, etf_metrics, catalysts_in_window(filings, session))
    return screen


# --------------------------------------------------------------------------------- cache


def screen_path(state_dir: Path, session: str) -> Path:
    return state_dir / "swing" / "screen" / f"{session}.json"


def save(screen: Screen, state_dir: Path) -> Path:
    path = screen_path(state_dir, screen.session)
    paths.assert_outside_repo(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(screen.to_json(), sort_keys=True, indent=1) + "\n")
    tmp.replace(path)
    return path


def load(state_dir: Path, session: str) -> Screen | None:
    try:
        raw = json.loads(screen_path(state_dir, session).read_text())
        return Screen(**raw)
    except (OSError, ValueError, TypeError):
        return None

