"""A simulated operator context for CliRunner tests of operator commands (m5-readiness §9.1).

CliRunner has no TTY and the suite may itself run under an agent (CLAUDECODE, CLAUDE_CODE_*), so an
operator command refuses there by design. `simulate_operator` builds the context the real guard
accepts, through the REAL rules (`guards.assert_operator_context` on the real environment):
- COUNCIL_ROLE=operator, a Terminal XPC_SERVICE_NAME, every agent/CI variable removed;
- both TTYs reported as terminals and a clean ancestor chain (the knobs below break them);
- the release pin passes (as from the installed release) unless `release=False`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from council.operator import guards

CLEAN_ANCESTORS = ("-zsh", "login", "Terminal", "launchd")
AGENT_VARS = ("CI", "GITHUB_ACTIONS", "CLAUDECODE", "COUNCIL_AGENT_CONTEXT")


def simulate_operator(
    monkeypatch,
    *,
    tty: bool = True,
    ancestors: Sequence[str] = CLEAN_ANCESTORS,
    release: bool = True,
) -> None:
    monkeypatch.setenv("COUNCIL_ROLE", "operator")
    monkeypatch.setenv("XPC_SERVICE_NAME", "application.com.apple.Terminal.test")
    for name in AGENT_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith(guards.FORBIDDEN_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    chain = list(ancestors)
    monkeypatch.setattr(guards, "process_ancestors", lambda *a, **k: list(chain))

    def current() -> None:
        guards.assert_operator_context(env=os.environ, stdin_isatty=tty, stdout_isatty=tty,
                                       ancestors=guards.process_ancestors())

    monkeypatch.setattr(guards, "assert_current_process_is_operator", current)
    if release:
        from council.operator import release as release_mod

        monkeypatch.setattr(release_mod, "assert_release_code", lambda **kwargs: None)
