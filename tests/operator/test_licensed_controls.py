"""M5-M licensed-content controls (LC3): one licensed tree, the daily sweeper in the watch, backups
that never hold licensed text, and purge-licensed refreshing every backup after a scrub."""
from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from council import paths, watch
from council.ledger.db import LEDGER_FILE
from council.operator import licensed
from council.operator.purge import RECEIPTS_DIR, purge_licensed
from council.ops import backup

from .test_purge import NOW, _canary_files, _sandbox


@pytest.fixture
def root() -> Path:
    r = paths.state_dir()
    r.mkdir(parents=True, exist_ok=True)
    return r


def _age(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_private_dirs_hold_the_licensed_tree_and_no_broker_raw(root):
    paths.ensure_private_dirs()
    assert {p.name for p in (root / "licensed").iterdir()} == set(licensed.KINDS)
    assert not (root / "broker_raw").exists()
    assert (root / "licensed").stat().st_mode & 0o077 == 0


def test_licensed_dir_only_knows_its_kinds(root):
    assert licensed.licensed_dir(root, "feed") == root / "licensed" / "feed"
    assert licensed.is_licensed_path(root, root / "licensed" / "feed" / "x.json")
    assert not licensed.is_licensed_path(root, root / "backups" / "x")
    with pytest.raises(ValueError):
        licensed.licensed_dir(root, "anything")
    assert licensed.ttl_days() == 7


def test_sweeper_removes_payloads_before_seven_days_and_keeps_fresh_ones(root):
    old, fresh = root / "licensed" / "feed" / "old.json", root / "licensed" / "feed" / "new.json"
    old.parent.mkdir(parents=True)
    old.write_text("{}"), fresh.write_text("{}")
    _age(old, 6.5)                                    # a day under the retention: swept today
    _age(fresh, 1)
    now = watch.datetime.now(watch.UTC)
    assert licensed.sweep(root, now) == []
    assert not old.exists() and fresh.exists()
    assert list((root / RECEIPTS_DIR).glob("*.json"))
    _age(fresh, 6.5)
    assert licensed.sweep(root, now + timedelta(hours=1)) == []   # once per UTC day
    assert fresh.exists()


def test_watch_runs_the_sweeper(tmp_path, monkeypatch):
    from tests.integration.test_end_to_end import _ctx

    calls = []
    monkeypatch.setattr(licensed, "sweep", lambda state, now: calls.append(state) or ["purge_error:x"])
    ctx = _ctx(tmp_path)
    out = watch.run_watch(ctx)
    assert calls == [ctx.state_dir] and "purge_error:x" in out.alerts


def test_watch_sweeper_never_raises(tmp_path, monkeypatch):
    from tests.integration.test_end_to_end import _ctx

    def boom(*_a):
        raise RuntimeError("x")

    monkeypatch.setattr(licensed, "sweep", boom)
    out = watch.run_watch(_ctx(tmp_path))
    assert "purge_error:RuntimeError" in out.alerts


def test_backup_check_flags_a_ledger_copy_of_held_licensed_text(root):
    _sandbox(root)
    res = backup.backup_ledger(root, now=NOW, licensed_check=licensed.backup_check(root))
    assert res is not None and res.licensed_free is False      # the sandbox ledger copied a title
    assert not any(p.startswith("backups/") and "licensed" in p for p in _canary_files(root))


def test_purge_deletes_every_backup_and_takes_a_fresh_clean_one(root):
    _sandbox(root)
    backup.backup_ledger(root, now=NOW - timedelta(days=2))
    stray = root / "backups" / "manual-copy.sqlite3"
    stray.write_bytes((root / LEDGER_FILE).read_bytes())
    assert any(p.startswith("backups/") for p in _canary_files(root))
    dry = purge_licensed(root, now=NOW, purge_all=True, dry_run=True)
    assert dry.counts["backups_deleted"] == 2 and "backups_taken" not in dry.counts
    assert stray.exists()
    receipt = purge_licensed(root, now=NOW, purge_all=True)
    assert receipt.errors == []
    assert receipt.counts["backups_deleted"] == 2 and receipt.counts["backups_taken"] == 1
    left = sorted(p.name for p in (root / "backups").iterdir())
    assert left == [backup.backup_name(NOW)]
    assert not any(p.startswith("backups/") for p in _canary_files(root))


def test_daily_sweep_without_a_scrub_keeps_backup_history(root):
    backup_dir = root / "backups"
    backup_dir.mkdir(parents=True)
    kept = backup_dir / "ledger-20260101.sqlite3"
    kept.write_bytes(b"")
    receipt = purge_licensed(root, now=NOW, older_than_days=6)
    assert "backups_deleted" not in receipt.counts and kept.exists()


def test_the_backup_module_never_copies_the_licensed_tree(root):
    import inspect

    src = inspect.getsource(backup)
    assert "copytree" not in src and "rglob" not in src    # the ledger file only, by path


def test_purge_refreshes_the_backups_of_a_nested_state_root(root):
    nested = root / "rehearsal"
    nested.mkdir()
    _sandbox(nested)
    backup.backup_ledger(nested, now=NOW - timedelta(days=2))
    assert any(p.startswith("rehearsal/backups/") for p in _canary_files(root))
    receipt = purge_licensed(root, now=NOW, purge_all=True)
    assert receipt.errors == []
    assert sorted(p.name for p in (nested / "backups").iterdir()) == [backup.backup_name(NOW)]
    assert not any(p.startswith("rehearsal/backups/") for p in _canary_files(root))


def test_a_failed_fresh_backup_still_deletes_the_stale_copies(root, monkeypatch):
    _sandbox(root)
    backup.backup_ledger(root, now=NOW - timedelta(days=2))

    def boom(*a, **k):
        raise backup.BackupError("disk full")

    monkeypatch.setattr(backup, "backup_ledger", boom)
    receipt = purge_licensed(root, now=NOW, purge_all=True)
    assert "backups: BackupError" in receipt.errors and "backups_taken" not in receipt.counts
    assert receipt.counts["backups_deleted"] == 1
    assert not any(p.startswith("backups/") for p in _canary_files(root))
