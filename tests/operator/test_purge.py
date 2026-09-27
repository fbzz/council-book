"""`council purge-licensed`: no eToro Licensed Content outlives the retention anywhere private."""

from __future__ import annotations

import gzip
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from council import paths
from council.deliberation.capture import licensed_path, load_inputs, load_licensed
from council.ledger.db import LEDGER_FILE, Ledger
from council.operator import purge
from council.operator.inputs_cli import readings_for, render_html, write_view
from council.operator.purge import (
    LICENSED_RETENTION_DAYS,
    REPLY_PLACEHOLDER,
    STORE_NAMES,
    STORES,
    LicensedFilter,
    daily_purge,
    purge_licensed,
    retention_days,
)

from ..council.factories import SLOT
from .conftest import CANARY_SUMMARY, CANARY_TITLE, capture_cycle
from .test_inputs_view import _app

CANARIES = ("canarybird", "zebrafish")
NOW = SLOT + timedelta(days=1)


def _all_text(root: Path) -> dict[str, str]:
    """Every file under the state dir, gunzipped when it is gzip, as lower-cased text."""
    out = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        out[str(path.relative_to(root))] = data.decode("utf-8", "replace").lower()
    return out


def _canary_files(root: Path) -> list[str]:
    return sorted(p for p, text in _all_text(root).items() if any(c in text for c in CANARIES))


def _sandbox(root: Path) -> None:
    """A state dir holding licensed text in every store the purge must cover."""
    cap = capture_cycle(root, canary=True)
    capture_cycle(root / "rehearsal", canary=True, ledger=False)
    inputs, lic = load_inputs(root, cap.cycle_id), load_licensed(root, cap.cycle_id)
    write_view(root, cap.cycle_id, render_html(inputs, lic, readings=readings_for(inputs, lic, None)))
    for rel in ("licensed/fixtures/2026-10-01/feed.json", "licensed/feed/page1.json",
                "fixtures/2026-10-01/licensed/feed.json", "broker_raw/snapshot.json"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"title": CANARY_TITLE, "summary": CANARY_SUMMARY}))
    public = root / "fixtures" / "2026-10-01" / "public" / "sec.json"
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_text("{}")


@pytest.fixture
def root() -> Path:
    r = paths.state_dir()
    r.mkdir(parents=True, exist_ok=True)
    return r


# ------------------------------------------------------------------------------ purge --all
def test_purge_all_leaves_no_canary_anywhere(root):
    _sandbox(root)
    before = _canary_files(root)
    assert any(p.startswith("licensed/calls/") for p in before)
    assert any(p.startswith("calls/") for p in before)             # the model copied the title
    assert any(p.startswith("transcripts/") for p in before)
    assert any(p.startswith("inputs-view/") for p in before)
    assert any(p.startswith("rehearsal/") for p in before)
    assert LEDGER_FILE in before or any(p.startswith(LEDGER_FILE) for p in before)
    receipt = purge_licensed(root, now=NOW, purge_all=True)
    assert receipt.errors == [] and receipt.unregistered == []
    assert _canary_files(root) == []
    assert (root / "fixtures" / "2026-10-01" / "public" / "sec.json").exists()
    # the capture keeps its commits and says when the licensed text went
    cap = load_inputs(root, "2026-10-01T1440Z")
    assert cap.licensed_purged_at == NOW and cap.licensed_items > 0
    assert all(s.purged_at == NOW for s in cap.sections.values() if s.licensed)
    assert all(c.replies_filtered for c in cap.calls)
    news = next(c for c in cap.calls if c.role == "news")
    assert news.replies == [REPLY_PLACEHOLDER] and news.input_hash
    assert not licensed_path(root, "2026-10-01T1440Z").exists()
    # counts only in the receipt, and a copy of it on disk
    assert receipt.counts["licensed_capture_files_deleted"] == 2
    assert receipt.counts["inputs_view_files_deleted"] == 1
    receipts = list((root / purge.RECEIPTS_DIR).glob("*.json"))
    assert len(receipts) == 1 and stat.S_IMODE(receipts[0].stat().st_mode) == 0o600
    assert not any(c in receipts[0].read_text().lower() for c in CANARIES)


