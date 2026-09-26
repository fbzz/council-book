"""Gather each line's signal history and the FRED macro set, through the cache (design §11.1).

Sources (a hard boundary, by asset class):
- Stock lines: the dedicated stock-history source, Alpaca (`council.data.alpaca`), whatever the
  line's policy signal source says. Tiingo is never called for a stock. Without Alpaca keys every
  stock line is omitted (the pack freezes it as no_data: the satellite is held, the core is not).
- Every other line: its policy signal source (Tiingo or Binance). eToro-sourced signals are fetched
  by the broker read client instead (flagged `history_unsupported`).
`history_source(line)` is that routing; `history_sources(policy)` gives the cycle the per-line
source override for `features.market_states`, so a stock's availability rule and label are Alpaca's.

Rules:
- Day key: a line's bars are cached under the trading day of the newest bar that should be
  available at `now` (`expected_day`: end-of-day sources, the last US trading day D with D 20:00
  New York <= now; Binance, yesterday UTC). The six slots of a day therefore fetch each symbol once.
  An answer that does not reach that day yet (a late publication) is used but not stored under the
  day, so the next slot asks again. Cached bars are re-filtered for availability at `now`.
- Request budget (`state_dir/<provider>_budget.json`, Tiingo and Alpaca): requests per hour and per
  day and distinct symbols per calendar month, reserved (and written) before a request is sent. A
  refusal skips the line (`history_budget:<line>`).
- Breaker: the first 429 from Tiingo or Alpaca is never retried and stops every further call to that
  provider for BREAKER_HOLD (persisted in the budget file, so the next run in the hour obeys it too):
  `history_rate_limited:<line>` for the lines that met it, `history_breaker:<line>` for the others.
- Time budget: no request is sent once `time_budget_s` (5 minutes) has passed since the gather
  started, so a slow source stays inside the cycle's 25-minute budget (`history_time_budget:<line>`).
- Fallback: a line that was not fetched (budget, breaker, time) or whose fetch failed or came back
  empty uses its last fetched copy when that copy's newest bar is still fresh under R18's daily-bar
  limit (closed days not counted), flagged `history_cached:<line>`; otherwise the line is omitted
  (no_data). Nothing here raises for a provider problem, and on the stock path nothing raises at
  all: any error there only holds the satellite (`history_failed:<line>`).
- Batching: Alpaca serves up to `alpaca.SYMBOLS_PER_REQUEST` stock lines per request.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from council import clock, paths
from council.data import alpaca, binance, fred, tiingo
from council.data.bars import (
    NEW_YORK,
    TIINGO_AVAILABLE_HOURS,
    available_only,
    bars_from_json,
    bars_to_json,
    last_available_at,
    to_utc,
)
from council.data.cache import FileCache, request_key
from council.data.http import DataError, RateLimited, client_scope
from council.facts.pack import is_stale
from council.policy import LineSpec, Policy

STOCK_HISTORY_SOURCE = alpaca.SOURCE
EOD_HISTORY_SOURCES = frozenset({"tiingo", alpaca.SOURCE})
HISTORY_TTL_S = 4 * 24 * 3600      # day-keyed entries: the key moves on every trading day
LAST_GOOD_TTL_S = 10 * 24 * 3600   # the fallback copy (it must also pass the freshness rule)
MACRO_TTL_S = 6 * 3600
DEFAULT_HISTORY_DAYS = 900         # >= 200-day SMA + one year of sigma estimates, with slack
HISTORY_TIME_BUDGET_S = 300.0      # per gather (design §11.1: 5 minutes of the 25-minute cycle)
HISTORY_RETRIES = 1                # one retry on 5xx/transport; never on 429
BREAKER_HOLD = timedelta(hours=1)


def default_start(now: datetime) -> date:
    return (to_utc(now) - timedelta(days=DEFAULT_HISTORY_DAYS)).date()


def history_source(line: LineSpec) -> str:
    """Where a line's daily history comes from: the stock source for every stock line (never
    Tiingo), else the line's policy signal source."""
    return STOCK_HISTORY_SOURCE if line.asset_class == "stock" else line.signal.source


def history_sources(policy: Policy) -> dict[str, str]:
    """{line: source} for every line whose history does not come from its policy signal source
    (the stock lines), for `features.market_states(sources=...)`."""
    return {ln.symbol: history_source(ln) for ln in policy.universe.lines
            if history_source(ln) != ln.signal.source}


