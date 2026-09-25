"""The shipped git hooks, exercised in a temporary repository (never the development clone)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from council.paths import REPO_ROOT

HOOKS = REPO_ROOT / "ops" / "githooks"
NOREPLY = "1+dev@users.noreply.github.com"


def _env(email: str) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": email,
        "COUNCIL_LEAKSCAN": f"{sys.executable} -m council.publish.leakscan",
    }


def _git(repo: Path, *args: str, email: str = NOREPLY, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, env=_env(email), capture_output=True, text=True, check=check)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "core.hooksPath", str(HOOKS))
    _git(work, "remote", "add", "origin", str(origin))
    (work / "README.md").write_text("x\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    return work


def _commit(repo: Path, rel: str, content: str, *, email: str = NOREPLY, verify: bool = True):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", rel, email=email)
    args = ["commit", "-q", "-m", f"add {rel}"] + ([] if verify else ["--no-verify"])
    return _git(repo, *args, email=email, check=False)


def test_hooks_are_executable():
    for name in ("pre-commit", "pre-push"):
        assert os.access(HOOKS / name, os.X_OK), name


def test_pre_commit_refuses_a_non_noreply_identity(repo):
    result = _commit(repo, "docs/a.md", "fine\n", email="someone@example.com")
    assert result.returncode != 0 and "noreply" in result.stderr


def test_pre_commit_accepts_clean_public_files(repo):
    assert _commit(repo, "journal/status.json", '{"state": "LIVE"}\n').returncode == 0


def test_pre_commit_refuses_a_leak_in_public_files(repo):
    result = _commit(repo, "docs/notes.md", "fees were $123.45\n")
    assert result.returncode != 0 and "leak scan failed" in result.stderr


def test_pre_commit_scans_the_staged_blob_not_the_working_tree(repo):
    (repo / "site").mkdir()
    (repo / "site" / "x.txt").write_text("position 2951234567\n")
    _git(repo, "add", "site/x.txt")
    (repo / "site" / "x.txt").write_text("clean now\n")          # working tree differs from the index
    result = _git(repo, "commit", "-q", "-m", "x", check=False)
    assert result.returncode != 0


def test_pre_commit_ignores_private_areas(repo):
    assert _commit(repo, "tests/fixture.txt", "fees were $123.45\n").returncode == 0


def test_pre_push_refuses_non_noreply_commits(repo):
    assert _commit(repo, "src.txt", "x\n", email="someone@example.com", verify=False).returncode == 0
    result = _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main", check=False)
    assert result.returncode != 0 and "noreply" in result.stderr


def test_pre_push_refuses_leaks_that_bypassed_pre_commit(repo):
    assert _commit(repo, "journal/x.json", '{"note": "USD 1000"}\n', verify=False).returncode == 0
    result = _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main", check=False)
    assert result.returncode != 0 and "leak scan failed" in result.stderr


def test_pre_push_allows_clean_commits(repo):
    assert _commit(repo, "journal/status.json", '{"state": "LIVE"}\n').returncode == 0
    assert _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main", check=False).returncode == 0
