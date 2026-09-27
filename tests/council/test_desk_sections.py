"""Structured desk (transparency-v2 §2.1, T1): the model input stays BYTE-IDENTICAL.

Three layers of proof over ten golden fixture packs (tests/council/golden/cases.py):
  1. every rendering of the live structured builders equals the frozen pre-refactor renderer
     (`golden/legacy.py`, a verbatim copy of the old code) byte for byte;
  2. the same renderings hash to the digests stored in `golden/renderings.json`, generated from
     the pre-refactor code before any line of it changed;
  3. an end-to-end council run on the stub gateway sends every model the same system prompt and
     user message (digests per call, in call order) as the pre-refactor code did.
Plus: the literal registry (every literal run a reviewer has read, each passing `literal_ok`),
the final scrub is a no-op, and the opaque fallback when a currency sign straddles two pieces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from council.deliberation import desk as live
from council.deliberation.capture import InputSink
from council.deliberation.debate import case_section, format_case, transcript, transcript_section
from council.deliberation.segments import (
    SCRUB_CROSSED_FLAG,
    Segmented,
    TextBuilder,
    joined,
    literal,
    strictest,
)
from council.llm.prompts import PromptRegistry
from council.llm.sanitize import scrub_amounts
from council.publish import redact

from .golden import legacy
from .golden.cases import CASE_NAMES, build
from .golden.make_golden import (
    GOLDEN,
    code_cards,
    debate_cases,
    desk_inputs,
    legacy_renderings,
    live_calls,
    sha,
)

LITERALS = Path(__file__).parent / "golden" / "literals.txt"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN.read_text())


def _live_renderings(case, policy) -> dict[str, str]:
    """The live code's version of every key in `legacy_renderings`."""
    cc = code_cards(case, policy)
    inputs = desk_inputs(case, policy)
    code = live.desk_pack(cards=cc, **inputs)
    full = live.desk_pack(cards=cc + case.extra_cards, **inputs)
    out = {
        "desk.code": code,
        "desk.full": full,
        "desk.nocards": live.desk_pack(cards=cc, include_cards=False, **inputs),
        "news_detail": live.news_detail(case.pack),
        "news_detail.5": live.news_detail(case.pack, max_items=5),
    }
    # the user messages, assembled from sections exactly as the roles do
    code_s = live.desk_sections(cards=cc, variant="code", **inputs)
    full_s = live.desk_sections(cards=cc + case.extra_cards, variant="full", **inputs)
    out["user.news"] = joined([*code_s, literal("sep.news", "\n"), live.news_detail_section(case.pack),
                               literal("tail.news", "\nWrite the news cards now. Reply with the JSON object only.")])
    out["user.macro"] = joined([*code_s, literal("t", "\nDescribe the regime now. Reply with the JSON object only.")])
    out["user.bull_open"] = joined([*full_s, literal("t", "\nOpen the debate with the bull case. Reply with the JSON object only.")])
    out["user.single_agent"] = joined([*code_s, literal("t", "\nDecide. Reply with the JSON object only.")])
    for name, deb in debate_cases().items():
        bull = case_section("BULL opening", deb.bull_open, key="case.bull_open.plain", speaker="bull_open")
        bear = case_section("BEAR reply", deb.bear, key="case.bear.plain", speaker="bear")
        out[f"transcript.{name}"] = transcript(deb)
        out[f"user.bear.{name}"] = joined([*full_s, literal("l", "\nBULL OPENING\n"), bull,
                                           literal("t", "\n\nReply with the bear case. Reply with the JSON object only.")])
        out[f"user.bull_rebuttal.{name}"] = joined([
            *full_s, literal("l", "\nYOUR OPENING\n"), bull, literal("l", "\n\nBEAR REPLY\n"), bear,
            literal("t", "\n\nAnswer the bear. Reply with the JSON object only.")])
        out[f"user.pm.{name}"] = joined([*full_s, literal("s", "\n"), transcript_section(deb),
                                         literal("t", "\nDecide. Reply with the JSON object only.")])
        out[f"case.bull.{name}"] = format_case("BULL opening", deb.bull_open, prefix="bull_open:")
        out[f"case.bear.{name}"] = format_case("BEAR reply", deb.bear, prefix="bear:", rebut_prefix="bull_open:")
    return out


@pytest.mark.parametrize("name", CASE_NAMES)
def test_every_rendering_is_byte_identical_to_the_frozen_renderer(name, policy, sleeve_policy):
    case = build(name)
    pol = case.policy(policy, sleeve_policy)
    old = legacy_renderings(case, pol)
    new = _live_renderings(case, pol)
    assert new.keys() == old.keys()
    for key in old:
        assert new[key] == old[key], f"{name}: {key} changed"


