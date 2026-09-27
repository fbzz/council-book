"""The operator-command guard matrix and release pinning (m5-readiness §9.1, M5-B).

- Every operator command of the CLI (`cli.OPERATOR_COMMANDS`, plus the private views T1 guards
  itself) refuses with exit 2 under CLAUDECODE=1, with no TTY, with an agent ancestor process and
  under a launchd job (XPC_SERVICE_NAME=com.fbzz.*), before its body runs; and passes the guard in
  the simulated operator context.
- Release-pinned commands refuse from the dev tree and print the release command; they pass from a
  release checkout (a real throwaway git repo) and under the REHEARSAL marker.
- `keys store-*` refuse a leftover rehearsal variable outside a marked sandbox; inside one the
  eToro items go only to COUNCIL_KEYCHAIN_FILE.
- `ops assert-operator` exits 0 only in the operator context.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from council import cli
from council.operator import keychain, release
from tests.cli.operator_sim import simulate_operator

DEV_REFUSAL = "operator command: COUNCIL_ROLE must be 'operator'"

# argv for every operator command (keys of cli.OPERATOR_COMMANDS)
MATRIX: dict[str, list[str]] = {
    "inbox": ["inbox"],
    "show": ["show", "d1"],
    "approve": ["approve", "d1"],
    "reject": ["reject", "d1", "--reason", "not now"],
    "ops resolve": ["ops", "resolve", "d1", "--filled"],
    "ops review": ["ops", "review", "d1", "--reason", "checked the broker"],
    "resume-exec": ["resume-exec", "d1"],
    "resume": ["resume", "--reason", "recovered"],
    "keys init-write-keychain": ["keys", "init-write-keychain"],
    "keys store-read": ["keys", "store-read"],
    "keys store-write": ["keys", "store-write"],
    "keys store": ["keys", "store", "tiingo"],
    "notify test": ["notify", "test"],
    "account set-mirror": ["account", "set-mirror", "--ratio", "0.2"],
    "doctor --live-read": ["doctor", "--live-read"],
    "doctor --record-fixtures": ["doctor", "--record-fixtures"],
    "keys verify": ["keys", "verify"],
    "instruments resolve": ["instruments", "resolve", "--dry-run"],
    "account set-mirror --from-broker": ["account", "set-mirror", "--funding-usd", "1000", "--from-broker"],
    "stocks rank": ["stocks", "rank", "--asof", "2026-08-20"],
    "stocks onboard": ["stocks", "onboard"],
    "stocks adopt": ["stocks", "adopt", "1"],
    "stocks status": ["stocks", "status"],
    "stocks prune": ["stocks", "prune"],
    "purge-licensed": ["purge-licensed", "--dry-run"],
    "ops attest": ["ops", "attest", "terms-version"],
    "ops capabilities": ["ops", "capabilities"],
}
# guarded inside their own module (transparency T1): refusal only
SELF_GUARDED: dict[str, list[str]] = {
    "inputs show": ["inputs", "show", "2026-10-01T1440Z"],
    "inputs verify": ["inputs", "verify", "2026-10-01T1440Z"],
}
PINNED = sorted(path for path, pinned in cli.OPERATOR_COMMANDS.items() if pinned)


class Stops:
    """Every body entry point an operator command could reach, stubbed to stop and be counted."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.security: list[tuple[list[str], str | None]] = []

    def stop(self, name: str):
        def fn(*args, **kwargs):
            self.hits.append(name)
            raise RuntimeError(f"stop: {name}")
        return fn


@pytest.fixture
def stops(monkeypatch) -> Stops:
    import council.context as context
    from council.policy import Policy
    from council.stocks import commands

    s = Stops()
    monkeypatch.setattr(context, "build_context", s.stop("build_context"))
    monkeypatch.setattr(context, "read_broker", s.stop("read_broker"))
    monkeypatch.setattr(cli, "_ledger_only", s.stop("ledger"))
    monkeypatch.setattr(Policy, "load", s.stop("policy"))
    monkeypatch.setattr(commands, "live_rank_services", s.stop("rank_services"))
    monkeypatch.setattr(keychain, "create_write_keychain", s.stop("create_write_keychain"))

    def fake_security(cmd, **kwargs):
        s.security.append((list(cmd), kwargs.get("input")))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    defaults = keychain.store_token_interactive.__kwdefaults__
    monkeypatch.setitem(defaults, "runner", fake_security)
    monkeypatch.setitem(defaults, "getpass_fn", lambda prompt: "synthetic" + "-token-" + "0123456789")
    return s


