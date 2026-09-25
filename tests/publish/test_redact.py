from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from council.publish import leakscan
from council.publish.public_models import PublicCycleV1
from council.publish.redact import (
    WITHHELD_LICENSED,
    clean_text,
    public_book,
    public_cycle,
    public_ops_row,
    public_status,
)
from tests.publish.conftest import (
    CANARIES,
    PRIVATE_AMOUNT,
    PRIVATE_DECISION_ID,
    PRIVATE_INSTRUMENT_ID,
    PRIVATE_NAV,
    PRIVATE_POSITION_ID,
    PRIVATE_SL_RATE,
    PRIVATE_UNITS,
    make_record,
)


@pytest.fixture
def doc(record, pack, policy) -> PublicCycleV1:
    return public_cycle(record, pack, lines=policy.universe)


def _text(doc: PublicCycleV1) -> str:
    return json.dumps(doc.model_dump(mode="json"), sort_keys=True)


def test_no_private_value_reaches_the_public_document(doc):
    text = _text(doc)
    for private in (str(PRIVATE_NAV), str(PRIVATE_AMOUNT), str(PRIVATE_UNITS), str(PRIVATE_SL_RATE),
                    str(PRIVATE_POSITION_ID), str(PRIVATE_INSTRUMENT_ID), PRIVATE_DECISION_ID,
                    "private_note", "main book", "localhost", "/Users/", "ConnectTimeout", "decision_id"):
        assert private not in text, private


def test_public_document_passes_the_leak_scan_with_canaries(doc):
    assert leakscan.scan(doc, canaries=CANARIES) == []


def test_every_public_key_is_allowed(doc):
    def keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from keys(v)

    denied = [k for k in keys(doc.model_dump(mode="json")) if leakscan.key_denied(k)]
    assert denied == []


def test_units_are_percentages_and_multiples(doc):
    assert doc.risk.gross_x == 0.855
    assert doc.risk.margin_use_pct == 43.21
    assert doc.risk.stop_at_risk_pct == 7.12
    assert doc.risk.ex_ante_vol_pct == 17.6
    assert doc.risk.raw_x["SEMIS"] == 0.075              # level 0.5 x unit 0.15
    assert doc.late_by_min == 12


def test_vehicle_symbols_map_to_lines_and_unknown_symbols_are_dropped(doc):
    assert doc.risk.final_x["SPX"] == 0.15                # from the CSPX.L vehicle
    assert all(k in {"NDX", "SEMIS", "SPX", "GOLD", "BTC", "ETH", "OIL", "EURUSD", "GBPUSD"} for k in doc.risk.final_x)
    assert doc.plan.legs[0].line == "SEMIS"               # from the SMH.L vehicle
    assert any(f.startswith("unmapped_symbols_dropped:") for f in doc.flags)


def test_evidence_refs_are_typed_and_feed_text_is_never_published(doc):
    refs = doc.cards[0].evidence
    assert refs[0].model_dump() == {"kind": "broker_feed", "id": "N:1a2b3c4d"}
    assert refs[1].kind == "market" and refs[1].id == "F:NDX:dist_sma200_pct"
    macro = {r.series: r for r in doc.cards[3].evidence}
    assert macro["DGS10"].publishable and macro["DGS10"].value == 4.11
    assert not macro["VIXCLS"].publishable and macro["VIXCLS"].value is None
    assert "Chipmakers" not in _text(doc)


def test_text_copied_from_a_licensed_item_is_withheld(doc):
    assert doc.cards[1].claim == WITHHELD_LICENSED
    assert any(f.startswith("licensed_overlap_withheld:") for f in doc.flags)
    assert doc.cards[0].claim.startswith("Foundry guidance raised")      # a paraphrase passes


def test_model_text_is_neutralised(doc):
    macro_claim = doc.cards[3].claim
    assert "https://" not in macro_claim and "@someone" not in macro_claim
    arg = doc.debate.rebuttal.argument
    assert "\x1b" not in arg and "\x07" not in arg
    assert "<script>" in arg          # kept as text; the site escapes it (see test_site)


