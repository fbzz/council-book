from __future__ import annotations

import subprocess

import pytest

from council.operator import guards
from council.operator.guards import (
    GuardError,
    assert_operator_context,
    process_ancestors,
    typed_nonce_confirm,
)

OK_ENV = {"COUNCIL_ROLE": "operator", "XPC_SERVICE_NAME": "application.com.apple.Terminal.1234",
          "TERM": "xterm-256color", "HOME": "/home/operator"}
OK_ANCESTORS = ["-zsh", "login", "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", "/sbin/launchd"]


def _check(env=None, stdin=True, stdout=True, ancestors=None):
    assert_operator_context(env=OK_ENV if env is None else env, stdin_isatty=stdin, stdout_isatty=stdout,
                            ancestors=OK_ANCESTORS if ancestors is None else ancestors)


def test_a_human_terminal_passes():
    _check()


@pytest.mark.parametrize("stdin, stdout", [(False, True), (True, False), (False, False)])
def test_non_tty_is_refused(stdin, stdout):
    with pytest.raises(GuardError, match="terminal"):
        _check(stdin=stdin, stdout=stdout)


@pytest.mark.parametrize("role", [None, "runner", "dev", "OPERATOR"])
def test_role_must_be_operator(role):
    env = {k: v for k, v in OK_ENV.items() if k != "COUNCIL_ROLE"}
    if role is not None:
        env["COUNCIL_ROLE"] = role
    with pytest.raises(GuardError, match="COUNCIL_ROLE"):
        _check(env=env)


@pytest.mark.parametrize("flag", ["CI", "GITHUB_ACTIONS", "CLAUDECODE", "COUNCIL_AGENT_CONTEXT",
                                  "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"])
@pytest.mark.parametrize("value", ["1", "", "false"])
def test_each_agent_or_ci_flag_is_refused_whatever_its_value(flag, value):
    with pytest.raises(GuardError, match=flag):
        _check(env={**OK_ENV, flag: value})


@pytest.mark.parametrize("xpc", ["com.fbzz.council.cycle", "com.fbzz.council.watch", "com.fbzz.council"])
def test_launchd_job_is_refused(xpc):
    with pytest.raises(GuardError, match="launchd"):
        _check(env={**OK_ENV, "XPC_SERVICE_NAME": xpc})


def test_terminal_xpc_names_are_allowed():
    _check(env={**OK_ENV, "XPC_SERVICE_NAME": "0"})
    _check(env={**OK_ENV, "XPC_SERVICE_NAME": "application.com.googlecode.iterm2.1"})


@pytest.mark.parametrize("ancestor", ["claude", "/usr/local/bin/Claude", "codex", "hermes", "ollama",
                                      "/opt/homebrew/bin/node", "openclaw-gateway"])
def test_agent_ancestor_is_refused(ancestor):
    with pytest.raises(GuardError, match="ancestor"):
        _check(ancestors=[*OK_ANCESTORS[:1], ancestor, *OK_ANCESTORS[1:]])


def test_all_violations_are_reported_together():
    problems = guards.operator_context_violations(env={"CI": "1"}, stdin_isatty=False, stdout_isatty=True,
                                                  ancestors=["claude"])
    assert len(problems) == 4


def _fake_ps(table: dict[int, tuple[int, str]]):
    def run(cmd, **kwargs):
        pid = int(cmd[-1])
        if pid not in table:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        ppid, comm = table[pid]
        return subprocess.CompletedProcess(cmd, 0, f"{ppid:>6} {comm}\n", "")
    return run


def test_process_ancestors_walks_to_launchd():
    table = {500: (400, "python3"), 400: (300, "claude"), 300: (200, "-zsh"), 200: (1, "login"),
             1: (0, "/sbin/launchd")}
    names = process_ancestors(500, runner=_fake_ps(table))
    assert names == ["claude", "-zsh", "login", "/sbin/launchd"]
    with pytest.raises(GuardError):
        assert_operator_context(env=OK_ENV, stdin_isatty=True, stdout_isatty=True, ancestors=names)


def test_process_ancestors_fails_closed_when_ps_fails():
    with pytest.raises(GuardError):
        process_ancestors(999, runner=_fake_ps({}))

    def broken(cmd, **kwargs):
        raise OSError("no ps")

    with pytest.raises(GuardError):
        process_ancestors(999, runner=broken)


def test_process_ancestors_stops_on_cycles():
    names = process_ancestors(10, runner=_fake_ps({10: (11, "a"), 11: (10, "b")}))
    assert names == ["b"]


def test_typed_nonce():
    nonce = guards.new_nonce()
    assert len(nonce) == 6 and set(nonce) <= set(guards.NONCE_ALPHABET)
    assert typed_nonce_confirm(lambda prompt: f" {nonce}\n", nonce)
    assert not typed_nonce_confirm(lambda prompt: nonce.lower(), nonce)
    assert not typed_nonce_confirm(lambda prompt: "y", nonce)

    def eof(prompt):
        raise EOFError

    assert not typed_nonce_confirm(eof, nonce)
    with pytest.raises(ValueError):
        typed_nonce_confirm(lambda p: "", "")


def test_prompt_shows_the_nonce():
    seen = []
    typed_nonce_confirm(lambda prompt: seen.append(prompt) or "", "ABC234")
    assert "ABC234" in seen[0]


def test_guard_refuses_under_this_test_runner():
    """pytest is not an interactive operator terminal (role is dev, stdin is captured)."""
    with pytest.raises(GuardError):
        guards.assert_current_process_is_operator()
