"""Data-provider credentials from the macOS Keychain (account `council`), with env overrides for tests.

Rules:
- Only the data services below are readable here. Broker tokens (`council-book.etoro.*`) are never
  read by the data layer; asking for one is a programming error.
- An explicit env-var override wins (tests and CI).
- Stub mode never touches the Keychain.
- Values are never logged, printed or placed in exception messages.
"""

from __future__ import annotations

import os
import subprocess

TIINGO = "council-book.tiingo"
FRED = "council-book.fred"
SEC_USER_AGENT = "council-book.sec-user-agent"
ALLOWED_SERVICES = frozenset({TIINGO, FRED, SEC_USER_AGENT})
KEYCHAIN_ACCOUNT = "council"
KEYCHAIN_TIMEOUT_S = 10


def keychain_command(service: str) -> list[str]:
    """The exact `security` invocation (no shell): print only the password of one generic item."""
    return ["security", "find-generic-password", "-s", service, "-a", KEYCHAIN_ACCOUNT, "-w"]


def secret(service: str, env_override: str | None = None) -> str | None:
    """Return the credential for `service`, or None when it is not configured.

    Order: the `env_override` variable (if named and non-empty), then the Keychain — except in
    stub mode (COUNCIL_MODE unset or "stub"), which never runs `security`."""
    if service not in ALLOWED_SERVICES:
        raise ValueError(f"service {service!r} is not a data-provider credential")
    if env_override:
        value = os.environ.get(env_override, "").strip()
        if value:
            return value
    if os.environ.get("COUNCIL_MODE", "stub") == "stub":
        return None
    try:
        proc = subprocess.run(
            keychain_command(service),
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None
