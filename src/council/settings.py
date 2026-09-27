"""Runtime settings. Secrets never come from files in the repo or from the lab's env files.

Roles:
  runner   — the unattended launchd cycle/watch. READ token only. Can never import the broker writer.
  operator — the human terminal that approves. Only role allowed to load the WRITE token.
  dev      — tests and local development (fake broker, stub LLM).
Modes:
  live     — real broker reads (and, for the operator, writes).
  dry_run  — full cycle against fixtures or the read token; publishes to site-preview/ only.
  stub     — no network at all (tests).

Private ops configuration (m5-readiness M5-E1): the ntfy topic and the healthcheck ping URL come from
`COUNCIL_NTFY_TOPIC` / `COUNCIL_HEALTHCHECK_URL` when set (env wins, for tests), else from the login
Keychain items `council-book.ntfy-topic` / `council-book.healthcheck-url` (account `council`). The
Keychain is read only outside stub mode (or when a caller asks, `keychain=True`) and never while
`COUNCIL_STATE_DIR` redirects the state dir (tests and rehearsal sandboxes never reach the real
login keychain). A malformed or unreadable item counts as absent. Both values are private: they are
kept out of `repr(Settings)`, and nothing prints them.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["runner", "operator", "dev"]
Mode = Literal["live", "dry_run", "stub"]

# Env vars from the old lab account. Their presence means the wrong environment was loaded.
FORBIDDEN_ENV = ("ETORO_USER_KEY", "ETORO_API_KEY")


NTFY_TOPIC_SERVICE = "council-book.ntfy-topic"
HEALTHCHECK_URL_SERVICE = "council-book.healthcheck-url"
NTFY_TOPIC_ENV = "COUNCIL_NTFY_TOPIC"
HEALTHCHECK_URL_ENV = "COUNCIL_HEALTHCHECK_URL"
KEYCHAIN_TIMEOUT_S = 10
# ntfy topics are [-_A-Za-z0-9]{1,64}; the topic is the only secret, so a short one is refused.
NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")
HEALTHCHECK_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[A-Za-z0-9._~/-]{1,256}$")

SecretReader = Callable[[str], "str | None"]

# httpx logs every request line ("HTTP Request: POST <url>") at INFO. The ntfy URL carries the topic
# and the healthcheck URL is itself the secret, so those loggers stay at WARNING whatever level a
# caller gives the root logger (M5-E1 canary: never in logs).
for _name in ("httpx", "httpcore"):
    if logging.getLogger(_name).level in (logging.NOTSET, logging.DEBUG, logging.INFO):
        logging.getLogger(_name).setLevel(logging.WARNING)
del _name


class SettingsError(RuntimeError):
    pass


def valid_ntfy_topic(value: str | None) -> bool:
    return isinstance(value, str) and bool(NTFY_TOPIC_RE.match(value))


def valid_healthcheck_url(value: str | None) -> bool:
    return isinstance(value, str) and bool(HEALTHCHECK_URL_RE.match(value))


def _timed_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, timeout=KEYCHAIN_TIMEOUT_S, **kwargs)  # type: ignore[call-overload]


def keychain_config_value(service: str) -> str | None:
    """The login-Keychain value of one ops configuration item, or None (absent, unreadable, refused,
    or a redirected state dir). Never raises and never puts the value in a message."""
    if os.environ.get("COUNCIL_STATE_DIR"):
        return None
    from council.operator.keychain import KeychainError, read_secret

    try:
        return read_secret(service, runner=_timed_run).strip() or None
    except (KeychainError, OSError, subprocess.SubprocessError):
        return None


def _config(env_name: str, service: str, valid: Callable[[str | None], bool], *, keychain: bool,
            reader: SecretReader) -> str | None:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value                      # env wins (tests); taken as given
    if not keychain:
        return None
    try:
        found = reader(service)
    except Exception:                     # a reader must never leak or break settings
        return None
    found = found.strip() if isinstance(found, str) else None
    return found if valid(found) else None


@dataclass(frozen=True)
class Settings:
    role: Role = "dev"
    mode: Mode = "stub"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "deepseek-v4.1-flash:cloud"
    agent_portfolio_id: str | None = None
    etoro_base_url: str = "https://public-api.etoro.com"
    tiingo_token_service: str = "council-book.tiingo"
    fred_key_service: str = "council-book.fred"
    sec_user_agent: str | None = None
    ntfy_topic: str | None = field(default=None, repr=False)
    healthcheck_url: str | None = field(default=None, repr=False)
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, allow_forbidden: bool = False, keychain: bool | None = None,
                 secret_reader: SecretReader | None = None) -> Settings:
        """Settings from the environment. `keychain` (default: every mode but stub) lets the ntfy
        topic and the healthcheck URL fall back to their Keychain items; `secret_reader` replaces
        the Keychain read (tests)."""
        if not allow_forbidden:
            present = [name for name in FORBIDDEN_ENV if os.environ.get(name)]
            if present:
                raise SettingsError(
                    "refusing to start: lab eToro credentials in environment "
                    f"({', '.join(present)}); council-book uses its own Keychain entries"
                )
        role = os.environ.get("COUNCIL_ROLE", "dev")
        mode = os.environ.get("COUNCIL_MODE", "stub")
        if role not in ("runner", "operator", "dev"):
            raise SettingsError(f"unknown COUNCIL_ROLE {role!r}")
        if mode not in ("live", "dry_run", "stub"):
            raise SettingsError(f"unknown COUNCIL_MODE {mode!r}")
        use_keychain = mode != "stub" if keychain is None else keychain
        reader = secret_reader if secret_reader is not None else keychain_config_value
        return cls(
            role=role,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            ollama_host=os.environ.get("COUNCIL_OLLAMA_HOST", cls.ollama_host),
            ollama_model=os.environ.get("COUNCIL_MODEL", cls.ollama_model),
            agent_portfolio_id=os.environ.get("COUNCIL_ETORO_AGENT_PORTFOLIO_ID") or None,
            etoro_base_url=os.environ.get("COUNCIL_ETORO_BASE_URL", cls.etoro_base_url),
            sec_user_agent=os.environ.get("COUNCIL_SEC_USER_AGENT") or None,
            ntfy_topic=_config(NTFY_TOPIC_ENV, NTFY_TOPIC_SERVICE, valid_ntfy_topic,
                               keychain=use_keychain, reader=reader),
            healthcheck_url=_config(HEALTHCHECK_URL_ENV, HEALTHCHECK_URL_SERVICE, valid_healthcheck_url,
                                    keychain=use_keychain, reader=reader),
        )