def us_trading_day(day: date) -> bool:
    """A weekday that is not a US exchange holiday (past the calendars: every weekday)."""
    return day.weekday() < 5 and day not in clock.US_HOLIDAYS


def expected_day(source: str, now: datetime) -> date:
    """The trading day of the newest daily bar that should be available at `now` under the source's
    availability rule (the day key): end-of-day sources, the last US trading day D whose bar was
    available by D 20:00 New York; Binance, the last completed UTC day."""
    asof = to_utc(now)
    if source in EOD_HISTORY_SOURCES:
        local = asof.tz_convert(NEW_YORK)
        day = local.date() if local.hour >= TIINGO_AVAILABLE_HOURS else local.date() - timedelta(days=1)
        while not us_trading_day(day):
            day -= timedelta(days=1)
        return day
    return asof.date() - timedelta(days=1)


def _newest_day(bars: pd.DataFrame) -> date | None:
    return None if bars.empty else pd.Timestamp(bars.index[-1]).tz_convert("UTC").date()


def fetch_line_history(
    line: LineSpec, *, now: datetime, tiingo_token: str | None, client: httpx.Client, start: date,
    retries: int = HISTORY_RETRIES,
) -> pd.DataFrame:
    """Raw daily signal bars for one non-stock line (no cache). Raises DataError on provider failure
    and RateLimited on a Tiingo 429. Stock lines are fetched in batches from the stock source by
    `gather_history`: asking for one here raises ValueError, as does any source not fetched here."""
    source, ticker = history_source(line), line.signal.ticker
    if source == "tiingo":
        if not tiingo_token:
            raise DataError("tiingo: no API token configured")
        return tiingo.fetch_daily(ticker, start, token=tiingo_token, client=client, now=now, retries=retries)
    if source == "binance":
        return binance.fetch_klines(ticker, "1d", binance.MAX_LIMIT, client=client, now=now)
    raise ValueError(f"history source {source!r} is not fetched line by line")


# --------------------------------------------------------------------------------- budgets


@dataclass(frozen=True)
class BudgetLimits:
    requests_per_hour: int
    requests_per_day: int
    symbols_per_month: int


# council-book's own ceilings, below the providers' free-plan limits (Tiingo: 50 requests an hour,
# 1,000 a day, 500 symbols a month, on a token other projects share; Alpaca: 200 requests a minute).
BUDGET_LIMITS: dict[str, BudgetLimits] = {
    "tiingo": BudgetLimits(requests_per_hour=40, requests_per_day=400, symbols_per_month=60),
    alpaca.SOURCE: BudgetLimits(requests_per_hour=600, requests_per_day=5_000, symbols_per_month=200),
}


def _wall_utc() -> datetime:
    return datetime.now(UTC)