def test_private_amounts_in_code_strings_are_removed(doc):
    assert all(str(PRIVATE_AMOUNT) not in s for s in doc.plan.skipped)
    names = {c.rule_id: c for c in doc.risk.checks}
    assert names["R7"].value == "[amount removed]"


def test_pm_block_reports_medoid_and_agreement(doc):
    assert doc.pm.medoid == 0 and doc.pm.valid_replicates == 2
    assert doc.pm.agreement["SEMIS"] == 2
    assert doc.pm.replicates[2].valid is False and doc.pm.replicates[2].violations == ["parse_fail"]
    assert doc.pm.replicates[0].deviations[0].line == "SEMIS"


def test_approval_time_is_rounded_to_the_slot(doc):
    assert doc.decision.approved_slot == datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
    assert doc.decision.human_outcome == "approved"


def test_calls_carry_no_error_text_and_normalised_shas(doc):
    news = [c for c in doc.calls if c.role == "news"][0]
    assert news.status == "timeout" and news.prompt_sha == "0e" * 32


def test_missing_pack_is_flagged(record, policy):
    doc = public_cycle(record, None, lines=policy.universe)
    assert "licensed_text_check_skipped" in doc.flags


def test_bad_cycle_id_fails_closed(policy):
    with pytest.raises(ValidationError):
        public_cycle(make_record(cycle_id="2026-10-01 14:40"), None, lines=policy.universe)


def test_public_models_forbid_extra_fields(doc):
    data = doc.model_dump(mode="json")
    data["amount_usd"] = PRIVATE_AMOUNT
    with pytest.raises(ValidationError):
        PublicCycleV1.model_validate(data)


def test_ops_row_counts_only(record):
    row = public_ops_row(record)
    assert (row.calls, row.calls_ok, row.timeouts, row.legs, row.duration_s) == (2, 1, 1, 1, 420)
    assert leakscan.scan(row, canaries=CANARIES) == []


def test_public_book_from_weights(policy):
    book = public_book("2026-10-01T1440Z", {"NDX": 0.35, "CSPX.L": 0.15, "EURUSD": -0.1}, lines=policy.universe,
                       reference_weights={"NDX": 0.35, "SPX": 0.15})
    assert book.lines["SPX"].weight_x == 0.15 and book.lines["EURUSD"].direction == "short"
    assert book.gross_x == 0.6 and book.net_x == 0.4 and book.cash_x == 0.4


def test_public_status_default_is_awaiting_account():
    assert public_status().state == "AWAITING_ACCOUNT"


@pytest.mark.parametrize("raw, gone", [
    ("pay $1,234.56 now", "1,234.56"),
    ("USD 1000 margin", "1000"),
    ("see /Users/someone/secret.txt", "/Users/"),
    ("mail me a@b.com", "a@b.com"),
    ("order 2951234567 filled", "2951234567"),
    ("id 5b0f2c4e-9a1d-4c3b-8e7f-0123456789ab", "5b0f2c4e"),
    ("\x1b[31mred\x1b[0m ‮evil", "\x1b"),
])
def test_clean_text_removes_leaks(raw, gone):
    assert gone not in clean_text(raw)


def test_clean_text_keeps_ordinary_prose():
    text = "Semis are 6.2% above the 200-day average; cut to 0.5 of the unit weight (EURUSD flat)."
    assert clean_text(text) == text


@pytest.mark.parametrize("field, value, ok", [
    ("gross_x", 1.9, True), ("gross_x", 7.0, False),
    ("margin_use_pct", 95.0, True), ("margin_use_pct", 5000.0, False),
    ("carry_bp_day", 2.0, True), ("carry_bp_day", float("nan"), False),
])
def test_public_ranges(doc, field, value, ok):
    data = doc.risk.model_dump()
    data[field] = value
    if ok:
        type(doc.risk).model_validate(data)
    else:
        with pytest.raises(ValidationError):
            type(doc.risk).model_validate(data)
