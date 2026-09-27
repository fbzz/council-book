"""M5-E1: SQLite online backup of the ledger, one per UTC day, keep 14, 0600, `ops.backup` recorded."""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime, timedelta

import pytest

from council import paths
from council.ledger.db import LEDGER_FILE, Ledger
from council.ops import backup

T0 = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)


@pytest.fixture
def state():
    root = paths.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(root / LEDGER_FILE)
    ledger.set_runtime("probe", {"v": 1}, now=T0)
    return root


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_backup_is_consistent_private_and_recorded(state):
    result = backup.backup_ledger(state, now=T0)
    assert result is not None and result.path.name == "ledger-20260901.sqlite3"
    assert mode(result.path) == 0o600 and mode(result.path.parent) == 0o700
    con = sqlite3.connect(result.path)
    try:
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        con.close()
    assert Ledger(result.path).get_runtime("probe") == {"v": 1}
    rec = Ledger(state / LEDGER_FILE).get_runtime(backup.RUNTIME_KEY)
    assert rec == {"at": T0.isoformat(), "licensed_free": True}
    assert not list(result.path.parent.glob(".*.tmp"))


def test_no_ledger_no_backup():
    root = paths.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    assert backup.backup_ledger(root, now=T0) is None
    assert not backup.backup_dir(root).exists()


def test_rotation_keeps_newest_14(state):
    for day in range(20):
        backup.backup_ledger(state, now=T0 + timedelta(days=day))
    files = backup.list_backups(state)
    assert len(files) == backup.KEEP
    assert files[0].name == "ledger-20260907.sqlite3" and files[-1].name == "ledger-20260920.sqlite3"


def test_same_day_replaces(state):
    backup.backup_ledger(state, now=T0)
    backup.backup_ledger(state, now=T0 + timedelta(hours=5))
    assert [p.name for p in backup.list_backups(state)] == ["ledger-20260901.sqlite3"]


def test_rotation_ignores_foreign_files(state):
    folder = backup.backup_dir(state)
    folder.mkdir(parents=True)
    other = folder / "notes.txt"
    other.write_text("x")
    for day in range(16):
        backup.backup_ledger(state, now=T0 + timedelta(days=day))
    assert other.exists()


def test_licensed_check_failure_marks_not_free(state):
    def boom(path):
        raise RuntimeError("scan crashed")

    result = backup.backup_ledger(state, now=T0, licensed_check=boom)
    assert result.licensed_free is False
    assert Ledger(state / LEDGER_FILE).get_runtime(backup.RUNTIME_KEY)["licensed_free"] is False


def test_only_the_ledger_is_copied(state):
    (state / "licensed").mkdir()
    (state / "licensed" / "payload.json").write_text("{}")
    backup.backup_ledger(state, now=T0)
    assert [p.name for p in backup.backup_dir(state).iterdir()] == ["ledger-20260901.sqlite3"]


def test_daily_hook_is_once_per_day_and_never_raises(state, monkeypatch):
    assert backup.maybe_daily_backup(state, T0) == []
    assert backup.backup_due(state, T0) is False
    assert backup.backup_due(state, T0 + timedelta(days=1)) is True

    def fail(*a, **k):
        raise backup.BackupError("x")

    monkeypatch.setattr(backup, "backup_ledger", fail)
    assert backup.maybe_daily_backup(state, T0 + timedelta(days=1)) == ["backup_error:BackupError"]


def test_keep_must_be_positive(state):
    with pytest.raises(backup.BackupError):
        backup.rotate(state, keep=0)
