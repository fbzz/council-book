"""M5-E1 CLI wiring: `keys store <name>` (allow-list, no echo, stdin only), `notify test` (exactly one
message), the doctor power check, and the topic/URL canary (never in output, logs or errors)."""

from __future__ import annotations

import functools

import pytest
from typer.testing import CliRunner

from council import cli
from council.operator import keystore
from council.operator import notify as notify_mod
from council.operator.keychain import KeychainError
from tests.cli.operator_sim import simulate_operator

runner = CliRunner()
TOPIC = "canarytopic-9c1e5b7a33d0"
URL = "https://hc-ping.example/canary-url-4b2d"


class FakeSecurity:
    def __init__(self, rc=0, stderr=""):
        self.calls: list[tuple[list[str], str]] = []
        self.rc, self.stderr = rc, stderr

    def __call__(self, cmd, input=None, **kwargs):
        import subprocess

        self.calls.append((list(cmd), input or ""))
        return subprocess.CompletedProcess(cmd, self.rc, "", self.stderr)


@pytest.fixture
def operator(monkeypatch):
    simulate_operator(monkeypatch)
    monkeypatch.delenv("COUNCIL_STATE_DIR", raising=False)   # _key_store_target refuses a leftover


def patch_store(monkeypatch, value, fake):
    real = keystore.store_item
    monkeypatch.setattr(keystore, "store_item",
                        functools.partial(real, getpass_fn=lambda prompt: value, runner=fake))


def test_keys_store_value_on_stdin_only(operator, monkeypatch):
    fake = FakeSecurity()
    patch_store(monkeypatch, TOPIC, fake)
    result = runner.invoke(cli.app, ["keys", "store", "ntfy"])
    assert result.exit_code == 0, result.output
    assert "stored council-book.ntfy-topic" in result.output and TOPIC not in result.output
    (cmd, stdin), = fake.calls
    assert cmd == ["/usr/bin/security", "-i"] and TOPIC not in " ".join(cmd)
    assert TOPIC in stdin and "council-book.ntfy-topic" in stdin


def test_keys_store_alias_pair(operator, monkeypatch):
    fake = FakeSecurity()
    patch_store(monkeypatch, "AKIA1234567890abcd", fake)
    assert runner.invoke(cli.app, ["keys", "store", "alpaca"]).exit_code == 0
    assert len(fake.calls) == 2


@pytest.mark.parametrize("name", ["gov-user-agent", "soak-probe", "healthcheck-url", "sec-user-agent"])
def test_design_items_are_allow_listed(name):
    assert keystore.resolve(name)


@pytest.mark.parametrize("name", ["etoro", "council-book.etoro.write", "etoro-write", "../x"])
def test_broker_and_unknown_items_refused(operator, monkeypatch, name):
    fake = FakeSecurity()
    patch_store(monkeypatch, "whatever-value-123", fake)
    result = runner.invoke(cli.app, ["keys", "store", name])
    assert result.exit_code != 0 and fake.calls == []


def test_invalid_value_refused_without_echo(operator, monkeypatch):
    fake = FakeSecurity()
    bad = "http://not-https/canary-bad-value"
    patch_store(monkeypatch, bad, fake)
    result = runner.invoke(cli.app, ["keys", "store", "healthcheck"])
    assert result.exit_code != 0 and fake.calls == [] and bad not in result.output


def test_store_failure_message_has_no_value():
    fake = FakeSecurity(rc=1, stderr=f"error {TOPIC}")
    with pytest.raises(KeychainError) as exc:
        keystore.store_item(keystore.ITEMS["ntfy-topic"], None, getpass_fn=lambda p: TOPIC, runner=fake)
    assert TOPIC not in str(exc.value)


def test_keys_store_refused_outside_operator():
    result = runner.invoke(cli.app, ["keys", "store", "tiingo"])
    assert result.exit_code != 0 and "operator" in result.output


def test_notify_test_sends_exactly_one(operator, monkeypatch):
    monkeypatch.setenv("COUNCIL_NTFY_TOPIC", TOPIC)
    sent = []
    monkeypatch.setattr(notify_mod, "default_sender", lambda ch, msg, topic: sent.append((ch, topic)))
    result = runner.invoke(cli.app, ["notify", "test"])
    assert result.exit_code == 0, result.output
    assert sent == [("ntfy", TOPIC)] and TOPIC not in result.output


def test_notify_test_without_topic(operator, monkeypatch):
    monkeypatch.delenv("COUNCIL_NTFY_TOPIC", raising=False)
    sent = []
    monkeypatch.setattr(notify_mod, "default_sender", lambda *a: sent.append(a))
    result = runner.invoke(cli.app, ["notify", "test"])
    assert result.exit_code == 1 and sent == []


def test_notify_delivery_error_hides_topic(operator, monkeypatch, caplog):
    monkeypatch.setenv("COUNCIL_NTFY_TOPIC", TOPIC)

    def boom(ch, msg, topic):
        raise ConnectionError(f"https://ntfy.sh/{topic} unreachable")

    monkeypatch.setattr(notify_mod, "default_sender", boom)
    result = runner.invoke(cli.app, ["notify", "test"])
    assert result.exit_code == 1 and "ConnectionError" in result.output
    assert TOPIC not in result.output and TOPIC not in caplog.text
    assert TOPIC not in repr(result.exception)


def test_notify_test_refused_outside_operator(monkeypatch):
    monkeypatch.setenv("COUNCIL_NTFY_TOPIC", TOPIC)
    sent = []
    monkeypatch.setattr(notify_mod, "default_sender", lambda *a: sent.append(a))
    assert runner.invoke(cli.app, ["notify", "test"]).exit_code != 0 and sent == []


def test_canary_never_in_public_tree(monkeypatch):
    """The real topic/URL are never written into the repo: the canaries appear only in tests."""
    from council import paths

    for path in (paths.REPO_ROOT / "src").rglob("*.py"):
        text = path.read_text()
        assert TOPIC not in text and URL not in text


@pytest.mark.parametrize("sleep,attested,good", [(0, False, True), (1, True, True), (1, False, False),
                                                  (None, False, False)])
def test_power_check(monkeypatch, sleep, attested, good):
    from council.operator import readiness

    class P:
        def ac_sleep(self):
            return sleep

        def attested(self, name):
            return attested and name == "power-ok"

    monkeypatch.setattr(readiness.Probes, "default", classmethod(lambda cls: P()))
    ok, detail = cli._power_check()
    assert ok is good and detail
