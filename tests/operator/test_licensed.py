"""M5-M acceptance (m5-readiness §6 LC2, LC3; run by CI job m5-acceptance through `readiness.ACCEPTANCE`).

LC2 — a stub cycle with a connected FAKE broker whose news feed carries canary text:
  - feed OFF (LC1 not attested, or the code ceiling off): zero feed requests, and no canary reaches
    any prompt (the stub gateway's log of every system and user message), the ledger, the private
    capture, a public file or a log;
  - feed ON (the user's decision: once a broker exists and the operator attests the licence, feed
    text may go to the model for personal use, never published): the canary reaches the news role's
    prompt and the private copy under `state_dir/licensed/` only — never the ledger, the main
    capture, a public file or a log.
LC3 — the 7-day licensed retention sweep: payloads older than the daily cut-off go, younger ones
stay, the ledger copy is scrubbed and the backups are refreshed (licensed-free); dry run changes
nothing. No network, no LLM, no broker: the stub gateway and FakeEtoro behind the real READ client.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from council import invariants
from council.cycle import run_cycle
from council.ledger.db import LEDGER_FILE
from council.operator import licensed
from council.operator.purge import RECEIPTS_DIR, purge_licensed
from council.ops import backup
from council.publish.gitops import Publisher
from tests.integration import test_news_wiring as news_wiring
from tests.integration.test_end_to_end import SLOT
from tests.integration.test_news_wiring import FEED_PATH, _cycle_ctx, _read_client, feed_entry

from .conftest import CANARY_TITLE, capture_cycle

fake_etoro = news_wiring.fake_etoro        # the connected FakeEtoro account, reused as a fixture


def _canary() -> str:
    """A fresh, word-like canary per test run (built at runtime, never a literal)."""
    word = "".join(secrets.choice(string.ascii_lowercase) for _ in range(10))
    return f"Quillbeak{word}"


def _texts(root: Path, *, skip: Path | None = None) -> dict[str, str]:
    """Every file under `root` (gunzipped when gzip) as text, except the `skip` tree."""
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or (skip is not None and (path == skip or skip in path.parents)):
            continue
        data = path.read_bytes()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        out[path.relative_to(root).as_posix()] = data.decode("utf-8", "replace")
    return out


def _ledger_strings(state: Path) -> list[str]:
    return list(licensed._sqlite_strings(state / LEDGER_FILE))


def _feed_cycle(tmp_path, fake_etoro, caplog, capsys, *, canary: str, licensed_on: bool):
    fake, fclock = fake_etoro
    fake.news = [feed_entry("post-lc2", f"{canary} index futures edge higher before the open",
                            SLOT - timedelta(hours=1))]
    for inst in fake.instruments.values():        # a rates canary: odd digits no public source would carry
        bump = 1 + (1000 + secrets.randbelow(9000)) / 1e6
        inst.bid, inst.ask = round(inst.bid * bump, 7), round(inst.ask * bump, 7)
    ctx = _cycle_ctx(tmp_path, broker=_read_client(fake, fclock), licensed=licensed_on)
    preview = tmp_path / "preview"
    ctx.publisher = Publisher(tmp_path / "state" / "publisher-clone", push=False, dry_run_dir=preview)
    with caplog.at_level(logging.DEBUG):
        out = run_cycle(ctx)
    captured = capsys.readouterr()
    logs = caplog.text + captured.out + captured.err
    return fake, ctx, out, preview, logs


def _assert_no_broker_rates_in_prompts(fake, ctx) -> None:
    """LC2: broker market data (rates) never enters a prompt, feed on or off."""
    assert fake.count("GET", "/api/v2/market-data/rates") >= 1                  # the canary was served
    marks = {f"{v:.4f}" for inst in fake.instruments.values() for v in (inst.bid, inst.ask) if v >= 1}
    prompts = "\n".join(c.system + "\n" + c.user for c in ctx.gateway.log)
    assert marks and not sorted(m for m in marks if m in prompts)


def _assert_nowhere_public(tmp_path: Path, ctx, preview: Path, logs: str, canary: str) -> None:
    state = ctx.state_dir
    assert canary not in logs                                                    # logs
    assert not any(canary in s for s in _ledger_strings(state))                  # ledger, every table
    private = _texts(state, skip=state / licensed.LICENSED_DIR)
    assert not [p for p, t in private.items() if canary in t]                    # capture, ledger file, logs
    public = _texts(preview)
    assert public and not [p for p, t in public.items() if canary in t]          # the published record


# ------------------------------------------------------------------------------------------ LC2
@pytest.mark.parametrize("why_off", ["licence_unattested", "code_ceiling_off"])
def test_lc2_feed_off_no_feed_text_in_any_prompt_ledger_file_or_log(tmp_path, fake_etoro, caplog, capsys,
                                                                    monkeypatch, why_off):
    canary = _canary()
    if why_off == "code_ceiling_off":
        monkeypatch.setattr(invariants, "BROKER_FEED_ENABLED", False)
    fake, ctx, out, preview, logs = _feed_cycle(tmp_path, fake_etoro, caplog, capsys, canary=canary,
                                                licensed_on=why_off == "code_ceiling_off")
    assert out.cycle_id
    assert fake.count("GET", FEED_PATH) == 0                                     # never requested
    assert "news_broker_feed:off" in ctx.ledger.get_cycle(out.cycle_id)["flags"]
    prompts = [c.system + "\n" + c.user for c in ctx.gateway.log]
    assert prompts and not [p for p in prompts if canary in p]                   # no prompt at all
    _assert_no_broker_rates_in_prompts(fake, ctx)
    _assert_nowhere_public(tmp_path, ctx, preview, logs, canary)
    assert not [p for p, t in _texts(ctx.state_dir).items() if canary in t]      # not even licensed/


def test_lc2_feed_on_text_reaches_the_prompt_and_licensed_only(tmp_path, fake_etoro, caplog, capsys):
    canary = _canary()
    fake, ctx, out, preview, logs = _feed_cycle(tmp_path, fake_etoro, caplog, capsys, canary=canary,
                                                licensed_on=True)
    assert fake.count("GET", FEED_PATH) == 1
    hits = {c.role for c in ctx.gateway.log if canary in c.system + "\n" + c.user}
    assert "news" in hits                                                        # personal use: the model reads it
    _assert_no_broker_rates_in_prompts(fake, ctx)
    for call in ctx.gateway.log:                                                 # ... only in the news sections
        section = ""
        for line in (call.system + "\n" + call.user).splitlines():
            if line and not line.startswith(" "):
                section = line
            if canary in line:
                assert section.startswith(("NEWS HEADLINES", "NEWS DETAIL")) and re.match(r"^\s+N:[0-9a-f]{8} ", line), \
                    (call.role, section, line)
    held = [p for p, t in _texts(ctx.state_dir).items() if canary in t]
    assert held and all(p.startswith(f"{licensed.LICENSED_DIR}/") for p in held)  # the private copy: licensed/ only
    _assert_nowhere_public(tmp_path, ctx, preview, logs, canary)


# ------------------------------------------------------------------------------------------ LC3
def _aged(path: Path, days: float, now: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"title": "licensed payload"}')
    t = now.timestamp() - days * 86400
    os.utime(path, (t, t))
    return path


def test_lc3_seven_day_sweep_removes_old_payloads_keeps_young_and_refreshes_backups(state):
    capture_cycle(state, canary=True)                   # licensed/calls at SLOT; the ledger copies the canary
    first = backup.backup_ledger(state, now=SLOT, licensed_check=licensed.backup_check(state))
    assert first is not None and first.licensed_free is False                     # the copy is caught
    now = SLOT + timedelta(days=8)
    lic = state / licensed.LICENSED_DIR
    old = [_aged(lic / "fixtures" / "old.json", 7.5, now), _aged(lic / "feed" / "edge.json", 6.5, now)]
    young = [_aged(lic / "feed" / "young.json", 2, now), _aged(lic / "fixtures" / "recent.json", 5, now)]

    dry = purge_licensed(state, now=now, older_than_days=6, dry_run=True)
    assert dry.errors == [] and all(p.exists() for p in old + young)             # dry run: counts only
    assert any(CANARY_TITLE.lower() in t.lower() for t in _texts(state).values())

    assert licensed.sweep(state, now) == []
    assert not any(p.exists() for p in old)                                      # nothing outlives 7 days
    assert all(p.exists() for p in young)
    assert not list((lic / "calls").rglob("*.json.gz"))                         # the 8-day-old capture text
    assert not any(CANARY_TITLE.lower() in s.lower() for s in _ledger_strings(state))   # ledger scrubbed
    kept = sorted(p for p in backup.backup_dir(state).rglob("*") if p.is_file())
    assert [p.name for p in kept] == [backup.backup_name(now)]                   # stale backup replaced
    assert licensed.backup_check(state)(kept[0])                                 # the fresh one is clean
    assert not [p for p, t in _texts(state).items() if CANARY_TITLE.lower() in t.lower()]
    assert list((state / RECEIPTS_DIR).glob("*.json"))
    assert licensed.sweep(state, now + timedelta(hours=1)) == []                 # once per UTC day
    assert all(p.exists() for p in young)
