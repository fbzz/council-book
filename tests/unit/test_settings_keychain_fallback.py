"""M5-E1: the ntfy topic and the healthcheck URL fall back to their Keychain items; env wins."""

from __future__ import annotations

import subprocess

import pytest

from council import settings as settings_mod
from council.settings import (
    HEALTHCHECK_URL_SERVICE,
    NTFY_TOPIC_SERVICE,
    Settings,
    keychain_config_value,
)

TOPIC = "council-0123456789abcdef"
URL = "https://hc-ping.com/5f1c2a9e-0000-4000-8000-123456789abc"
KEYCHAIN = {NTFY_TOPIC_SERVICE: TOPIC, HEALTHCHECK_URL_SERVICE: URL}


def reader(values):
    calls: list[str] = []

    def read(service: str):
        calls.append(service)
        return values.get(service)

    read.calls = calls  # type: ignore[attr-defined]
    return read


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("COUNCIL_NTFY_TOPIC", raising=False)
    monkeypatch.delenv("COUNCIL_HEALTHCHECK_URL", raising=False)


def test_keychain_fallback_when_env_absent():
    s = Settings.from_env(keychain=True, secret_reader=reader(KEYCHAIN))
    assert s.ntfy_topic == TOPIC and s.healthcheck_url == URL


def test_env_wins_over_keychain(monkeypatch):
    monkeypatch.setenv("COUNCIL_NTFY_TOPIC", "env-topic-for-tests")
    monkeypatch.setenv("COUNCIL_HEALTHCHECK_URL", "http://127.0.0.1:9/ping")
    r = reader(KEYCHAIN)
    s = Settings.from_env(keychain=True, secret_reader=r)
    assert s.ntfy_topic == "env-topic-for-tests"
    assert s.healthcheck_url == "http://127.0.0.1:9/ping"
    assert r.calls == []                                   # the keychain is not even asked


def test_stub_mode_does_not_read_keychain_by_default():
    r = reader(KEYCHAIN)
    s = Settings.from_env(secret_reader=r)                  # conftest: COUNCIL_MODE=stub
    assert s.ntfy_topic is None and s.healthcheck_url is None and r.calls == []


def test_live_mode_reads_keychain_by_default(monkeypatch):
    monkeypatch.setenv("COUNCIL_MODE", "dry_run")
    s = Settings.from_env(secret_reader=reader(KEYCHAIN))
    assert s.ntfy_topic == TOPIC


@pytest.mark.parametrize("bad_topic", ["short", "has space in it!", "x" * 65, ""])
def test_malformed_topic_counts_as_absent(bad_topic):
    s = Settings.from_env(keychain=True, secret_reader=reader({NTFY_TOPIC_SERVICE: bad_topic,
                                                               HEALTHCHECK_URL_SERVICE: "http://x/y"}))
    assert s.ntfy_topic is None and s.healthcheck_url is None


def test_reader_exception_is_absent_and_silent():
    def boom(service):
        raise RuntimeError(f"leaked {TOPIC}")

    s = Settings.from_env(keychain=True, secret_reader=boom)
    assert s.ntfy_topic is None and s.healthcheck_url is None


def test_values_kept_out_of_repr():
    s = Settings.from_env(keychain=True, secret_reader=reader(KEYCHAIN))
    assert TOPIC not in repr(s) and URL not in repr(s) and TOPIC not in str(s)


def test_redirected_state_dir_never_reaches_real_keychain(monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    assert keychain_config_value(NTFY_TOPIC_SERVICE) is None   # conftest sets COUNCIL_STATE_DIR
    assert called == []


def test_keychain_read_without_value_in_errors(monkeypatch):
    monkeypatch.delenv("COUNCIL_STATE_DIR")
    from council.operator import keychain

    seen = {}

    def fake_read(service, runner=None, **kwargs):
        seen["service"] = service
        raise keychain.KeychainError("not found")

    monkeypatch.setattr(keychain, "read_secret", fake_read)
    assert keychain_config_value(HEALTHCHECK_URL_SERVICE) is None
    assert seen["service"] == HEALTHCHECK_URL_SERVICE

    monkeypatch.setattr(keychain, "read_secret", lambda service, runner=None, **k: f" {TOPIC}\n")
    assert keychain_config_value(NTFY_TOPIC_SERVICE) == TOPIC
    assert settings_mod.valid_ntfy_topic(TOPIC)
