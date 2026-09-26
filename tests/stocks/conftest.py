"""Stock-area fixtures: no network, no Keychain, no sleeping. A fake EDGAR serves canned documents."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

UA = "Council Book Tests tests@example.org"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """HTTP retries back off instantly; the requested delays are recorded."""
    waits: list[float] = []
    monkeypatch.setattr("council.data.http._sleep", waits.append)
    return waits


class FakeClock:
    """Monotonic clock whose sleep advances time (the token bucket's pacing becomes observable)."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@dataclass
class FakeEdgar:
    """Handler for httpx.MockTransport: company_tickers, submissions and companyfacts from dicts.
    `fail` maps a path to a list of status codes returned (in order) before the document."""

    tickers: list[dict[str, Any]] = field(default_factory=list)
    submissions: dict[int, dict[str, Any]] = field(default_factory=dict)
    companyfacts: dict[int, dict[str, Any]] = field(default_factory=dict)
    fail: dict[str, list[int]] = field(default_factory=dict)
    seen: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        queue = self.fail.get(path)
        if queue:
            return httpx.Response(queue.pop(0), text="error")
        if path == "/files/company_tickers.json":
            return httpx.Response(200, json={str(i): t for i, t in enumerate(self.tickers)})
        if path.startswith("/submissions/CIK"):
            cik = int(path.removeprefix("/submissions/CIK").removesuffix(".json"))
            doc = self.submissions.get(cik)
            return httpx.Response(200, json=doc) if doc is not None else httpx.Response(404, text="nope")
        if path.startswith("/api/xbrl/companyfacts/CIK"):
            cik = int(path.removeprefix("/api/xbrl/companyfacts/CIK").removesuffix(".json"))
            doc = self.companyfacts.get(cik)
            return httpx.Response(200, json=doc) if doc is not None else httpx.Response(404, text="nope")
        return httpx.Response(404, text="unknown")

    def paths(self) -> list[str]:
        return [r.url.path for r in self.seen]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def submissions_doc(cik: int, sic: str | None, forms: tuple[str, ...] = ("10-Q", "10-K"), name: str = "Co") -> dict:
    n = len(forms)
    return {
        "cik": str(cik), "name": name, "sic": sic, "sicDescription": "x", "tickers": [], "exchanges": [],
        "entityType": "operating", "description": "dropped by the trim", "addresses": {"mailing": {}},
        "filings": {"recent": {
            "accessionNumber": [f"000000000{i}" for i in range(n)], "filingDate": ["2026-08-01"] * n,
            "reportDate": ["2026-06-30"] * n, "acceptanceDateTime": ["2026-08-01T16:05:00.000Z"] * n,
            "form": list(forms), "items": [""] * n, "primaryDocument": ["x.htm"] * n, "size": [1] * n,
        }, "files": []},
    }


def dumps(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True)