def _run(argv: list[str]):
    return CliRunner().invoke(cli.app, argv)


def test_the_matrix_covers_every_operator_command():
    assert set(MATRIX) == set(cli.OPERATOR_COMMANDS)
    assert {"approve", "resume-exec", "ops review", "resume", "ops resolve", "keys init-write-keychain",
            "keys store-read", "keys store-write", "purge-licensed"} <= set(PINNED)


BAD_CONTEXTS = {
    "claudecode": ({"CLAUDECODE": "1"}, {}, "CLAUDECODE is set"),
    "no_tty": ({}, {"tty": False}, "stdin is not a terminal"),
    "agent_ancestor": ({}, {"ancestors": ["python", "node", "claude", "zsh"]}, "agent runtime"),
    "launchd_council": ({"XPC_SERVICE_NAME": "com.fbzz.council.watch"}, {}, "launchd"),
    "launchd_rehearsal": ({"XPC_SERVICE_NAME": "com.fbzz.council.rehearsal.cycle"}, {}, "launchd"),
    "launchd_other": ({"XPC_SERVICE_NAME": "com.fbzz.soak-probe"}, {}, "launchd"),
}


@pytest.mark.parametrize("context", sorted(BAD_CONTEXTS))
@pytest.mark.parametrize("path", sorted(MATRIX) + sorted(SELF_GUARDED))
def test_every_operator_command_refuses_outside_the_operator_terminal(path, context, stops, monkeypatch):
    env, knobs, needle = BAD_CONTEXTS[context]
    simulate_operator(monkeypatch, **knobs)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    result = _run(MATRIX.get(path) or SELF_GUARDED[path])
    assert result.exit_code == 2, (path, context, result.output, result.exception)
    assert needle in result.output, (path, context, result.output)
    assert stops.hits == [] and stops.security == []          # the body never ran


@pytest.mark.parametrize("path", sorted(MATRIX))
def test_every_operator_command_passes_the_guard_in_the_operator_terminal(path, stops, monkeypatch):
    simulate_operator(monkeypatch)                # COUNCIL_STATE_DIR stays the test's tmp dir
    result = _run(MATRIX[path])
    assert DEV_REFUSAL not in result.output and "release-pinned command refused" not in result.output, (
        path, result.output)


def test_the_dev_role_still_reads_as_the_old_refusal(monkeypatch, stops):
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    result = _run(["ops", "resolve", "d1", "--filled"])
    assert result.exit_code == 2 and "COUNCIL_ROLE must be 'operator'" in result.output


# ------------------------------------------------------------------------------ release pinning
@pytest.mark.parametrize("path", PINNED)
def test_pinned_commands_refuse_from_the_dev_tree_and_print_the_release_command(path, stops, monkeypatch):
    simulate_operator(monkeypatch, release=False)
    for name in cli.SANDBOX_VARIABLES[1:]:
        monkeypatch.delenv(name, raising=False)
    result = _run(MATRIX[path])
    assert result.exit_code == 2, (path, result.output)
    assert "release-pinned command refused" in result.output
    assert f"council-op {path}" in result.output
    assert stops.hits == [] and stops.security == []


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def installed_release(tmp_path, monkeypatch) -> Path:
    """A throwaway release checkout at state_dir/releases/current, recorded as installed, with the
    CLI's REPO_ROOT pointing at it (as when the operator runs the release's binary)."""
    from council import paths

    state = Path(paths.state_dir())
    current = state / "releases" / "current"
    current.mkdir(parents=True)
    _git(current, "init", "-q")
    (current / "README").write_text("release\n")
    _git(current, "add", "README")
    _git(current, "commit", "-q", "-m", "release")
    (state / "releases" / "installed.json").write_text(json.dumps({"commit": _git(current, "rev-parse", "HEAD"),
                                                                   "tag": "council-spec-v0"}))
    monkeypatch.setattr(paths, "REPO_ROOT", current)
    return current


def test_a_pinned_command_passes_from_the_installed_release(installed_release, stops, monkeypatch):
    simulate_operator(monkeypatch, release=False)
    result = _run(MATRIX["approve"])
    assert "release-pinned command refused" not in result.output, result.output
    assert stops.hits == ["build_context"]                      # it reached the body


