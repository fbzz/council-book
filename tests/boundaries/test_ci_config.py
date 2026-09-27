"""CI and Pages workflows keep their safety properties (least privilege, leak scan, noreply)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from council.paths import REPO_ROOT

WF = REPO_ROOT / ".github" / "workflows"
WORKFLOWS = sorted([*WF.glob("*.yml"), *WF.glob("*.yaml")])

# `uses: owner/repo[/path]@<40-hex commit> # vX[.Y[.Z]]`: a tag or branch can be moved under us, a
# commit cannot; the comment is the release Dependabot reads and rewrites.
PINNED_USES = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<action>[\w.-]+/[\w.-]+(?:/[\w./-]+)?)@(?P<sha>[0-9a-f]{40})"
    r"\s+#\s*(?P<version>v\d+(?:\.\d+){0,2})\s*$"
)


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


def _uses_values(node) -> list[str]:
    """Every `uses:` value anywhere in a parsed workflow (steps and reusable-workflow jobs)."""
    if isinstance(node, dict):
        found = [v for k, v in node.items() if k == "uses"]
        return found + [u for v in node.values() for u in _uses_values(v)]
    if isinstance(node, list):
        return [u for v in node for u in _uses_values(v)]
    return []


def test_every_action_is_pinned_to_a_full_commit_sha():
    assert {"ci.yml", "pages.yml"} <= {p.name for p in WORKFLOWS}
    seen: dict[str, tuple[str, str]] = {}
    for path in WORKFLOWS:
        text = path.read_text()
        lines = [line for line in text.splitlines() if re.match(r"\s*(-\s*)?uses:", line)]
        values = _uses_values(yaml.safe_load(text))
        assert lines, path.name
        # no `uses:` hidden from the line check (flow mappings, anchors)
        assert len(lines) == len(values), path.name
        for value in values:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}", value), (path.name, value)
        for line in lines:
            m = PINNED_USES.match(line)
            assert m, (path.name, line.strip())
            # one action, one pin: the same action may not sit on two commits or two labels
            pin = (m["sha"], m["version"])
            assert seen.setdefault(m["action"], pin) == pin, (path.name, line.strip())


def test_dependabot_bumps_the_pinned_actions_weekly():
    cfg = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text())
    assert cfg["version"] == 2
    gha = [u for u in cfg["updates"] if u["package-ecosystem"] == "github-actions"]
    assert len(gha) == 1
    assert gha[0]["directory"] == "/" and gha[0]["schedule"]["interval"] == "weekly"


def test_gitleaks_tarball_is_verified_before_it_is_unpacked():
    steps = _load("ci.yml")["jobs"]["gitleaks"]["steps"]
    step = next(s for s in steps if "/tmp/gitleaks git" in s.get("run", ""))
    assert re.fullmatch(r"\d+\.\d+\.\d+", step["env"]["GITLEAKS_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", step["env"]["GITLEAKS_SHA256"])
    run = step["run"]
    assert not re.search(r"\|\s*(tar|sh|bash)\b", run), "never pipe a download straight into tar or a shell"
    assert run.index("sha256sum -c") < run.index("tar xzf") < run.index("/tmp/gitleaks git")


def _identity_script() -> str:
    steps = _load("ci.yml")["jobs"]["identity"]["steps"]
    return next(s["run"] for s in steps if s.get("name", "").startswith("Every author and committer"))


NOREPLY = "1234+someone@users.noreply.github.com"
DEPENDABOT = "49699333+dependabot[bot]@users.noreply.github.com"
WEB_FLOW = "noreply@github.com"


@pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")
@pytest.mark.parametrize(
    ("identities", "passes"),
    [
        ([(NOREPLY, NOREPLY)], True),
        ([(NOREPLY, NOREPLY), (DEPENDABOT, WEB_FLOW)], True),        # Dependabot branch in --all
        ([(NOREPLY, NOREPLY), ("me@example.com", WEB_FLOW)], False),  # web-flow excuses no author
        ([(WEB_FLOW, NOREPLY)], False),                               # web-flow is a committer only
        ([(NOREPLY, "me@example.com")], False),
        ([(NOREPLY, "x" + WEB_FLOW)], False),                         # exact match, not a suffix
        ([(NOREPLY, WEB_FLOW + ".example")], False),
    ],
)
def test_identity_check_allows_web_flow_only_as_committer(tmp_path: Path, identities, passes):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    for i, (author, committer) in enumerate(identities):
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", f"c{i}"],
            cwd=repo, check=True,
            env=env | {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": author,
                       "GIT_COMMITTER_NAME": "c", "GIT_COMMITTER_EMAIL": committer},
        )
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _identity_script()], cwd=repo, env=env,
                            capture_output=True, text=True)
    assert (result.returncode == 0) is passes, result.stdout + result.stderr


def test_gitleaks_config_extends_the_defaults():
    text = (REPO_ROOT / ".gitleaks.toml").read_text()
    assert "[extend]" in text and "useDefault = true" in text
