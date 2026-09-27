"""External dead-man switch (healthchecks.io style, m5-readiness O2 / G5 / G23).

`ping(url)` reports a good run; `ping(url, fail_code=...)` reports a failed one to `<url>/fail` with a
fixed code as the body (a code, never free text: no amount, id, path or exception message). The ping
URL is private configuration (`Settings.healthcheck_url`): it never appears in a result, a message or
an exception, and `ping` never raises. `record` stores the outcome as the ledger runtime key
`ops.healthcheck` = {"at": iso, "ok": bool} for `doctor --ready`. The watch calls these (M5-E2).
Nothing here imports the broker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

RUNTIME_KEY = "ops.healthcheck"
TIMEOUT_S = 10.0
CODE_RE = re.compile(r"^[a-z0-9_.:-]{1,64}$")


@dataclass(frozen=True)
class PingResult:
    sent: bool                # a request went out
    ok: bool                  # it was delivered (2xx)
    failure: bool             # it reported a failed run
    error: str = ""           # a code or an exception type name; never the URL


def ping(url: str | None, *, fail_code: str | None = None, client: Any | None = None,
         timeout: float = TIMEOUT_S) -> PingResult:
    """One ping. `client` is an httpx.Client (tests pass one on a loopback or mock transport)."""
    failure = fail_code is not None
    if fail_code is not None and not CODE_RE.match(fail_code):
        fail_code = "invalid_code"
    if not url:
        return PingResult(False, False, failure, "no_url")
    target = url.rstrip("/") + ("/fail" if failure else "")
    body = (fail_code or "").encode()
    try:
        import httpx

        owned = client is None
        http = client if client is not None else httpx.Client(timeout=timeout)
        try:
            response = http.post(target, content=body, timeout=timeout)
        finally:
            if owned:
                http.close()
        status = int(response.status_code)
    except Exception as exc:              # the URL may sit in the exception: report the type only
        return PingResult(True, False, failure, type(exc).__name__)
    if 200 <= status < 300:
        return PingResult(True, True, failure)
    return PingResult(True, False, failure, f"http_{status}")


def record(ledger: Any, result: PingResult, *, now: datetime) -> None:
    """Store {"at", "ok"} under `ops.healthcheck` when a request went out; `ok` means a delivered
    success ping (a delivered `/fail` records ok = false: the runs are failing)."""
    if result.sent:
        ledger.set_runtime(RUNTIME_KEY, {"at": now.astimezone(UTC).isoformat(),
                                         "ok": result.ok and not result.failure}, now=now)
