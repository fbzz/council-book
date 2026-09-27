"""The watch republishes the public ops rows `ops review` / `resume-exec` queue (M5-B hand-off)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from council import watch
from council.cycle import run_cycle
from council.operator import approve
from council.publish.gitops import Publisher
from tests.integration.test_end_to_end import NOW, _ctx


def _setup(tmp_path):
    preview = tmp_path / "preview"
    pub = Publisher(tmp_path / "state" / "publisher-clone", push=False, dry_run_dir=preview)
    ctx = _ctx(tmp_path, publisher=pub)
    out = run_cycle(ctx)
    return ctx, preview, out.cycle_id


def _rows(preview):
    path = preview / "journal" / "ops" / "cycles.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_the_key_matches_the_operator_module():
    assert watch.OPS_ROWS_PENDING == approve.OPS_ROWS_PENDING


def test_queued_ops_row_is_republished_then_cleared(tmp_path):
    ctx, preview, cycle_id = _setup(tmp_path)
    d = SimpleNamespace(cycle_id=cycle_id)
    approve._queue_public_outcome(ctx.ledger, d, "blocked", "operator review: broker refused", NOW)
    assert ctx.ledger.get_runtime(approve.OPS_ROWS_PENDING) == [cycle_id]
    out = watch.run_watch(ctx)
    assert out.ops_rows_published == [cycle_id]
    rows = [r for r in _rows(preview) if r["cycle_id"] == cycle_id]
    assert len(rows) == 1 and rows[0]["decision_state"] == "blocked"
    assert ctx.ledger.get_runtime(approve.OPS_ROWS_PENDING) == []
    assert watch.run_watch(ctx).ops_rows_published == []          # nothing left to do


def test_failed_publish_keeps_the_queue(tmp_path, monkeypatch):
    ctx, _preview, cycle_id = _setup(tmp_path)
    approve._queue_public_outcome(ctx.ledger, SimpleNamespace(cycle_id=cycle_id), "blocked", "x", NOW)

    def boom(*_a, **_k):
        raise OSError("push failed")

    monkeypatch.setattr(ctx.publisher, "publish", boom)
    out = watch.run_watch(ctx)
    assert any(a.startswith("publish_error") for a in out.alerts)
    assert ctx.ledger.get_runtime(approve.OPS_ROWS_PENDING) == [cycle_id]


def test_unknown_or_bad_cycle_stays_queued_and_never_stops_the_watch(tmp_path, monkeypatch):
    ctx, _preview, cycle_id = _setup(tmp_path)
    ctx.ledger.set_runtime(approve.OPS_ROWS_PENDING, ["2026-01-01T0000Z", cycle_id])
    from council.publish import redact

    monkeypatch.setattr(redact, "public_ops_row", lambda rec: (_ for _ in ()).throw(ValueError("bad")))
    out = watch.run_watch(ctx)
    assert out.ops_rows_published == []
    assert "ops_row_unpublished" in out.urgent
    assert ctx.ledger.get_runtime(approve.OPS_ROWS_PENDING) == ["2026-01-01T0000Z", cycle_id]
