"""Operator-context guards. Every approving command calls `assert_operator_context` first.

Rules (all must hold, otherwise GuardError lists every failed rule):
- COUNCIL_ROLE == "operator".
- stdin and stdout are both TTYs.
- None of CI, GITHUB_ACTIONS, CLAUDECODE, COUNCIL_AGENT_CONTEXT or any CLAUDE_CODE_* variable is
  set (present with any value, even empty, counts as set).
- XPC_SERVICE_NAME does not start with "com.fbzz.council" (the launchd jobs).
- No ancestor process name contains claude, codex, hermes, ollama, node or openclaw.
- A typed nonce shown on the confirmation screen (never sent in notifications) must match.
"""

from __future__ import annotations

import hmac
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

FORBIDDEN_ENV = ("CI", "GITHUB_ACTIONS", "CLAUDECODE", "COUNCIL_AGENT_CONTEXT")
FORBIDDEN_ENV_PREFIXES = ("CLAUDE_CODE_",)
LAUNCHD_PREFIX = "com.fbzz.council"
FORBIDDEN_ANCESTORS = ("claude", "codex", "hermes", "ollama", "node", "openclaw")
NONCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I
MAX_ANCESTOR_DEPTH = 64


class GuardError(RuntimeError):
    pass


def operator_context_violations(
    *,
    env: Mapping[str, str],
    stdin_isatty: bool,
    stdout_isatty: bool,
    ancestors: Sequence[str],
) -> list[str]:
    """Every rule the context breaks (empty = allowed). Pure: inputs are passed in."""
    problems: list[str] = []
    if env.get("COUNCIL_ROLE") != "operator":
        problems.append("COUNCIL_ROLE is not 'operator'")
    if not stdin_isatty:
        problems.append("stdin is not a terminal")
    if not stdout_isatty:
        problems.append("stdout is not a terminal")
    for name in FORBIDDEN_ENV:
        if name in env:
            problems.append(f"{name} is set")
    for name in sorted(env):
        if name.startswith(FORBIDDEN_ENV_PREFIXES):
            problems.append(f"{name} is set")
    xpc = env.get("XPC_SERVICE_NAME", "")
    if xpc.startswith(LAUNCHD_PREFIX):
        problems.append("running under a council launchd job")
    for proc in ancestors:
        lowered = proc.lower()
        hit = next((word for word in FORBIDDEN_ANCESTORS if word in lowered), None)
        if hit:
            problems.append(f"an ancestor process looks like an agent runtime ({hit})")
    return problems


def assert_operator_context(
    *,
    env: Mapping[str, str],
    stdin_isatty: bool,
    stdout_isatty: bool,
    ancestors: Sequence[str],
) -> None:
    """Raise GuardError unless this is a human operator's interactive terminal."""
    problems = operator_context_violations(
        env=env, stdin_isatty=stdin_isatty, stdout_isatty=stdout_isatty, ancestors=ancestors,
    )
    if problems:
        raise GuardError("operator command refused: " + "; ".join(problems))


def _parse_ps_line(output: str) -> tuple[int, str] | None:
    line = output.strip()
    if not line:
        return None
    ppid_text, _, comm = line.partition(" ")
    try:
        ppid = int(ppid_text)
    except ValueError:
        return None
    return ppid, comm.strip()


def process_ancestors(
    pid: int | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    max_depth: int = MAX_ANCESTOR_DEPTH,
) -> list[str]:
    """Names (`comm`) of every ancestor of `pid` (default: this process), nearest first, via
    `ps -o ppid=,comm= -p <pid>`. Fails closed: if `ps` cannot be read, GuardError."""
    current = os.getpid() if pid is None else pid
    names: list[str] = []
    seen: set[int] = set()
    for depth in range(max_depth):
        try:
            result = runner(["ps", "-o", "ppid=,comm=", "-p", str(current)],
                            capture_output=True, text=True, check=False)
        except OSError as exc:
            raise GuardError("cannot inspect ancestor processes") from exc
        parsed = _parse_ps_line(result.stdout) if result.returncode == 0 else None
        if parsed is None:
            if depth == 0:
                raise GuardError("cannot inspect ancestor processes")
            break
        ppid, comm = parsed
        if depth > 0:
            names.append(comm)
        seen.add(current)
        if ppid <= 0 or ppid in seen:
            break
        current = ppid
    return names


def assert_current_process_is_operator() -> None:
    """The real-process wrapper used by the operator CLI."""
    assert_operator_context(
        env=os.environ,
        stdin_isatty=sys.stdin.isatty(),
        stdout_isatty=sys.stdout.isatty(),
        ancestors=process_ancestors(),
    )


def new_nonce(length: int = 6) -> str:
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(length))


def typed_nonce_confirm(input_fn: Callable[[str], str], nonce: str) -> bool:
    """Ask the operator to type the nonce shown on screen; True only on an exact match."""
    if not nonce:
        raise ValueError("empty nonce")
    try:
        typed = input_fn(f"Type {nonce} to approve (anything else cancels): ")
    except (EOFError, KeyboardInterrupt):
        return False
    return hmac.compare_digest(typed.strip().encode(), nonce.encode())
