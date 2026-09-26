"""Alpaca market data: daily US stock bars, split- and dividend-adjusted. The dedicated history source
for the stock lines (design §11.1; the user's choice for Q5: Alpaca's free plan).

Rules:
- Stock lines only. Tiingo stays the source of the core lines and never serves a stock line; the
  routing is by asset class in `council.facts.market`, whatever a line's policy signal source says.
- Keys: the Keychain items `council-book.alpaca-key-id` and `council-book.alpaca-secret` (account
  `council`, like every data credential). The env overrides `COUNCIL_ALPACA_KEY_ID` /
  `COUNCIL_ALPACA_SECRET` are honoured in stub mode only (tests): a live or dry run reads the
  Keychain and nothing else, so a stray environment variable can never become a live credential.
  Stub mode never runs `security`. Either key missing or
  malformed means no keys: the stock lines get no history (no_data; the satellite is held, the
  core is unaffected). Only these two items are readable here (never a broker token).
- The keys travel only in the `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers: never in a URL,
  a log line, a repr or an error message (council.data.http names requests by a label).
- Request: the multi-symbol bars endpoint, at most SYMBOLS_PER_REQUEST symbols per request (one
  page covers them over the history window; `next_page_token` is followed up to MAX_PAGES),
  `timeframe=1Day`, `adjustment=all`, the SIP feed (every US exchange). `end` is the as-of time
  minus END_LAG: the free plan withholds the latest 15 minutes of SIP data, and only completed days
  are used anyway.
- Index: the trading date D (the New York date of Alpaca's bar timestamp, which is New York
  midnight) at 00:00 UTC, the canonical bar START label, as for Tiingo.
- Availability: the end-of-day rule (`council.data.bars.EOD_SOURCES`): D's bar is usable from
  D 20:00 New York.
- 429: `council.data.http.RateLimited` at once, never retried (the caller's breaker stops further
  calls); transport errors and 5xx get `retries` extra attempts.
- Rights: the free plan is for personal use. The public record may show derived percentages only,
  never a price or a volume (docs/data-rights.md).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd

from council.clock import utcnow
from council.data import credentials
from council.data.bars import NEW_YORK, available_only, bars_from_rows, empty_bars, to_utc
from council.data.http import DEFAULT_RETRIES, DataError, client_scope, get_with_retry, json_body

SOURCE = "alpaca"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
KEY_ID_SERVICE = "council-book.alpaca-key-id"
SECRET_SERVICE = "council-book.alpaca-secret"
KEY_ID_ENV = "COUNCIL_ALPACA_KEY_ID"
SECRET_ENV = "COUNCIL_ALPACA_SECRET"
SERVICES = frozenset({KEY_ID_SERVICE, SECRET_SERVICE})
FEED = "sip"
ADJUSTMENT = "all"
PAGE_LIMIT = 10_000              # bars per page (Alpaca's maximum), across the request's symbols
SYMBOLS_PER_REQUEST = 12         # ~12 x 630 trading days fits one page
MAX_PAGES = 20
END_LAG = timedelta(minutes=20)
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.]{0,14}$")
_KEY = re.compile(r"^[\x21-\x7e]{8,128}$")        # one printable token, no whitespace


# ------------------------------------------------------------------------------------ keys


@dataclass(frozen=True, repr=False)
class AlpacaKeys:
    """The API key id and secret. Never printed: the repr is redacted and neither value appears in
    an error message."""

    key_id: str = field(repr=False)
    secret: str = field(repr=False)

    def __repr__(self) -> str:
        return "AlpacaKeys(<redacted>)"

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret,
                "Accept": "application/json"}


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _keychain(service: str, runner: Runner) -> str | None:
    """One Keychain item (account `council`) through `security`, never in stub mode. The command is
    `credentials.keychain_command`; failures and timeouts are None."""
    if service not in SERVICES:
        raise ValueError(f"service {service!r} is not an Alpaca credential")
    if os.environ.get("COUNCIL_MODE", "stub") == "stub":
        return None
    try:
        proc = runner(credentials.keychain_command(service), capture_output=True, text=True,
                      timeout=credentials.KEYCHAIN_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _secret(service: str, env: str, runner: Runner) -> str | None:
    """Stub mode (tests): the env override only, never `security`. Any other mode: the Keychain
    only; the env override is ignored."""
    if os.environ.get("COUNCIL_MODE", "stub") == "stub":
        return os.environ.get(env, "").strip() or None
    return _keychain(service, runner)


def load_keys(*, runner: Runner = subprocess.run) -> AlpacaKeys | None:
    """The Alpaca keys (stub mode: the env overrides; otherwise the Keychain only), or None when
    either is missing or is not one printable token of 8-128 characters."""
    key_id = _secret(KEY_ID_SERVICE, KEY_ID_ENV, runner)
    secret = _secret(SECRET_SERVICE, SECRET_ENV, runner)
    if not key_id or not secret or not _KEY.match(key_id) or not _KEY.match(secret):
        return None
    return AlpacaKeys(key_id=key_id, secret=secret)


# ------------------------------------------------------------------------------------ symbols


def alpaca_symbol(ticker: str) -> str:
    """The history ticker in Alpaca's spelling: class shares use "." (BRK-B, BRK/B, BRK.B -> BRK.B)."""
    text = str(ticker).strip().upper().replace("-", ".").replace("/", ".")
    if not _SYMBOL.match(text):
        raise ValueError(f"bad alpaca symbol {ticker!r}")
    return text