@pytest.mark.parametrize("name", CASE_NAMES)
def test_renderings_match_the_digests_taken_before_the_refactor(name, policy, sleeve_policy, golden):
    case = build(name)
    pol = case.policy(policy, sleeve_policy)
    digests = {k: sha(v) for k, v in _live_renderings(case, pol).items()}
    assert digests == golden[name]["renderings"]


@pytest.mark.parametrize("name", CASE_NAMES)
def test_every_model_call_reads_what_it_read_before(name, policy, sleeve_policy, golden, reg):
    case = build(name)
    pol = case.policy(policy, sleeve_policy)
    calls, _ = live_calls(case, pol, reg)
    assert calls == golden[name]["calls"]
    # and the private capture does not change a byte either
    sink = InputSink()
    with_sink, _ = live_calls(case, pol, reg, input_sink=sink)
    assert with_sink == golden[name]["calls"]
    assert [sha(sink.user_of(i)) for i in range(len(sink.calls))] == [c[3] for c in calls]


def test_the_golden_set_covers_the_named_shapes():
    assert len(CASE_NAMES) == 10
    shapes = {
        "core_only", "stocks", "broker_candles", "frozen_lines", "licensed_fred", "news_40",
        "broker_earnings", "broker_cost",
    }
    assert shapes <= set(CASE_NAMES)
    assert len(build("news_40").pack.news) > 40


# ------------------------------------------------------------------------ the literal registry
def _all_sections(policy, sleeve_policy) -> list[Segmented]:
    out: list[Segmented] = []
    reg = PromptRegistry()
    for name in CASE_NAMES:
        case = build(name)
        pol = case.policy(policy, sleeve_policy)
        cc = code_cards(case, pol)
        inputs = desk_inputs(case, pol)
        out += live.desk_sections(cards=cc, variant="code", **inputs)
        out += live.desk_sections(cards=cc + case.extra_cards, variant="full", **inputs)
        out += live.desk_sections(cards=cc, include_cards=False, **inputs)
        out += [live.news_detail_section(case.pack), live.news_detail_section(case.pack, max_items=5)]
        sink = InputSink()
        live_calls(case, pol, reg, input_sink=sink)
        out += list(sink.sections.values())
    for deb in debate_cases().values():
        out.append(transcript_section(deb))
        out.append(case_section("BULL opening", deb.bull_open, key="c.b", speaker="bull_open"))
        out.append(case_section("BEAR reply", deb.bear, key="c.r", prefix="bear:",
                                rebut_prefix="bull_open:", speaker="bear"))
    return out


def _literal_ok(text: str) -> bool:
    fn = getattr(redact, "literal_ok", None)
    if fn is not None:
        return bool(fn(text))
    patterns = [getattr(redact, n) for n in ("_URL", "_EMAIL", "_HANDLE", "_PATH", "_MONEY",
                                            "_LONG_NUMBER", "_UUID", "_UNMAPPED", "_LEVEL")]
    return not any(p.search(text) for p in patterns)


def test_every_literal_is_registered_and_publishable(policy, sleeve_policy):
    literals = sorted({lit for s in _all_sections(policy, sleeve_policy) for lit in s.literals()})
    registered = [json.loads(line) for line in LITERALS.read_text().splitlines() if line.strip()]
    new = sorted(set(literals) - set(registered))
    assert not new, ("literal runs not in golden/literals.txt (review each, then add it): "
                     + "; ".join(json.dumps(x) for x in new))
    bad = [lit for lit in registered if not _literal_ok(lit)]
    assert not bad, f"literal runs that would not publish as written: {bad}"


def test_data_values_never_sit_in_literals(policy, sleeve_policy):
    """The cycle id, the slot, line symbols and evidence ids are items, never literal text."""
    for s in _all_sections(policy, sleeve_policy):
        for lit in s.literals():
            assert "2026-10-01T1440Z" not in lit and "14:40" not in lit
            for token in ("F:NDX", "N:1a2b3c4d", "K:vol:1", "E:fomc", "SEMIS"):
                assert token not in lit, (s.key, lit)


# ---------------------------------------------------------------------------- scrub and items
@pytest.mark.parametrize("name", CASE_NAMES)
def test_the_final_whole_text_scrub_is_a_no_op(name, policy, sleeve_policy):
    case = build(name)
    pol = case.policy(policy, sleeve_policy)
    sections = live.desk_sections(cards=code_cards(case, pol), variant="full", **desk_inputs(case, pol))
    assert not live.scrub_crossed(sections)
    text = joined(sections)
    assert scrub_amounts(text) == text
    detail = live.news_detail_section(case.pack)
    assert scrub_amounts(detail.text()) == detail.text()


