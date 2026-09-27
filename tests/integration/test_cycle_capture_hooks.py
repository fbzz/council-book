"""The cycle hooks of transparency-v2 T1 / T1v / T0, end to end on a stub cycle (no network):

- every model call's exact input is captured privately (0600 file, 0700 folders, under the state
  dir), each captured call's input hash is a ledger call's, and the capture verifies;
- the first cycle of a UTC day runs the licensed-content purge (a receipt), a later cycle does not;
- the final leak scan before a publish is armed with the pack's licensed texts and the private
  canaries (large NAV figures only), and the published material-change fingerprint is keyed by the
  install key.
"""

from __future__ import annotations

import json
import stat
from datetime import timedelta

import pytest

from council.cycle import NAV_CANARY_MIN, arm_leak_scan, publish_canaries, run_cycle
from council.deliberation.capture import calls_path, load_inputs, verify
from council.models.facts import NewsItem, broker_news_id
from council.publish import install_key
from council.publish.gitops import Publisher, PublishError
from council.publish.install_key import keyed_hex
from council.runtime import Sources
from tests.integration.test_end_to_end import NOW, SLOT, _ctx, _history, _no_events

FEED_TITLE = "Chipmakers rally as export curbs look set to ease for advanced parts next quarter"


def feed_item() -> NewsItem:
    at = SLOT - timedelta(hours=1)
    return NewsItem(id=broker_news_id("post-42", b"t" * 32), title=FEED_TITLE,
                    summary="A licensed feed summary that must never be published anywhere.",
                    symbols=["SMH"], published_at=at, available_at=at)


class RecordingPublisher:
    """A publisher that records what its final leak scan was armed with at each publish."""

    def __init__(self) -> None:
        self.canaries: list = ["BASE-CANARY-VALUE"]
        self.licensed_texts: list[str] = []
        self.seen: list[dict] = []
        self.files: dict[str, bytes] = {}

    def publish(self, files, message):
        from council.publish.gitops import PublishResult

        self.seen.append({"canaries": list(self.canaries), "licensed": list(self.licensed_texts)})
        self.files.update(files)
        return PublishResult(commit_sha=None, pushed=False, paths=tuple(files), dry_run=True)


def _run(tmp_path, *, publisher=None, news=None, clock=lambda: NOW):
    ctx = _ctx(tmp_path, publisher=publisher, clock=clock)
    ctx.sources = Sources(history=_history, events=_no_events,
                          news=(lambda slot: list(news)) if news is not None else None)
    return ctx, run_cycle(ctx)


def test_every_call_is_captured_privately_and_verifies(tmp_path):
    ctx, out = _run(tmp_path, news=[feed_item()])
    path = calls_path(ctx.state_dir, out.cycle_id)
    assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600
    folder = path.parent
    while folder != ctx.state_dir:
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
        folder = folder.parent
    inputs = load_inputs(ctx.state_dir, out.cycle_id)
    ledger_calls = ctx.ledger.role_calls(out.cycle_id)
    made = {c["input_hash"] for c in ledger_calls if c["status"] != "skipped"}
    assert made and made == {c.input_hash for c in inputs.calls}
    assert {c.role for c in inputs.calls} >= {"news", "bull_open", "bear", "bull_rebuttal", "pm"}
    problems, _ = verify(inputs, None, ledger_calls)
    assert problems == []                           # the licensed item's text is held apart ...
    assert inputs.licensed_items >= 1               # ... and counted, never in the main file
    assert FEED_TITLE not in path.read_bytes().decode("latin-1")
    record = ctx.ledger.get_cycle(out.cycle_id)
    assert not any(f.startswith("inputs_capture_error") for f in record["flags"])


