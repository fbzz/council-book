"""SW-7b: the public swing record wired into the cycle (swing-book.md rev 2, §7.2-§7.4): the private
record in `rec.extras["swing"]`, the sealed swing section, the swing page's document, the swing
roles' calls on the run page, the Skeptic-health record, closed_cycle from the watch, and the
public codes / site words of the swing flags."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish import leakscan, trace_rules
from council.publish.redact import public_flags
from council.swing import sources as ss

SWING_ROLES = {"scout", "skeptic", "swing_bull", "swing_bear", "swing_pm"}


def _load_site():
    spec = importlib.util.spec_from_file_location("council_site_build_sw7b", REPO_ROOT / "site" / "build.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build_sw7b"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site():
    return _load_site()


# ------------------------------------------------------------------------------ end to end
def _doc(preview, cycle_id):
    from council.publish import journal

    return json.loads((preview / journal.cycle_path(cycle_id)).read_text())


@pytest.fixture(scope="module")
def swing_run(tmp_path_factory):
    """One stubbed cycle at a swing slot with the fixture sources and a funded paper account, sealed
    and published to a dry-run preview (no broker: the reveal is published at once)."""
    from council.cycle import run_cycle
    from council.publish.gitops import Publisher
    from tests.integration import test_end_to_end as e2e

    tmp = tmp_path_factory.mktemp("sw7b")
    preview = tmp / "preview"
    ctx = e2e._ctx(tmp, publisher=Publisher(tmp / "state" / "publisher-clone", push=False, dry_run_dir=preview))
    (ctx.state_dir / "account").mkdir(exist_ok=True)
    (ctx.state_dir / "account" / "swing.json").write_text(json.dumps({"funded_real_nav_usd": 2000.0}))
    ctx.sources.swing = ss.fixture_swing_sources(skeptic_gateway=ss.fixture_skeptic_gateway(ctx.policy))
    out = run_cycle(ctx)
    return ctx, out, preview


def test_the_cycle_keeps_the_private_swing_record(swing_run):
    ctx, out, _ = swing_run
    rec = ctx.ledger.get_cycle(out.cycle_id)
    record = rec["extras"]["swing"]
    assert record["schema"] == "council-book/private-swing/v1" and record["live"] is False
    (idea,) = record["ideas"]
    assert idea["ticker"] == ss.FIXTURE_TICKER and idea["idea_id"].startswith("idea:")
    assert idea["stage"] == "risk" and idea["drop_code"] == "swing_book_paper_only"     # accepted, paper-only
    assert idea["carried_from"] == []
    assert not [f for f in rec["flags"] if f.startswith(("swing_error", "swing_record_error"))], rec["flags"]
    # the drop code is persisted on the idea row and on its paper row (codes only)
    assert ctx.ledger.swing_idea(idea["idea_id"])["record"]["drop_code"] == "swing_book_paper_only"
    papers = [p for p in ctx.ledger.paper_trades() if p["origin_cycle"] == out.cycle_id]
    assert papers and all(p["record"]["drop_code"] == "swing_book_paper_only" for p in papers)


def test_the_swing_roles_calls_join_the_cycle_calls_once(swing_run):
    ctx, out, preview = swing_run
    rec = ctx.ledger.get_cycle(out.cycle_id)
    assert {c["role"] for c in rec["calls"]} >= SWING_ROLES
    stored = [c["role"] for c in ctx.ledger.role_calls(out.cycle_id)]
    assert len(stored) == len(rec["calls"])                    # recorded once, not twice
    doc = _doc(preview, out.cycle_id)
    assert {c["role"] for c in doc["calls"]} >= SWING_ROLES


def test_the_swing_inputs_are_captured_with_the_core(swing_run):
    from council.deliberation.capture import load_inputs

    ctx, out, _ = swing_run
    roles = {c.role for c in load_inputs(ctx.state_dir, out.cycle_id).calls}
    assert roles >= SWING_ROLES, roles


def test_the_sealed_cycle_carries_the_swing_section_and_is_clean(swing_run):
    from tests.integration import test_end_to_end as e2e

    ctx, out, preview = swing_run
    doc = _doc(preview, out.cycle_id)
    sec = doc["swing"]
    assert sec["live"] is False and [i["ticker"] for i in sec["ideas"]] == [ss.FIXTURE_TICKER.replace(".", "_")]
    assert sec["health"]["canary_last"] == "none_yet"
    assert "swing_licensed_texts_unavailable" not in sec.get("flags", [])
    e2e._assert_clean(preview)


def test_the_swing_page_document_is_published(swing_run):
    ctx, out, preview = swing_run
    book = json.loads((preview / "journal" / "swing" / "latest.json").read_text())
    assert book["live"] is False and book.get("live_since") is None
    assert book["paper_since"] == "2026-10-01"
    groups = {g["group"]: g["ideas"] for g in book["funnel"]}
    assert groups["missed"] == 1 and sum(groups.values()) == 1      # the would-have-executed paper row
    assert book["health"]["pass_share_20_pct"] is not None          # the Skeptic's own verdicts count


def test_the_site_builds_with_the_published_swing_record(swing_run, site, tmp_path):
    ctx, out, preview = swing_run
    dest = tmp_path / "site"
    site.build(preview / "journal", PROMPTS_DIR, POLICY_DIR, dest, now=datetime(2026, 10, 1, 16, 0, tzinfo=UTC))
    run = (dest / "cycles" / f"{out.cycle_id}.html").read_text()
    assert 'id="swing-ideas"' in run
    assert (dest / "swing" / "index.html").exists()
    assert "swing_paper_assumed_book</code> (paper run: the swing rules assumed a flat book" in run
    assert leakscan.scan_paths([dest]) == []


def test_skeptic_health_is_kept_in_the_runtime_record(swing_run):
    from council.cycle import SKEPTIC_HEALTH_KEY, record_canary_grade, swing_health

    ctx, _, _ = swing_run
    state = ctx.ledger.get_runtime(SKEPTIC_HEALTH_KEY)
    assert state["verdicts"] and set(state["verdicts"]) <= {"pass", "wait", "reject"}
    record_canary_grade(ctx.ledger, "missed", datetime(2026, 10, 5, 10, 45, tzinfo=UTC))
    record_canary_grade(ctx.ledger, "bogus", datetime(2026, 10, 5, 10, 46, tzinfo=UTC))
    h = swing_health(ctx.ledger)
    assert h.canary_last == "missed" and h.canaries_missed_total == 1


# ------------------------------------------------------------------------------ canary hook
def test_the_weekly_canary_is_graded_and_kept_private(policy, tmp_path):
    from council.cycle import SKEPTIC_HEALTH_KEY, run_swing
    from tests.swing.test_sw5c_sources import ctx_for

    monday = datetime(2026, 10, 5, 10, 40, tzinfo=UTC)
    src = ss.fixture_swing_sources(skeptic_gateway=ss.fixture_skeptic_gateway(policy))
    asked: list[datetime] = []

    def no_event(slot):
        asked.append(slot)

    src.canary_event = no_event
    c = ctx_for(policy, tmp_path, src)
    out = asyncio.run(run_swing(c, SimpleNamespace(cycle_id="2026-10-05T1040Z", extras={}), snapshot=None,
                                kill_state="NORMAL", nav=None, slot=monday, now=monday))
    assert asked == [monday] and "swing_canary_no_event" in out.flags
    assert not out.calls and not out.private_calls
    assert c.ledger.get_runtime(SKEPTIC_HEALTH_KEY) is None
    tuesday = monday + timedelta(days=1)
    asyncio.run(run_swing(c, SimpleNamespace(cycle_id="2026-10-06T1040Z", extras={}), snapshot=None,
                          kill_state="NORMAL", nav=None, slot=tuesday, now=tuesday))
    assert asked == [monday]                                # not due: never asked


# ------------------------------------------------------------------------------ watch
def test_the_watch_writes_closed_cycle_and_days_held(tmp_path, policy, monkeypatch):
    from council import watch
    from council.broker.fake import FakeClock
    from council.ledger.db import Ledger
    from tests.swing import test_sw5b_watch as w

    fclock = FakeClock()
    ledger = Ledger(tmp_path / "ledger.sqlite3", clock=fclock.now)
    ledger.set_runtime("last_cycle", {"cycle_id": "2026-10-01T1840Z", "at": "x"})
    monkeypatch.setattr(watch, "closed_trade_route_ok", lambda state_dir: True)
    w.trade(ledger, "trade:l", 1)
    c = w.ctx(tmp_path, ledger, policy, w.Read({1: {"positionId": 1, "closeRate": 110.0}}))
    now = fclock.now()
    watch._stop_hits(c, SimpleNamespace(positions=[w.pos(1, close=109.0)]), now)
    watch._stop_hits(c, SimpleNamespace(positions=[]), now + timedelta(minutes=15))
    d = ledger.swing_trade("trade:l").detail
    assert d["closed_cycle"] == "2026-10-01T1840Z" and isinstance(d["days_held"], int)


# ------------------------------------------------------------------------------ codes and words
SWING_FLAGS = [
    "swing_source_unavailable:alpaca", "swing_source_error:alpaca:RuntimeError", "swing_eligibility_unverified",
    "swing_paper_assumed_book", "paper_reference_last_close", "swing_screen_missing",
    "swing_drop:reproposal_limit", "swing_book_not_live",
    "day2_catalyst_gone", "day2_superseded", "swing_wide:3", "llm_billing_error", "news_source_backoff:rss:reuters_markets",
]


def test_every_swing_flag_has_a_public_code_and_site_words(site):
    assert set(site.SWING_FLAG_WORDS) == trace_rules.SWING_FLAG_CODES
    for flag in SWING_FLAGS:
        key = trace_rules.swing_flag_key(flag)
        assert key in trace_rules.SWING_FLAG_CODES, flag
        assert site.flag_words(flag), flag
    assert site.flag_words("expired:2") == ""
    assert site.SKIP_WORDS["swing_book_not_live"] and site.DROP_WORDS["reproposal_limit"]
    for code in ("day2_unconfirmed", "net_rr_below_min", "S6:net_rr_below_min", "skeptic_wait_debated"):
        assert site.DROP_WORDS[code], code
    assert site.GROUP_WORDS["skeptic_wait_debated"] and site.SETUP_WORDS["day2_confirmation"]
    assert site.flag_words("swing_wide:nope!") == ""


def test_swing_source_flags_publish_plain_words_only():
    assert public_flags(SWING_FLAGS) == SWING_FLAGS
    assert public_flags(["swing_source_error:membership S&P 500:HTTP Error", "swing_source_unavailable:/Users/x"]) == [
        "swing_source_error:other:other", "swing_source_unavailable:other"]
