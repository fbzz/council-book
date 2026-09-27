"""The one place for eToro Licensed Content (m5-readiness M5-M).

Every eToro payload, broker feed text and prompt section carrying eToro data lives ONLY under
`state_dir/licensed/{fixtures,calls,feed}/` and is deleted before it is 7 days old
(`invariants.LICENSED_RETENTION_DAYS`):

  - `licensed_dir(state_dir, kind)` is the only way code obtains a folder for such a payload;
  - `sweep` is the once-a-day deletion both unattended loops call (the cycle hook and the watch);
    whichever runs first on a UTC day writes the receipt, the other does nothing;
  - `council purge-licensed` (council.operator.purge) is the operator's on-demand purge: the
    licensed tree, the ledger, transcripts, captures, fixtures, and every backup (deleted and taken
    fresh after a ledger scrub);
  - `backup_check` is the licensed-content scan a ledger backup runs (`ops.backup.licensed_free`).

Nothing here imports the broker writer, and nothing here ever prints or returns licensed text.
"""

from __future__ import annotations

import gzip
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

LICENSED_DIR = "licensed"
KINDS = ("fixtures", "calls", "feed")
DIR_MODE = 0o700


def ttl_days() -> int:
    """The retention in force (never above 7 days)."""
    from council.operator.purge import retention_days

    return retention_days()


def licensed_root(state_dir: Path) -> Path:
    return Path(state_dir) / LICENSED_DIR


def licensed_dir(state_dir: Path, kind: str, *, create: bool = True) -> Path:
    """`state_dir/licensed/<kind>/`, private (0700) and outside the repository."""
    if kind not in KINDS:
        raise ValueError(f"licensed payload kind must be one of {KINDS}")
    from council import paths

    folder = licensed_root(state_dir) / kind
    paths.assert_outside_repo(folder)
    if create:
        for p in (licensed_root(state_dir), folder):
            p.mkdir(parents=True, exist_ok=True)
            p.chmod(DIR_MODE)
    return folder


def ensure_dirs(state_dir: Path) -> None:
    for kind in KINDS:
        licensed_dir(state_dir, kind)


def is_licensed_path(state_dir: Path, path: Path) -> bool:
    root = licensed_root(state_dir).resolve()
    resolved = Path(path).resolve()
    return resolved == root or root in resolved.parents


def sweep(state_dir: Path, now: datetime) -> list[str]:
    """The daily sweeper: on the first call of each UTC day, delete licensed payloads a day before
    they reach the retention (so none outlives 7 days between two daily runs) and filter the model
    output that copied them. Never raises; returns `purge_error:*` flags."""
    try:
        from council.operator.purge import maybe_daily_purge

        return list(maybe_daily_purge(Path(state_dir), now))
    except Exception as exc:  # noqa: BLE001
        return [f"purge_error:{type(exc).__name__}"]


# ------------------------------------------------------------------------------- backup scan
def held_texts(state_dir: Path) -> list[str]:
    """Every licensed text still held in `licensed/calls/` (the texts a copy would be matched on)."""
    from council.models.inputs import LicensedInputs

    folder = licensed_root(state_dir) / "calls"
    texts: list[str] = []
    if not folder.is_dir():
        return texts
    for path in sorted(folder.rglob("*.json.gz")):
        try:
            with gzip.open(path, "rb") as fh:
                lic = LicensedInputs.model_validate_json(fh.read())
        except Exception:  # noqa: BLE001 - an unreadable file cannot be matched on
            continue
        texts += [t for group in lic.texts.values() for t in group.values() if t]
    return texts


def _sqlite_strings(path: Path) -> Iterator[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            for row in conn.execute(f"SELECT * FROM {quoted}"):  # noqa: S608 - names from the schema
                for value in row:
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", "replace")
                    if isinstance(value, str) and value:
                        yield value
    finally:
        conn.close()


def ledger_copies_licensed(db_path: Path, texts: list[str]) -> bool:
    """True when any string in any table of the SQLite file copies a licensed text."""
    from council.publish.leakscan import LicensedMatcher

    matcher = LicensedMatcher(texts)
    if not matcher:
        return False
    return any(matcher.hits(value) for value in _sqlite_strings(db_path))


def backup_check(state_dir: Path) -> Callable[[Path], bool]:
    """The `licensed_check` for `ops.backup`: True (licensed-free) when the backup is outside the
    licensed tree and none of its strings copies a licensed text still held."""
    def check(backup_path: Path) -> bool:
        if is_licensed_path(state_dir, backup_path):
            return False
        return not ledger_copies_licensed(backup_path, held_texts(state_dir))

    return check
