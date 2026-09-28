"""Rehearsal fixtures (m5-readiness §7): a network guard that fails any non-loopback connect, and
the throwaway sandbox (marker, throwaway keychain file on the in-memory `security`, loopback fake
broker, local bare remote)."""

from __future__ import annotations

import socket

import pytest

from council.rehearsal import security as fake_security
from council.rehearsal.onboarding import Sandbox

LOOPBACK = ("127.0.0.1", "::1", "localhost")


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    """Any socket.connect to a non-loopback address fails the test (no eToro, ntfy, Tiingo, Ollama,
    news feeds). Unix sockets pass."""
    real_connect = socket.socket.connect
    attempts: list[object] = []

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in LOOPBACK:
            attempts.append(address)
            raise AssertionError(f"non-loopback connect attempted: {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield attempts
    assert not attempts, attempts


def use_sandbox(box: Sandbox, monkeypatch) -> Sandbox:
    """Point this test at the sandbox: env, the fake `security` for every keychain call."""
    box.activate(monkeypatch.setenv)
    monkeypatch.delenv("COUNCIL_ROLE", raising=False)
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    fake_security.install(box.security, setitem=monkeypatch.setitem)
    return box


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A fresh sandbox with the fake broker running (one test)."""
    box = Sandbox.create(tmp_path / "rehearsal")
    box.start_broker()
    use_sandbox(box, monkeypatch)
    yield box
    box.stop()
