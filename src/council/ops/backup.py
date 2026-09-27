"""Daily ledger backup (m5-readiness M5-E1, G21): `state_dir/backups/ledger-YYYYMMDD.sqlite3`.

Rules:
- The ledger ONLY, copied with SQLite's online backup API (consistent while the runner writes).
  Never `calls/`, `transcripts/`, `fixtures/`, `licensed/` or `broker_raw/` (the purge's M5-E
  contract), so a backup holds no licensed payload file.
- One file per UTC day (a second run the same day replaces it), the newest 14 kept; each file is
  mode 0600 in a 0700 folder, self-contained (rollback journal, no -wal/-shm), and passes
  `PRAGMA quick_check` before it replaces anything.
- Each backup records the ledger runtime key `ops.backup` = {"at": iso, "licensed_free": bool}
  (read by `doctor --ready` O4). `licensed_free` is the structural guarantee above AND, when given,
  `licensed_check(backup_path)` (licensed-content controls plug their scan in there).
- `maybe_daily_backup` (the watch hook) is best effort: a failure becomes the flag
  `backup_error:<type>` and never raises. Messages carry no path or content.
- Nothing here imports the broker, the executor or the LLM gateway.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from council import paths

BACKUP_DIR = "backups"
KEEP = 14
RUNTIME_KEY = "ops.backup"
NAME_RE = re.compile(r"^ledger-(\d{8})\.sqlite3$")
FILE_MODE = 0o600
DIR_MODE = 0o700

LicensedCheck = Callable[[Path], bool]


class BackupError(RuntimeError):
    """Never carries a path or ledger content."""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    at: datetime
    licensed_free: bool
    removed: tuple[Path, ...]


def backup_dir(state_dir: Path) -> Path:
    return state_dir / BACKUP_DIR


def backup_name(now: datetime) -> str:
    return f"ledger-{now.astimezone(UTC):%Y%m%d}.sqlite3"


def list_backups(state_dir: Path) -> list[Path]:
    """Backup files, oldest first (by the date in the name). Other files are ignored."""
    folder = backup_dir(state_dir)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.is_file() and NAME_RE.match(p.name)),
                  key=lambda p: p.name)


def rotate(state_dir: Path, keep: int = KEEP) -> tuple[Path, ...]:
    """Delete all but the newest `keep` backups; returns the removed paths."""
    if keep < 1:
        raise BackupError("keep must be at least 1")
    files = list_backups(state_dir)
    removed = tuple(files[:-keep]) if len(files) > keep else ()
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


def _ledger_path(state_dir: Path) -> Path:
    from council.ledger.db import LEDGER_FILE

    return state_dir / LEDGER_FILE


def _private_file(path: Path) -> None:
    """Create `path` empty at 0600 (whatever the umask); an existing file is replaced."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    os.close(fd)
    os.chmod(path, FILE_MODE)


def _copy(source: Path, dest: Path) -> None:
    _private_file(dest)
    src = sqlite3.connect(source, timeout=10.0)       # read only by the backup; WAL-safe
    try:
        dst = sqlite3.connect(dest, timeout=10.0)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")          # one self-contained file
            check = dst.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise BackupError("the backup copy failed its integrity check")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    with open(dest, "rb") as fh:
        os.fsync(fh.fileno())
    os.chmod(dest, FILE_MODE)


def backup_ledger(
    state_dir: Path | None = None,
    *,
    now: datetime | None = None,
    keep: int = KEEP,
    licensed_check: LicensedCheck | None = None,
) -> BackupResult | None:
    """Back up the ledger, rotate, record `ops.backup`. None when there is no ledger yet."""
    from council.clock import utcnow
    from council.ledger.db import Ledger

    root = Path(state_dir) if state_dir is not None else paths.state_dir()
    now = now or utcnow()
    source = _ledger_path(root)
    if not source.is_file():
        return None
    folder = backup_dir(root)
    paths.assert_outside_repo(folder)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, DIR_MODE)
    final = folder / backup_name(now)
    tmp = folder / f".{final.name}.tmp"
    try:
        _copy(source, tmp)
        os.replace(tmp, final)
    except BackupError:
        tmp.unlink(missing_ok=True)
        raise
    except (sqlite3.Error, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"ledger backup failed ({type(exc).__name__})") from None
    licensed_free = True
    if licensed_check is not None:
        try:
            licensed_free = bool(licensed_check(final))
        except Exception:
            licensed_free = False
    removed = rotate(root, keep)
    Ledger(source).set_runtime(RUNTIME_KEY, {"at": now.astimezone(UTC).isoformat(),
                                             "licensed_free": licensed_free}, now=now)
    return BackupResult(final, now, licensed_free, removed)


def backup_due(state_dir: Path, now: datetime) -> bool:
    """True when a ledger exists and today's (UTC) backup file does not."""
    return _ledger_path(state_dir).is_file() and not (backup_dir(state_dir) / backup_name(now)).is_file()


def maybe_daily_backup(state_dir: Path, now: datetime, *,
                       licensed_check: LicensedCheck | None = None) -> list[str]:
    """The watch hook: back up once per UTC day. Returns flags; never raises."""
    try:
        if not backup_due(state_dir, now):
            return []
        backup_ledger(state_dir, now=now, licensed_check=licensed_check)
    except Exception as exc:
        return [f"backup_error:{type(exc).__name__}"]
    return []
