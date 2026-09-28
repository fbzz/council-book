"""SW-7 acceptance (swing-book.md rev 2, §7.2 / §7.3 / §9 SW-7): the swing public record.

- sealed fixtures re-serialise byte-identically (the swing field is omitted while empty);
- three-NAV test: $1.5k below the fee reference with a $2k peak, $2k and $20k -> identical public JSON;
- no price, unit, amount, position / instrument id, fee, NAV or live-layer value in any public field;
- an `N:` catalyst renders id-only; a carried-forward idea quoting its origin cycle's feed text (or
  whose origin texts were purged) is withheld; a reason citing a live-layer id loses its numbers.
"""

from __future__ import annotations

import json

import pytest

from council.paths import REPO_ROOT
from council.publish import commit_reveal, leakscan, redact
from council.publish.public_models import (
    PublicCycleV1,
    PublicSwingBook,
    PublicSwingCatalyst,
    PublicSwingSection,
)
from tests.swing import public_fixture as F

SEALED = REPO_ROOT / "tests" / "fixtures" / "site_journal" / "journal" / "cycles"


@pytest.fixture(scope="module")
def record(_core_policy):
    return F.slot_record(_core_policy)


def _section(record, nav=2000.0, peak=2000.0, texts=None) -> PublicSwingSection:
    return redact.public_swing_section(record, cycle_id=F.CYCLE, origin_texts=texts or F.texts(),
                                       trades=F.trade_rows(nav, peak), health=F.health())


def _canaries(nav: float, peak: float) -> list[str | float]:
    out: list[str | float] = [F.LIVE_VALUE, F.FEED_TITLE, nav, peak]
    for t in F.trade_rows(nav, peak):
        out += [t.open_rate, t.sl_rate, t.tp_rate, str(t.instrument_id), *map(str, t.position_ids),
                t.units, t.detail["amount_usd"], t.detail["fee_usd"], t.detail["actual_cost_pct"]]
        if t.close_rate:
            out.append(t.close_rate)
    return [c for c in out if not isinstance(c, float) or c >= 10.0]    # small values collide with percentages


# ------------------------------------------------------------------------------ sealed bytes
def test_sealed_fixtures_are_byte_identical():
    files = [p for p in SEALED.rglob("*.json") if not p.name.endswith(".reveal.json")]
    assert files
    for path in files:
        raw = path.read_bytes()
        doc = PublicCycleV1.model_validate_json(raw)
        assert doc.swing is None
        assert commit_reveal.canonical_json(doc) == raw, path.name


def test_a_swing_cycle_seals_and_round_trips(record):
    doc = PublicCycleV1.model_validate_json(
        (SEALED / "2026" / "09" / "2026-09-25T0640Z.json").read_bytes()).model_copy(update={"swing": _section(record)})
    sealed = commit_reveal.canonical_json(doc)
    assert commit_reveal.canonical_json(PublicCycleV1.model_validate_json(sealed)) == sealed
    assert b'"swing":' in sealed


# ------------------------------------------------------------------------------ NAV invariance
def test_three_nav_test_gives_identical_public_json(record):
    docs = []
    for nav, peak in F.NAVS.values():
        sec = _section(record, nav, peak)
        book = F.swing_book_doc(nav, peak)
        docs.append((sec.model_dump_json(), book.model_dump_json()))
    assert docs[0] == docs[1] == docs[2]
    assert json.loads(docs[0][1])["closed_trades"]            # the documents are not trivially empty


def test_public_cycle_carries_the_swing_part_nav_invariantly(record, _core_policy):
    from council.publish.public_models import PublicCycleV1 as Doc
    from tests.fixtures import make_site_journal as msj

    uni = msj.universe()
    out = []
    for nav, peak in F.NAVS.values():
        spec = msj.Spec(F.SWING_CYCLE, "live", "calm", "reviewed_no_action", "none", 0.3, 2)
        rec, pk = msj.record(spec, uni, msj.PromptRegistry())
        rec.extras["swing"] = record
        texts = {F.SWING_CYCLE: [F.FEED_TITLE], F.ORIGIN: [F.ORIGIN_FEED]}
        doc = redact.public_cycle(rec, pk, lines=uni, swing_texts=texts, swing_trades=F.trade_rows(nav, peak),
                                  swing_health=F.health())
        assert isinstance(doc, Doc) and doc.swing is not None and len(doc.swing.ideas) == 4
        out.append(doc.swing.model_dump_json())
    assert out[0] == out[1] == out[2]


# ------------------------------------------------------------------------------ nothing private
@pytest.mark.parametrize("which", list(F.NAVS))
def test_no_price_unit_amount_id_or_live_value_in_any_public_field(record, which):
    nav, peak = F.NAVS[which]
    sec, book = _section(record, nav, peak), F.swing_book_doc(nav, peak)
    for doc in (sec, book):
        findings = leakscan.scan(doc, canaries=_canaries(nav, peak), licensed_texts=[F.FEED_TITLE, F.ORIGIN_FEED])
        assert findings == [], findings
    text = sec.model_dump_json() + book.model_dump_json()
    for word in ("open_rate", "sl_rate", "units", "position_ids", "instrument_id", "amount", "fee_usd", "nav_usd"):
        assert word not in text


def test_the_live_layer_is_withheld_as_broker_data(record):
    widg = _section(record).ideas[1]
    assert widg.facts_withheld["move_since_news_live_sigma"] == "broker_data"
    assert not any("live" in k for k in widg.facts)