def test_replies_are_filtered_before_the_licensed_texts_go(root):
    cap = capture_cycle(root, canary=True)
    ledger = Ledger(root / LEDGER_FILE)
    assert CANARY_TITLE in json.dumps(ledger.get_cycle(cap.cycle_id))
    purge_licensed(root, now=NOW, purge_all=True)
    rec = ledger.get_cycle(cap.cycle_id)
    assert CANARY_TITLE not in json.dumps(rec)
    card = next(c for c in rec["cards"] if c["card_id"] == "K:news:2")
    assert card["claim"] == REPLY_PLACEHOLDER and card["evidence_ids"] == ["N:5e6f7a8b"]
    assert rec["cycle_id"] == cap.cycle_id and rec["status"] == "dry_run"
    with gzip.open(root / "transcripts" / f"{cap.cycle_id}.json.gz", "rt") as fh:
        raw = json.load(fh)["raw"]
    assert raw["news"] == REPLY_PLACEHOLDER and raw["bear"] != REPLY_PLACEHOLDER


def test_older_than_seven_days_keeps_day_six_and_purges_day_eight(root):
    now = datetime(2026, 10, 20, 10, 40, tzinfo=UTC)
    six = capture_cycle(root, slot=now - timedelta(days=6), canary=True)
    eight = capture_cycle(root, slot=now - timedelta(days=8), canary=True)
    receipt = purge_licensed(root, now=now, older_than_days=7)
    assert receipt.cutoff == now - timedelta(days=7)
    assert licensed_path(root, six.cycle_id).exists()
    assert load_inputs(root, six.cycle_id).licensed_purged_at is None
    assert not licensed_path(root, eight.cycle_id).exists()
    assert load_inputs(root, eight.cycle_id).licensed_purged_at == now
    # the default cut-off is the retention itself
    assert purge_licensed(root, now=now + timedelta(days=2), write_receipt=False).counts[
        "licensed_capture_files_deleted"] == 1
    assert not licensed_path(root, six.cycle_id).exists()


def test_old_licensed_files_go_by_age_and_views_always_go(root):
    now = datetime(2026, 10, 20, tzinfo=UTC)
    fresh = root / "licensed" / "feed" / "fresh.json"
    stale = root / "licensed" / "feed" / "stale.json"
    view = root / "inputs-view" / "x.html"
    for path in (fresh, stale, view):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    old = (now - timedelta(days=9)).timestamp()
    os.utime(stale, (old, old))
    recent = (now - timedelta(days=1)).timestamp()
    os.utime(fresh, (recent, recent))
    purge_licensed(root, now=now)
    assert fresh.exists() and not stale.exists() and not view.exists()


def test_a_dry_run_changes_nothing(root):
    _sandbox(root)
    snapshot = _all_text(root)
    receipt = purge_licensed(root, now=NOW, purge_all=True, dry_run=True)
    assert receipt.counts["licensed_capture_files_deleted"] == 2
    assert _all_text(root) == snapshot


def test_an_orphan_capture_is_marked_when_its_licensed_file_is_gone(root):
    cap = capture_cycle(root, canary=True)
    licensed_path(root, cap.cycle_id).unlink()
    receipt = purge_licensed(root, now=NOW, purge_all=True)
    assert receipt.counts["capture_orphans_marked"] == 1
    orphan = load_inputs(root, cap.cycle_id)
    assert orphan.licensed_purged_at == NOW
    assert not any(c.replies_filtered for c in orphan.calls)   # nothing left to filter against


def test_an_unreadable_licensed_file_still_goes_past_the_cut_off(root):
    bad = root / "licensed" / "calls" / "2026" / "10" / "2026-10-01T1440Z.json.gz"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not gzip")
    receipt = purge_licensed(root, now=NOW, purge_all=True, write_receipt=False)
    assert not bad.exists() and receipt.errors == ["licensed-calls: BadGzipFile"]
    assert daily_purge(root, NOW) == []


