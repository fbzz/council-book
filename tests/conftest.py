"""Shared fixtures. Area-specific fixtures live in tests/<area>/conftest.py.

Policy pins: `policy` is the CORE-ONLY policy (`Policy.load(include_sleeve=False)`) and is what
`council.policy.default_policy()` returns inside every test, so the suite keeps testing the core
book when a quarterly `policy/stock-sleeve.yaml` lands. `sleeve_policy` is `policy/` plus the
synthetic sleeve in `tests/fixtures/policy_sleeve/` (TSTA, TSTB, TSTC_B, F; a shortlist of two).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from council import policy as policy_module
from council.paths import POLICY_DIR
from council.policy import Policy

SLEEVE_FIXTURE = Path(__file__).parent / "fixtures" / "policy_sleeve"


def make_sleeve_policy_dir(dest: Path, *, overlay: Path = SLEEVE_FIXTURE) -> Path:
    """`dest` = every top-level `policy/*.yaml`, then the overlay's `*.yaml` on top."""
    dest.mkdir(parents=True, exist_ok=True)
    for src in (*sorted(POLICY_DIR.glob("*.yaml")), *sorted(overlay.glob("*.yaml"))):
        shutil.copyfile(src, dest / src.name)
    return dest


@pytest.fixture(scope="session")
def _core_policy() -> Policy:
    return Policy.load(include_sleeve=False)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch, _core_policy):
    """Every test gets a private state dir and a stub-mode dev environment; lab keys are removed.
    `default_policy()` is pinned to the core-only policy and restored afterwards."""
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COUNCIL_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    monkeypatch.setenv("COUNCIL_ENV_FILE", str(tmp_path / "no.env"))   # never the developer's .env
    for name in ("ETORO_USER_KEY", "ETORO_API_KEY", "CLAUDECODE"):
        monkeypatch.delenv(name, raising=False)
    policy_module.default_policy.cache_clear()
    policy_module.install_default_policy(_core_policy)
    yield
    policy_module.install_default_policy(None)
    policy_module.default_policy.cache_clear()


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "capability_gates: use the real M5-D1 capability gates "
                                       "(state_dir/account/capabilities.json + ledger cross-check)")


@pytest.fixture(autouse=True)
def _capabilities_proven(request, monkeypatch):
    """M5-D1: a connected cycle consults `capabilities.load`, which is all-false without a smoke
    ledger. Tests written before the gates keep their proven-broker world; tests marked
    `capability_gates` (and `tests/operator/test_capabilities.py`) see the real, fail-closed load."""
    if request.node.get_closest_marker("capability_gates") is not None:
        yield
        return
    from council.operator import capabilities

    monkeypatch.setattr(capabilities, "load", lambda *a, **k: capabilities.Capabilities.all_verified())
    yield


@pytest.fixture(scope="session")
def policy(_core_policy) -> Policy:
    return _core_policy


@pytest.fixture(scope="session")
def sleeve_policy_dir(tmp_path_factory) -> Path:
    return make_sleeve_policy_dir(tmp_path_factory.mktemp("policy_sleeve"))


@pytest.fixture(scope="session")
def sleeve_policy(sleeve_policy_dir) -> Policy:
    return Policy.load(sleeve_policy_dir)


@pytest.fixture
def slot() -> datetime:
    return datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
