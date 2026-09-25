"""Runtime settings. Secrets never come from files in the repo or from the lab's env files.

Roles:
  runner   — the unattended launchd cycle/watch. READ token only. Can never import the broker writer.
  operator — the human terminal that approves. Only role allowed to load the WRITE token.
  dev      — tests and local development (fake broker, stub LLM).
Modes:
  live     — real broker reads (and, for the operator, writes).
  dry_run  — full cycle against fixtures or the read token; publishes to site-preview/ only.
  stub     — no network at all (tests).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["runner", "operator", "dev"]
Mode = Literal["live", "dry_run", "stub"]

# Env vars from the old lab account. Their presence means the wrong environment was loaded.
FORBIDDEN_ENV = ("ETORO_USER_KEY", "ETORO_API_KEY")


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    role: Role = "dev"
    mode: Mode = "stub"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "deepseek-v4-flash:cloud"
    agent_portfolio_id: str | None = None
    etoro_base_url: str = "https://public-api.etoro.com"
    tiingo_token_service: str = "council-book.tiingo"
    fred_key_service: str = "council-book.fred"
    sec_user_agent: str | None = None
    ntfy_topic: str | None = None
    healthcheck_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, allow_forbidden: bool = False) -> Settings:
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
        return cls(
            role=role,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            ollama_host=os.environ.get("COUNCIL_OLLAMA_HOST", cls.ollama_host),
            ollama_model=os.environ.get("COUNCIL_MODEL", cls.ollama_model),
            agent_portfolio_id=os.environ.get("COUNCIL_ETORO_AGENT_PORTFOLIO_ID") or None,
            etoro_base_url=os.environ.get("COUNCIL_ETORO_BASE_URL", cls.etoro_base_url),
            sec_user_agent=os.environ.get("COUNCIL_SEC_USER_AGENT") or None,
            ntfy_topic=os.environ.get("COUNCIL_NTFY_TOPIC") or None,
            healthcheck_url=os.environ.get("COUNCIL_HEALTHCHECK_URL") or None,
        )
