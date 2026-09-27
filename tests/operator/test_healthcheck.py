"""M5-E1: the dead-man switch ping. Loopback/mock transport only; the URL never leaks."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from council import paths
from council.ledger.db import LEDGER_FILE, Ledger
from council.operator import healthcheck

URL = "https://hc-ping.example/secret-uuid-canary-7f3e"
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def client(status=200, raise_exc=None):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        if raise_exc:
            raise raise_exc
        return httpx.Response(status)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_success_ping():
    c, seen = client()
    r = healthcheck.ping(URL, client=c)
    assert r == healthcheck.PingResult(True, True, False)
    assert len(seen) == 1 and str(seen[0].url) == URL and seen[0].content == b""


def test_fail_ping_sends_fixed_code():
    c, seen = client()
    r = healthcheck.ping(URL, fail_code="urgent_alert", client=c)
    assert r.sent and r.ok and r.failure
    assert str(seen[0].url) == URL + "/fail" and seen[0].content == b"urgent_alert"


def test_free_text_code_is_replaced():
    c, seen = client()
    healthcheck.ping(URL, fail_code="Error at /Users/x: $1,234", client=c)
    assert seen[0].content == b"invalid_code"


def test_no_url_sends_nothing():
    assert healthcheck.ping(None) == healthcheck.PingResult(False, False, False, "no_url")


def test_errors_never_carry_the_url():
    c, _ = client(raise_exc=httpx.ConnectError(f"cannot reach {URL}"))
    r = healthcheck.ping(URL, client=c)
    assert r.sent and not r.ok and r.error == "ConnectError" and URL not in repr(r)
    c, _ = client(status=503)
    assert healthcheck.ping(URL, client=c).error == "http_503"


def test_record_writes_runtime_key():
    root = paths.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(root / LEDGER_FILE)
    healthcheck.record(ledger, healthcheck.PingResult(True, True, False), now=NOW)
    assert ledger.get_runtime(healthcheck.RUNTIME_KEY) == {"at": NOW.isoformat(), "ok": True}
    healthcheck.record(ledger, healthcheck.PingResult(True, True, True), now=NOW)
    assert ledger.get_runtime(healthcheck.RUNTIME_KEY)["ok"] is False
    ledger.set_runtime(healthcheck.RUNTIME_KEY, {"at": "keep", "ok": True}, now=NOW)
    healthcheck.record(ledger, healthcheck.PingResult(False, False, False, "no_url"), now=NOW)
    assert ledger.get_runtime(healthcheck.RUNTIME_KEY)["at"] == "keep"


def test_module_does_not_import_broker():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(healthcheck))
    names = {getattr(n, "module", None) or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(("broker" in n) or ("execution" in n) or ("llm" in n) for n in names)
