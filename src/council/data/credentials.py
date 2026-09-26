"""Data-provider credentials from the macOS Keychain (account `council`), with env overrides for tests.

Rules:
- Only the data services below are readable here. Broker tokens (`council-book.etoro.*`) are never
  read by the data layer; asking for one is a programming error.
- An explicit env-var override wins (tests and CI).
- Stub mode never touches the Keychain.
- Values are never logged, printed or placed in exception messages.
- The SEC user agent (EDGAR's fair-access rule: a name and a contact address on every request) is a
  required credential: `sec_user_agent()` raises when it is missing or malformed, and its errors
  name the Keychain item and the env override, never the value.
"""

from __future__ import annotations

import os
import subprocess

TIINGO = "council-book.tiingo"
FRED = "council-book.fred"
SEC_USER_AGENT = "council-book.sec-user-agent"
SEC_USER_AGENT_ENV = "COUNCIL_SEC_USER_AGENT"
ALLOWED_SERVICES = frozenset({TIINGO, FRED, SEC_USER_AGENT})
KEYCHAIN_ACCOUNT = "council"
KEYCHAIN_TIMEOUT_S = 10
_SEC_UA_MIN, _SEC_UA_MAX = 8, 200


class MissingCredential(RuntimeError):
    """A required data credential is absent or malformed. The message never contains the value."""


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


def check_sec_user_agent(value: str) -> str:
    """`value` when it is one printable ASCII line of 8-200 characters with a space and an `@` (SEC
    refuses anonymous clients); otherwise MissingCredential, whose message never echoes the value."""
    text = value.strip() if isinstance(value, str) else ""
    printable = all(32 <= ord(ch) < 127 for ch in text)
    if not (printable and _SEC_UA_MIN <= len(text) <= _SEC_UA_MAX and " " in text and "@" in text):
        raise MissingCredential(
            f"SEC user agent in {SEC_USER_AGENT} / {SEC_USER_AGENT_ENV} is malformed: it must be one "
            "printable line with a name and a contact e-mail address"
        )
    return text


def sec_user_agent() -> str:
    """The SEC EDGAR user agent ("Name contact@example.org"): `COUNCIL_SEC_USER_AGENT`, else the
    Keychain item `council-book.sec-user-agent` (account `council`; never read in stub mode).

    Raises MissingCredential when it is absent or malformed (`check_sec_user_agent`). The value is
    never logged or echoed."""
    value = secret(SEC_USER_AGENT, env_override=SEC_USER_AGENT_ENV)
    if not value:
        raise MissingCredential(
            f"SEC user agent not configured: add the Keychain item {SEC_USER_AGENT} "
            f"(account {KEYCHAIN_ACCOUNT}) or set {SEC_USER_AGENT_ENV}; stub mode reads only the env var"
        )
    return check_sec_user_agent(value)
