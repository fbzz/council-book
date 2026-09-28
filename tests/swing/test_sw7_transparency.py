"""SW-7 private transparency (swing-book.md rev 2, §7.1): capture of every swing role's input, the
private swing record, the Scout's reading list (idea / cited / not_used), `council inputs --role
scout` and `council why <cycle> NVDA` printing the full chain."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from council.deliberation.capture import InputSink, write_cycle_inputs
from council.deliberation.reading import (
    scout_counts,
    scout_reading_list,
    swing_citations,
    swing_reads_from_inputs,
)
from council.operator import inputs_cli, why
from council.swing import trail
from council.swing.record import origin_texts, stage_of, swing_record
from tests.swing import public_fixture as F
from tests.swing import stubs as s

CAPTURED = datetime(2026, 9, 29, 14, 45, tzinfo=UTC)


@pytest.fixture(scope="module")
def captured(_core_policy):
    sink = InputSink(salt=lambda: "00" * 32)
    res, cats = F.run_slot(_core_policy, sink=sink)
    record = swing_record(res, catalysts=cats, live=True, accepted=[a.ref for a in res.entries()],
                          carried_from={"idea:2": [F.ORIGIN]})
    inputs, licensed = sink.build(cycle_id=F.CYCLE, captured_at=CAPTURED)
    return res, record, inputs, licensed, sink


# ------------------------------------------------------------------------------ capture
def test_every_swing_role_input_is_captured(captured):
    _, _, inputs, licensed, _ = captured
    roles = [c.role for c in inputs.calls]
    for role in ("scout", "skeptic", "swing_bull", "swing_bear", "swing_pm"):
        assert role in roles, role
    assert roles.count("skeptic") == 3 and roles.count("swing_pm") == 3
    # the Skeptic is blind: its captured input never holds the thesis
    for call in inputs.calls:
        if call.role == "skeptic":
            text = "".join(inputs.sections[k].items[0].text or "" for k in call.sections)
            assert s.THESIS not in text
    assert licensed is not None and inputs.licensed_items >= 1     # the N: item's section is held apart


def test_origin_texts_come_from_the_capture_and_fail_closed(captured, tmp_path):
    *_, sink = captured
    write_cycle_inputs(tmp_path, sink, cycle_id=F.CYCLE, captured_at=CAPTURED)
    got = origin_texts(tmp_path, [F.CYCLE, "2026-09-01T1440Z"])
    assert got["2026-09-01T1440Z"] is None                      # never captured: unverifiable
    assert got[F.CYCLE] and any(F.FEED_TITLE in t for t in got[F.CYCLE])


# ------------------------------------------------------------------------------ the record
def test_the_record_keeps_the_chain_but_no_feed_text(captured):
    _, record, *_ = captured
    by = {i["ticker"]: i for i in record["ideas"]}
    assert set(by) == {"ACME", "WIDG", "GLOBEX", "HOOLI"}
    assert by["GLOBEX"]["catalysts"] == [{"id": F.N_GLOBEX}]    # an N: catalyst: id only, no title
    assert F.FEED_TITLE not in repr(record)
    assert by["WIDG"]["facts"]["move_since_news_live_sigma"] == float(F.LIVE_VALUE)   # private: kept
    assert by["ACME"]["votes"]["enter"] == 3 and by["ACME"]["stage"] == "planned"
    assert by["WIDG"]["verdict"]["override"] == "skeptic_mostly_wait"
    assert "adv_usd_20d" not in by["ACME"]["facts"]


def test_a_canary_never_enters_the_record(captured):
    res, *_ = captured
    fake = type(res)(slot=res.slot, ideas=dict(res.ideas))
    first = next(iter(fake.ideas.values()))
    first.canary = True                                  # H11: code-side flag
    try:
        rec = swing_record(fake, catalysts={}, live=False)
        assert first.ticker not in [i["ticker"] for i in rec["ideas"]]
    finally:
        first.canary = False


def test_stage_of_covers_an_aborted_slot():
    class V:
        status, code = "pass", None
    assert stage_of(None, V(), paper_only=False, entered=False, accepted=False, rule_code=None, live=True) \
        == ("debate", "stage_aborted")
    assert stage_of(None, None, paper_only=False, entered=False, accepted=True, rule_code=None, live=False) \
        == ("risk", "swing_book_paper_only")


# ------------------------------------------------------------------------------ reading list
def test_the_scout_reading_list_has_one_disposition_per_item(captured):
    _, record, inputs, licensed, _ = captured
    items, read_by = swing_reads_from_inputs(inputs, licensed)
    ids = [i.id for i in items]
    assert s.P_ACME in ids and F.N_GLOBEX in ids and s.P_FED in ids and len(ids) == len(set(ids))
    readings = {r.id: r for r in scout_reading_list(items, read_by, swing_citations(record))}
    assert readings[s.P_ACME].disposition == "idea" and readings[s.P_ACME].used
    assert readings[F.N_GLOBEX].disposition == "idea"
    assert readings[s.P_FED].disposition == "not_used" and not readings[s.P_FED].used
    assert "scout" in read_by[s.P_ACME]
    c = scout_counts(list(readings.values()))
    assert c["read"] == len(readings) and c["idea"] + c["cited"] + c["not_used"] == c["read"]


def test_council_inputs_role_scout_prints_the_reading_list(captured):
    _, record, inputs, licensed, _ = captured
    readings = inputs_cli.scout_readings_for(inputs, licensed, {"extras": {"swing": record}})
    text = inputs_cli.render_text(inputs, licensed, role="scout", scout_readings=readings)
    assert "swing Scout reading list" in text and "=== scout:0:0" in text
    assert f"{s.P_ACME} " in text and "(idea)" in text and "(not_used)" in text
    assert inputs_cli.ETORO_MARK in text                        # the broker item is marked as eToro's


# ------------------------------------------------------------------------------ council why
def _ledger_with_swing(tmp_path, record):
    from council.ledger.db import LEDGER_FILE, Ledger
    from tests.fixtures import make_site_journal as msj

    rec_ideas = [dict(i) for i in record["ideas"]]
    rec_ideas[0] = {**rec_ideas[0], "ticker": "NVDA", "idea_id": "idea:nvda_1"}
    swing = {**record, "ideas": rec_ideas}
    spec = msj.Spec(F.CYCLE, "live", "calm", "reviewed_no_action", "none", 0.3, 2)
    rec, _ = msj.record(spec, msj.universe(), msj.PromptRegistry())
    rec.extras["swing"] = swing
    ledger = Ledger(tmp_path / LEDGER_FILE)
    ledger.record_cycle(rec)
    t = ledger.create_swing_trade("trade:nvda_1", ticker="NVDA", side="long", idea_id="idea:nvda_1",
                                  origin_cycle=F.CYCLE, sl_rate=170.0, tp_rate=208.0, time_stop_date="2026-10-14",
                                  detail={"size_nav": 0.08, "stop_pct": 0.06, "target_pct": 0.15})
    ledger.transition_swing_trade(t.trade_id, "entry_executing", reason="approved")
    ledger.transition_swing_trade(t.trade_id, "open", reason="filled")
    return ledger


def test_council_why_prints_the_full_chain_for_nvda(captured, tmp_path, monkeypatch):
    from council.operator import guards

    _, record, *_ = captured
    _ledger_with_swing(tmp_path, record)
    monkeypatch.setattr(guards, "assert_current_process_is_operator", lambda: None)
    lines: list[str] = []
    code = why.run_why(F.CYCLE, line="NVDA", state_dir=tmp_path, journal_dir=tmp_path / "journal", echo=lines.append)
    text = "\n".join(lines)
    assert code == 0, text
    text = text[text.index("swing chain for NVDA"):]         # NVDA is also a core line: its trail comes first
    order = ["swing chain for NVDA", "source: ledger", "scout: catalysts", "code gate · resolve: pass",
             "code gate · chase: pass", "skeptic (glm-5.3-flash:cloud): pass", "priced in partly",
             "debate · bull c1", "PM: enter 3 of 3", "S-rules final: pass", "engine book limits",
             "plan:", "fill: trade trade:nvda_1 state open", "trade trade:nvda_1 NVDA long",
             "proposed -> entry_executing", "entry_executing -> open"]
    pos = [text.find(w) for w in order]
    assert all(p >= 0 for p in pos), [w for w, p in zip(order, pos, strict=True) if p < 0]
    assert pos == sorted(pos), text
    for private in ("170.0", "208.0", F.LIVE_VALUE):
        assert private not in text


def test_why_stops_the_chain_where_the_idea_stopped(captured):
    _, record, *_ = captured
    lines = trail.ledger_lines({"extras": {"swing": record}}, "HOOLI")
    text = "\n".join(lines)
    assert "not reached (dropped at the Scout check: setup_paper_only)" in text and "skeptic" not in text
    widg = "\n".join(trail.ledger_lines({"extras": {"swing": record}}, "WIDG"))
    assert "said pass; code: skeptic_mostly_wait" in widg and "stopped at the Skeptic" in widg
    assert "PM:" not in widg
    assert trail.ledger_lines({"extras": {"swing": record}}, "MSFT") is None


def test_why_reads_the_revealed_public_section_outside_the_operator_terminal(captured, _core_policy, tmp_path):
    root = tmp_path / "pub"
    journal_dir = F.make_swing_journal(root, _core_policy)
    lines: list[str] = []
    code = why.run_why(F.SWING_CYCLE, line="ACME", source="journal", journal_dir=journal_dir, echo=lines.append)
    text = "\n".join(lines)
    assert code == 0 and "source: revealed public record" in text and "PM: enter 3 of 3" in text
