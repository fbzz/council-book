"""SEC EDGAR client for the quarterly stock rank: ticker to CIK, filer submissions (SIC, forms) and
XBRL companyfacts. Read-only; the data are US public domain.

Rules:
- User agent: `council.data.credentials.sec_user_agent()` (Keychain item
  `council-book.sec-user-agent`, env override `COUNCIL_SEC_USER_AGENT`). A missing one raises before
  any request. The value travels only in the request header: never in a log line, a repr or an
  error message (council.data.http names requests by a label, never by URL or header).
- Fair access: a token bucket paces EVERY attempt, retries included, at no more than 7 requests a
  second with no burst (SEC's published limit is 10). Asking for a faster rate is refused.
- Retries: `council.data.http.get_with_retry` (408/425/429/5xx; Retry-After honoured; 30 s cap).
  A 404 on companyfacts means the filer has no XBRL facts: a normal answer (`None`), not an error.
- Cache: gzip JSON under `state_dir()/cache/sec-*/` with a TTL. `refresh=True` is a forced refresh
  that never reads the entry; the rank refetches companyfacts on every run, so a stale copy can
  never be ranked (the lab's EDGAR cache never expired).
- Documents are trimmed before caching. companyfacts keeps only the taxonomies and concepts that
  the frozen rule module `council.stocks.pit` reads, taken from pit's own constants, so the rule's
  output is unchanged (a test pins it); submissions keep the header and the recent-filings columns
  the rank and the earnings estimate use.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from council.data import credentials
from council.data.cache import FileCache
from council.data.http import (
    DEFAULT_BACKOFF_S,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_S,
    DataError,
    get_with_retry,
    json_body,
)
from council.stocks import pit

SEC_MAX_RPS = 7.0
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

NS_TICKERS = "sec-tickers"
NS_SUBMISSIONS = "sec-submissions"
NS_COMPANYFACTS = "sec-companyfacts"
TTL_TICKERS_S = 24 * 3600
TTL_SUBMISSIONS_S = 20 * 3600
TTL_COMPANYFACTS_S = 20 * 3600

# The recent-filings columns kept from submissions (the rank's foreign-filer check; the 8-K Item 2.02
# history for the earnings estimate).
SUBMISSION_KEYS = ("cik", "entityType", "sic", "sicDescription", "name", "tickers", "exchanges",
                   "category", "fiscalYearEnd", "stateOfIncorporation", "formerNames")
RECENT_KEYS = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "items")
FACT_KEYS = ("start", "end", "val", "accn", "fy", "fp", "form", "filed")
# Stands in for the concepts trimmed away, so a taxonomy that had facts stays present and non-empty,
# exactly as `pit.detect_taxonomy` sees the untrimmed document.
TRIM_PLACEHOLDER = "_council_trimmed"


class SecError(DataError):
    """An SEC document is missing or malformed. Messages never contain the user agent."""


# ------------------------------------------------------------------------------------ pacing


class TokenBucket:
    """Tokens refill at `rate` a second up to `capacity`; `acquire` blocks until a token is free and
    takes it. With capacity 1 (the default: no burst) consecutive acquisitions are at least 1/rate
    seconds apart, so no one-second window [t, t + 1) holds more than `rate` requests."""

    def __init__(self, rate: float, capacity: float = 1.0, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not rate > 0:
            raise ValueError("rate must be positive")
        if capacity < 1:
            raise ValueError("capacity must be at least one token")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Take one token; returns the seconds spent waiting."""
        waited = 0.0
        with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + max(0.0, now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0 - 1e-12:
                    self._tokens = max(0.0, self._tokens - 1.0)
                    return waited
                wait = (1.0 - self._tokens) / self.rate
                self._sleep(wait)
                waited += wait


# ------------------------------------------------------------------------------------ documents


@dataclass(frozen=True)
class TickerRow:
    """One row of SEC `company_tickers.json`: the ticker as SEC spells it (`BRK-B`)."""

    cik: int
    ticker: str
    title: str


def parse_company_tickers(payload: Any) -> list[TickerRow]:
    """`company_tickers.json` ({"0": {"cik_str", "ticker", "title"}, ...}) in file order; rows with
    a missing CIK or ticker are skipped."""
    if isinstance(payload, Mapping):
        items: Iterable[Any] = payload.values()
    elif isinstance(payload, list):
        items = payload
    else:
        raise SecError("sec company_tickers: payload is neither an object nor a list")
    out: list[TickerRow] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        cik, ticker = raw.get("cik_str", raw.get("cik")), raw.get("ticker")
        try:
            cik_i = int(cik)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if cik_i <= 0 or not isinstance(ticker, str) or not ticker.strip():
            continue
        out.append(TickerRow(cik=cik_i, ticker=ticker.strip(), title=str(raw.get("title") or "")))
    if not out:
        raise SecError("sec company_tickers: no usable rows")
    return out


def _pit_concepts() -> dict[str, frozenset[str]]:
    """Every (taxonomy, concept) `council.stocks.pit` reads from companyfacts, from its constants."""
    keep: dict[str, set[str]] = defaultdict(set)
    for table in (*pit.FLOW_CONCEPTS.values(), pit.COST_CONCEPTS, pit.COUNCIL_COST_CONCEPTS, pit.CASH_CONCEPTS):
        for taxonomy, names in table.items():
            keep[taxonomy] |= set(names)
    for taxonomy, combos in pit.DEBT_COMBOS.items():
        for combo in combos:
            keep[taxonomy] |= set(combo)
    for taxonomy, concept in (pit.SHARES_CONCEPT, *pit.SHARES_FALLBACK_CONCEPTS):
        keep[taxonomy].add(concept)
    return {k: frozenset(v) for k, v in keep.items()}


PIT_CONCEPTS: dict[str, frozenset[str]] = _pit_concepts()


def trim_companyfacts(doc: Mapping[str, Any]) -> dict[str, Any]:
    """companyfacts reduced to what the frozen rule reads: the taxonomies and concepts in
    PIT_CONCEPTS, every unit of each concept (in document order), and each fact's eight fields.
    A taxonomy that had facts but none of these concepts keeps a placeholder concept without facts."""
    if not isinstance(doc, Mapping) or not isinstance(doc.get("facts", {}), Mapping):
        raise SecError("sec companyfacts: malformed document")
    facts = doc.get("facts") or {}
    out: dict[str, Any] = {}
    for taxonomy, keep in PIT_CONCEPTS.items():
        if taxonomy not in facts:
            continue
        node = facts[taxonomy] or {}
        kept: dict[str, Any] = {}
        for concept in node:
            if concept not in keep:
                continue
            units = (node[concept] or {}).get("units") or {}
            kept[concept] = {"units": {unit: [{k: f[k] for k in FACT_KEYS if k in f}
                                              for f in rows if isinstance(f, Mapping)]
                                       for unit, rows in units.items()}}
        if not kept and node:
            kept = {TRIM_PLACEHOLDER: {"units": {}}}
        out[taxonomy] = kept
    return {"cik": doc.get("cik"), "entityName": doc.get("entityName"), "facts": out}


def trim_submissions(doc: Mapping[str, Any]) -> dict[str, Any]:
    """submissions reduced to the header fields and the recent-filings columns the stock code uses."""
    if not isinstance(doc, Mapping):
        raise SecError("sec submissions: malformed document")
    out = {k: doc[k] for k in SUBMISSION_KEYS if k in doc}
    recent = ((doc.get("filings") or {}).get("recent") or {})
    out["filings"] = {"recent": {k: list(recent.get(k) or []) for k in RECENT_KEYS}}
    return out


def recent_filings(submissions: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The recent filings as rows ({form, filingDate, acceptanceDateTime, items, ...}), newest first
    as SEC lists them."""
    recent = ((submissions.get("filings") or {}).get("recent") or {})
    forms = list(recent.get("form") or [])
    cols = {k: list(recent.get(k) or []) for k in RECENT_KEYS}
    return [{k: (cols[k][i] if i < len(cols[k]) else None) for k in RECENT_KEYS} for i in range(len(forms))]


# ------------------------------------------------------------------------------------ client


class SecClient:
    """Paced, cached, read-only EDGAR client. Close it (or use it as a context manager)."""

    def __init__(self, user_agent: str | None = None, *, rate: float = SEC_MAX_RPS,
                 transport: httpx.BaseTransport | None = None, cache_root: Path | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 retries: int = DEFAULT_RETRIES, backoff_s: float = DEFAULT_BACKOFF_S) -> None:
        if not 0 < rate <= SEC_MAX_RPS:
            raise ValueError(f"SEC request rate must be in (0, {SEC_MAX_RPS}] per second")
        agent = credentials.sec_user_agent() if user_agent is None else credentials.check_sec_user_agent(user_agent)
        self._bucket = TokenBucket(rate, clock=clock, sleep=sleep)
        self._retries, self._backoff_s = retries, backoff_s
        self._cache_root = cache_root
        self._last_status: int | None = None
        self.requests = 0
        self._http = httpx.Client(
            transport=transport, timeout=DEFAULT_TIMEOUT_S, follow_redirects=True,
            headers={"User-Agent": agent, "Accept-Encoding": "gzip, deflate"},
            event_hooks={"request": [self._pace], "response": [self._record]},
        )

    def __repr__(self) -> str:                      # never the user agent
        return f"SecClient(rate={self._bucket.rate:g}/s, requests={self.requests})"

    def __enter__(self) -> SecClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _pace(self, request: httpx.Request) -> None:
        self._bucket.acquire()
        self.requests += 1

    def _record(self, response: httpx.Response) -> None:
        self._last_status = response.status_code

    def _cache(self, namespace: str) -> FileCache:
        return FileCache(namespace, root=self._cache_root)

    def _get_json(self, url: str, what: str, *, allow_404: bool = False) -> Any:
        self._last_status = None
        try:
            response = get_with_retry(self._http, url, what=what, retries=self._retries, backoff_s=self._backoff_s)
        except DataError:
            if allow_404 and self._last_status == 404:
                return None
            raise
        return json_body(response, what=what)

    def company_tickers(self, *, refresh: bool = False) -> list[TickerRow]:
        """Every ticker SEC maps to a CIK (TTL 24 h)."""
        raw = self._cache(NS_TICKERS).get_or_fetch(
            {"url": TICKERS_URL}, TTL_TICKERS_S, lambda: self._get_json(TICKERS_URL, "sec company_tickers"),
            fmt="json.gz", refresh=refresh)
        return parse_company_tickers(raw)

    def submissions(self, cik: int, *, refresh: bool = False) -> dict[str, Any]:
        """The filer's header (SIC, name, tickers) and recent filings, trimmed (TTL 20 h)."""
        url = SUBMISSIONS_URL.format(cik=_cik(cik))

        def fetch() -> dict[str, Any]:
            doc = self._get_json(url, f"sec submissions {_cik(cik)}")
            return trim_submissions(doc)

        return self._cache(NS_SUBMISSIONS).get_or_fetch({"url": url}, TTL_SUBMISSIONS_S, fetch, fmt="json.gz",
                                                        refresh=refresh)

    def companyfacts(self, cik: int, *, refresh: bool = False) -> dict[str, Any] | None:
        """XBRL companyfacts trimmed to what the rule reads, or None when the filer has none (404)."""
        url = COMPANYFACTS_URL.format(cik=_cik(cik))

        def fetch() -> dict[str, Any]:
            doc = self._get_json(url, f"sec companyfacts {_cik(cik)}", allow_404=True)
            return {"found": False} if doc is None else {"found": True, "doc": trim_companyfacts(doc)}

        entry = self._cache(NS_COMPANYFACTS).get_or_fetch({"url": url}, TTL_COMPANYFACTS_S, fetch, fmt="json.gz",
                                                          refresh=refresh)
        return entry.get("doc") if entry.get("found") else None


def _cik(cik: Any) -> int:
    value = int(cik)
    if not 0 < value < 10**10:
        raise ValueError(f"bad CIK {cik!r}")
    return value