# ----------------------------------------------------------------------------- the registry
def test_every_folder_of_a_state_dir_is_registered(root):
    paths.ensure_private_dirs()
    _sandbox(root)
    for extra in ("keys", "account", "releases/current", "publisher-clone", "backtests", "design",
                  "salts", "cache/history", "dryrun"):
        (root / extra).mkdir(parents=True, exist_ok=True)
    purge_licensed(root, now=NOW)              # writes purge-receipts/ too
    folders = sorted(p.name for p in root.iterdir() if p.is_dir())
    missing = [f for f in folders if f not in STORE_NAMES]
    assert missing == [], f"register these folders in operator/purge.py STORES: {missing}"
    for store in STORES:
        assert store.note, store.name
        if store.licensed:
            assert store.handler is not None, f"{store.name} holds licensed content but has no handler"
    (root / "mystery").mkdir()
    assert purge_licensed(root, now=NOW, write_receipt=False).unregistered == ["mystery"]


def test_ensure_private_dirs_creates_only_registered_folders(root):
    paths.ensure_private_dirs()
    assert {p.name for p in root.iterdir() if p.is_dir()} <= STORE_NAMES


# ------------------------------------------------------------------------------ hook and cli
def test_the_daily_hook_never_raises_and_flags_a_failing_store(root, monkeypatch):
    capture_cycle(root, canary=True)
    assert daily_purge(root, NOW) == []

    def boom(run, where):
        raise OSError("disk")

    broken = tuple(s if s.name != "inputs-view" else purge.Store(s.name, s.licensed, s.note, boom)
                   for s in STORES)
    monkeypatch.setattr(purge, "STORES", broken)
    assert daily_purge(root, NOW) == ["purge_error:inputs-view:OSError"]


def test_retention_is_seven_days_at_most():
    assert LICENSED_RETENTION_DAYS == 7 and retention_days() <= 7


def test_the_command_purges_for_the_operator(root, as_operator):
    capture_cycle(root, canary=True)
    res = CliRunner().invoke(_app(), ["purge-licensed", "--all"])
    assert res.exit_code == 0, res.output
    assert "purge-licensed: all" in res.output and "licensed_capture_files_deleted: 1" in res.output
    assert _canary_files(root) == []
    res = CliRunner().invoke(_app(), ["purge-licensed", "--older-than", "30d"])
    assert res.exit_code != 0 and "longer than 7 days" in res.output


def test_the_filter_catches_copies_but_not_short_or_changed_titles():
    six = "Chipmaker halts shipments after export ruling"
    flt = LicensedFilter([six, "Nvidia", "Markets wrap", CANARY_SUMMARY])
    assert flt.hits(f"The bear said: {six.lower()}.")
    assert not flt.hits("Chipmaker halts deliveries after export ruling")
    assert not flt.hits("Markets wrap: nvidia up")
    assert flt.hits("... text that must never outlive seven days in any private store ...")
    assert not flt.hits("seven days")


def test_the_daily_purge_runs_once_per_utc_day(root):
    from council.operator.purge import daily_purge_due, maybe_daily_purge

    now = datetime(2026, 10, 20, 0, 40, tzinfo=UTC)
    old = capture_cycle(root, slot=now - timedelta(days=8), canary=True)
    assert daily_purge_due(root, now)
    assert maybe_daily_purge(root, now) == []
    assert not licensed_path(root, old.cycle_id).exists()
    assert not daily_purge_due(root, now + timedelta(hours=4))
    later = capture_cycle(root, slot=now - timedelta(days=9), canary=True)
    assert maybe_daily_purge(root, now + timedelta(hours=4)) == []      # not again the same day
    assert licensed_path(root, later.cycle_id).exists()
    assert daily_purge_due(root, now + timedelta(days=1))
    assert maybe_daily_purge(root, now + timedelta(days=1)) == []
    assert not licensed_path(root, later.cycle_id).exists()
