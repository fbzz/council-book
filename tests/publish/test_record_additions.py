"""Fields added for the site redesign: full agent text, the macro output, the facts table, call
error codes, the descriptive book and the 1-day move. Each is optional (old journal files still
load and verify) and percent-only (leak-scanned with the private canaries)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from council.models.cards import CardDraft, EvidenceCard, MacroAnalystOutput, MacroDriver
from council.models.debate import AdvocateCase, BearCase
from council.models.facts import EventItem, Fact, FilingItem, FilingSentence, MarketState
from council.paths import REPO_ROOT
from council.policy import LineSpec
from council.publish import commit_reveal, journal, labels, leakscan
from council.publish.public_models import (
    PublicBookLine,
    PublicCall,
    PublicCycleV1,
    PublicFact,
    PublicMacro,
)
from council.publish.redact import (
    clean_text,
    day_change_pct,
    error_kind,
    public_book,
    public_cycle,
)
from tests.publish.conftest import (
    CANARIES,
    PRIVATE_AMOUNT,
    PRIVATE_NAV,
    PRIVATE_SL_RATE,
    PRIVATE_UNITS,
    SLOT,
    make_pack,
    make_record,
)

OLD_CYCLE = "2026-09-25T1440Z"
OLD_CYCLE_FILE = REPO_ROOT / "journal" / "cycles" / "2026" / "09" / f"{OLD_CYCLE}.json"
OPEN_RATE, CLOSE_RATE = 412.5, 433.125           # private: +5% since open
LONG_ARGUMENT = " ".join(
    f"Sentence {i} weighs the semiconductor volatility card against the Nasdaq-100 trend and "
    f"explains why the reference stays the right default for this line." for i in range(1, 10)
)


def _text(doc) -> str:
    return json.dumps(doc.model_dump(mode="json"), sort_keys=True)


def _state(sym: str, *, source: str = "tiingo:QQQ", z: float | None = 1.5, sigma: float | None = 0.01) -> MarketState:
    return MarketState(symbol=sym, asset_class="index", trend="up", history_source=source,
                       ret1d_sigma=z, sigma_daily=sigma)


def _rich_pack():
    """The conftest pack plus Tiingo / Binance / broker-candle facts, an event, a filing, a
    licensed and an unknown FRED series, and a fundamental."""
    pack = make_pack()
    at = SLOT - timedelta(hours=2)
    facts = [
        *pack.facts,
        Fact(id="F:SEMIS:mom63d", kind="market", symbol="SEMIS", value=-9.4567, unit="pct", available_at=at,
             source="tiingo:SOXX"),
        Fact(id="F:SEMIS:trend", kind="market", symbol="SEMIS", value="up", unit="state", available_at=at,
             source="tiingo:SOXX"),
        Fact(id="F:SEMIS:market_open", kind="market", symbol="SEMIS", value=True, unit="state", available_at=SLOT,
             source="clock"),
        Fact(id="V:BTC:vol_ratio", kind="vol", symbol="BTC", value=1.23456, unit="ratio", available_at=at,
             source="binance:BTCUSDT"),
        Fact(id="F:GOLD:mom63d", kind="market", symbol="GOLD", value=3.3, unit="pct", available_at=at,
             source="etoro"),
        Fact(id="F:GOLD:trend", kind="market", symbol="GOLD", value="mixed", unit="state", available_at=at,
             source="etoro"),
        Fact(id="V:GOLD:vol_ratio", kind="vol", symbol="GOLD", value=0.9, unit="ratio", available_at=at,
             source="etoro"),
        Fact(id="C:NDX:per_side_bps", kind="cost", symbol="NDX", value=5.04, unit="bps", available_at=SLOT,
             source="costs:floor"),
        Fact(id="C:SPX:per_side_bps", kind="cost", symbol="SPX", value=7.7, unit="bps", available_at=SLOT,
             source="costs:whatif"),
        Fact(id="M:BAMLH0A0HYM2@2026-09-30", kind="macro", value=3.1, unit="pct", available_at=SLOT,
             source="fred", publishable=False),
        Fact(id="M:T10Y3M@2026-09-30", kind="macro", value=0.4, unit="pct", available_at=SLOT, source="fred"),
        Fact(id="F:NDX:pe_fwd", kind="fundamental", symbol="NDX", value=31.2, unit="x", available_at=at,
             source="tiingo"),
        Fact(id="F:XYZ:trend", kind="market", symbol="XYZ", value="up", unit="state", available_at=at,
             source="tiingo"),
    ]
    events = [EventItem(id="E:fomc@2026-10-28", kind="fomc", at_utc=datetime(2026, 10, 28, 18, tzinfo=UTC),
                        severity=3, source="policy_calendar")]
    filings = [FilingItem(accession="0000000001-26-000001", cik="1", symbol="SEMIS", form="8-K",
                          accepted_at=at, available_at=at, url="https://example.com/f",
                          sentences=[FilingSentence(id="S:0000000001-26-000001#p1", text="Guidance raised.")])]
    states = {**pack.states, "NDX": _state("NDX"), "BTC": _state("BTC", source="binance:BTCUSDT", z=-2.0, sigma=0.03),
              "GOLD": _state("GOLD", source="etoro")}
    return pack.model_copy(update={"facts": facts, "events": events, "filings": filings, "states": states})


@pytest.fixture
def rich(policy) -> PublicCycleV1:
    return public_cycle(make_record(), _rich_pack(), lines=policy.universe)


# ------------------------------------------------------------------------------ 1. full agent text
def test_the_full_argument_is_published(policy):
    assert 1300 < len(LONG_ARGUMENT) <= 1500
    case = AdvocateCase(argument=LONG_ARGUMENT, proposal={}, claims=[], strongest_opposing_fact_id="V:SEMIS:vol_ratio",
                        concessions=["c1: " + "the volatility card is real and it qualifies a cut on this line " * 3])
    rec = make_record()
    rec = rec.model_copy(update={"debate": rec.debate.model_copy(update={"bull_open": case})})
    doc = public_cycle(rec, make_pack(), lines=policy.universe)
    assert doc.debate.bull.argument == LONG_ARGUMENT
    assert doc.debate.bull.concessions[0] == " ".join(case.concessions[0].split())    # ~200 chars, not cut


def test_an_over_long_text_is_cut_at_a_sentence_end():
    text = "First sentence is short. Second sentence is also here. Third one runs on and on and on"
    out = clean_text(text, 60)
    assert out == "First sentence is short. Second sentence is also here. …" and len(out) <= 60


def test_without_a_sentence_end_the_cut_is_at_a_word_end():
    out = clean_text("alpha beta gamma delta epsilon zeta eta theta iota kappa", 30)
    assert out.endswith("…") and len(out) <= 30 and out[:-1].split()[-1] in "alpha beta gamma delta epsilon zeta"


def test_cleaner_placeholders_never_push_a_capped_text_past_its_public_cap(policy):
    argument = ("Costs $5 and $7 and $9. " * 60)[:1500]
    case = AdvocateCase(argument=argument, proposal={}, claims=[], strongest_opposing_fact_id="V:SEMIS:vol_ratio")
    rec = make_record()
    rec = rec.model_copy(update={"debate": rec.debate.model_copy(update={"bear": BearCase(**case.model_dump())})})
    doc = public_cycle(rec, make_pack(), lines=policy.universe)
    assert "$" not in doc.debate.bear.argument and len(doc.debate.bear.argument) <= 1600


# ------------------------------------------------------------------------------ 2. macro + card roles
def _with_macro(rec):
    macro_card = EvidenceCard(card_id="K:macro:1", role="macro", scope=["market"], card_type="macro_context",
                              direction="neutral", claim="Rates steady", evidence_ids=["M:DGS10@2026-09-30"],
                              horizon_days=20)
    out = MacroAnalystOutput(
        regime="risk_off",
        drivers=[MacroDriver(text="The 10-year yield rose 12 bp in 20 days; see https://x.io/a",
                             evidence_ids=["M:DGS10.chg20@2026-09-30", "M:VIXCLS@2026-09-30"])],
        sleeve_tilts={"core": -1, "crypto": 0, "Bad Key": 1},
        cards=[CardDraft(**macro_card.model_dump(include=set(CardDraft.model_fields)))],
    )
    return rec.model_copy(update={"macro": out, "cards": [*rec.cards, macro_card.model_copy(
        update={"card_id": "K:macro:2"})]})


def test_macro_output_is_published_from_the_typed_record(policy):
    doc = public_cycle(_with_macro(make_record()), make_pack(), lines=policy.universe)
    m = doc.macro
    assert isinstance(m, PublicMacro) and m.regime == "risk_off"
    assert m.sleeve_tilts == {"core": -1, "crypto": 0}                       # a bad key is dropped
    assert m.cards == ["K:macro:1", "K:macro:2"]
    assert m.drivers[0].text.startswith("The 10-year yield rose 12 bp") and "https" not in m.drivers[0].text
    refs = {r.series: r for r in m.drivers[0].evidence}
    assert refs["DGS10"].value == -12.5 and refs["VIXCLS"].value is None and not refs["VIXCLS"].publishable
    assert leakscan.scan(doc, canaries=CANARIES) == []


def test_no_macro_section_when_the_analyst_did_not_run(policy):
    doc = public_cycle(make_record(), make_pack(), lines=policy.universe)
    assert doc.macro is None and "macro" not in doc.model_dump(mode="json")


def test_every_card_records_the_role_that_wrote_it(policy):
    doc = public_cycle(_with_macro(make_record()), make_pack(), lines=policy.universe)
    roles = {c.card_id: c.role for c in doc.cards}
    assert roles["K:news:1"] == roles["K:news:2"] == "news"
    assert roles["K:vol:1"] == "vol" and roles["K:macro:1"] == "macro"
    assert all(c.card_id.split(":")[1] == c.role for c in doc.cards)


# ------------------------------------------------------------------------------ 3. facts table
def test_the_facts_table_lists_every_fact_event_news_item_and_filing(rich):
    ids = [f.id for f in rich.facts]
    assert len(ids) == len(set(ids))
    pack = _rich_pack()
    expected = ({f.id for f in pack.facts if f.symbol != "XYZ"} | {e.id for e in pack.events}
                | {n.id for n in pack.news} | {s.id for f in pack.filings for s in f.sentences})
    assert set(ids) == expected
    assert "facts_dropped:1" in rich.flags                       # F:XYZ:trend: not a line


def test_fact_values_are_rounded_and_labelled(rich):
    facts = {f.id: f for f in rich.facts}
    mom = facts["F:SEMIS:mom63d"]
    assert (mom.value, mom.unit, mom.line, mom.source, mom.label) == (-9.46, "pct", "SEMIS", "tiingo", "3-month change")
    assert facts["V:BTC:vol_ratio"].value == 1.235 and facts["V:BTC:vol_ratio"].source == "binance"
    assert facts["F:SEMIS:trend"].value == "up" and facts["F:SEMIS:market_open"].value is True
    assert facts["C:NDX:per_side_bps"].value == 5.0 and facts["C:NDX:per_side_bps"].source == "policy"
    assert facts["M:DGS10@2026-09-30"].label == labels.FRED_SERIES["DGS10"]
    assert facts["F:SEMIS:mom63d"].as_of == SLOT - timedelta(hours=2)
    for f in rich.facts:
        assert f.label == labels.fact_label(f.id)


def test_non_publishable_fred_series_carry_no_value(rich):
    facts = {f.id: f for f in rich.facts}
    assert facts["M:VIXCLS@2026-09-30"].value is None and facts["M:VIXCLS@2026-09-30"].withheld == "licensed_series"
    assert facts["M:BAMLH0A0HYM2@2026-09-30"].withheld == "licensed_series"
    assert facts["M:T10Y3M@2026-09-30"].value is None and facts["M:T10Y3M@2026-09-30"].withheld == "not_publishable"
    assert facts["M:DGS10.chg20@2026-09-30"].value == -12.5 and facts["M:DGS10.chg20@2026-09-30"].withheld is None


def test_broker_data_is_shown_only_as_coarse_states_and_ratios(rich):
    facts = {f.id: f for f in rich.facts}
    assert facts["F:GOLD:mom63d"].value is None and facts["F:GOLD:mom63d"].withheld == "broker_data"
    assert facts["F:GOLD:trend"].value == "mixed" and facts["V:GOLD:vol_ratio"].value == 0.9
    assert facts["C:SPX:per_side_bps"].value is None and facts["C:SPX:per_side_bps"].withheld == "broker_data"
    assert facts["V:SEMIS:vol_ratio"].withheld == "unknown_source"          # source "code": fail closed
    assert facts["F:NDX:pe_fwd"].value is None and facts["F:NDX:pe_fwd"].withheld == "not_publishable"


def test_news_and_filings_are_ids_only(rich):
    facts = {f.id: f for f in rich.facts}
    news = facts["N:1a2b3c4d"]
    assert (news.kind, news.value, news.label, news.line) == ("news", None, labels.NEWS_LABEL, "SEMIS")
    assert facts["S:0000000001-26-000001#p1"].value is None and facts["E:fomc@2026-10-28"].source == "calendar"
    text = _text(rich)
    assert "Chipmakers" not in text and "Guidance raised" not in text and "example.com" not in text


def test_the_facts_table_passes_the_leak_scan(rich):
    assert leakscan.scan(rich, canaries=CANARIES) == []
    assert [k for f in rich.facts for k in f.model_dump() if leakscan.key_denied(k)] == []


@pytest.mark.parametrize("bad", [
    {"value": "costs 1,234.56 USD"},                          # free text is not a state word
    {"value": 12.5, "withheld": "broker_data"},               # a withheld fact has no value
    {"value": 2951234567.0},                                  # an id-like number
    {"id": "X:NDX:trend"},                                    # not an evidence id
])
def test_a_public_fact_refuses_values_that_could_leak(bad):
    base = {"id": "F:NDX:trend", "kind": "market", "label": "trend"}
    with pytest.raises(ValidationError):
        PublicFact(**{**base, **bad})


def test_news_facts_may_not_carry_a_value():
    with pytest.raises(ValidationError):
        PublicFact(id="N:1a2b3c4d", kind="news", label="broker news item", value="up")


# ------------------------------------------------------------------------------ 4. call log
@pytest.mark.parametrize("status, error, kind", [
    ("ok", "", None),
    ("ok", "corrected: cards.0.claim: String should have at most 200 characters", "corrected"),
    ("cached", "", None),
    ("timeout", "timeout after 90s", "timeout"),
    ("parse_fail", "reply is not a JSON object | after correction: reply is not a JSON object", "not_json"),
    ("parse_fail", "reply is not a JSON object | after correction: regime: Input should be 'risk_on'", "schema"),
    ("parse_fail", "drivers.0.text: too long | correction timeout: timeout after 90s", "correction_failed"),
    ("transport", "http 429", "http_429"),
    ("transport", "http 503", "http_5xx"),
    ("transport", "http 404", "http_4xx"),
    ("transport", "server error: model not found", "server_error"),
    ("transport", "non-JSON response body", "bad_response"),
    ("transport", "unexpected KeyError", "internal_error"),
    ("transport", "ConnectError", "connection"),
    ("skipped", "call_budget", "call_budget"),
    ("skipped", "council_unavailable", "council_unavailable"),
    ("skipped", "something else", "skipped"),
    ("invalid", "", "invalid"),
])
def test_error_kind_is_a_fixed_code(status, error, kind):
    assert error_kind(status, error) == kind


def test_calls_publish_the_code_never_the_error_text(policy):
    rec = make_record()
    calls = [*rec.calls, rec.calls[0].model_copy(update={
        "role": "macro", "status": "parse_fail",
        "error": "reply is not a JSON object | after correction: reply is not a JSON object ($1,234.56, /Users/x)"})]
    doc = public_cycle(rec.model_copy(update={"calls": calls}), make_pack(), lines=policy.universe)
    by_role = {c.role: c for c in doc.calls}
    assert by_role["macro"].error_kind == "not_json" and by_role["news"].error_kind == "timeout"
    assert by_role["pm"].error_kind is None and "error_kind" not in by_role["pm"].model_dump(mode="json")
    assert "/Users" not in _text(doc) and "1,234" not in _text(doc)
    with pytest.raises(ValidationError):
        PublicCall(role="pm", replicate=0, status="ok", latency_ms=1, tokens_in=1, tokens_out=1, prompt_id="p",
                   prompt_sha="", error_kind="ConnectTimeout http://localhost")


# ------------------------------------------------------------------------------ 5. day change + book
def test_day_change_is_derived_from_open_histories_only():
    assert day_change_pct(_state("NDX", z=1.5, sigma=0.01)) == 1.51               # e^0.015 - 1
    assert day_change_pct(_state("BTC", source="binance:BTCUSDT", z=-2.0, sigma=0.03)) == -5.82
    assert day_change_pct(_state("GOLD", source="etoro")) is None                  # broker candles
    assert day_change_pct(_state("NDX", source="")) is None
    assert day_change_pct(_state("NDX", z=None)) is None and day_change_pct(None) is None


def test_the_cycle_carries_the_1d_move_per_line(rich):
    assert rich.reference["NDX"].day_change_pct == 1.51 and rich.reference["BTC"].day_change_pct == -5.82
    assert rich.reference["GOLD"].day_change_pct is None
    assert "day_change_pct" not in rich.model_dump(mode="json")["reference"]["GOLD"]


def _position(symbol: str, *, is_buy: bool = True, leverage: int = 1, amount: float = PRIVATE_AMOUNT,
              open_rate: float = OPEN_RATE, close_rate: float | None = CLOSE_RATE, settlement: str = "real"):
    return SimpleNamespace(symbol=symbol, is_buy=is_buy, leverage=leverage, amount=amount, open_rate=open_rate,
                           close_rate=close_rate, settlement=settlement, units=PRIVATE_UNITS,
                           sl_rate=PRIVATE_SL_RATE, position_id=2951234567, instrument_id=100123)


def test_book_lines_are_described_from_policy_positions_and_the_pack(policy):
    positions = [
        _position("EQQQ.L", amount=300.0),                                      # +5%
        _position("NSDQ100", amount=100.0, leverage=2, settlement="cfd",       # +5% x2 = +10%
                  open_rate=100.0, close_rate=105.0),
        _position("SOXX", is_buy=False, leverage=2, settlement="cfd",           # short, price +5%: -10%
                  open_rate=100.0, close_rate=105.0),
        _position("UNMAPPED_100123"),
    ]
    book = public_book("2026-10-01T1440Z", {"NDX": 0.4, "SEMIS": -0.1}, lines=policy.universe,
                       reference_weights={"NDX": 0.35, "SPX": 0.15}, positions=positions, pack=_rich_pack())
    ndx, semis, spx = book.lines["NDX"], book.lines["SEMIS"], book.lines["SPX"]
    assert (ndx.name, ndx.asset_class, ndx.session) == ("Nasdaq-100", "index", "lse")
    assert (ndx.settlement, ndx.leverage) == ("real", 1)                      # the largest position
    assert ndx.pnl_since_open_pct == 6.25                                     # (300*5 + 100*10) / 400
    assert ndx.day_change_pct == 1.51
    assert (semis.direction, semis.settlement, semis.leverage, semis.pnl_since_open_pct) == ("short", "cfd", 2, -10.0)
    assert spx.direction == "flat" and spx.pnl_since_open_pct is None and spx.settlement is None
    dumped = json.dumps(book.model_dump(mode="json"))
    assert "pnl_since_open_pct" not in json.dumps(book.model_dump(mode="json")["lines"]["SPX"])
    assert leakscan.scan(book, canaries=(*CANARIES, OPEN_RATE, CLOSE_RATE)) == []
    for private in (str(PRIVATE_AMOUNT), str(OPEN_RATE), str(CLOSE_RATE), "UNMAPPED", "100123", "2951234567"):
        assert private not in dumped, private


def test_book_line_names_are_cleaned(policy):
    spec = policy.universe.lines[0].model_copy(update={"name": f"Nasdaq ${PRIVATE_NAV} desk at a@b.com"})
    book = public_book("2026-10-01T1440Z", {"NDX": 0.35}, lines=[spec])
    assert str(PRIVATE_NAV) not in book.lines["NDX"].name and "a@b.com" not in book.lines["NDX"].name
    assert leakscan.scan(book, canaries=CANARIES) == []


def test_book_works_for_stock_lines(policy):
    nvda = LineSpec.model_validate({
        "symbol": "NVDA", "name": "NVIDIA", "asset_class": "stock", "sleeve": "satellite", "in_reference": False,
        "base_weight": 0.05, "signal": {"source": "tiingo", "ticker": "NVDA"},
        "vehicles": {"long": [{"symbol": "NVDA", "settlement": "real"}], "short": [{"symbol": "NVDA", "settlement": "cfd"}]},
    })
    book = public_book("2026-10-01T1440Z", {"NVDA": 0.05}, lines=[*policy.universe.lines, nvda],
                       positions=[_position("NVDA", open_rate=100.0, close_rate=90.0)])
    line = book.lines["NVDA"]
    assert (line.name, line.asset_class, line.session, line.pnl_since_open_pct) == ("NVIDIA", "stock", "us", -10.0)


def test_a_book_line_refuses_fields_that_could_carry_money():
    with pytest.raises(ValidationError):
        PublicBookLine(direction="long", weight_x=0.1, amount_usd=PRIVATE_AMOUNT)
    with pytest.raises(ValidationError):
        PublicBookLine(direction="long", weight_x=0.1, leverage=250)
    with pytest.raises(ValidationError):
        PublicBookLine(direction="long", weight_x=0.1, pnl_since_open_pct=float("inf"))


# ------------------------------------------------------------------------------ 6. backward compatibility
def test_a_document_without_the_new_fields_serialises_without_them(policy):
    rec = make_record(calls=[])
    doc = public_cycle(rec, None, lines=policy.universe)
    data = doc.model_dump(mode="json")
    assert "macro" not in data and "facts" not in data
    assert all("day_change_pct" not in v for v in data["reference"].values())


def test_the_existing_journal_cycle_loads_and_reserialises_to_its_sealed_bytes():
    sealed = OLD_CYCLE_FILE.read_bytes()
    doc = PublicCycleV1.model_validate_json(sealed)
    assert doc.macro is None and doc.facts == [] and all(c.error_kind is None for c in doc.calls)
    assert commit_reveal.canonical_json(doc) == sealed          # a pending old cycle can still be revealed


def test_the_existing_journal_cycle_still_verifies():
    from council.cli import app

    result = CliRunner().invoke(app, ["verify", OLD_CYCLE, "--journal-dir", str(REPO_ROOT / "journal")])
    assert result.exit_code == 0 and "verified" in result.output


def test_a_cycle_sealed_before_the_upgrade_can_still_be_revealed(policy):
    """Seal a document the old way (no new keys), then reveal it with the new models."""
    doc = public_cycle(make_record(), None, lines=policy.universe)
    old = {k: v for k, v in doc.model_dump(mode="json").items() if k not in ("macro", "facts")}
    commitment, salt, sealed = commit_reveal.seal_bytes(old, sealed_at=SLOT)
    files = journal.reveal_files(sealed, salt, commitment)
    assert files[journal.cycle_path(doc.cycle_id)] == sealed


def test_new_fields_round_trip_through_the_sealed_bytes(rich):
    commitment, salt, sealed = commit_reveal.seal_bytes(rich, sealed_at=SLOT)
    files = journal.reveal_files(sealed, salt, commitment)
    back = PublicCycleV1.model_validate_json(files[journal.cycle_path(rich.cycle_id)])
    assert back == rich and back.facts and back.reference["NDX"].day_change_pct == 1.51


def test_new_percent_fields_pass_the_key_denylist_only_with_their_unit():
    for key in ("pnl_since_open_pct", "day_change_pct", "error_kind", "settlement", "leverage", "withheld"):
        assert not leakscan.key_denied(key), key
    for key in ("pnl_since_open", "open_price", "open_rate", "units", "amount_invested"):
        assert leakscan.key_denied(key), key


# ------------------------------------------------------------------------------ review fixes
def _stock(symbol: str, name: str, vehicle: str) -> LineSpec:
    return LineSpec.model_validate({
        "symbol": symbol, "name": name, "asset_class": "stock", "sleeve": "satellite", "in_reference": True,
        "base_weight": 0.02, "signal": {"source": "tiingo", "ticker": vehicle.replace(".", "-")},
        "vehicles": {"long": [{"symbol": vehicle, "settlement": "real"}], "short": [{"symbol": vehicle, "settlement": "cfd"}]},
    })


def test_class_share_and_one_letter_line_ids_publish(policy):
    """Single stocks use the plain ticker as the line id ("." written "_"): BRK_B and V must pass
    the public models end to end (book and cycle) and the leak scan."""
    import re as _re

    from council.models.reference import ReferenceEntry
    from council.publish.public_models import LINE_PATTERN
    for ok in ("BRK_B", "V", "NVDA", "EURUSD", "A1"):
        assert _re.fullmatch(LINE_PATTERN, ok), ok
    for bad in ("_B", "BRK_", "brk_b", "BRK.B", "ABCDEFGHIJKLM", ""):
        assert not _re.fullmatch(LINE_PATTERN, bad), bad
    lines = [*policy.universe.lines, _stock("BRK_B", "Berkshire Hathaway B", "BRK.B"), _stock("V", "Visa", "V")]
    book = public_book("2026-10-01T1440Z", {"BRK_B": 0.02, "V": 0.01}, lines=lines,
                       positions=[_position("BRK.B", open_rate=OPEN_RATE, close_rate=CLOSE_RATE)])
    assert set(book.lines) >= {"BRK_B", "V"} and book.lines["BRK_B"].pnl_since_open_pct == 5.0
    assert leakscan.scan(book, canaries=CANARIES) == []
    rec = make_record()
    extra = {s: ReferenceEntry(symbol=s, sleeve="satellite", asset_class="stock", in_reference=True, trend="up",
                               level_ref=1.0, unit_weight=0.02, weight_ref=0.02, sigma_ann=0.3, stop_distance=0.1)
             for s in ("BRK_B", "V")}
    rec = rec.model_copy(update={"reference": rec.reference.model_copy(
        update={"entries": {**rec.reference.entries, **extra}})})
    doc = public_cycle(rec, make_pack(), lines=lines)
    assert {"BRK_B", "V"} <= set(doc.reference)
    assert leakscan.scan(doc, canaries=CANARIES) == []


def test_the_packs_never_publish_bit_wins_for_every_source(policy):
    pack = make_pack()
    at = SLOT - timedelta(hours=2)
    hidden = Fact(id="F:NDX:mom63d", kind="market", symbol="NDX", value=4.2, unit="pct", available_at=at,
                  source="tiingo:QQQ", publishable=False)
    shown = Fact(id="F:SPX:mom63d", kind="market", symbol="SPX", value=3.1, unit="pct", available_at=at,
                 source="tiingo:SPY")
    pack = pack.model_copy(update={"facts": [*pack.facts, hidden, shown]})
    facts = {f.id: f for f in public_cycle(make_record(), pack, lines=policy.universe).facts}
    assert facts["F:NDX:mom63d"].value is None and facts["F:NDX:mom63d"].withheld == "not_publishable"
    assert facts["F:SPX:mom63d"].value == 3.1 and facts["F:SPX:mom63d"].withheld is None


def test_a_floored_quote_with_a_broker_what_if_is_not_labelled_policy(policy):
    """floor_applied only says the spread was floored; the carry can still be the broker's. Such a
    quote's facts come from the broker what-if and are withheld (docs/data-rights.md)."""
    from council.facts.pack import cost_facts_from_quotes
    from council.models.broker import CostQuote
    whatif = CostQuote(symbol="EQQQ.L", direction="long", settlement="cfd", leverage=2, per_side_bps=7.0,
                       what_if_bps=6.1, carry_bps_day=0.93, floor_applied=True, quoted_at=SLOT)
    floor = whatif.model_copy(update={"what_if_bps": None, "symbol": "CSPX.L"})
    facts = cost_facts_from_quotes({"NDX": whatif, "SPX": floor}, slot=SLOT)
    by_id = {f.id: f for f in facts}
    assert by_id["C:NDX:carry_bps_day"].source == "costs:whatif" and by_id["C:SPX:carry_bps_day"].source == "costs:floor"
    pack = make_pack()
    pack = pack.model_copy(update={"facts": [f for f in pack.facts if not f.id.startswith("C:")] + facts})
    public = {f.id: f for f in public_cycle(make_record(), pack, lines=policy.universe).facts}
    assert public["C:NDX:carry_bps_day"].value is None and public["C:NDX:carry_bps_day"].withheld == "broker_data"
    assert public["C:SPX:carry_bps_day"].withheld is None


@pytest.mark.parametrize("text, kept", [
    ("gold near 2,650.40 after the release", "gold near [level removed] after the release"),
    ("NDX near 21,450 into the close", "NDX near [level removed] into the close"),
    ("the fund last traded at 1098.40", "the fund last traded at [level removed]"),
    ("48.1% below its high", "48.1% below its high"),
    ("a 0.35x position", "a 0.35x position"),
    ("costs 12.5 bps a side", "costs 12.5 bps a side"),
    ("the worst quarter in 2026", "the worst quarter in 2026"),
    ("the S&P 500 and the Nasdaq-100 trend up", "the S&P 500 and the Nasdaq-100 trend up"),
    ("above its 200-day average", "above its 200-day average"),
    ("F:NDX:dist_sma200 and bear:c2", "F:NDX:dist_sma200 and bear:c2"),
])
def test_price_levels_are_scrubbed_from_model_text(text, kept):
    assert clean_text(text, 400) == kept