def test_a_dirty_or_moved_release_is_refused(installed_release, stops, monkeypatch):
    simulate_operator(monkeypatch, release=False)
    (installed_release / "README").write_text("edited\n")
    dirty = _run(MATRIX["approve"])
    assert dirty.exit_code == 2 and "local changes" in dirty.output
    _git(installed_release, "commit", "-q", "-am", "moved")
    moved = _run(MATRIX["approve"])
    assert moved.exit_code == 2 and "differs from the commit recorded at install" in moved.output
    assert stops.hits == []


def test_a_pinned_command_passes_under_the_rehearsal_marker(stops, monkeypatch):
    from council import paths

    simulate_operator(monkeypatch, release=False)
    state = Path(paths.state_dir())
    state.mkdir(parents=True, exist_ok=True)
    (state / release.REHEARSAL_MARKER).write_text("")
    result = _run(MATRIX["resume-exec"])
    assert "release-pinned command refused" not in result.output, result.output
    assert stops.hits == ["build_context"]


def test_the_marker_is_ignored_in_the_real_state_dir(tmp_path, monkeypatch):
    real = tmp_path / "home" / "Library" / "Application Support" / "council-book"
    real.mkdir(parents=True)
    (real / release.REHEARSAL_MARKER).write_text("")
    monkeypatch.setattr(release, "default_state_dir", lambda: real)
    assert release.is_marked_sandbox(real) is False
    assert release.release_problems(state_dir=real) == [
        "no installed release (state_dir/releases/current is missing)"]


# ------------------------------------------------------------------------------ keys store-*
@pytest.mark.parametrize("variable", cli.SANDBOX_VARIABLES)
@pytest.mark.parametrize("command", ["store-read", "store-write", "init-write-keychain"])
def test_keys_refuse_a_leftover_rehearsal_variable(variable, command, stops, monkeypatch):
    simulate_operator(monkeypatch)                # COUNCIL_STATE_DIR is the test's tmp dir (unmarked)
    if variable != "COUNCIL_STATE_DIR":
        monkeypatch.setenv(variable, "/tmp/leftover" if variable != "COUNCIL_ETORO_BASE_URL" else "http://127.0.0.1:9")
    result = _run(["keys", command])
    assert result.exit_code == 2 and "set outside a marked rehearsal sandbox" in result.output
    assert variable in result.output
    assert stops.security == [] and stops.hits == []


def test_keys_under_the_marker_store_only_in_the_rehearsal_keychain_file(stops, monkeypatch, tmp_path):
    from council import paths

    simulate_operator(monkeypatch)
    state = Path(paths.state_dir())
    state.mkdir(parents=True, exist_ok=True)
    (state / release.REHEARSAL_MARKER).write_text("")
    monkeypatch.delenv("COUNCIL_KEYCHAIN_FILE", raising=False)
    refused = _run(["keys", "store-read"])
    assert refused.exit_code == 2 and "without COUNCIL_KEYCHAIN_FILE" in refused.output
    assert stops.security == []
    rehearsal_keychain = tmp_path / "rehearsal.keychain-db"
    monkeypatch.setenv("COUNCIL_KEYCHAIN_FILE", str(rehearsal_keychain))
    for command in ("store-read", "store-write"):
        assert _run(["keys", command]).exit_code == 0
    stored = [shlex.split(stdin or "") for _, stdin in stops.security]
    assert len(stored) == 3 and all(parts[-1] == str(rehearsal_keychain) for parts in stored)


# ------------------------------------------------------------------------------ ops assert-operator
def test_ops_assert_operator(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    refused = _run(["ops", "assert-operator"])
    assert refused.exit_code == 2 and "CLAUDECODE is set" in refused.output
    simulate_operator(monkeypatch)
    ok = _run(["ops", "assert-operator"])
    assert ok.exit_code == 0 and "operator context: ok" in ok.output


def test_agent_safe_commands_stay_open(stops, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "operator command" not in _run(["stocks", "rank", "--no-eligibility", "--asof", "2026-08-20"]).output
    assert "operator command" not in _run(["doctor"]).output


def test_a_release_without_its_install_record_is_refused(installed_release, stops, monkeypatch):
    simulate_operator(monkeypatch, release=False)
    (installed_release.parent / release.RECORD_NAME).unlink()
    result = _run(MATRIX["resume"])
    assert result.exit_code == 2 and "no install record" in result.output
    assert stops.hits == []
