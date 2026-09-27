"""Pre-token publication safety (transparency-v2 package T0).

- Public hold reasons and the R15 check go through the closed trace table: R11 is one bare code,
  an R15 value survives only when the line's cost came from the policy floors and its volatility
  from Tiingo / Binance history.
- `P:` ids (public-domain news: SEC filing notices, Federal Reserve Board, BLS, BEA, Treasury, EIA)
  are accepted by the public models, redaction, the leak scan and the labels; news and event rows
  are labelled from their own source (an SEC item is never "broker feed").
- `literal_ok` checks a literal run without collapsing whitespace.
- The material-change fingerprint can be keyed by the private install key.
- Old journals still load, re-serialise to their exact bytes and open their commitments; the site
  builds from a journal with code-only hold reasons.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from council.models.cards import EvidenceCard
from council.models.facts import EventItem, Fact, FactPack
from council.models.risk import RiskCheck
from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish import commit_reveal, install_key, journal, labels, leakscan, redact
from council.publish.public_models import (
    EVIDENCE_ID_PATTERN,
    PUBLIC_NEWS_SOURCES,
    EvidenceRef,
    PublicCycleV1,
    PublicFact,
    PublicNewsRef,
    PublicReveal,
)
from council.publish.redact import (
    clean_text,
    literal_ok,
    public_cycle,
    public_ops_row,
    public_status,
)
from tests.publish.conftest import (
    CYCLE_ID,
    LICENSED_TITLE,
    PRIVATE_FINGERPRINT,
    SLOT,
    make_pack,
    make_record,
)

SEC_TITLE = "8-K: Item 2.02 Results of Operations and Financial Condition for the largest foundry"
FED_TITLE = "Federal Reserve issues FOMC statement on the target range for the federal funds rate"


def public_item(pid: str, source: str, title: str, *, licence: str = "public_domain", symbols=("SEMIS",)):
    """A public-domain news item as the T3a model will carry it (id P:, publisher, licence, link);
    duck-typed so these tests do not depend on that model."""
    at = SLOT - timedelta(hours=2)
    return SimpleNamespace(id=pid, title=title, summary="", symbols=list(symbols), published_at=at, available_at=at,
                           source=source, licence=licence, link="https://www.sec.gov/cgi-bin/browse-edgar")


def pack_with(*, news=(), events=(), facts=None, states=None) -> FactPack:
    base = make_pack()
    return FactPack.model_construct(**{
        **dict(base), "news": [*base.news, *news], "events": list(events),
        "facts": list(base.facts if facts is None else facts),
        "states": base.states if states is None else states,
    })


def card(card_id: str, claim: str, evidence: list[str]) -> EvidenceCard:
    return EvidenceCard(card_id=card_id, role="news", scope=["SEMIS"], card_type="news_context", direction="neutral",
                        claim=claim, evidence_ids=evidence, horizon_days=5)


# ------------------------------------------------------------------------------ literal_ok
@pytest.mark.parametrize("text", [
    "\n  LINES …\n", "", "  NDX · Nasdaq-100 · trend up\n\t band 0.50 to 1.00 · may cut\n",
    "HEADLINES (newest first)\n", "level grid: -0.5 -0.25 0 0.25 0.5 0.75 1 1.25 1.5\n",
])
def test_literal_ok_passes_code_text_with_its_whitespace(text):
    assert literal_ok(text)
    assert clean_text(text) != text or text == ""        # the old check would have failed these


@pytest.mark.parametrize("text", [
    "$5 fee", "fee 5 USD", "contact ops@example.com", "see https://example.com/x", "/Users/someone/state",
    "@handle", "5b0f2c4e-9a1d-4c3b-8e7f-0123456789ab", "UNMAPPED_100123", "position 2951234567",
    "NDX near 21,450", "gold 2650.40", "\x1b[2J", "bidi ‮",
])
def test_literal_ok_refuses_any_value_the_cleaner_would_remove(text):
    assert not literal_ok(text)


# ------------------------------------------------------------------------------ P: ids everywhere
def test_p_ids_survive_the_cleaner_and_the_leak_scan():
    text = "cites P:12345678, P:0a1b2c3d and N:1a2b3c4d"
    assert clean_text(text) == text
    assert leakscan.scan(text) == []
    assert [f.rule for f in leakscan.scan("order 12345678")] == ["long_number"]
    assert [f.rule for f in leakscan.scan("Q:12345678")] == ["long_number"]
    assert redact._LONG_NUMBER.pattern == dict(leakscan.VALUE_PATTERNS)["long_number"].pattern.replace(r"(?![\w])", r"(?!\w)")


def test_public_models_accept_p_ids_and_public_sources():
    assert re.match(EVIDENCE_ID_PATTERN, "P:0123abcd") and not re.match(EVIDENCE_ID_PATTERN, "P:0123ABCD")
    ref = TypeAdapter(EvidenceRef).validate_python({"kind": "public_news", "id": "P:0123abcd", "source": "sec"})
    assert isinstance(ref, PublicNewsRef) and ref.source == "sec"
    assert PublicNewsRef(id="P:0123abcd").model_dump() == {"kind": "public_news", "id": "P:0123abcd"}
    with pytest.raises(ValidationError):
        PublicNewsRef(id="N:0123abcd")
    with pytest.raises(ValidationError):
        PublicNewsRef(id="P:0123abcd", source="etoro_feed")
    assert {"sec", "fed_board", "bls", "bea", "treasury", "eia"} == PUBLIC_NEWS_SOURCES
    for source in PUBLIC_NEWS_SOURCES:
        PublicFact(id="P:0123abcd", kind="news", label="x", source=source)
    PublicFact(id="P:0123abcd", kind="news", label="x", source="unknown")
    with pytest.raises(ValidationError):
        PublicFact(id="P:0123abcd", kind="news", label="x", source="broker_feed")
    with pytest.raises(ValidationError):
        PublicFact(id="N:0123abcd", kind="news", label="x", source="sec")


def test_labels_split_broker_and_public_news():
    assert labels.fact_label("N:1a2b3c4d") == labels.NEWS_LABEL == "broker news item"
    assert labels.fact_label("P:1a2b3c4d") == labels.PUBLIC_NEWS_LABEL
    assert labels.news_label("P:1a2b3c4d", "sec") == "SEC filing notice"
    assert labels.news_label("P:1a2b3c4d", "etoro_feed") == labels.PUBLIC_NEWS_LABEL
    assert labels.news_label("N:1a2b3c4d", "sec") == "broker news item"
    assert "broker" not in " ".join(labels.PUBLIC_NEWS_LABELS.values()).lower()
    assert labels.attribution("sec") == "Source: U.S. Securities and Exchange Commission"
    assert "EDGAR" not in " ".join(labels.ATTRIBUTIONS.values())
    assert labels.attribution("eia", "2026-09-23").endswith("(2026-09-23)")
    assert labels.attribution("etoro_feed") == ""


def test_public_evidence_refs_carry_their_publisher_and_mismatches_are_dropped(policy):
    news = [public_item("P:0000aaaa", "sec", SEC_TITLE), public_item("P:0000bbbb", "etoro_feed", "Broker story")]
    rec = make_record()
    rec = rec.model_copy(update={"cards": [*rec.cards, card("K:news:3", "Filing notice on the foundry",
                                                            ["P:0000aaaa", "P:0000bbbb", "P:0000cccc"])]})
    doc = public_cycle(rec, pack_with(news=news), lines=policy.universe)
    refs = [r.model_dump() for r in doc.cards[-1].evidence]
    assert refs == [{"kind": "public_news", "id": "P:0000aaaa", "source": "sec"},
                    {"kind": "public_news", "id": "P:0000cccc"}]          # not in the pack: the id alone
    assert "news_licence_mismatch:1" in doc.flags
    assert "evidence_ids_dropped:1" in doc.flags


def test_news_and_event_rows_are_labelled_from_their_own_source(policy):
    news = [public_item("P:0000aaaa", "sec", SEC_TITLE), public_item("P:0000dddd", "fed_board", FED_TITLE, symbols=()),
            public_item("P:0000eeee", "treasury", "Treasury auction results", licence="federal_work_unverified",
                        symbols=()),
            public_item("P:0000bbbb", "sec", "A licensed copy", licence="broker_licensed"),
            public_item("P:0000ffff", "etoro_feed", "Broker story")]
    at = SLOT + timedelta(days=5)
    events = [EventItem(id="E:earnings:SEMIS@2026-10-06", kind="earnings", at_utc=at, symbols=["SEMIS"], severity=2,
                        source="sec_estimate"),
              EventItem(id="E:earnings:NDX@2026-10-02", kind="earnings", at_utc=at, symbols=["NDX"], severity=2,
                        source="sec_8k"),
              EventItem(id="E:fomc@2026-10-28", kind="fomc", at_utc=at, severity=3, source="policy_calendar"),
              EventItem(id="E:cpi@2026-10-14", kind="cpi", at_utc=at, severity=3, source="fred_release_10"),
              EventItem(id="E:earnings:SPX@2026-10-07", kind="earnings", at_utc=at, symbols=["SPX"], severity=2,
                        source="etoro_feed"),
              EventItem(id="E:pce@2026-10-30", kind="pce", at_utc=at, severity=2, source="somewhere")]
    doc = public_cycle(make_record(), pack_with(news=news, events=events), lines=policy.universe)
    rows = {f.id: f for f in doc.facts}
    assert (rows["P:0000aaaa"].source, rows["P:0000aaaa"].label) == ("sec", "SEC filing notice")
    assert rows["P:0000dddd"].source == "fed_board" and rows["P:0000eeee"].source == "treasury"
    assert rows["P:0000bbbb"].source == rows["P:0000ffff"].source == "unknown"     # failed the public test
    assert (rows["N:1a2b3c4d"].source, rows["N:1a2b3c4d"].label) == ("broker_feed", "broker news item")
    assert rows["E:earnings:SEMIS@2026-10-06"].source == rows["E:earnings:NDX@2026-10-02"].source == "sec"
    assert rows["E:fomc@2026-10-28"].source == rows["E:cpi@2026-10-14"].source == "calendar"
    assert rows["E:earnings:SPX@2026-10-07"].source == "broker_feed"
    assert rows["E:pce@2026-10-30"].source == "unknown"
    assert "news_licence_mismatch:2" in doc.flags
    assert all(rows[i].value is None for i in rows if i.startswith(("N:", "P:")))


def test_only_licensed_items_feed_the_overlap_check(policy):
    news = [public_item("P:0000aaaa", "sec", SEC_TITLE), public_item("P:0000bbbb", "sec", FED_TITLE, licence="")]
    rec = make_record()
    rec = rec.model_copy(update={"cards": [
        card("K:news:3", SEC_TITLE, ["P:0000aaaa"]),                  # a public title may be repeated
        card("K:news:4", FED_TITLE, ["P:0000aaaa"]),                  # an item without a licence counts as licensed
        card("K:news:5", f"Chipmakers rally as the largest foundry lifts guidance: {LICENSED_TITLE}", ["N:1a2b3c4d"]),
    ]})
    doc = public_cycle(rec, pack_with(news=news), lines=policy.universe)
    claims = {c.card_id: c.claim for c in doc.cards}
    assert claims["K:news:3"] == SEC_TITLE
    assert claims["K:news:4"] == redact.WITHHELD_LICENSED
    assert claims["K:news:5"] == redact.WITHHELD_LICENSED
    assert redact.licensed_texts(pack_with(news=news)) == [
        f"{make_pack().news[0].title} {make_pack().news[0].summary}", f"{FED_TITLE} "]


# ------------------------------------------------------------------------------ hold reasons and R15
def _floor_pack(source: str = "costs:floor", history: str = "tiingo:SPY") -> FactPack:
    base = make_pack()
    states = {s: st.model_copy(update={"history_source": history}) for s, st in base.states.items()}
    costs = [Fact(id=f"C:{s}:per_side_bps", kind="cost", symbol=s, value=5.0, unit="bps", available_at=SLOT,
                  source=source) for s in base.states]
    facts = [f for f in base.facts if not f.id.startswith("C:")] + costs
    return pack_with(facts=facts, states=states)


HOLDS = ["SEMIS: R11 below the minimum trade size", "GOLD: R11 deadband (level step -0.25)",
         "NDX: R11 reference rule (level unchanged, drift below the threshold)",
         "SPX: R15 SR_be 0.32 above 0.20", "BTC: R15_fee net-of-cost gate (fixed fee)",
         "ETH: R10 level +1.50 outside band [+0.50, +1.00]", "OIL: limited by R16 event window",
         "no broker snapshot: current book taken as flat"]


def _with_holds(r15_value: float = 0.41):
    rec = make_record()
    r15 = RiskCheck(rule_id="R15", name="net_of_cost_gate", passed=True, value=r15_value,
                    limit="reference 0.3, council 0.2")
    return rec.model_copy(update={"risk": rec.risk.model_copy(update={"hold_reasons": HOLDS,
                                                                      "checks": [*rec.risk.checks, r15]})})


def test_public_hold_reasons_publish_codes_without_nav_numbers(policy):
    doc = public_cycle(_with_holds(), _floor_pack("costs:whatif"), lines=policy.universe)
    assert doc.risk.hold_reasons == [
        "SEMIS: R11", "GOLD: R11", "NDX: R11", "SPX: R15", "BTC: R15_fee",
        "ETH: R10 level +1.50 outside band [+0.50, +1.00]", "OIL: limited by R16 event window",
        "no broker snapshot: current book taken as flat"]
    assert next(c for c in doc.risk.checks if c.rule_id == "R15").value is None


def test_an_r15_hold_priced_from_the_floors_keeps_its_value(policy):
    doc = public_cycle(_with_holds(), _floor_pack(), lines=policy.universe)
    assert "SPX: R15 SR_be 0.32 above 0.20" in doc.risk.hold_reasons
    assert next(c for c in doc.risk.checks if c.rule_id == "R15").value == 0.41


@pytest.mark.parametrize("pack_kind", ["broker_history", "unlabelled_cost", "no_pack"])
def test_r15_withholds_its_value_when_any_input_is_not_public(policy, pack_kind):
    pack = {"broker_history": _floor_pack(history="etoro:SPX500"), "unlabelled_cost": _floor_pack("costs"),
            "no_pack": None}[pack_kind]
    doc = public_cycle(_with_holds(), pack, lines=policy.universe)
    assert "SPX: R15" in doc.risk.hold_reasons and "SPX: R15 SR_be 0.32 above 0.20" not in doc.risk.hold_reasons
    assert next(c for c in doc.risk.checks if c.rule_id == "R15").value is None


def test_no_public_hold_reason_carries_a_number_unless_the_table_allows_it(policy):
    doc = public_cycle(_with_holds(), _floor_pack("costs:whatif"), lines=policy.universe)
    for reason in doc.risk.hold_reasons:
        if reason.startswith("ETH: R10"):
            continue                                         # public levels and band edges
        assert not re.search(r"\d", re.sub(r"\bR\d{1,2}[a-z]?(?:_fee)?\b", "", reason)), reason


# ------------------------------------------------------------------------------ keyed fingerprint
def test_the_keyed_fingerprint_is_stable_per_key_and_differs_across_keys(policy, tmp_path):
    key = install_key.load_or_create(tmp_path / "state")
    other = bytes(range(32))
    a = redact.short_fingerprint(PRIVATE_FINGERPRINT, key)
    assert a == redact.short_fingerprint(PRIVATE_FINGERPRINT, key) and len(a) == 16
    assert a != redact.short_fingerprint(PRIVATE_FINGERPRINT, other)
    assert a != redact.short_fingerprint(PRIVATE_FINGERPRINT)                        # not the unkeyed hash
    assert redact.short_fingerprint("sha256:" + "ab" * 32) == "ab" * 8               # older callers: unchanged
    assert redact.short_fingerprint("", key) == ""
    first = public_cycle(make_record(), make_pack(), lines=policy.universe, install_key=key)
    second = public_cycle(make_record(cycle_id="2026-10-01T1840Z"), make_pack(), lines=policy.universe,
                          install_key=key)
    assert first.material_fingerprint == second.material_fingerprint == a
    assert public_cycle(make_record(), make_pack(), lines=policy.universe,
                        install_key=other).material_fingerprint != a


def test_the_install_key_is_private_state(tmp_path):
    state = tmp_path / "state"
    key = install_key.load_or_create(state)
    path = install_key.key_path(state)
    assert len(key) == 32 and install_key.load_or_create(state) == key == install_key.load(state)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    os.chmod(path, 0o644)
    assert install_key.load(state) == key and stat.S_IMODE(path.stat().st_mode) == 0o600   # tightened
    path.write_bytes(b"short")
    with pytest.raises(install_key.InstallKeyError) as exc:
        install_key.load_or_create(state)
    assert "short" not in str(exc.value)
    with pytest.raises(RuntimeError):
        install_key.load_or_create(REPO_ROOT / "tmp-state")
    assert not (REPO_ROOT / "tmp-state").exists()
    assert install_key.key_path() == Path(os.environ["COUNCIL_STATE_DIR"]) / "keys" / "install.key"


# ------------------------------------------------------------------------------ keys the later packages add
LATER_KEYS = ("published", "link", "form", "items", "commit", "runs", "lits", "base", "patch", "sections",
              "triage", "why", "code", "line_trace", "before_x", "after_x", "lo_x", "hi_x", "masked",
              "disposition", "read_by", "cited_by", "attribution", "inputs_file", "attempt", "cited_ids",
              "not_used", "code_dirty", "source", "kind", "id")


def test_the_transparency_keys_pass_the_key_denylist():
    assert [k for k in LATER_KEYS if leakscan.key_denied(k)] == []


# ------------------------------------------------------------------------------ old journals
def _journals() -> list[Path]:
    roots = [REPO_ROOT / "journal", REPO_ROOT / "tests" / "fixtures" / "site_journal" / "journal"]
    return [p for root in roots for p in sorted((root / "cycles").rglob("*.json"))
            if not p.name.endswith(".reveal.json")]


@pytest.mark.parametrize("path", _journals(), ids=lambda p: f"{p.parts[-5]}/{p.stem}")
def test_old_journals_load_reserialise_and_open_their_commitments(path):
    sealed = path.read_bytes()
    doc = PublicCycleV1.model_validate_json(sealed)
    assert commit_reveal.canonical_json(doc) == sealed
    reveal = PublicReveal.model_validate_json(path.with_name(f"{path.stem}.reveal.json").read_text())
    root = path.parents[3]
    commitment = (root / "commitments" / path.parts[-3] / path.parts[-2] / path.name).read_text()
    assert commit_reveal.verify_bytes(sealed, reveal.salt, json.loads(commitment)["commitment_sha256"])


# ------------------------------------------------------------------------------ the site
def _site():
    spec = importlib.util.spec_from_file_location("council_site_build_tr", REPO_ROOT / "site" / "build.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build_tr"] = module
    spec.loader.exec_module(module)
    return module


def test_the_site_builds_with_code_only_hold_reasons(policy, tmp_path):
    doc = public_cycle(_with_holds(), _floor_pack("costs:whatif"), lines=policy.universe)
    assert "SEMIS: R11" in doc.risk.hold_reasons
    commitment, salt, sealed = commit_reveal.seal_bytes(doc, sealed_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC))
    files = journal.commitment_files(commitment) | journal.reveal_files(sealed, salt, commitment)
    files |= journal.status_files(public_status("AWAITING_ACCOUNT", last_cycle_id=CYCLE_ID, last_cycle_at=SLOT))
    files |= journal.ops_files(None, [public_ops_row(make_record())])
    journal.write_files(tmp_path / "pub", files)
    out = tmp_path / "out"
    _site().build(tmp_path / "pub" / "journal", PROMPTS_DIR, POLICY_DIR, out, now=SLOT + timedelta(hours=3))
    page = (out / "cycles" / f"{CYCLE_ID}.html").read_text()
    assert "minimum trade size" not in page and "SR_be" not in page and "level step" not in page
    assert leakscan.scan_paths([out]) == []


def test_the_private_size_floor_flag_is_never_published(policy):
    rec = make_record(flags=["late", "size_floor_binding:SPX", " size_floor_binding:GOLD"])
    assert public_cycle(rec, make_pack(), lines=policy.universe).flags[:1] == ["late"]
    assert not any("size_floor" in f for f in public_cycle(rec, make_pack(), lines=policy.universe).flags)
    assert public_ops_row(rec).flags == ["late"]