def test_a_reason_citing_the_live_layer_loses_its_numbers(record):
    reason = _section(record).ideas[1].verdict.reasons[0]
    assert "X:WIDG:move_since_news_live_sigma" in reason.evidence
    assert F.LIVE_VALUE not in reason.text and "[value removed]" in reason.text
    untouched = _section(record).ideas[0].verdict.reasons[0]
    assert "[value removed]" not in untouched.text


def test_alpaca_fields_are_unknown_source_until_the_row_is_widened(record):
    acme = _section(record).ideas[0]
    assert acme.facts_withheld["move_since_news_close_sigma"] == "unknown_source"
    widened = redact.public_swing_section(record, cycle_id=F.CYCLE, origin_texts=F.texts(), alpaca_public=True)
    assert widened.ideas[0].facts["move_since_news_close_sigma"] == 0.8
    assert widened.ideas[0].facts["vol_ratio_since"] == "1-2"                # a bucket, never the ratio


# ------------------------------------------------------------------------------ catalysts and carry
def test_an_n_catalyst_renders_id_only(record):
    globex = _section(record).ideas[2]
    [cat] = globex.catalysts
    assert cat.kind == "broker_feed" and cat.id == F.N_GLOBEX
    assert cat.model_dump() == {"id": F.N_GLOBEX, "kind": "broker_feed"}
    with pytest.raises(ValueError):
        PublicSwingCatalyst(id=F.N_GLOBEX, kind="broker_feed", title="a feed headline")
    acme = _section(record).ideas[0].catalysts[0]
    assert acme.kind == "public_news" and acme.title and acme.link.startswith("https://www.sec.gov/")


def test_a_carried_forward_idea_quoting_its_origin_feed_text_is_withheld(_core_policy):
    quoting = F.slot_record(_core_policy, quoting=True)
    sec = redact.public_swing_section(quoting, cycle_id=F.CYCLE, origin_texts=F.texts())
    widg = sec.ideas[1]
    assert widg.carried_from == [F.ORIGIN] and widg.text_withheld
    assert widg.thesis == redact.WITHHELD_LICENSED
    assert leakscan.scan_origins(sec, F.texts(), [F.ORIGIN]) == []
    # scanned against this cycle's texts only, the quote would have passed: the origin matters
    assert "data center" in quoting["ideas"][1]["thesis"]
    assert leakscan.scan(quoting["ideas"][1]["thesis"], licensed_texts=[F.FEED_TITLE]) == []
    assert any(f.startswith("swing_text_withheld:") for f in sec.flags)


def test_an_unreadable_origin_withholds_the_carried_text(record):
    sec = redact.public_swing_section(record, cycle_id=F.CYCLE, origin_texts=F.texts(origin_ok=False))
    assert sec.ideas[1].text_withheld and sec.ideas[1].thesis == redact.SWING_WITHHELD
    assert sec.ideas[0].thesis.startswith("Zebra-quill")                  # other ideas are unaffected
    assert leakscan.scan_origins("x", F.texts(origin_ok=False), [F.ORIGIN])[0].rule == "origin_unavailable"
    with pytest.raises(leakscan.OriginTextsUnavailable):
        leakscan.origin_matcher(F.texts(origin_ok=False), [F.ORIGIN])


def test_missing_own_feed_texts_withhold_every_swing_text(record):
    sec = redact.public_swing_section(record, cycle_id=F.CYCLE, origin_texts={})
    assert "swing_licensed_texts_unavailable" in sec.flags
    assert all(i.thesis == redact.SWING_WITHHELD for i in sec.ideas)


# ------------------------------------------------------------------------------ stages and book
def test_every_idea_is_shown_with_its_stage(record):
    sec = _section(record)
    assert [(i.ticker, i.stage_reached, i.drop_code, i.live_setup) for i in sec.ideas] == [
        ("ACME", "planned", None, True), ("WIDG", "waiting", "skeptic_wait", True),
        ("GLOBEX", "skeptic", "skeptic_reject", True), ("HOOLI", "dropped_by_code", "setup_paper_only", False)]
    assert sec.ideas[0].votes.enter == 3 and sec.ideas[1].verdict.said == "pass"
    assert sec.ideas[1].verdict.code_override == "skeptic_mostly_wait"
    assert sec.ideas[0].verdict.model_family == "glm" and not sec.ideas[0].verdict.same_family


def test_trades_are_percent_only_and_net_of_the_declared_cost(record):
    book = F.swing_book_doc()
    assert isinstance(book, PublicSwingBook)
    won = next(t for t in book.closed_trades if t.trade_id == "trade:won_1")
    gross = 100 * (65.77 / 57.19 - 1)
    assert won.net_declared_pct == pytest.approx(gross - 2.5, abs=0.01)
    assert won.r_declared == pytest.approx((gross - 2.5) / 5.0, abs=0.001)
    assert won.contribution_declared_bp == pytest.approx(0.08 * (gross - 2.5) * 100, abs=0.1)
    assert [t.state for t in book.open_trades] == ["open"] and book.open_trades[0].weight_x == 0.08
    assert [g.group for g in book.funnel] == ["executed", "pm_passed", "skeptic_rejected", "skeptic_wait",
                                               "code_dropped", "paper_only", "missed"]
    wait = next(g for g in book.funnel if g.group == "skeptic_wait")
    assert wait.closed == 2 and wait.r_declared.n == 2
    assert book.metrics.n_closed == 2 and book.benchmarks[0].sq8 is not None
    assert book.health.canary_last == "caught" and book.health.canaries_missed_total == 1


def test_the_book_section_of_a_cycle_holds_open_trades_only(record):
    assert [t.trade_id for t in _section(record).trades] == ["trade:open_1"]
