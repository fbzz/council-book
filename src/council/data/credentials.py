"""Data-provider credentials from the macOS Keychain (account `council`), with env overrides for tests.

Rules:
- Only the data services below are readable here. Broker tokens (`council-book.etoro.*`) are never
  read by the data layer; asking for one is a programming error.
- An explicit env-var override wins (tests and CI).
- Then the git-ignored `.env` file (the repository root, or `COUNCIL_ENV_FILE`), outside stub mode
  only, owner-readable only (mode 0600), data credentials only: `dotenv_secret`.
- Stub mode never touches the Keychain or the `.env` file.
- Values are never logged, printed or placed in exception messages.
- The SEC user agent (EDGAR's fair-access rule: a name and a contact address on every request) is a
  required credential: `sec_user_agent()` raises when it is missing or malformed, and its errors
  name the Keychain item and the env override, never the value.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

TIINGO = "council-book.tiingo"
FRED = "council-book.fred"
SEC_USER_AGENT = "council-book.sec-user-agent"
SEC_USER_AGENT_ENV = "COUNCIL_SEC_USER_AGENT"
ALLOWED_SERVICES = frozenset({TIINGO, FRED, SEC_USER_AGENT})
KEYCHAIN_ACCOUNT = "council"
KEYCHAIN_TIMEOUT_S = 10
_SEC_UA_MIN, _SEC_UA_MAX = 8, 200


# The `.env` names of the data credentials (user decision 2026-09-29: data keys may live in a
# git-ignored .env). Broker tokens are never read from it: a line naming eToro is ignored.
DOTENV_NAMES = {
    TIINGO: "COUNCIL_TIINGO_TOKEN",
    FRED: "COUNCIL_FRED_TOKEN",
    SEC_USER_AGENT: SEC_USER_AGENT_ENV,
    "council-book.alpaca-key-id": "COUNCIL_ALPACA_KEY_ID",
    "council-book.alpaca-secret": "COUNCIL_ALPACA_SECRET",
}
ENV_FILE_ENV = "COUNCIL_ENV_FILE"
_REPO_ENV = Path(__file__).resolve().parents[3] / ".env"


def env_file() -> Path:
    """The `.env` path: `COUNCIL_ENV_FILE`, else the repository root's `.env`."""
    override = os.environ.get(ENV_FILE_ENV, "").strip()
    return Path(override).expanduser() if override else _REPO_ENV


def _dotenv_values(path: Path) -> dict[str, str]:
    """The data-credential lines of `path` (KEY=VALUE, optional quotes/`export`); empty when the file
    is missing, unreadable or readable by group/others. Values are never logged."""
    try:
        st = path.stat()
    except OSError:
        return {}
    if not stat.S_ISREG(st.st_mode) or st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return {}
    wanted = set(DOTENV_NAMES.values())
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        name = name.removeprefix("export ").strip()
        if name not in wanted or "ETORO" in name.upper():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:
            out[name] = value
    return out


def dotenv_secret(service: str) -> str | None:
    """`service` from the `.env` file, or None (stub mode, no file, unsafe mode, no line)."""
    name = DOTENV_NAMES.get(service)
    if name is None or os.environ.get("COUNCIL_MODE", "stub") == "stub":
        return None
    return _dotenv_values(env_file()).get(name)


class MissingCredential(RuntimeError):
    """A required data credential is absent or malformed. The message never contains the value."""


def keychain_command(service: str) -> list[str]:
    """The exact `security` invocation (no shell): print only the password of one generic item."""
    return ["security", "find-generic-password", "-s", service, "-a", KEYCHAIN_ACCOUNT, "-w"]


def secret(service: str, env_override: str | None = None) -> str | None:
    """Return the credential for `service`, or None when it is not configured.

    Order: the `env_override` variable (if named and non-empty), then the `.env` file, then the
    Keychain — except in stub mode (COUNCIL_MODE unset or "stub"), which reads neither."""
    if service not in ALLOWED_SERVICES:
        raise ValueError(f"service {service!r} is not a data-provider credential")
    if env_override:
        value = os.environ.get(env_override, "").strip()
        if value:
            return value
    if os.environ.get("COUNCIL_MODE", "stub") == "stub":
        return None
    from_file = dotenv_secret(service)
    if from_file:
        return from_file
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