class RequestBudget:
    """One provider's request budget and 429 breaker, in `state_dir/<provider>_budget.json`.

    Buckets use the wall clock (the providers' quotas do): requests per UTC hour and per UTC day,
    distinct symbols per UTC month. `reserve` checks every limit, then counts the request and writes
    the file BEFORE the caller sends it. An unreadable file starts empty. Writes are atomic; only the
    current buckets are kept."""

    def __init__(self, provider: str, path: Path, limits: BudgetLimits, *,
                 now_fn: Callable[[], datetime] = _wall_utc) -> None:
        self.provider = provider
        self.path = path
        self.limits = limits
        self._now = now_fn
        self.state = self._load()

    @classmethod
    def for_provider(cls, provider: str, state_dir: Path, *,
                     now_fn: Callable[[], datetime] = _wall_utc) -> RequestBudget:
        return cls(provider, state_dir / f"{provider}_budget.json", BUDGET_LIMITS[provider], now_fn=now_fn)

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        def table(key: str) -> dict[str, Any]:
            value = raw.get(key)
            return dict(value) if isinstance(value, dict) else {}

        return {"hours": table("hours"), "days": table("days"),
                "months": {k: list(v) for k, v in table("months").items() if isinstance(v, list)},
                "breaker_until": raw.get("breaker_until")}

    def _keys(self) -> tuple[str, str, str]:
        now = self._now().astimezone(UTC)
        return now.strftime("%Y-%m-%dT%H"), now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    def _save(self) -> None:
        hour, day, month = self._keys()
        self.state = {"hours": {hour: int(self.state["hours"].get(hour, 0))},
                      "days": {day: int(self.state["days"].get(day, 0))},
                      "months": {month: sorted(set(self.state["months"].get(month, [])))},
                      "breaker_until": self.state.get("breaker_until")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        paths.assert_outside_repo(self.path.parent)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.provider}-budget-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps({"provider": self.provider, **self.state}, indent=1, sort_keys=True) + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise

    def breaker_open(self) -> bool:
        raw = self.state.get("breaker_until")
        if not raw:
            return False
        try:
            until = datetime.fromisoformat(str(raw))
        except ValueError:
            return False
        return until.tzinfo is not None and self._now() < until

    def trip(self) -> None:
        """After a 429: no further call to this provider for BREAKER_HOLD."""
        self.state["breaker_until"] = (self._now().astimezone(UTC) + BREAKER_HOLD).isoformat()
        self._save()

    def reserve(self, symbols: Iterable[str]) -> bool:
        """Count one request for `symbols` if every limit allows it (True), else change nothing."""
        hour, day, month = self._keys()
        hours = int(self.state["hours"].get(hour, 0))
        days = int(self.state["days"].get(day, 0))
        seen = set(self.state["months"].get(month, []))
        new = set(symbols) - seen
        lim = self.limits
        if (hours + 1 > lim.requests_per_hour or days + 1 > lim.requests_per_day
                or len(seen) + len(new) > lim.symbols_per_month):
            return False
        self.state["hours"][hour] = hours + 1
        self.state["days"][day] = days + 1
        self.state["months"][month] = sorted(seen | new)
        self._save()
        return True


class _Skip(Exception):
    """A request not sent; `flag` names the reason (history_budget, history_time_budget)."""

    def __init__(self, flag: str) -> None:
        super().__init__(flag)
        self.flag = flag


# --------------------------------------------------------------------------------- history


def _day_request(source: str, ticker: str, begin: date, day: date) -> dict[str, str]:
    return {"source": source, "ticker": ticker, "interval": "1d", "start": begin.isoformat(),
            "day": day.isoformat()}


def _last_request(source: str, ticker: str) -> dict[str, Any]:
    return {"source": source, "ticker": ticker, "interval": "1d", "last_good": True}


@dataclass(frozen=True)
class _Pending:
    line: LineSpec
    source: str
    day: date
    begin: date


