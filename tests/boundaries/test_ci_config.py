"""CI and Pages workflows keep their safety properties (least privilege, leak scan, noreply)."""

from __future__ import annotations

import re

import yaml

from council.paths import REPO_ROOT

WF = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    data = yaml.safe_load((WF / name).read_text())
    data["on"] = data.pop(True, data.get("on"))        # YAML 1.1 reads the key `on` as True
    return data


def _steps(job: dict) -> str:
    return "\n".join(str(step) for step in job["steps"])


def test_ci_is_read_only_and_runs_every_gate():
    ci = _load("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    test = _steps(ci["jobs"]["test"])
    for needle in ("astral-sh/setup-uv", "uv sync", "ruff check", 'pytest -m "not live"',
                   "python -m council.publish.leakscan journal docs site README.md"):
        assert needle in test, needle
    assert "/tmp/gitleaks git" in _steps(ci["jobs"]["gitleaks"])
    identity = _steps(ci["jobs"]["identity"])
    assert "@users" in identity and "noreply" in identity and "council-publisher" in identity


def test_pages_builds_from_the_journal_with_minimal_permissions():
    pages = _load("pages.yml")
    assert pages["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    assert set(pages["on"]["push"]["paths"]) == {"journal/**", "site/**", "prompts/**", "policy/**"}
    build = _steps(pages["jobs"]["build"])
    assert "python site/build.py" in build and "actions/upload-pages-artifact" in build
    assert "actions/deploy-pages" in _steps(pages["jobs"]["deploy"])


def test_actions_are_pinned_with_a_sha_pinning_todo():
    for name in ("ci.yml", "pages.yml"):
        text = (WF / name).read_text()
        uses = [line for line in text.splitlines() if re.match(r"\s*(-\s*)?uses:", line)]
        assert uses and all("@v" in line and "SHA-pin" in line for line in uses), name


def test_gitleaks_config_extends_the_defaults():
    text = (REPO_ROOT / ".gitleaks.toml").read_text()
    assert "[extend]" in text and "useDefault = true" in text