def test_a_currency_sign_across_pieces_falls_back_to_one_opaque_section():
    b = TextBuilder("desk.full.flags", "desk", scrub=scrub_amounts)
    b.lit("fee " + chr(36)).item("flag", "x", "5 per unit", sources=("broker",), licence="broker_licensed")
    section = b.build()
    assert section.opaque and section.key == "desk.full.flags.opaque"
    assert section.text() == scrub_amounts("fee " + chr(36) + "5 per unit")
    item = section.items[0]
    assert item.licence == "broker_licensed" and item.sources == ("broker",)


def test_an_opaque_desk_is_flagged_by_the_council():
    assert SCRUB_CROSSED_FLAG == "desk_scrub_crossed_sections"
    opaque = TextBuilder("desk.code.x", "desk", scrub=scrub_amounts)
    opaque.lit(chr(36)).item("text", "v", "7")
    assert live.scrub_crossed([opaque.build()])


def test_desk_sections_come_in_the_design_order(policy):
    case = build("frozen_lines")
    sections = live.desk_sections(cards=code_cards(case, policy), variant="code", **desk_inputs(case, policy))
    parts = [s.key.split(".")[-1] for s in sections]
    assert parts == [p for p in live.DESK_PARTS if p in parts]
    assert all(s.key.startswith("desk.code.") for s in sections)


def _items(sections, **match):
    return [it for s in sections for it in s.items
            if all(getattr(it, k) == v for k, v in match.items())]


def test_items_name_their_evidence_sources_and_licence(policy, sleeve_policy):
    case = build("broker_candles")
    s = live.desk_sections(cards=code_cards(case, policy), **desk_inputs(case, policy))
    semis = _items(s, line="SEMIS", field="vs_sma50")[0]
    assert semis.sources == ("etoro",) and semis.licence == "broker_licensed" and semis.text == "+1.5%"
    ndx = _items(s, line="NDX", field="vs_sma50")[0]
    assert ndx.licence == "public"
    missing = _items(s, line="SEMIS", field="vs_sma200")[0]
    assert missing.text == "n/a" and missing.sources == ()
    fact = _items(s, kind="fact", ref="F:SEMIS:trend")[0]
    assert fact.text == "F:SEMIS:trend=up" and fact.licence == "broker_licensed"
    header = _items(s, ref="cycle_id")[0]
    assert header.sources == ("clock",) and header.text == case.pack.cycle_id

    cost = build("broker_cost")
    s = live.desk_sections(cards=[], **desk_inputs(cost, policy))
    side = _items(s, line="NDX", field="cost_side")[0]
    assert side.text == "12" and side.sources == ("broker_quote",) and side.licence == "broker_licensed"
    assert _items(s, line="SPX", field="cost_side")[0].sources == ("cost_quote",)
    assert _items(s, line="GOLD", field="carry")[0].text == "n/a"

    fred = build("licensed_fred")
    s = live.desk_sections(cards=[], **desk_inputs(fred, policy))
    vix = _items(s, ref="M:VIXCLS@2026-09-30")[0]
    assert vix.licence == "restricted" and vix.sources == ("fred",)

    earn = build("broker_earnings")
    s = live.desk_sections(cards=[], **desk_inputs(earn, sleeve_policy))
    row = [it for it in _items(s, kind="event", field="row") if it.ref.startswith("E:earnings:TSTA")][0]
    assert row.licence == "broker_licensed" and "clock" in row.sources
    assert "(+26.2h)" in row.text     # the offset travels with the time: one item

    news = live.news_detail_section(build("core_only").pack)
    title = _items([news], ref="N:1a2b3c4d", field="title")[0]
    assert title.licence == "broker_licensed" and title.sources == ("broker_feed",)
    assert _items([news], ref="N:1a2b3c4d", field="age")[0].licence == "public"


def test_card_items_inherit_what_they_cite(policy):
    case = build("broker_candles")
    cards = code_cards(case, policy) + case.extra_cards
    s = live.desk_sections(cards=cards, **desk_inputs(case, policy))
    vol = _items(s, kind="card", ref="K:vol:1")[0]
    assert vol.sources[0] == "code" and "broker" in vol.sources and vol.licence == "broker_licensed"
    news = _items(s, kind="card", ref="K:news:1")[0]
    assert news.sources[0] == "council" and news.licence == "public"


def test_case_items_name_their_speaker():
    deb = debate_cases()["full"]
    s = transcript_section(deb)
    refs = {it.ref for it in s.items}
    assert {"bull_open:c1", "bear:c1", "bear:rebuttal:c2", "bull_rebuttal:c1"} <= refs
    assert s.text() == legacy.transcript(deb)


def test_strictest_licence():
    assert strictest([]) == "public"
    assert strictest(["public", "restricted"]) == "restricted"
    assert strictest(["public_domain", "broker_licensed", "restricted"]) == "broker_licensed"