class _Gather:
    """One `gather_history` run: its cache, budgets, breakers, deadline, results and flags."""

    def __init__(self, policy: Policy, asof: datetime, store: FileCache, root: Path,
                 budgets: Mapping[str, RequestBudget] | None, deadline: float,
                 monotonic: Callable[[], float]) -> None:
        self.asof = asof
        self.store = store
        self.root = root
        self.max_h = float(policy.risk["freshness"]["daily_bar_max_h"])
        self._budgets: dict[str, RequestBudget] = dict(budgets or {})
        self.deadline = deadline
        self.monotonic = monotonic
        self.history: dict[str, pd.DataFrame] = {}
        self.flags: list[str] = []

    def budget(self, provider: str) -> RequestBudget:
        if provider not in self._budgets:
            self._budgets[provider] = RequestBudget.for_provider(provider, self.root)
        return self._budgets[provider]

    def accept(self, line: LineSpec, source: str, raw: Mapping[str, Any]) -> None:
        bars = available_only(bars_from_json(dict(raw)), source=source, interval="1d", now=self.asof)
        if bars.empty:
            self.flags.append(f"history_empty:{line.symbol}")
        self.history[line.symbol] = bars

    def remember(self, p: _Pending, bars: pd.DataFrame) -> dict[str, Any]:
        """Store fetched bars as the line's fallback copy, and under the day key once they reach the
        expected day (a late answer is used, not stored under the day)."""
        raw = bars_to_json(bars)
        ticker = p.line.signal.ticker
        newest = _newest_day(bars)
        if newest is not None:
            self.store.put(request_key(_last_request(p.source, ticker)), raw, now=self.asof)
            if newest >= p.day:
                self.store.put(request_key(_day_request(p.source, ticker, p.begin, p.day)), raw, now=self.asof)
        return raw

    def fallback(self, p: _Pending, flag: str) -> None:
        """Record why the line was not fetched; use its last copy only while it is still fresh."""
        self.flags.append(flag)
        raw = self.store.get(request_key(_last_request(p.source, p.line.signal.ticker)), LAST_GOOD_TTL_S,
                             now=self.asof)
        if not isinstance(raw, dict):
            return
        bars = available_only(bars_from_json(raw), source=p.source, interval="1d", now=self.asof)
        at = last_available_at(bars, source=p.source, interval="1d")
        if at is None or is_stale(at.to_pydatetime(), self.asof, asset_class=p.line.asset_class, max_h=self.max_h):
            return
        self.history[p.line.symbol] = bars
        self.flags.append(f"history_cached:{p.line.symbol}")

    def guard(self, provider: str, tickers: Sequence[str]) -> None:
        """Raise _Skip unless a request may be sent now: the time budget, then the request budget
        (which counts the request when it allows it)."""
        if self.monotonic() >= self.deadline:
            raise _Skip("history_time_budget")
        if provider in BUDGET_LIMITS and not self.budget(provider).reserve(tickers):
            raise _Skip("history_budget")

    def breaker_open(self, provider: str) -> bool:
        return provider in BUDGET_LIMITS and self.budget(provider).breaker_open()

    def trip(self, provider: str) -> None:
        """After a 429 from a budgeted provider: its breaker opens for BREAKER_HOLD."""
        if provider in BUDGET_LIMITS:
            self.budget(provider).trip()

    def fetch_one(self, p: _Pending, *, tiingo_token: str | None, http: httpx.Client) -> None:
        sym = p.line.symbol
        if self.breaker_open(p.source):
            self.fallback(p, f"history_breaker:{sym}")
            return
        try:
            self.guard(p.source, [p.line.signal.ticker])
            bars = fetch_line_history(p.line, now=self.asof, tiingo_token=tiingo_token, client=http,
                                      start=p.begin)
        except _Skip as skip:
            self.fallback(p, f"{skip.flag}:{sym}")
            return
        except RateLimited:
            self.trip(p.source)
            self.fallback(p, f"history_rate_limited:{sym}")
            return
        except DataError:
            self.fallback(p, f"history_failed:{sym}")
            return
        if bars.empty:              # an empty answer never discards a still-fresh copy
            self.fallback(p, f"history_empty:{sym}")
            return
        self.accept(p.line, p.source, self.remember(p, bars))

    def fetch_stocks(self, pending: Sequence[_Pending], *, keys: alpaca.AlpacaKeys, http: httpx.Client) -> None:
        """Stock lines in batches of alpaca.SYMBOLS_PER_REQUEST (one request each, plus pages)."""
        provider = STOCK_HISTORY_SOURCE
        valid: list[_Pending] = []
        for p in pending:
            try:
                alpaca.alpaca_symbol(p.line.signal.ticker)
            except ValueError:
                self.fallback(p, f"history_failed:{p.line.symbol}")
                continue
            valid.append(p)
        size = alpaca.SYMBOLS_PER_REQUEST
        for i in range(0, len(valid), size):
            chunk = valid[i:i + size]
            if self.breaker_open(provider):
                for p in chunk:
                    self.fallback(p, f"history_breaker:{p.line.symbol}")
                continue
            tickers = [p.line.signal.ticker for p in chunk]
            try:
                result = alpaca.fetch_daily(tickers, min(p.begin for p in chunk), keys=keys, client=http,
                                            now=self.asof, reserve=lambda t=tickers: self.guard(provider, t),
                                            retries=HISTORY_RETRIES)
            except _Skip as skip:
                for p in chunk:
                    self.fallback(p, f"{skip.flag}:{p.line.symbol}")
                continue
            except RateLimited:
                self.trip(provider)
                for p in chunk:
                    self.fallback(p, f"history_rate_limited:{p.line.symbol}")
                continue
            except Exception:       # DataError or anything unexpected: stock data hold the satellite only
                for p in chunk:
                    self.fallback(p, f"history_failed:{p.line.symbol}")
                continue
            for p in chunk:
                bars = result.get(p.line.signal.ticker)
                if bars is None or bars.empty:
                    self.fallback(p, f"history_empty:{p.line.symbol}")
                    continue
                self.accept(p.line, p.source, self.remember(p, bars))