def test_the_first_cycle_of_the_day_purges_and_a_later_one_does_not(tmp_path):
    ctx, out = _run(tmp_path, news=[feed_item()])
    receipts = sorted((ctx.state_dir / "purge-receipts").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["mode"] == "older_than" and not receipt["dry_run"]
    record = ctx.ledger.get_cycle(out.cycle_id)
    assert not any(f.startswith("purge_error") for f in record["flags"])
    later, _ = _run(tmp_path, news=[feed_item()], clock=lambda: NOW + timedelta(hours=4))
    assert len(list((later.state_dir / "purge-receipts").glob("*.json"))) == 1      # once a UTC day


def test_the_final_leak_scan_is_armed_and_the_fingerprint_keyed(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_LEAK_CANARIES", "PRIVATE-ENV-CANARY")
    pub = RecordingPublisher()
    ctx, out = _run(tmp_path, publisher=pub, news=[feed_item()])
    armed = pub.seen[0]
    assert "BASE-CANARY-VALUE" in armed["canaries"] and "PRIVATE-ENV-CANARY" in armed["canaries"]
    assert any(FEED_TITLE in text for text in armed["licensed"])
    cycle_files = [v for k, v in pub.files.items() if "/cycles/" in k and k.endswith(".json")]
    assert cycle_files                               # no proposal: revealed at once
    doc = json.loads(cycle_files[0])
    record = ctx.ledger.get_cycle(out.cycle_id)
    key = install_key.load(ctx.state_dir)
    body = doc.get("document", doc)
    assert body["material_fingerprint"] == keyed_hex(key, record["material_fingerprint"], 16)
    assert FEED_TITLE not in b"".join(pub.files.values()).decode()


def test_an_armed_publisher_refuses_licensed_text(tmp_path):
    pub = Publisher(tmp_path / "clone", push=False, dry_run_dir=tmp_path / "preview")
    arm_leak_scan(pub, licensed=[f"{FEED_TITLE} summary"], canaries=[])
    with pytest.raises(PublishError):
        pub.publish({"journal/ops/x.json": json.dumps({"note": FEED_TITLE}).encode()}, "test")
    arm_leak_scan(pub, licensed=[], canaries=[])     # re-arming replaces the cycle's lists
    assert pub.licensed_texts == [] and pub.canaries == []


def test_small_nav_figures_are_left_to_the_redaction_layer(tmp_path, monkeypatch):
    monkeypatch.delenv("COUNCIL_LEAK_CANARIES", raising=False)
    ctx = _ctx(tmp_path)

    class Snap:
        equity_usd = 1440.4          # would match every 14:40 cycle id

    assert publish_canaries(ctx, Snap()) == []
    Snap.equity_usd = NAV_CANARY_MIN + 2345.678
    assert publish_canaries(ctx, Snap()) == [round(NAV_CANARY_MIN + 2345.678, 2)]
    assert publish_canaries(ctx, None) == []


def test_a_council_timeout_keeps_the_inputs_of_the_calls_that_started(tmp_path, monkeypatch):
    import asyncio

    from council.context import hold_reference_stub
    from council.llm.stub import StubGateway

    class HangsOnBear(StubGateway):
        async def complete(self, *, role, **kw):
            if role == "bear":
                await asyncio.sleep(3600)
            return await super().complete(role=role, **kw)

    real_wait_for = asyncio.wait_for

    async def quick(awaitable, timeout):             # the cycle's council budget, shortened
        return await real_wait_for(awaitable, timeout=min(timeout, 1.0))

    monkeypatch.setattr(asyncio, "wait_for", quick)
    ctx = _ctx(tmp_path)
    ctx.gateway = HangsOnBear(hold_reference_stub())
    ctx.sources = Sources(history=_history, events=_no_events, news=lambda slot: [feed_item()])
    out = run_cycle(ctx)
    assert "council_timeout" in out.flags
    calls = {c.role: c for c in load_inputs(ctx.state_dir, out.cycle_id).calls}
    assert calls["bull_open"].status == "ok" and calls["bear"].status == "sent"   # sent, never answered