# ------------------------------------------------------------------------------------ parsing


def _num(row: Mapping[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bar_day(stamp: Any) -> pd.Timestamp:
    """The canonical index label of a daily bar: its trading date at 00:00 UTC. Alpaca stamps a
    daily bar at New York midnight; a UTC-midnight stamp is read as that UTC date. Any other time
    of day is refused (a wrong day would shift every signal by one bar)."""
    ts = pd.Timestamp(stamp)
    if ts.tzinfo is None:
        raise DataError("alpaca: naive bar timestamp")
    local = ts.tz_convert(NEW_YORK)
    if local.hour == 0 and local.minute == 0:
        day = local.date()
    else:
        utc = ts.tz_convert("UTC")
        if utc.hour or utc.minute:
            raise DataError("alpaca: daily bar timestamp is not a day boundary")
        day = utc.date()
    return pd.Timestamp(day).tz_localize("UTC")


def parse_bars(rows: Iterable[Any], *, now: datetime) -> pd.DataFrame:
    """Canonical bars from one symbol's Alpaca bar rows ({t, o, h, l, c, v, ...}), filtered to the
    bars available at `now`. Rows without a positive close are dropped."""
    out = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise DataError("alpaca: malformed bar row")
        close = _num(raw, "c")
        if close is None or close <= 0 or raw.get("t") is None:
            continue
        o = _num(raw, "o") or close
        h = _num(raw, "h") or max(o, close)
        low = _num(raw, "l") or min(o, close)
        out.append((bar_day(raw["t"]), o, h, low, close, _num(raw, "v") or 0.0))
    bars = bars_from_rows(out) if out else empty_bars()
    return available_only(bars, source=SOURCE, interval="1d", now=now)


def parse_page(payload: Any) -> tuple[dict[str, list[Any]], str | None]:
    """({alpaca symbol: bar rows}, next_page_token) from one multi-symbol bars page."""
    if not isinstance(payload, Mapping):
        raise DataError("alpaca: payload is not an object")
    bars = payload.get("bars")
    if bars is None:
        bars = {}
    if not isinstance(bars, Mapping):
        raise DataError("alpaca: bars is not an object")
    rows: dict[str, list[Any]] = {}
    for symbol, items in bars.items():
        if items is None:
            continue
        if not isinstance(items, list):
            raise DataError("alpaca: bar list is not a list")
        rows[str(symbol)] = items
    token = payload.get("next_page_token")
    return rows, (str(token) if token else None)


# ------------------------------------------------------------------------------------ fetch


def _rfc3339(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_daily(
    tickers: Sequence[str],
    start: date | datetime | str,
    *,
    keys: AlpacaKeys,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    reserve: Callable[[], None] | None = None,
    retries: int = DEFAULT_RETRIES,
) -> dict[str, pd.DataFrame]:
    """{ticker: adjusted daily bars from `start`, available at `now`} for up to SYMBOLS_PER_REQUEST
    history tickers (policy spelling, e.g. BRK-B), in one request (plus continuation pages).

    `reserve` is called before every page is requested (the caller's request budget and time
    budget; it raises to stop). A ticker Alpaca returns nothing for maps to empty bars."""
    if keys is None:
        raise DataError("alpaca: no API keys configured")
    symbols: dict[str, str] = {}
    for ticker in tickers:
        symbols.setdefault(alpaca_symbol(ticker), ticker)
    if not symbols:
        return {}
    if len(symbols) > SYMBOLS_PER_REQUEST:
        raise ValueError(f"at most {SYMBOLS_PER_REQUEST} symbols per alpaca request")
    asof = to_utc(now or utcnow())
    start_s = start.isoformat()[:10] if isinstance(start, date | datetime) else str(start)[:10]
    params: dict[str, Any] = {
        "symbols": ",".join(sorted(symbols)), "timeframe": "1Day", "start": start_s,
        "end": _rfc3339(asof - pd.Timedelta(END_LAG)), "adjustment": ADJUSTMENT, "feed": FEED,
        "limit": PAGE_LIMIT, "sort": "asc",
    }
    what = f"alpaca daily bars ({len(symbols)} symbols)"
    rows: dict[str, list[Any]] = {s: [] for s in symbols}
    token: str | None = None
    with client_scope(client) as http:
        for _ in range(MAX_PAGES):
            if reserve is not None:
                reserve()
            page = dict(params)
            if token:
                page["page_token"] = token
            response = get_with_retry(http, BARS_URL, what=what, params=page, headers=keys.headers(),
                                      retries=retries, fail_fast_429=True)
            found, token = parse_page(json_body(response, what=what))
            for symbol, items in found.items():
                if symbol in rows:
                    rows[symbol].extend(items)
            if not token:
                break
        else:
            raise DataError(f"{what}: more than {MAX_PAGES} pages")
    return {symbols[s]: parse_bars(items, now=asof.to_pydatetime()) for s, items in rows.items()}
