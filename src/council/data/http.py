"""HTTP plumbing shared by the fetchers: one retry/backoff rule, and errors that never echo URLs.

Provider URLs can carry keys (FRED `api_key=`), so error messages name the request with a short
`what` label instead of the URL, and transport exceptions are reduced to their class name."""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import httpx

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_SLEEP_S = 30.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_S = 1.0
DEFAULT_TIMEOUT_S = 20.0
USER_AGENT = "council-book/0.1"

# Indirection so tests can replace sleeping without patching the global `time` module.
_sleep = time.sleep


class DataError(RuntimeError):
    """A provider failed or returned something unusable. Messages never contain URLs or secrets."""


@contextmanager
def client_scope(client: httpx.Client | None) -> Iterator[httpx.Client]:
    """Yield the caller's client untouched, or a short-lived owned client that is always closed."""
    if client is not None:
        yield client
        return
    owned = httpx.Client(
        timeout=DEFAULT_TIMEOUT_S, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    try:
        yield owned
    finally:
        owned.close()


def retry_after_s(response: httpx.Response) -> float | None:
    """Seconds from a numeric `Retry-After` header; None when absent or not a number."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def backoff_delay(attempt: int, base_s: float, retry_after: float | None) -> float:
    """Rule: honour Retry-After when sent, else base * 2**attempt; never wait more than 30 s."""
    delay = retry_after if retry_after is not None else base_s * (2**attempt)
    return min(delay, MAX_SLEEP_S)


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    what: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    retries: int = DEFAULT_RETRIES,
    backoff_s: float = DEFAULT_BACKOFF_S,
) -> httpx.Response:
    """GET with retries on transport errors and on 408/425/429/5xx. Other 4xx fail at once.

    Rule: at most `retries` extra attempts; a definite client error (e.g. 400, 401, 404) is never
    retried because repeating it cannot succeed and can burn a rate-limit pool."""
    last = "no attempt"
    for attempt in range(retries + 1):
        wait_hint: float | None = None
        try:
            response = client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            last = f"transport {type(exc).__name__}"
        else:
            if response.status_code < 400:
                return response
            if response.status_code not in RETRY_STATUS:
                raise DataError(f"{what}: HTTP {response.status_code}")
            last = f"HTTP {response.status_code}"
            wait_hint = retry_after_s(response)
        if attempt < retries:
            _sleep(backoff_delay(attempt, backoff_s, wait_hint))
    raise DataError(f"{what}: gave up after {retries + 1} attempts ({last})")


def json_body(response: httpx.Response, *, what: str) -> Any:
    """Decode a JSON body or raise DataError (HTML error pages and truncated bodies land here)."""
    try:
        return response.json()
    except ValueError as exc:
        raise DataError(f"{what}: response is not JSON") from exc