def gather_history(
    policy: Policy,
    *,
    now: datetime,
    tiingo_token: str | None,
    client: httpx.Client | None = None,
    start: date | None = None,
    cache: FileCache | None = None,
    alpaca_keys: alpaca.AlpacaKeys | None = None,
    state_dir: Path | None = None,
    budgets: Mapping[str, RequestBudget] | None = None,
    time_budget_s: float = HISTORY_TIME_BUDGET_S,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """({line: daily bars available at now}, quality flags), lines in policy order. See the module
    docstring for the source routing, the day key, the budgets, the breaker, the time budget and the
    fallback.

    `budgets` overrides the per-provider RequestBudget (default: files under `state_dir`, default
    `paths.state_dir()`); `monotonic` is the time-budget clock (tests)."""
    asof = to_utc(now).to_pydatetime()
    root = state_dir or paths.state_dir()
    run = _Gather(policy, asof, cache or FileCache("history"), root, budgets,
                  monotonic() + float(time_budget_s), monotonic)
    single: list[_Pending] = []
    stocks: list[_Pending] = []
    for line in policy.universe.lines:
        source = history_source(line)
        if source == "etoro":
            run.flags.append(f"history_unsupported:{line.symbol}")
            continue
        if source == "tiingo" and not tiingo_token:
            run.flags.append(f"history_missing:{line.symbol}:no_tiingo_token")
            continue
        if source == STOCK_HISTORY_SOURCE and alpaca_keys is None:
            run.flags.append(f"history_missing:{line.symbol}:no_alpaca_keys")
            continue
        day = expected_day(source, asof)
        begin = start or (day - timedelta(days=DEFAULT_HISTORY_DAYS))
        pending = _Pending(line=line, source=source, day=day, begin=begin)
        cached = run.store.get(request_key(_day_request(source, line.signal.ticker, begin, day)),
                               HISTORY_TTL_S, now=asof)
        if isinstance(cached, dict):
            run.accept(line, source, cached)
        elif source == STOCK_HISTORY_SOURCE:
            stocks.append(pending)
        else:
            single.append(pending)
    if single or stocks:
        with client_scope(client) as http:
            for p in single:
                run.fetch_one(p, tiingo_token=tiingo_token, http=http)
            if stocks and alpaca_keys is not None:
                run.fetch_stocks(stocks, keys=alpaca_keys, http=http)
    ordered = {ln.symbol: run.history[ln.symbol] for ln in policy.universe.lines if ln.symbol in run.history}
    return ordered, run.flags


# --------------------------------------------------------------------------------- macro


def _series_to_json(series: pd.Series) -> dict[str, list[Any]]:
    return {
        "date": [pd.Timestamp(d).date().isoformat() for d in series.index],
        "value": [float(v) for v in series.to_numpy()],
    }


def _series_from_json(obj: dict[str, list[Any]], series_id: str) -> pd.Series:
    index = pd.DatetimeIndex(pd.to_datetime(obj.get("date", [])), name="date").tz_localize("UTC")
    return pd.Series([float(v) for v in obj.get("value", [])], index=index, name=series_id, dtype="float64")


def gather_macro(
    series_ids: Iterable[str] = fred.DEFAULT_MACRO,
    *,
    now: datetime,
    client: httpx.Client | None = None,
    cache: FileCache | None = None,
) -> tuple[dict[str, pd.Series], list[str]]:
    """({series_id: observations}, quality flags). Availability is applied by the pack builder."""
    asof = to_utc(now).to_pydatetime()
    slot_key = clock.slot_at_or_before(asof).isoformat()
    store = cache or FileCache("macro")
    out: dict[str, pd.Series] = {}
    flags: list[str] = []
    with client_scope(client) as http:
        for sid in series_ids:

            def fetch(sid: str = sid) -> dict[str, list[Any]]:
                return _series_to_json(fred.fetch_series(sid, client=http))

            try:
                raw = store.get_or_fetch({"fred": sid, "slot": slot_key}, MACRO_TTL_S, fetch, now=asof)
            except DataError:
                flags.append(f"macro_failed:{sid}")
                continue
            out[sid] = _series_from_json(raw, sid)
    return out, flags
