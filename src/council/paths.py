"""Filesystem locations. Private state lives OUTSIDE the repository so a stray `git add` cannot leak it."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = REPO_ROOT / "policy"
PROMPTS_DIR = REPO_ROOT / "prompts"
JOURNAL_DIR = REPO_ROOT / "journal"
SITE_DIR = REPO_ROOT / "site"


def state_dir() -> Path:
    """Private state root (ledger, transcripts, caches, salts). Overridable for tests."""
    override = os.environ.get("COUNCIL_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "council-book"


def log_dir() -> Path:
    override = os.environ.get("COUNCIL_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Logs" / "council-book"


def ensure_private_dirs() -> Path:
    root = state_dir()
    for sub in ("cache", "transcripts", "salts", "broker_raw", "backups"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    return root


def assert_outside_repo(path: Path) -> None:
    """Refuse to write private data inside the public repository tree."""
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise RuntimeError(f"private path {resolved} is inside the public repo")
