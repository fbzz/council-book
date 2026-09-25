from __future__ import annotations

import subprocess

import pytest

from council.data import credentials
from council.data.credentials import FRED, SEC_USER_AGENT, TIINGO, keychain_command, secret


class _Recorder:
    def __init__(self, *, returncode=0, stdout="", exc=None):
        self.calls: list[list[str]] = []
        self.returncode, self.stdout, self.exc = returncode, stdout, exc

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        assert kwargs.get("capture_output") is True and "shell" not in kwargs
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr="")


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder(stdout="tok-from-keychain\n")
    monkeypatch.setattr(credentials.subprocess, "run", rec)
    return rec


def test_env_override_wins_without_keychain(monkeypatch, recorder):
    monkeypatch.setenv("TEST_TIINGO_TOKEN", "  env-token  ")
    monkeypatch.setenv("COUNCIL_MODE", "live")
    assert secret(TIINGO, env_override="TEST_TIINGO_TOKEN") == "env-token"
    assert recorder.calls == []


def test_stub_mode_never_touches_keychain(monkeypatch, recorder):
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    assert secret(TIINGO) is None
    monkeypatch.delenv("COUNCIL_MODE")
    assert secret(FRED) is None                         # unset mode defaults to stub
    assert recorder.calls == []


def test_live_mode_reads_keychain_with_exact_command(monkeypatch, recorder):
    monkeypatch.setenv("COUNCIL_MODE", "live")
    monkeypatch.setenv("EMPTY_OVERRIDE", "")
    assert secret(TIINGO, env_override="EMPTY_OVERRIDE") == "tok-from-keychain"
    assert recorder.calls == [
        ["security", "find-generic-password", "-s", "council-book.tiingo", "-a", "council", "-w"]
    ]
    assert keychain_command(SEC_USER_AGENT)[3] == "council-book.sec-user-agent"


@pytest.mark.parametrize(
    "rec",
    [
        _Recorder(returncode=44, stdout=""),                            # item not found
        _Recorder(stdout="   \n"),                                        # empty value
        _Recorder(exc=FileNotFoundError("security")),                     # not macOS
        _Recorder(exc=subprocess.TimeoutExpired(cmd="security", timeout=10)),
    ],
)
def test_missing_or_failed_keychain_returns_none(monkeypatch, rec):
    monkeypatch.setenv("COUNCIL_MODE", "dry_run")
    monkeypatch.setattr(credentials.subprocess, "run", rec)
    assert secret(FRED) is None


@pytest.mark.parametrize(
    "service", ["council-book.etoro.write", "council-book.etoro.read", "login", ""]
)
def test_broker_and_unknown_services_are_refused(service, recorder):
    with pytest.raises(ValueError):
        secret(service)
    assert recorder.calls == []
