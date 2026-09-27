"""Release pinning for operator commands (m5-readiness §9.1, M-14).

Commands that move money, touch broker keys or clear a blocker run only from the INSTALLED release,
never from the development tree that coding agents edit. This is an accident guard: it stops
`uv run council approve` in the dev checkout; it is not a security boundary (§9.4).

The code is the installed release when all of these hold:
- `paths.REPO_ROOT` resolves to `state_dir/releases/current` (a symlink or a directory);
- `state_dir/releases/installed.json` records the commit `ops/install.sh` verified on `origin`
  (`{"commit": "<40 hex>", "tag": "<tag>", ...}`, written by install.sh, never by an agent);
- `git status --porcelain` in that checkout is empty (clean) and its `HEAD` equals that commit.

A marked rehearsal sandbox also passes (L2 dress rehearsal): a state dir that holds a `REHEARSAL`
marker file and is NOT the real default state dir (the real one never carries the marker, gate B4;
if it does, the marker is ignored here).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from council import paths

RELEASES_DIR = "releases"
CURRENT = "current"
RECORD_NAME = "installed.json"
REHEARSAL_MARKER = "REHEARSAL"
RELEASE_ALIAS = "council-op"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

Runner = Callable[..., subprocess.CompletedProcess[str]]


class ReleaseError(RuntimeError):
    """The running code is not the installed release. The message names the release command."""


def default_state_dir() -> Path:
    """The real private state dir (ignores COUNCIL_STATE_DIR)."""
    return Path.home() / "Library" / "Application Support" / "council-book"


def is_marked_sandbox(state_dir: Path | None = None) -> bool:
    """True for a rehearsal sandbox: the state dir holds a `REHEARSAL` marker file and is not the
    real state dir (a marker there is ignored: gate B4 reports it)."""
    root = (state_dir if state_dir is not None else paths.state_dir()).expanduser()
    if not (root / REHEARSAL_MARKER).is_file():
        return False
    try:
        return root.resolve() != default_state_dir().resolve()
    except OSError:
        return False


def release_dir(state_dir: Path | None = None) -> Path:
    return (state_dir if state_dir is not None else paths.state_dir()) / RELEASES_DIR / CURRENT


def record_path(state_dir: Path | None = None) -> Path:
    return (state_dir if state_dir is not None else paths.state_dir()) / RELEASES_DIR / RECORD_NAME


def recorded_commit(state_dir: Path | None = None) -> str | None:
    """The commit install.sh recorded, or None when absent or malformed."""
    path = record_path(state_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    commit = str(data.get("commit", "")).strip().lower() if isinstance(data, dict) else ""
    return commit if _COMMIT.match(commit) else None


def release_command(argv: list[str] | None = None) -> str:
    """The command the operator should type instead (the release alias from runbook §11.1)."""
    args = " ".join(argv or [])
    return f"{RELEASE_ALIAS} {args}".strip()


def _git(runner: Runner, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return runner(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ReleaseError("cannot run git to check the installed release") from exc


def release_problems(
    *,
    repo_root: Path | None = None,
    state_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> list[str]:
    """Every reason the running code is not the installed release (empty = it is, or a sandbox)."""
    root = state_dir if state_dir is not None else paths.state_dir()
    if is_marked_sandbox(root):
        return []
    code = (repo_root if repo_root is not None else paths.REPO_ROOT)
    current = release_dir(root)
    if not current.exists():
        return ["no installed release (state_dir/releases/current is missing)"]
    try:
        if code.resolve() != current.resolve():
            return ["this is not the installed release (the code runs from another checkout)"]
    except OSError:
        return ["cannot resolve the installed release"]
    problems: list[str] = []
    commit = recorded_commit(root)
    if commit is None:
        problems.append("no install record (state_dir/releases/installed.json)")
    status = _git(runner, current, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode != 0:
        problems.append("the installed release is not a git checkout")
    elif status.stdout.strip():
        problems.append("the installed release has local changes")
    head = _git(runner, current, "rev-parse", "HEAD")
    head_sha = head.stdout.strip().lower() if head.returncode == 0 else ""
    if commit is not None and head_sha != commit:
        problems.append("the installed release's HEAD differs from the commit recorded at install")
    return problems


def assert_release_code(
    *,
    argv: list[str] | None = None,
    repo_root: Path | None = None,
    state_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> None:
    """Raise ReleaseError unless the running code is the installed release (or a marked sandbox).
    The message tells the operator which command to run instead."""
    problems = release_problems(repo_root=repo_root, state_dir=state_dir, runner=runner)
    if problems:
        raise ReleaseError(
            "release-pinned command refused: " + "; ".join(problems)
            + f". Run it from the installed release: {release_command(argv)}"
        )
