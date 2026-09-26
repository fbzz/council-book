"""`credentials.sec_user_agent()`: required, env override first, Keychain only outside stub mode, and
the value never appears in an error message."""

from __future__ import annotations

import subprocess

import pytest

from council.data import credentials
from council.data.credentials import MissingCredential, check_sec_user_agent, sec_user_agent

GOOD = "Council Book Operator contact@example.org"


def test_env_override_is_used_and_trimmed(monkeypatch):
    monkeypatch.setenv("COUNCIL_SEC_USER_AGENT", f"  {GOOD}  ")
    assert sec_user_agent() == GOOD


def test_missing_in_stub_mode_raises_without_touching_the_keychain(monkeypatch):
    monkeypatch.delenv("COUNCIL_SEC_USER_AGENT", raising=False)
    calls = []
    monkeypatch.setattr(credentials.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(MissingCredential, match="council-book.sec-user-agent"):
        sec_user_agent()
    assert calls == []


def test_live_mode_reads_the_keychain_item(monkeypatch):
    monkeypatch.delenv("COUNCIL_SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("COUNCIL_MODE", "dry_run")
    seen = []

    def run(args, **kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout=GOOD + "\n", stderr="")

    monkeypatch.setattr(credentials.subprocess, "run", run)
    assert sec_user_agent() == GOOD
    assert seen == [["security", "find-generic-password", "-s", "council-book.sec-user-agent", "-a", "council", "-w"]]


@pytest.mark.parametrize("bad", ["short", "no-at-sign here", "tab\tcontact@example.org", "é contact@example.org",
                                 "line one a@b.org\nline two"])
def test_malformed_values_are_refused_and_never_echoed(monkeypatch, bad):
    monkeypatch.setenv("COUNCIL_SEC_USER_AGENT", bad)
    with pytest.raises(MissingCredential) as err:
        sec_user_agent()
    assert bad.strip() not in str(err.value)
    with pytest.raises(MissingCredential):
        check_sec_user_agent(bad)
