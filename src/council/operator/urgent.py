"""Rate-limited URGENT alerts and fixed broker-failure codes (m5-readiness M5-A).

A broker read failure must never stop a cycle or a watch, and it must never flood the phone: each
URGENT *kind* (e.g. `broker_error:auth`, `keychain_unavailable`, `redact_error`) is sent at most
once per `URGENT_WINDOW`. The last-sent times live in the private ledger runtime table.

Codes are fixed strings (never an exception message, a URL or a header), so they are safe in flags,
logs and the public ops row.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

URGENT_WINDOW = timedelta(hours=4)
RUNTIME_KEY = "urgent_last_sent"
KEYCHAIN_UNAVAILABLE = "keychain_unavailable"


def broker_error_kind(exc: BaseException) -> str:
    """A fixed code for a broker read failure."""
    from council.broker.http import (
        BrokerAuthError,
        BrokerConfigError,
        BrokerHTTPError,
        BrokerUnavailable,
    )

    if isinstance(exc, BrokerAuthError):
        return "auth"
    if isinstance(exc, BrokerConfigError):
        return "config"
    if isinstance(exc, BrokerUnavailable):
        return "unavailable"
    if isinstance(exc, BrokerHTTPError):
        return "http"
    return "unexpected"


def due(ledger: Any, kind: str, now: datetime, *, window: timedelta = URGENT_WINDOW) -> bool:
    """True when no URGENT of `kind` went out inside the window. A ledger failure answers True:
    a missed rate limit is better than a missed alert."""
    try:
        last = (ledger.get_runtime(RUNTIME_KEY, {}) or {}).get(kind)
        return last is None or now - datetime.fromisoformat(str(last)) >= window
    except Exception:  # noqa: BLE001 - never lose an alert to bookkeeping
        return True


def mark(ledger: Any, kind: str, now: datetime) -> None:
    try:
        sent = dict(ledger.get_runtime(RUNTIME_KEY, {}) or {})
        sent[kind] = now.isoformat()
        ledger.set_runtime(RUNTIME_KEY, sent)
    except Exception:  # noqa: BLE001
        pass


def send_once(ctx: Any, kind: str, title: str, body: str, now: datetime) -> bool:
    """Send one URGENT notification of `kind` unless one went out inside the window. Never raises.
    Returns True when a notification was sent."""
    if ctx.notifier is None or not due(ctx.ledger, kind, now):
        return False
    try:
        ctx.notifier.send(title, body, priority="urgent")
    except Exception:  # noqa: BLE001 - an alert failure never stops a cycle or a watch
        return False
    mark(ctx.ledger, kind, now)
    return True
