"""M5-A boundary hardening: the base-URL pin (V9), the env-blind transport and the agent refusal of
broker Keychain items."""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from council.broker import http as broker_http
from council.broker.etoro_read import EtoroReadClient
from council.broker.http import DEFAULT_BASE_URL, BrokerConfigError, BrokerHTTP, check_base_url
from council.operator import keychain
from council.operator.keychain import KeychainError
from council.operator.release import REHEARSAL_MARKER

SRC = Path(broker_http.__file__).resolve().parent
API, USER = "api-key-SECRET-1", "user-key-SECRET-2"


def _sandbox(tmp_path: Path, port: int = 18765) -> Path:
    root = tmp_path / "sandbox"
    root.mkdir(parents=True)
    (root / REHEARSAL_MARKER).write_text("")
    (root / "fake-broker.port").write_text(f"{port}\n")
    return root


# ------------------------------------------------------------------------------ V9: the pin
def test_default_url_passes(tmp_path):
    assert check_base_url(DEFAULT_BASE_URL + "/", tmp_path) == DEFAULT_BASE_URL


@pytest.mark.parametrize("url", [
    "https://example.invalid", "http://public-api.etoro.com", "https://public-api.etoro.com.evil.io",
    "https://user:pw@public-api.etoro.com", "", "http://127.0.0.1:18765",
])
def test_other_urls_refused_outside_a_sandbox(tmp_path, url):
    with pytest.raises(BrokerConfigError) as err:
        check_base_url(url, tmp_path)
    assert "example.invalid" not in str(err.value)          # never echoes the env value


def test_loopback_only_with_marker_and_pinned_port(tmp_path):
    root = _sandbox(tmp_path)
    assert check_base_url("http://127.0.0.1:18765", root) == "http://127.0.0.1:18765"
    for bad in ("http://127.0.0.1:18766", "http://localhost:18765", "https://127.0.0.1:18765",
                "http://127.0.0.1:18765/redirect", "http://127.0.0.1"):
        with pytest.raises(BrokerConfigError):
            check_base_url(bad, root)
    (root / REHEARSAL_MARKER).unlink()
    with pytest.raises(BrokerConfigError):
        check_base_url("http://127.0.0.1:18765", root)


def test_both_clients_refuse_before_any_socket(tmp_path, monkeypatch):
    opened: list[object] = []
    from council.broker.etoro_write import EtoroWriteClient  # imports under stub mode

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: opened.append(k))
    for cls in (EtoroReadClient, EtoroWriteClient):
        with pytest.raises(BrokerConfigError):
            cls(API, USER, base_url="https://example.invalid")
    assert opened == []


# ------------------------------------------------------------------------------ transport
def _client_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "Client"
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "httpx"]


def test_ast_broker_clients_ignore_env_and_redirects():
    for name in ("http.py", "etoro_read.py", "etoro_write.py"):
        for call in _client_calls(SRC / name):
            kw = {k.arg: k.value for k in call.keywords}
            assert isinstance(kw.get("trust_env"), ast.Constant) and kw["trust_env"].value is False, name
            assert isinstance(kw.get("follow_redirects"), ast.Constant) and kw["follow_redirects"].value is False
            assert "verify" in kw
    assert _client_calls(SRC / "http.py"), "BrokerHTTP must build its httpx.Client"
    for name in ("etoro_read.py", "etoro_write.py"):                  # both go through BrokerHTTP
        assert "BrokerHTTP(" in (SRC / name).read_text()


def test_proxy_env_does_not_change_the_request_path(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    http = BrokerHTTP(API, USER, transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    assert http._client._trust_env is False                               # noqa: SLF001
    assert http._client.follow_redirects is False
    http._client.get("/api/v1/ping")                                      # noqa: SLF001
    assert seen[0].url.host == "public-api.etoro.com" and seen[0].url.path == "/api/v1/ping"
    assert "proxy" not in repr(http) and API not in repr(http) and USER not in repr(http)


def test_redirect_is_not_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.invalid/x"})

    http = BrokerHTTP(API, USER, transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    resp = http._client.get("/api/v1/ping")                               # noqa: SLF001
    assert resp.status_code == 302 and resp.url.host == "public-api.etoro.com"


# ------------------------------------------------------------------------------ agent refusal
class Security:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, cmd, **_kw):
        self.calls.append(cmd)
        raise AssertionError("security must not run")


@pytest.mark.parametrize("env,ancestors", [
    ({"CLAUDECODE": "1"}, []),
    ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, []),
    ({}, ["/usr/local/bin/node /opt/claude/cli.js"]),
    ({}, ["codex exec"]),
])
def test_agent_context_refuses_broker_items_without_calling_security(env, ancestors):
    fake = Security()
    for service in (keychain.READ_SERVICE, keychain.API_KEY_SERVICE, keychain.WRITE_SERVICE):
        with pytest.raises(KeychainError, match="agent context"):
            keychain.read_secret(service, runner=fake, env={**env, "COUNCIL_ROLE": "operator"},
                                 ancestors=ancestors)
    assert fake.calls == []


def test_runner_env_passes_the_agent_check():
    runner_env = {"COUNCIL_AGENT_CONTEXT": "1", "COUNCIL_ROLE": "runner",
                  "XPC_SERVICE_NAME": "com.fbzz.council.cycle"}
    keychain.assert_not_agent_for_broker(keychain.READ_SERVICE, env=runner_env,
                                         ancestors=["/sbin/launchd", "/bin/zsh -c council cycle"])


def test_non_broker_items_are_not_refused():
    keychain.assert_not_agent_for_broker("council-book.ntfy-topic", env={"CLAUDECODE": "1"}, ancestors=[])


def test_unreadable_ancestors_fail_closed(monkeypatch):
    def boom() -> list[str]:
        raise OSError("ps failed")

    monkeypatch.setattr(keychain, "_process_ancestors", boom)
    with pytest.raises(KeychainError, match="ancestor"):
        keychain.assert_not_agent_for_broker(keychain.READ_SERVICE, env={})
