"""`council keys store-read` / `store-write` through the CLI (M5-0, m5-readiness M-1 / G25).

`store-read` used to raise a TypeError: the CLI called `store_token_interactive(service)` without its
positional `keychain` argument, and the existing tests only called the function directly. Here the
real CLI runs with a fake `getpass` and a fake `security` runner (no real Keychain is touched):
- both commands exit 0 in a simulated operator context (COUNCIL_ROLE=operator);
- no token is ever on a command line: every `security` call is exactly `security -i`, and the
  value travels on its stdin;
- the READ items (the app key and the READ token) land in the default (login) keychain, the WRITE
  item only in the separate write keychain;
- outside the operator role both refuse before prompting, and nothing reaches `security`.
"""

from __future__ import annotations

import shlex
import subprocess

import pytest
from typer.testing import CliRunner

from council.cli import app
from council.operator import keychain

# built at runtime so no literal token-shaped string sits in the source (see .gitleaksignore)
TOKENS = {
    keychain.API_KEY_SERVICE: "app_" + "synthetic" + "-key-0001",
    keychain.READ_SERVICE: "read_" + "synthetic" + "-token-0002",
    keychain.WRITE_SERVICE: "write_" + "synthetic" + "-token-0003",
}


class FakeSecurity:
    """Records every `security` invocation (argv and stdin); always succeeds."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs.get("input")))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def stored(self) -> list[list[str]]:
        """The parsed `add-generic-password` command of every call, from stdin."""
        return [shlex.split(stdin or "") for _, stdin in self.calls]


@pytest.fixture
def security(monkeypatch) -> FakeSecurity:
    """Swap the keyword defaults of `store_token_interactive` (bound at definition time, so patching
    `getpass.getpass` or `subprocess.run` would not reach them) for fakes."""
    fake = FakeSecurity()
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        service = next(s for s in TOKENS if s in prompt)
        return TOKENS[service] + "\n"           # a trailing newline from the paste is stripped

    defaults = keychain.store_token_interactive.__kwdefaults__
    monkeypatch.setitem(defaults, "runner", fake)
    monkeypatch.setitem(defaults, "getpass_fn", fake_getpass)
    fake.prompts = prompts  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setenv("COUNCIL_ROLE", "operator")


def _run(*args: str):
    return CliRunner().invoke(app, ["keys", *args])


def test_store_read_exits_0_and_stores_both_read_items_in_the_default_keychain(security, operator):
    result = _run("store-read")
    assert result.exit_code == 0, (result.output, result.exception)
    assert result.output.strip() == "stored"
    assert [argv for argv, _ in security.calls] == [[keychain.SECURITY, "-i"]] * 2     # nothing else on argv
    assert security.stored() == [
        ["add-generic-password", "-U", "-s", keychain.API_KEY_SERVICE, "-a", keychain.DEFAULT_ACCOUNT,
         "-w", TOKENS[keychain.API_KEY_SERVICE]],
        ["add-generic-password", "-U", "-s", keychain.READ_SERVICE, "-a", keychain.DEFAULT_ACCOUNT,
         "-w", TOKENS[keychain.READ_SERVICE]],
    ]                                                   # no keychain path: the login keychain
    write_keychain = str(keychain.write_keychain_path())
    assert all(write_keychain not in (stdin or "") for _, stdin in security.calls)
    assert not any(token in result.output for token in TOKENS.values())


def test_store_write_exits_0_and_stores_only_in_the_write_keychain(security, operator):
    result = _run("store-write")
    assert result.exit_code == 0, (result.output, result.exception)
    assert [argv for argv, _ in security.calls] == [[keychain.SECURITY, "-i"]]
    (stored,) = security.stored()
    assert stored == ["add-generic-password", "-U", "-s", keychain.WRITE_SERVICE, "-a", keychain.DEFAULT_ACCOUNT,
                      "-w", TOKENS[keychain.WRITE_SERVICE], str(keychain.write_keychain_path())]
    assert TOKENS[keychain.WRITE_SERVICE] not in result.output


def test_no_token_ever_reaches_a_command_line(security, operator):
    assert _run("store-read").exit_code == 0 and _run("store-write").exit_code == 0
    argv_text = " ".join(" ".join(argv) for argv, _ in security.calls)
    assert not any(token in argv_text for token in TOKENS.values())
    assert all(stdin and stdin.endswith("\n") for _, stdin in security.calls)


@pytest.mark.parametrize("command", ["store-read", "store-write"])
def test_outside_the_operator_role_the_commands_refuse_before_prompting(security, monkeypatch, command):
    monkeypatch.setenv("COUNCIL_ROLE", "runner")
    result = _run(command)
    assert result.exit_code == 2 and "COUNCIL_ROLE must be 'operator'" in result.output
    assert security.calls == [] and security.prompts == []


def test_a_failed_security_call_is_an_error_without_the_token(monkeypatch, operator):
    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "security: error")

    monkeypatch.setitem(keychain.store_token_interactive.__kwdefaults__, "runner", failing)
    monkeypatch.setitem(keychain.store_token_interactive.__kwdefaults__, "getpass_fn",
                        lambda prompt: TOKENS[keychain.API_KEY_SERVICE])
    result = _run("store-read")
    assert result.exit_code != 0 and isinstance(result.exception, keychain.KeychainError)
    assert TOKENS[keychain.API_KEY_SERVICE] not in str(result.exception)
    assert TOKENS[keychain.API_KEY_SERVICE] not in result.output
