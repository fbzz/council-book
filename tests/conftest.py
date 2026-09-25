"""Shared fixtures. Area-specific fixtures live in tests/<area>/conftest.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from council.policy import Policy


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every test gets a private state dir and a stub-mode dev environment; lab keys are removed."""
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COUNCIL_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    for name in ("ETORO_USER_KEY", "ETORO_API_KEY", "CLAUDECODE"):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(scope="session")
def policy() -> Policy:
    return Policy.load()


@pytest.fixture
def slot() -> datetime:
    return datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
