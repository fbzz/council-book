"""macOS Keychain access through `/usr/bin/security`. Secrets are never logged, echoed or passed
as process arguments.

Rules:
- READ token and API key live in the login keychain and are read non-interactively.
- The WRITE token lives alone in a SEPARATE keychain file (state_dir/council-write.keychain-db)
  with its own password, auto-locking after 60 s and on sleep. Only the operator role may read it,
  and unlocking it is interactive: the human types the password at the `security` prompt.
- A password or token is never placed on a command line (visible to `ps`); tokens are stored by
  feeding `security -i` on stdin.
- Any `council-book.etoro.*` item is refused in an agent context (CLAUDECODE, any CLAUDE_CODE_*
  variable, or an agent ancestor from `guards.FORBIDDEN_ANCESTORS`) BEFORE `security` runs.
  COUNCIL_AGENT_CONTEXT is deliberately NOT an agent signal here: the launchd runner sets it.
- Inside a marked rehearsal sandbox (state dir with a `REHEARSAL` marker, never the real one), a
  `council-book.etoro.*` item never touches the login keychain: a lookup or store with no explicit
  keychain goes to `COUNCIL_KEYCHAIN_FILE` (the throwaway file), and raises BEFORE `security` runs
  when that variable is unset. An explicit keychain must be that file or the sandbox's own write
  keychain (`<sandbox>/council-write.keychain-db`).
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from council import paths

READ_SERVICE = "council-book.etoro.read"
WRITE_SERVICE = "council-book.etoro.write"
API_KEY_SERVICE = "council-book.etoro.api-key"
BROKER_SERVICE_PREFIX = "council-book.etoro."
AGENT_ENV = ("CLAUDECODE",)
AGENT_ENV_PREFIXES = ("CLAUDE_CODE_",)
DEFAULT_ACCOUNT = "council"
WRITE_KEYCHAIN_NAME = "council-write.keychain-db"
AUTO_LOCK_SECONDS = 60
SECURITY = "/usr/bin/security"
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9._~+/=:-]{8,4096}$")

Runner = Callable[..., subprocess.CompletedProcess[str]]


class KeychainError(RuntimeError):
    """Never carries a secret value in its message."""


def write_keychain_path() -> Path:
    path = paths.state_dir() / WRITE_KEYCHAIN_NAME
    paths.assert_outside_repo(path)
    return path


def _is_write_keychain(keychain: Path | str | None) -> bool:
    if keychain is None:
        return False
    return Path(keychain).expanduser().resolve() == write_keychain_path().resolve()


def _sandbox_keychain(service: str, keychain: Path | str | None,
                      env: Mapping[str, str] | None = None) -> Path | str | None:
    """The keychain a broker item may use inside a marked rehearsal sandbox (unchanged outside
    one, and for non-broker items). Raises before `security` runs if the sandbox would reach the
    login keychain or any keychain other than the throwaway file / the sandbox write keychain."""
    from council.operator.release import is_marked_sandbox

    if not service.startswith(BROKER_SERVICE_PREFIX) or not is_marked_sandbox():
        return keychain
    environ = env if env is not None else os.environ
    throwaway = environ.get("COUNCIL_KEYCHAIN_FILE", "").strip()
    if not throwaway:
        raise KeychainError("rehearsal sandbox without COUNCIL_KEYCHAIN_FILE: broker items are never "
                            "read from or stored in the login keychain under the REHEARSAL marker")
    throwaway_path = Path(throwaway).expanduser()
    if keychain is None:
        return throwaway_path
    chosen = Path(keychain).expanduser().resolve()
    if chosen not in (throwaway_path.resolve(), write_keychain_path().resolve()):
        raise KeychainError("rehearsal sandbox: broker items use only COUNCIL_KEYCHAIN_FILE or the "
                            "sandbox write keychain")
    return keychain


def _process_ancestors() -> list[str]:
    from council.operator.guards import process_ancestors

    return process_ancestors()


def agent_context_signals(
    env: Mapping[str, str], ancestors: Sequence[str]
) -> list[str]:
    """Why this process looks like an agent (empty = not an agent). Pure."""
    from council.operator.guards import FORBIDDEN_ANCESTORS

    found = [f"{name} is set" for name in AGENT_ENV if name in env]
    found += [f"{name} is set" for name in sorted(env) if name.startswith(AGENT_ENV_PREFIXES)]
    for proc in ancestors:
        lowered = str(proc).lower()
        hit = next((word for word in FORBIDDEN_ANCESTORS if word in lowered), None)
        if hit:
            found.append(f"ancestor process matches {hit!r}")
    return found


def assert_not_agent_for_broker(
    service: str,
    *,
    env: Mapping[str, str] | None = None,
    ancestors: Sequence[str] | None = None,
) -> None:
    """Refuse a `council-book.etoro.*` lookup in an agent context. Fails closed: if the ancestors
    cannot be inspected, the lookup is refused."""
    if not service.startswith(BROKER_SERVICE_PREFIX):
        return
    environ = env if env is not None else os.environ
    signals = agent_context_signals(environ, [])
    if not signals:
        if ancestors is None:
            try:
                ancestors = _process_ancestors()
            except Exception as exc:  # GuardError or OSError: fail closed
                raise KeychainError(
                    "broker credentials refused: cannot inspect ancestor processes"
                ) from exc
        signals = agent_context_signals({}, ancestors)
    if signals:
        raise KeychainError(
            "broker credentials are never readable from an agent context ("
            + "; ".join(signals) + ")"
        )


def read_secret(
    service: str,
    account: str = DEFAULT_ACCOUNT,
    keychain: Path | str | None = None,
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
    ancestors: Sequence[str] | None = None,
) -> str:
    """`security find-generic-password -s <service> -a <account> -w [keychain]`.
    The write token (service or keychain) is readable only when COUNCIL_ROLE=operator, and no
    broker item is readable from an agent context (checked before `security` runs)."""
    assert_not_agent_for_broker(service, env=env, ancestors=ancestors)
    keychain = _sandbox_keychain(service, keychain, env)
    role = (env if env is not None else os.environ).get("COUNCIL_ROLE")
    if (service == WRITE_SERVICE or _is_write_keychain(keychain)) and role != "operator":
        raise KeychainError("the write token is readable only from the operator terminal")
    cmd = [SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"]
    if keychain is not None:
        cmd.append(str(keychain))
    result = runner(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise KeychainError(f"no Keychain item for service {service!r} (exit {result.returncode})")
    secret = result.stdout.rstrip("\n")
    if not secret:
        raise KeychainError(f"empty Keychain item for service {service!r}")
    return secret


def _search_list(runner: Runner) -> list[str]:
    result = runner([SECURITY, "list-keychains", "-d", "user"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    return [line.strip().strip('"') for line in result.stdout.splitlines() if line.strip()]


def _remove_from_search_list(path: Path, runner: Runner) -> bool:
    """`create-keychain` adds the new keychain to the user search list; take it out again so
    ordinary lookups never reach it. Leaves the list untouched if it cannot be parsed safely."""
    current = _search_list(runner)
    target = str(path.resolve())
    remaining = [k for k in current if Path(k).resolve().as_posix() != target]
    if not current or len(remaining) == len(current) or not remaining:
        return False
    runner([SECURITY, "list-keychains", "-d", "user", "-s", *remaining], check=True)
    return True


def create_write_keychain(*, runner: Runner = subprocess.run) -> Path:
    """Create the separate write keychain. `security` prompts for the new password on the TTY."""
    path = write_keychain_path()
    if path.exists():
        raise KeychainError("the write keychain already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    runner([SECURITY, "create-keychain", str(path)], check=True)
    runner([SECURITY, "set-keychain-settings", "-l", "-u", "-t", str(AUTO_LOCK_SECONDS), str(path)], check=True)
    _remove_from_search_list(path, runner)
    return path


def unlock_write_keychain(*, runner: Runner = subprocess.run) -> None:
    """Interactive unlock: the operator types the password at the `security` prompt."""
    path = write_keychain_path()
    if not path.exists():
        raise KeychainError("the write keychain does not exist; run the onboarding step first")
    result = runner([SECURITY, "unlock-keychain", str(path)], check=False)
    if result.returncode != 0:
        raise KeychainError("the write keychain was not unlocked")


def lock_write_keychain(*, runner: Runner = subprocess.run) -> None:
    path = write_keychain_path()
    if path.exists():
        runner([SECURITY, "lock-keychain", str(path)], check=False)


def store_token_interactive(
    service: str,
    keychain: Path | str | None,
    *,
    account: str = DEFAULT_ACCOUNT,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    runner: Runner = subprocess.run,
) -> None:
    """Prompt for a token without echo and store it. The value goes to `security -i` on stdin,
    never on a command line, and is never printed or logged."""
    keychain = _sandbox_keychain(service, keychain)
    value = getpass_fn(f"Paste the token for {service} (input hidden): ").strip()
    if not _TOKEN_CHARS.match(value):
        raise KeychainError("token rejected: empty, too short or unexpected characters")
    parts = ["add-generic-password", "-U", "-s", service, "-a", account, "-w", value]
    if keychain is not None:
        parts.append(str(keychain))
    command = " ".join(shlex.quote(p) for p in parts) + "\n"
    result = runner([SECURITY, "-i"], input=command, capture_output=True, text=True, check=False)
    del value, command
    if result.returncode != 0 or "error" in (result.stderr or "").lower():
        raise KeychainError(f"storing the token for {service!r} failed")
