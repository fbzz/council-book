"""HTTP plumbing shared by the eToro clients, plus the broker error taxonomy.

Rules enforced here:
- Auth is ALWAYS the key pair `x-api-key` + `x-user-key`. The Bearer mode is never used: sending
  both modes is a 422 at eToro, and a single mode keeps the header audit trivial.
- `x-request-id` is ALWAYS present and is a UUID: uuid4 unless the caller supplies one (writes pass
  a deterministic uuid5 that doubles as the idempotency key / `referenceId`).
- READS retry: 429 honours `Retry-After` (capped), 5xx and transport errors back off. After the
  retries a read raises `BrokerUnavailable`; 401/403 raise `BrokerAuthError` at once.
- WRITES never retry: `send_once` makes exactly one request and lets the write client classify it.
- Credentials never appear in `repr`, error messages or exception bodies.
"""

from __future__ import annotations

import email.utils
import json
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://public-api.etoro.com"
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
READ_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0)   # waits between read attempts on 5xx/transport
MAX_429_RETRIES = 3
RETRY_AFTER_DEFAULT_S = 5.0
RETRY_AFTER_MAX_S = 60.0
BODY_EXCERPT_CHARS = 300

Sleep = Callable[[float], None]


class BrokerError(RuntimeError):
    """Base class for every broker failure."""


class BrokerAuthError(BrokerError):
    """401/403: the token is wrong, expired or lacks the scope. Never retried."""

    def __init__(self, status: int) -> None:
        super().__init__(f"broker auth failed (HTTP {status})")
        self.status = status


class BrokerUnavailable(BrokerError):
    """5xx, 429 or transport failure that outlived the read retries."""


class BrokerHTTPError(BrokerError):
    """Any other non-2xx read response."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"broker HTTP {status}")
        self.status = status
        self.body = body


class DefiniteRejection(BrokerError):
    """A write the broker definitely did NOT accept (4xx, including a clean 401/403 and 429).
    It is safe to reason that nothing was placed; a 429 may be retried with a NEW attempt id."""

    def __init__(self, status: int, body: str = "", retry_after: float | None = None) -> None:
        super().__init__(f"broker rejected the write (HTTP {status})")
        self.status = status
        self.body = body
        self.retry_after = retry_after


class AmbiguousWriteError(BrokerError):
    """A write that MAY have reached the broker (timeout, transport error, 5xx). It must never be
    resubmitted blindly: look it up by its request id (opens) or re-read the portfolio (closes)."""

    def __init__(self, message: str, request_id: str, status: int | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.status = status


class MissingStopLoss(ValueError):
    """Raised BEFORE sending an open without a valid stop-loss rate (invariant: SL on every open)."""


def new_request_id() -> str:
    return str(uuid.uuid4())


def check_request_id(request_id: str) -> str:
    """x-request-id must be a UUID (eToro requires it; the executor derives it with uuid5)."""
    try:
        return str(uuid.UUID(str(request_id)))
    except ValueError as exc:
        raise ValueError("x-request-id must be a UUID") from exc


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date), clamped to
    [0, RETRY_AFTER_MAX_S]; missing or unparseable → RETRY_AFTER_DEFAULT_S."""
    if value is None or not str(value).strip():
        return RETRY_AFTER_DEFAULT_S
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return RETRY_AFTER_DEFAULT_S
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    return max(0.0, min(RETRY_AFTER_MAX_S, seconds))


def body_excerpt(response: httpx.Response) -> str:
    try:
        text = response.text
    except Exception:  # pragma: no cover - undecodable body
        return ""
    return text[:BODY_EXCERPT_CHARS]


def decode_json(response: httpx.Response) -> Any:
    """Decode a 2xx body; an empty body is `{}`."""
    if not response.content:
        return {}
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BrokerHTTPError(response.status_code, "non-JSON body") from exc


class BrokerHTTP:
    """A thin synchronous httpx wrapper holding the key pair. One instance per token."""

    def __init__(
        self,
        api_key: str,
        user_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        sleep: Sleep = time.sleep,
        read_backoff: tuple[float, ...] = READ_BACKOFF_S,
        max_429_retries: int = MAX_429_RETRIES,
    ) -> None:
        if not api_key or not user_key:
            raise ValueError("both x-api-key and x-user-key are required (key-pair auth only)")
        self.__api_key = api_key
        self.__user_key = user_key
        self._sleep = sleep
        self._read_backoff = tuple(read_backoff)
        self._max_429 = max_429_retries
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=timeout
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={str(self._client.base_url)!r}, keys=<redacted>)"

    def close(self) -> None:
        self._client.close()

    def headers(self, request_id: str | None = None) -> dict[str, str]:
        """Key-pair auth + x-request-id. Never an Authorization header."""
        return {
            "x-api-key": self.__api_key,
            "x-user-key": self.__user_key,
            "x-request-id": check_request_id(request_id) if request_id else new_request_id(),
            "accept": "application/json",
        }

    def read(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        request_id: str | None = None,
        allow_404: bool = False,
    ) -> Any:
        """A read with retries. Returns decoded JSON, or None for a 404 when `allow_404`."""
        headers = self.headers(request_id)
        failures = 0
        throttles = 0
        while True:
            try:
                response = self._client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.TransportError as exc:
                if failures >= len(self._read_backoff):
                    raise BrokerUnavailable(f"broker unreachable ({type(exc).__name__})") from exc
                self._sleep(self._read_backoff[failures])
                failures += 1
                continue
            status = response.status_code
            if status == 429:
                if throttles >= self._max_429:
                    raise BrokerUnavailable("broker rate limit persisted (HTTP 429)")
                self._sleep(parse_retry_after(response.headers.get("retry-after")))
                throttles += 1
                continue
            if status >= 500:
                if failures >= len(self._read_backoff):
                    raise BrokerUnavailable(f"broker unavailable (HTTP {status})")
                self._sleep(self._read_backoff[failures])
                failures += 1
                continue
            if status in (401, 403):
                raise BrokerAuthError(status)
            if status == 404 and allow_404:
                return None
            if status >= 400:
                raise BrokerHTTPError(status, body_excerpt(response))
            return decode_json(response)

    def send_once(
        self, method: str, path: str, *, json_body: Any, request_id: str
    ) -> httpx.Response:
        """Exactly one request, no retry. Transport errors propagate as httpx exceptions."""
        return self._client.request(
            method, path, json=json_body, headers=self.headers(request_id)
        )
