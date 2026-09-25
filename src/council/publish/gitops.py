"""Publisher: commits journal files in a DEDICATED publisher clone and pushes them.

Rules:
- Every path is a normalised relative POSIX path under `journal/` (no `..`, no absolute paths, no
  backslashes, no symlink that leaves `journal/`).
- Every file and the commit message are leak-scanned first; any finding aborts before git runs.
- Refuses to run in the development tree, or when anything is already staged in the clone.
- `git add -- <explicit paths>`, then aborts unless `git diff --cached --name-only` lists only the
  published paths (all under `journal/`); the commit is checked again after it is made.
- Author AND committer are set explicitly and must be GitHub noreply addresses.
- `push=True`: up to 3 push attempts, with a fetch-and-rebase between attempts.
- `dry_run_dir`: the scanned files are written there instead and git is never run.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from council.paths import REPO_ROOT
from council.publish import journal, leakscan

NOREPLY_SUFFIX = "@users.noreply.github.com"
DEFAULT_AUTHOR = ("council-publisher", "18754232+fbzz@users.noreply.github.com")
PUSH_ATTEMPTS = 3
_SCRUBBED_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")

Runner = Callable[..., subprocess.CompletedProcess[str]]


class PublishError(RuntimeError):
    def __init__(self, message: str, findings: Sequence[leakscan.Finding] = ()):
        detail = "; ".join(str(f) for f in list(findings)[:20])
        super().__init__(f"{message}: {detail}" if detail else message)
        self.findings = list(findings)


@dataclass(frozen=True)
class PublishResult:
    commit_sha: str | None
    pushed: bool
    paths: tuple[str, ...] = field(default_factory=tuple)
    dry_run: bool = False


def check_journal_path(rel: str) -> str:
    """Return the normalised path, or raise if it is not a plain relative path under journal/."""
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
        raise PublishError(f"refusing path {rel!r}")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in rel.split("/")):
        raise PublishError(f"refusing non-normalised path {rel!r}")
    if pure.parts[0] != journal.JOURNAL or len(pure.parts) < 2:
        raise PublishError(f"refusing path outside journal/: {rel!r}")
    return str(pure)


def check_identity(author: tuple[str, str]) -> tuple[str, str]:
    name, email = author
    if not name or any(c in name for c in "<>\n\r") or not email.endswith(NOREPLY_SUFFIX):
        raise PublishError("publisher identity must use a GitHub noreply e-mail")
    if any(c in email for c in "<> \n\r"):
        raise PublishError("malformed publisher e-mail")
    return name, email


class Publisher:
    def __init__(
        self,
        clone_dir: Path | str,
        *,
        push: bool,
        remote: str = "origin",
        author: tuple[str, str] = DEFAULT_AUTHOR,
        ssh_command: str | None = None,
        dry_run_dir: Path | str | None = None,
        canaries: Sequence[str | float | int] = (),
        licensed_texts: Sequence[str] = (),
        push_attempts: int = PUSH_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        runner: Runner = subprocess.run,
    ):
        self.clone_dir = Path(clone_dir)
        self.push = push
        self.remote = remote
        self.author = check_identity(author)
        self.ssh_command = ssh_command
        self.dry_run_dir = Path(dry_run_dir) if dry_run_dir is not None else None
        self.canaries = list(canaries)
        self.licensed_texts = list(licensed_texts)
        self.push_attempts = max(1, push_attempts)
        self.sleep = sleep
        self.runner = runner

    # -------------------------------------------------------------------------------- git plumbing
    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_GIT_ENV}
        name, email = self.author
        env.update({
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
            "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
        })
        if self.ssh_command:
            env["GIT_SSH_COMMAND"] = self.ssh_command
        return env

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = ["git", "-c", "commit.gpgsign=false", "-c", f"user.name={self.author[0]}",
               "-c", f"user.email={self.author[1]}", *args]
        result = self.runner(cmd, cwd=self.clone_dir, env=self._env(), capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise PublishError(f"git {args[0]} failed (exit {result.returncode})")
        return result

    def _staged(self) -> list[str]:
        out = self._git("diff", "--cached", "--name-only", "-z").stdout
        return [p for p in out.split("\x00") if p]

    def _check_clone(self) -> None:
        clone = self.clone_dir.resolve()
        if not clone.is_dir():
            raise PublishError("publisher clone does not exist")
        repo = REPO_ROOT.resolve()
        if clone == repo or repo in clone.parents or clone in repo.parents:
            raise PublishError("refusing to publish from the development tree; use the publisher clone")
        top = self._git("rev-parse", "--show-toplevel").stdout.strip()
        if Path(top).resolve() != clone:
            raise PublishError("publisher clone is not the top of a git work tree")

    # -------------------------------------------------------------------------------- publish
    def scan(self, files: Mapping[str, bytes], message: str) -> list[leakscan.Finding]:
        findings: list[leakscan.Finding] = []
        for rel, data in files.items():
            findings += leakscan.scan_bytes(rel, data, canaries=self.canaries, licensed_texts=self.licensed_texts)
        findings += leakscan.scan(message, canaries=self.canaries, where="commit-message")
        return findings

    def publish(self, files: Mapping[str, bytes], message: str) -> PublishResult:
        if not files:
            raise PublishError("nothing to publish")
        if not message.strip() or len(message) > 500:
            raise PublishError("commit message must be 1-500 characters")
        normalised = {check_journal_path(rel): bytes(data) for rel, data in files.items()}
        paths = tuple(sorted(normalised))
        findings = self.scan(normalised, message)
        if findings:
            raise PublishError("leak scan failed; nothing was published", findings)

        if self.dry_run_dir is not None:
            journal.write_files(self.dry_run_dir, normalised)
            return PublishResult(commit_sha=None, pushed=False, paths=paths, dry_run=True)

        self._check_clone()
        if self._staged():
            raise PublishError("the publisher clone already has staged changes; refusing to mix them in")
        clone = self.clone_dir.resolve()
        journal_root = (clone / journal.JOURNAL).resolve()
        if journal_root != clone / journal.JOURNAL:
            raise PublishError("journal/ in the publisher clone is a link; refusing")
        for rel in normalised:
            if journal_root not in (clone / rel).resolve().parents:
                raise PublishError(f"path escapes journal/ through a link: {rel!r}")
        for rel, data in normalised.items():
            target = clone / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        self._git("add", "--", *paths)
        staged = self._staged()
        stray = [p for p in staged if p not in normalised or not p.startswith(journal.JOURNAL + "/")]
        if stray:
            self._git("reset", "-q", "--", *staged, check=False)
            raise PublishError(f"staged paths outside the publish set: {len(stray)}")
        if not staged:
            return PublishResult(commit_sha=None, pushed=False, paths=paths)

        self._git("commit", "-q", "--no-edit", "-m", message)
        changed = [p for p in self._git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD")
                   .stdout.split("\x00") if p]
        if any(p not in normalised for p in changed):
            raise PublishError("commit touched paths outside the publish set; not pushing")
        sha = self._git("rev-parse", "HEAD").stdout.strip()
        pushed = False
        if self.push:
            sha, pushed = self._push()
        return PublishResult(commit_sha=sha, pushed=pushed, paths=paths)

    def _push(self) -> tuple[str, bool]:
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not branch or branch == "HEAD":
            raise PublishError("publisher clone is on a detached HEAD; refusing to push")
        for attempt in range(1, self.push_attempts + 1):
            if self._git("push", "-q", self.remote, f"HEAD:refs/heads/{branch}", check=False).returncode == 0:
                return self._git("rev-parse", "HEAD").stdout.strip(), True
            if attempt < self.push_attempts:
                self.sleep(2.0 ** attempt)
                rebase = self._git("pull", "-q", "--rebase", self.remote, branch, check=False)
                if rebase.returncode != 0:
                    self._git("rebase", "--abort", check=False)
        return self._git("rev-parse", "HEAD").stdout.strip(), False
