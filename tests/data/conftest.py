"""Data/facts test fixtures: no network, no Keychain, no sleeping."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Retries back off instantly in tests; the delays requested are recorded."""
    waits: list[float] = []
    monkeypatch.setattr("council.data.http._sleep", waits.append)
    return waits


@pytest.fixture
def mock_client() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]:
    """Build an httpx.Client whose transport is the given handler (records nothing by itself)."""
    clients: list[httpx.Client] = []

    def make(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()
