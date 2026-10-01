"""Private cycle record -> public document, built FIELD BY FIELD from allow-listed models.

Rules:
- Nothing is produced by deleting fields from a private object: every public value is read from a
  named private field, converted to a percentage unit and rounded (x 0.001, % 0.01, bp 0.1).
- Private execution fields (amounts, units, stop rates, position/instrument ids, request ids,
  error strings) are never read.
- Model text is untrusted: control/bidi characters, URLs, @handles, e-mails, paths, money amounts
  and long numbers are removed, and any text sharing an 8-word n-gram with a licensed feed item
  is withheld. Licensed means every news item except a `P:` item that passes the public test
  (`public_news_ok`: a `P:` id, a public publisher and a public-domain licence).
- Evidence ids become typed refs: N: -> broker_feed id only; P: -> public_news id with its
  publisher (a `P:` id whose pack item fails the public test is dropped and counted in
  `news_licence_mismatch:<n>`); M: -> FRED series (value only when the series is in
  `council.data.fred.PUBLISHABLE`, the single publishability list, and the pack's fact is not
  marked unpublishable); F:/V:/C:/E:/S:/K: -> ids.
- Risk-engine notes go through `trace_rules` (one closed table): every R11 variant publishes as
  `R11` alone, an R15 hold or check shows its SR_be only when the line's cost came from the policy
  floors and its volatility from Tiingo / Binance history, fee codes never carry a value, and a
  note the table does not list publishes as its bare code (transparency-v2 §4.2).
- Source labels come from each item's own source: a news row is `broker_feed` for an `N:` item
  and its publisher for a public `P:` item; an event is `calendar` (policy calendar, FRED release
  dates), `sec` (SEC-derived earnings), `broker_feed` (the broker feed) or `unknown`.
- Symbols are published only as exposure LINES; unknown symbols are dropped and counted, and
  `UNMAPPED_<instrument id>` is scrubbed to `UNMAPPED` in any published text.
- Approval timestamps are rounded down to their slot; the material-change fingerprint is
  published as a short hash only, keyed by the private install key when the caller passes it
  (`public_cycle(..., install_key=...)`; an unkeyed hash of low-entropy content could be
  brute-forced).
- `literal_ok(text)` is the check for a literal run of a structured prompt section: True only when
  none of the cleaner's value substitutions would fire (whitespace and length are left alone).
- The execution record (`public_execution`) carries weights (x NAV), slippage and cost in bp,
  and leg states: never amounts, units, prices or ids.
- Model text is published whole up to its public cap; a text that must be cut (a cleaner
  placeholder pushed it over) is cut at a sentence end, else a word end, and marked "…".
- The facts table lists every fact of the pack the agents saw, with a value only where
  docs/data-rights.md allows it (`_facts`); call errors are published as fixed codes only
  (`error_kind`), never the private error text.
- The book (`public_book`) may describe each line (name, asset class, session from the public
  policy; settlement, leverage and P/L % since open from the open positions; the 1-day market
  move from the pack): percentages and words only.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, get_args

from council import clock
from council.data import fred
from council.models.cards import EvidenceCard
from council.models.cycle import CycleRecord, PMReplicate
from council.models.debate import AdvocateCase, BearCase
from council.models.facts import Fact, FactPack, MarketState
from council.models.plan import Leg, Plan
from council.models.risk import RiskDecision
from council.policy import LineSpec, Universe, default_policy
from council.publish import labels, leakscan, trace_rules
from council.publish.install_key import keyed_hex
from council.publish.public_models import (
    PUBLIC_NEWS_SOURCES,
    CallStatus,
    CycleStatus,
    DecisionState,
    KillState,
    LegKind,
    LegState,
    PublicAdvocate,
    PublicBand,
    PublicBook,
    PublicBookLine,
    PublicCall,
    PublicCard,
    PublicCheck,
    PublicClaim,
    PublicCycleV1,
    PublicDebate,
    PublicDecision,
    PublicDecisiveFact,
    PublicDeviation,
    PublicDismissal,
    PublicExecution,
    PublicFact,
    PublicFill,
    PublicLeg,
    PublicMacro,
    PublicMacroDriver,
    PublicOpsRow,
    PublicPlan,
    PublicPM,
    PublicPMReplicate,
    PublicRebuttal,
    PublicReferenceLine,
    PublicRisk,
    PublicStatus,
    Settlement,
    StatusState,
)

WITHHELD_LICENSED = "[withheld: overlaps licensed feed text]"
X_DP, PCT_DP, BP_DP, LEVEL_DP = 3, 2, 1, 2

# ------------------------------------------------------------------------------ text hygiene
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")
_URL = re.compile(r"(?i)\b(?:https?://|ftp://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|ai|co|xyz|app|dev)/\S*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{2,}")
_PATH = re.compile(r"(?:~[/\\]|/Users/|/home/|/private/|/var/folders/|\b[A-Za-z]:\\)\S*")
_MONEY = re.compile(
    r"(?i)[$€£]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|bn|b|mn)\b)?"
    r"|\b(?:USD|EUR|GBP)\s?\d[\d,]*(?:\.\d+)?"
    r"|\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|dollars?)\b"
)
_LONG_NUMBER = re.compile(r"(?<![\w.])(?<!\b[NPSMECFVK]:)\d{7,}(?!\w)")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# Positions on symbols outside the universe are keyed UNMAPPED_<instrument id>; the id is private.
_UNMAPPED = re.compile(r"(?i)UNMAPPED_[0-9A-Za-z]+")
# A bare price level a model may paraphrase from broker data ("Brent at 78.40" is too short to
# tell from a percentage, but "gold near 2,650.40", "NDX near 21,450" or "1098.4" are levels):
# a number with thousands separators, 3+ integer digits with decimals, or 4+ digits, unless a
# unit the record allows follows it. Years (19xx / 20xx), index names (S&P 500, Nasdaq-100) and
# identifiers (F:NDX:..., bear:c2) are left alone.
_LEVEL = re.compile(
    r"(?<![\w.:%,/#@-])"
    r"(?!(?:19|20)\d{2}(?![\d,.]))"
    r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{3,}\.\d+|\d{4,})"
    r"(?![\w%])(?!\.\d)"
    r"(?![\s-]?(?:x\b|×|bps?\b|basis\b|σ|sigma\b|days?\b|h\b|hours?\b|min\b|minutes?\b|s\b|"
    r"seconds?\b|chars?\b|characters\b|tokens?\b|lines?\b|calls?\b|runs?\b|percent\b|pct\b|"
    r"per ?cent\b|%))"
)


def _clip(text: str, max_len: int) -> str:
    """Cut an over-long text at its last sentence end (else word end) inside the cap, marked "…".
    A cut never lands mid-word unless a single word fills most of the cap."""
    if len(text) <= max_len:
        return text
    if max_len < 2:
        return text[:max_len]
    window = text[: max_len - 1]
    floor = int(max_len * 0.6)
    sentence = max(window[:-1].rfind(p) for p in (". ", "! ", "? ")) if len(window) > 1 else -1
    if sentence >= floor and sentence + 3 <= max_len:
        return window[: sentence + 1] + " …"
    space = window.rfind(" ")
    if space >= floor:
        return window[:space].rstrip(",;:—- ") + "…"
    return window.rstrip() + "…"


def clean_text(value: str | None, max_len: int = 200) -> str:
    """Neutralise untrusted text for the terminal, the site and notifications."""
    if not value:
        return ""
    text = _ANSI.sub("", str(value))
    text = _CONTROL.sub(" ", text)
    text = _EMAIL.sub("[email removed]", text)
    text = _URL.sub("[link removed]", text)
    text = _PATH.sub("[path removed]", text)
    text = _HANDLE.sub("[handle removed]", text)
    text = _UUID.sub("[id removed]", text)
    text = _UNMAPPED.sub("UNMAPPED", text)
    text = _MONEY.sub("[amount removed]", text)
    text = _LONG_NUMBER.sub("[number removed]", text)
    text = _LEVEL.sub("[level removed]", text)
    text = " ".join(text.split())
    return _clip(text, max_len)


# The value substitutions of `clean_text`, in its order (whitespace collapsing and clipping aside).
_VALUE_SUBS: tuple[re.Pattern[str], ...] = (
    _ANSI, _CONTROL, _EMAIL, _URL, _PATH, _HANDLE, _UUID, _UNMAPPED, _MONEY, _LONG_NUMBER, _LEVEL,
)


def literal_ok(text: str | None) -> bool:
    """True when a literal run (code text and policy numbers of a structured prompt section) is
    publishable as written: none of `clean_text`'s value substitutions (control or escape
    characters, e-mail, URL, path, handle, UUID, UNMAPPED id, money, long number, level) would
    change it. Unlike `clean_text(text) == text` it neither collapses whitespace nor clips, so
    newlines and indentation pass. `literal_ok("\n  LINES\n")` is True; `literal_ok("$5 fee")`
    is False."""
    value = str(text or "")
    return not any(pattern.search(value) for pattern in _VALUE_SUBS)


class _Text:
    """Text cleaner bound to one cycle's licensed texts (broker feed titles and summaries)."""

    def __init__(self, licensed_texts: Iterable[str], n: int = 8):
        self.n = n
        self.grams: set[tuple[str, ...]] = set()
        for text in licensed_texts:
            self.grams |= leakscan.ngrams(text, n)
        self.withheld = 0

    def __call__(self, value: str | None, max_len: int = 200) -> str:
        text = clean_text(value, max_len)
        if self.grams and leakscan.ngrams(text, self.n) & self.grams:
            self.withheld += 1
            return WITHHELD_LICENSED[:max_len]
        return text


def _code(value: str, max_len: int = 120) -> str:
    return clean_text(value, max_len)


# Record flags that stay private: `size_floor_binding:<line>` says the size floor exceeds the public
# deadband share at the current NAV (m5-readiness M5-N gate P2), which bounds the NAV.
PRIVATE_FLAG_PREFIXES: tuple[str, ...] = ("size_floor_binding",)


def public_flags(flags: Iterable[str]) -> list[str]:
    """Record flags as public codes, without the private ones."""
    return [_code(trace_rules.public_swing_flag(str(f))) for f in flags if not str(f).strip().startswith(PRIVATE_FLAG_PREFIXES)]


# ------------------------------------------------------------------------------ numbers
def _x(v: float | None) -> float:
    return round(float(v or 0.0), X_DP) + 0.0


def _pct(fraction: float | None) -> float:
    return round(float(fraction or 0.0) * 100.0, PCT_DP) + 0.0


def _bp(v: float | None) -> float:
    return round(float(v or 0.0), BP_DP) + 0.0


def _level(v: float) -> float:
    return round(float(v), LEVEL_DP) + 0.0


_SHA = re.compile(r"^[0-9a-f]{8,64}$")
_MODEL = re.compile(r"^[A-Za-z0-9:._/-]{0,80}$")


def _sha(value: str | None) -> str:
    v = (value or "").strip().lower().removeprefix("sha256:")
    return v if _SHA.match(v) else ""


def _model_name(value: str | None) -> str:
    v = (value or "").strip()
    return v if _MODEL.match(v) else ""


def _role(value: str) -> str:
    v = re.sub(r"[^a-z0-9_]", "_", (value or "").strip().lower())[:32]
    if not v or not v[0].isalpha():
        v = ("r_" + v)[:32]
    return v


# ------------------------------------------------------------------------------ lines
class LineMap:
    """Maps line symbols and their vehicle symbols to the exposure line; nothing else passes."""

    def __init__(self, lines: Iterable[LineSpec] | Universe | Mapping[str, LineSpec] | None):
        if lines is None:
            specs = list(default_policy().universe.lines)
        elif isinstance(lines, Universe):
            specs = list(lines.lines)
        elif isinstance(lines, Mapping):
            specs = list(lines.values())
        else:
            specs = list(lines)
        self.order = [s.symbol for s in specs]
        self.specs: dict[str, LineSpec] = {s.symbol: s for s in specs}
        self._map: dict[str, str] = {s.symbol: s.symbol for s in specs}
        for spec in specs:
            for vehicle in (*spec.vehicles.long, *spec.vehicles.short):
                self._map.setdefault(vehicle.symbol, spec.symbol)
        self.unmapped = 0

    def line(self, symbol: str) -> str | None:
        """The line for a symbol; an unknown symbol is dropped and counted (never published)."""
        found = self._map.get(symbol)
        if found is None:
            self.unmapped += 1
        return found

    def lookup(self, symbol: str) -> str | None:
        """Like `line` but without counting: for free-form scopes such as "market"."""
        return self._map.get(symbol)

    def sum_by_line(self, values: Mapping[str, float], convert=_x) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym, v in values.items():
            line = self.line(sym)
            if line is not None:
                out[line] = out.get(line, 0.0) + float(v)
        return {k: convert(out[k]) for k in self.ordered(out)}

    def first_by_line(self, values: Mapping[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym, v in values.items():
            line = self.line(sym)
            if line is not None and (line not in out or sym == line):
                out[line] = _level(v)
        return {k: out[k] for k in self.ordered(out)}

    def ordered(self, d: Mapping[str, Any]) -> list[str]:
        return sorted(d, key=lambda k: (self.order.index(k) if k in self.order else len(self.order), k))


# ------------------------------------------------------------------------------ evidence
_ID_KIND = {"F": "market", "V": "vol", "C": "cost", "E": "event", "S": "filing", "K": "card",
            "N": "broker_feed", "P": "public_news"}
_ID_OK = re.compile(r"^[FVCESK]:[A-Za-z0-9_.:@#+-]{1,80}$")
_NEWS_OK = re.compile(r"^N:[0-9a-f]{8}$")                # a broker feed item (licensed)
_PUBLIC_NEWS_OK = re.compile(r"^P:[0-9a-f]{8}$")         # a public-domain news item
# Licences under which a public item's title and link may be republished (Treasury's site carries
# no explicit statement: a federal work, title and link only; see docs/data-rights.md).
PUBLIC_LICENCES = frozenset({"public_domain", "federal_work_unverified"})
# M:<SERIES>[.<measure>][@YYYY-MM-DD], e.g. M:DGS10@2026-09-24 or M:DGS10.chg20@2026-09-24.
_MACRO = re.compile(r"^M:([A-Z0-9_]{1,32})(?:\.([a-z0-9_]{1,16}))?(?:@(\d{4}-\d{2}-\d{2}))?$")
_FRED_UNITS = frozenset({"pct", "bps", "x", "ratio"})
_NO_PUBLISH_SOURCES = frozenset({"fred:no_publish"})


def public_news_ok(item: Any) -> bool:
    """The public test of a news item (transparency-v2 T-D6), decided by three facts and never by
    the id prefix alone: a `P:` id, a publisher in the public set and a public-domain licence. An
    item without a licence fails (fail closed); every item that fails is treated as licensed."""
    return bool(
        _PUBLIC_NEWS_OK.match(str(getattr(item, "id", "") or ""))
        and getattr(item, "source", None) in PUBLIC_NEWS_SOURCES
        and getattr(item, "licence", None) in PUBLIC_LICENCES
    )


def licensed_texts(pack: FactPack | None) -> list[str]:
    """The texts the licensed-overlap check guards: every news item that fails the public test."""
    if pack is None:
        return []
    return [f"{n.title} {n.summary}" for n in pack.news if not public_news_ok(n)]


def _fred_publishable(series: str, fact: Fact | None) -> bool:
    """One list decides (council.data.fred.PUBLISHABLE); a pack fact can only make it stricter."""
    if not fred.is_publishable(series):
        return False
    return fact is None or (fact.publishable and fact.source not in _NO_PUBLISH_SOURCES)


class _Evidence:
    def __init__(self, pack: FactPack | None):
        self.facts: dict[str, Fact] = {}
        self.news: dict[str, Any] = {}
        if pack is not None:
            self.facts = {f.id: f for f in pack.facts if f.id.startswith("M:")}
            self.news = {str(n.id): n for n in pack.news}
        self.dropped = 0
        self.mismatch: set[str] = set()

    def ref(self, evidence_id: str | None) -> dict[str, Any] | None:
        eid = (evidence_id or "").strip()
        if _NEWS_OK.match(eid):
            return {"kind": "broker_feed", "id": eid}
        if _PUBLIC_NEWS_OK.match(eid):
            item = self.news.get(eid)
            if item is None:                  # not checkable here: the id alone (a public hash)
                return {"kind": "public_news", "id": eid}
            if not public_news_ok(item):      # a P: id on a licensed or unknown item: fail closed
                self.mismatch.add(eid)
                self.dropped += 1
                return None
            return {"kind": "public_news", "id": eid, "source": item.source}
        macro = _MACRO.match(eid)
        if macro:
            series, measure, as_of = macro.group(1), macro.group(2), macro.group(3)
            fact = self.facts.get(eid)
            publishable = _fred_publishable(series, fact)
            ref: dict[str, Any] = {
                "kind": "fred", "series": series, "measure": measure, "as_of": as_of, "publishable": publishable,
            }
            value = fact.value if fact is not None else None
            if (publishable and isinstance(value, int | float) and not isinstance(value, bool)
                    and math.isfinite(float(value))):
                ref["value"] = round(float(value), 4)
                if fact is not None and fact.unit in _FRED_UNITS:
                    ref["unit"] = fact.unit
            return ref
        if _ID_OK.match(eid):
            return {"kind": _ID_KIND[eid[0]], "id": eid}
        self.dropped += 1
        return None

    def refs(self, ids: Iterable[str], limit: int) -> list[dict[str, Any]]:
        out = [r for r in (self.ref(i) for i in ids) if r is not None]
        return out[:limit]


# ------------------------------------------------------------------------------ market moves
# Histories whose derived percentages the record may show (docs/data-rights.md); broker candles
# are shown only as coarse states and volatility ratios.
_OPEN_HISTORY = frozenset({"tiingo", "binance"})
_PCT_BOUND = 1000.0


def _history_source(value: str | None) -> str:
    return (value or "").split(":", 1)[0].strip().lower()


def day_change_pct(state: MarketState | None) -> float | None:
    """The last completed daily return in percent, derived from the state's own numbers
    (ret1d_sigma x sigma_daily is the daily log return). None without them, or when the history
    is not Tiingo / Binance (broker candles are not republished)."""
    if state is None or _history_source(state.history_source) not in _OPEN_HISTORY:
        return None
    z, sigma = state.ret1d_sigma, state.sigma_daily
    if z is None or sigma is None or not (math.isfinite(z) and math.isfinite(sigma)) or sigma <= 0:
        return None
    pct = round(math.expm1(z * sigma) * 100.0, PCT_DP) + 0.0
    return pct if abs(pct) <= _PCT_BOUND else None


def day_changes(pack: FactPack | None, lm: LineMap) -> dict[str, float]:
    """{line: day_change_pct} for every line of the pack that has one."""
    if pack is None:
        return {}
    out: dict[str, float] = {}
    for sym, state in pack.states.items():
        line = lm.lookup(sym)
        value = day_change_pct(state)
        if line is not None and value is not None:
            out[line] = value
    return out


# ------------------------------------------------------------------------------ builders
def _reference(rec: CycleRecord, lm: LineMap, day: Mapping[str, float]) -> dict[str, PublicReferenceLine]:
    if rec.reference is None:
        return {}
    out: dict[str, PublicReferenceLine] = {}
    for sym, entry in rec.reference.entries.items():
        line = lm.line(sym)
        if line is None:
            continue
        out[line] = PublicReferenceLine(
            trend=entry.trend, level_ref=_level(entry.level_ref), weight_ref_x=_x(entry.weight_ref),
            day_change_pct=day.get(line),
        )
    return out


def _card(card: EvidenceCard, lm: LineMap, text: _Text, ev: _Evidence) -> PublicCard:
    scope = [lm.lookup(s) or clean_text(s, 24) for s in card.scope]
    return PublicCard(
        card_id=card.card_id,
        role=_role(card.role),
        card_type=card.card_type,
        scope=scope[:6],
        direction=card.direction,
        claim=text(card.claim, 220),
        horizon_days=card.horizon_days,
        falsifier=text(card.falsifier, 180),
        qualifying=card.qualifying,
        corroborated_by=[c for c in card.corroborated_by if re.match(r"^K:[a-z_]+:\d+$", c)][:8],
        evidence=ev.refs(card.evidence_ids, 8),
    )


def _advocate(case: AdvocateCase | None, lm: LineMap, text: _Text, ev: _Evidence) -> PublicAdvocate | None:
    if case is None:
        return None
    rebuttals = []
    if isinstance(case, BearCase):
        rebuttals = [
            PublicRebuttal(
                claim_id=clean_text(r.claim_id, 16), verdict=r.verdict, text=text(r.text, 270),
                evidence=ev.refs(r.evidence_ids, 4),
            )
            for r in case.rebuttals
        ]
    return PublicAdvocate(
        argument=text(case.argument, 1600),
        proposal_levels=lm.first_by_line(case.proposal),
        claims=[
            PublicClaim(claim_id=c.claim_id, text=text(c.text, 330), evidence=ev.refs(c.evidence_ids, 6))
            for c in case.claims
        ],
        concessions=[text(c, 300) for c in case.concessions][:4],
        strongest_opposing=ev.ref(case.strongest_opposing_fact_id),
        rebuttals=rebuttals[:6],
    )


def _replicate(
    rep: PMReplicate, ref_levels: dict[str, float], lm: LineMap, text: _Text, ev: _Evidence,
) -> PublicPMReplicate:
    decision = rep.decision
    if rep.enforced_levels:
        levels = lm.first_by_line(rep.enforced_levels)
    elif decision is not None:
        levels = lm.first_by_line(decision.levels(ref_levels))
    else:
        levels = {}
    deviations: list[PublicDeviation] = []
    if decision is not None:
        for dev in decision.deviations:
            line = lm.line(dev.symbol)
            if line is None:
                continue
            deviations.append(PublicDeviation(
                line=line, level=_level(dev.level), direction=dev.direction,
                reason=text(dev.reason, 180), evidence=ev.refs(dev.evidence_ids, 6),
            ))
    return PublicPMReplicate(
        replicate=rep.replicate,
        valid=rep.valid,
        levels=levels,
        deviations=deviations[:3],
        decisive_fact=(
            PublicDecisiveFact(
                text=text(decision.decisive_fact.text, 220),
                evidence=ev.ref(decision.decisive_fact.evidence_id),
            )
            if decision is not None else None
        ),
        sided_with=decision.sided_with if decision is not None else None,
        dismissed=(
            [PublicDismissal(claim_id=clean_text(d.claim_id, 16), why=text(d.why, 180)) for d in decision.dismissed][:6]
            if decision is not None else []
        ),
        no_change_reason=text(decision.no_change_reason, 220) if decision is not None else "",
        violations=[_code(v) for v in rep.audit_violations],
        reverted=[_code(v) for v in rep.reverted],
    )


def _agreement_pct(agreement: Mapping[str, float], lm: LineMap) -> dict[str, float]:
    """Per-line share of valid replicates in the medoid's action class (0..1) -> percent."""
    out: dict[str, float] = {}
    for sym, share in agreement.items():
        line = lm.line(sym)
        if line is None or not isinstance(share, int | float) or not math.isfinite(float(share)):
            continue
        out[line] = _pct(min(1.0, max(0.0, float(share))))
    return {k: out[k] for k in lm.ordered(out)}


def _pm_block(
    reps: list[PMReplicate], medoid: int | None, ref_levels: dict[str, float],
    lm: LineMap, text: _Text, ev: _Evidence, *,
    levels: Mapping[str, float], agreement: Mapping[str, float],
) -> PublicPM:
    public = [_replicate(r, ref_levels, lm, text, ev) for r in reps]
    return PublicPM(
        replicates=public,
        medoid=medoid,
        levels=lm.first_by_line(levels),
        agreement_pct=_agreement_pct(agreement, lm),
        valid_replicates=sum(1 for p in public if p.valid),
    )


def _single_agent(rec: CycleRecord, ref_levels, lm, text, ev) -> PublicPM | None:
    """The single-agent control (C10) from the typed record fields; None when it did not run."""
    if not rec.single_agent and not rec.single_agent_levels:
        return None
    return _pm_block(rec.single_agent, None, ref_levels, lm, text, ev,
                     levels=rec.single_agent_levels, agreement={})


_CARD_ID = re.compile(r"^K:[a-z_]+:\d+$")
_SLEEVE = re.compile(r"^[a-z][a-z_]{0,15}$")


def _macro(rec: CycleRecord, text: _Text, ev: _Evidence) -> PublicMacro | None:
    """The macro analyst's typed output (None when it did not run or its reply was unusable)."""
    out = rec.macro
    if out is None:
        return None
    return PublicMacro(
        regime=out.regime,
        drivers=[PublicMacroDriver(text=text(d.text, 220), evidence=ev.refs(d.evidence_ids, 6))
                 for d in out.drivers][:4],
        sleeve_tilts={k: v for k, v in sorted(out.sleeve_tilts.items()) if _SLEEVE.match(k)},
        cards=[c.card_id for c in rec.cards if c.role == "macro" and _CARD_ID.match(c.card_id)][:8],
    )


# ------------------------------------------------------------------------------ facts table
# Rounding by unit (digits); the record's units: pct 0.01, ratio / x 0.001, bps 0.1.
_FACT_DIGITS = {"pct": 2, "sigma": 2, "ratio": 3, "x": 3, "bps": 1, "bps_day": 2, "hours": 1, "days": 1}
_FACT_UNITS = frozenset({*_FACT_DIGITS, "state"})
_STATE_WORD = re.compile(r"^[a-z][a-z_]{0,15}$")
# Broker-candle facts the record may show: coarse states and volatility ratios only.
_BROKER_OK = frozenset({"F:trend", "F:market_open", "V:vol_ratio", "V:ewma5_60"})
_FACT_GROUP = {"market": 0, "vol": 0, "cost": 0, "fundamental": 0, "macro": 1, "event": 2, "news": 3,
               "filing": 4}


def _fact_source(fact: Fact) -> str:
    src = (fact.source or "").strip().lower()
    head = _history_source(src)
    if head in ("tiingo", "binance", "clock", "fred"):
        return head
    if head == "etoro":
        return "broker"
    if src == "costs:floor":
        return "policy"
    if src.startswith("costs"):
        return "broker"            # a broker what-if (or an unlabelled quote): costs in NAV bp only
    return "unknown"


def _fact_value(fact: Fact) -> bool | float | str | None:
    """The fact's value in its public form (rounded; states as short words), else None."""
    v = fact.value
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        f = float(v)
        if not math.isfinite(f) or abs(f) > 1_000_000:
            return None
        return round(f, _FACT_DIGITS.get(fact.unit, 3)) + 0.0
    if isinstance(v, str) and fact.unit == "state" and _STATE_WORD.match(v):
        return v
    return None


def _withheld(fact: Fact, source: str) -> str | None:
    """Why the value may not be published, or None when it may (docs/data-rights.md)."""
    prefix, _, rest = fact.id.partition(":")
    if fact.kind == "fundamental":
        return "not_publishable"   # not yet covered by docs/data-rights.md: fail closed
    if not fact.publishable and prefix != "M":
        return "not_publishable"   # the pack's own "never publish" bit always wins (FRED: below)
    if prefix == "M":
        macro = _MACRO.match(fact.id)
        series = macro.group(1) if macro else ""
        if _fred_publishable(series, fact):
            return None
        return "licensed_series" if series in fred.READ_ONLY else "not_publishable"
    if source in ("tiingo", "binance", "clock", "policy"):
        return None
    if source == "broker":
        field = rest.rsplit(":", 1)[-1]
        return None if f"{prefix}:{field}" in _BROKER_OK else "broker_data"
    return "unknown_source"


def event_source(source: str | None) -> str:
    """An event's public source label from its own source: the policy calendar and FRED release
    dates are `calendar`, SEC-derived earnings (`sec_8k`, `sec_periodic`, `sec_estimate`) are
    `sec`, the broker feed is `broker_feed`, anything else `unknown`."""
    src = (source or "").strip().lower()
    if src.startswith(("policy", "fred_release")):
        return "calendar"
    if src == "sec" or src.startswith("sec_"):
        return "sec"
    if src.startswith(("etoro", "broker")):
        return "broker_feed"
    return "unknown"


_RSS_FEED = re.compile(r"[a-z][a-z0-9_]{0,23}")


def news_source(item: Any) -> str:
    """A news item's public source label: `rss` for an `N:` RSS headline, `broker_feed` for any
    other `N:` id, its publisher for a `P:` item that passes the public test, else `unknown`."""
    eid = str(getattr(item, "id", "") or "")
    if _NEWS_OK.match(eid):
        return "rss" if getattr(item, "source", None) == "rss" else "broker_feed"
    return str(item.source) if public_news_ok(item) else "unknown"


def _facts(pack: FactPack | None, lm: LineMap) -> tuple[list[PublicFact], int, set[str]]:
    """The evidence table: one entry per fact, event, news item and filing sentence of the pack.
    Returns (entries, count dropped: a symbol that is not a line, or an id or kind the table
    does not know, and the `P:` ids whose item failed the public test)."""
    if pack is None:
        return [], 0, set()
    rows: list[tuple[tuple[int, int, int, str], PublicFact]] = []
    dropped = 0
    mismatch: set[str] = set()

    def order(kind: str, line: str | None, eid: str) -> tuple[int, int, int, str]:
        pos = lm.order.index(line) if line in lm.order else len(lm.order)
        return (_FACT_GROUP.get(kind, 5), pos, "FVC".find(eid[:1]) % 4, eid)

    for fact in pack.facts:
        line = None
        if fact.symbol is not None:
            line = lm.lookup(fact.symbol)
            if line is None:
                dropped += 1
                continue
        kind = fact.kind if fact.kind in _FACT_GROUP else None
        if kind is None or not (_ID_OK.match(fact.id) or _MACRO.match(fact.id)):
            dropped += 1
            continue
        source = _fact_source(fact)
        withheld = _withheld(fact, source)
        value = _fact_value(fact) if withheld is None else None
        rows.append((order(kind, line, fact.id), PublicFact(
            id=fact.id, kind=kind, label=labels.fact_label(fact.id), line=line, value=value,
            unit=fact.unit if fact.unit in _FACT_UNITS else None,
            as_of=_as_datetime(fact.available_at), source=source, withheld=withheld,
        )))
    for event in pack.events:
        lines = {lm.lookup(s) for s in event.symbols}
        if not _ID_OK.match(event.id) or None in lines:
            dropped += 1
            continue
        line = next(iter(lines)) if len(lines) == 1 else None
        source = event_source(event.source)
        rows.append((order("event", line, event.id), PublicFact(
            id=event.id, kind="event", label=labels.fact_label(event.id), line=line, source=source,
        )))
    for item in pack.news:
        if not (_NEWS_OK.match(item.id) or _PUBLIC_NEWS_OK.match(item.id)):
            dropped += 1
            continue
        source = news_source(item)
        if _PUBLIC_NEWS_OK.match(item.id) and source == "unknown":
            mismatch.add(item.id)
        mapped = {m for m in (lm.lookup(s) for s in item.symbols) if m is not None}
        line = next(iter(mapped)) if len(mapped) == 1 else None
        rows.append((order("news", line, item.id), PublicFact(
            id=item.id, kind="news", label=labels.news_label(item.id, source), line=line, source=source,
        )))
    for filing in pack.filings:
        line = lm.lookup(filing.symbol)
        for sentence in filing.sentences:
            if not _ID_OK.match(sentence.id):
                dropped += 1
                continue
            rows.append((order("filing", line, sentence.id), PublicFact(
                id=sentence.id, kind="filing", label=labels.FILING_LABEL, line=line, source="filing",
            )))
    unique: dict[str, tuple[tuple[int, int, int, str], PublicFact]] = {}
    for key, row in rows:
        unique.setdefault(row.id, (key, row))
    return [row for _, row in sorted(unique.values(), key=lambda kr: kr[0])], dropped, mismatch


def _check_value(v: float | str | None) -> float | str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return round(float(v), 4)
    return clean_text(v, 40)


def _rule_id(v: str) -> str:
    rid = re.sub(r"[^A-Za-z0-9_]", "", v or "")[:16]
    return rid if rid and rid[0].isupper() else "R_unknown"


def value_lines(pack: FactPack | None, lm: LineMap) -> tuple[frozenset[str], bool]:
    """(the lines whose R15 SR_be may be published, whether the largest SR_be of any change may be)
    from the pack (the strictest source wins, `trace_rules`): a line qualifies when every cost
    fact of the pack is a pure policy quote (`costs:floor`; a broker what-if anywhere may price any
    leg, the pack carries only each line's 1x long quote) and its state's history is Tiingo or
    Binance (sigma is an input of SR_be). The second answer needs every line state of the pack to
    qualify too. Without a pack, nothing qualifies."""
    if pack is None:
        return frozenset(), False
    costs = [f for f in pack.facts if f.kind == "cost" or f.id.startswith("C:")]
    if not costs or any((f.source or "").strip().lower() != "costs:floor" for f in costs):
        return frozenset(), False
    priced = {line for f in costs if f.symbol is not None and (line := lm.lookup(f.symbol)) is not None}
    open_lines: set[str] = set()
    every_open = True
    for sym, state in pack.states.items():
        line = lm.lookup(sym)
        if line is None:
            continue
        if _history_source(state.history_source) in _OPEN_HISTORY:
            open_lines.add(line)
        else:
            every_open = False
    lines = frozenset(priced & open_lines)
    return lines, bool(lines) and every_open and bool(open_lines)


def _risk(risk: RiskDecision | None, unit_w: dict[str, float], lm: LineMap,
          pack: FactPack | None = None) -> PublicRisk | None:
    if risk is None:
        return None
    shown, r15_public = value_lines(pack, lm)
    # The R15 check is the largest SR_be over the changed lines: public only when each of them is
    # a value line itself (a changed line the pack does not price may have been priced by a broker
    # quote the pack never saw). A symbol that is no line has no quote, so it never adds an SR_be.
    changed = {line for s in set(risk.final_w) | set(risk.base_w)
               if abs(risk.final_w.get(s, 0.0) - risk.base_w.get(s, 0.0)) > 1e-12
               and (line := lm.lookup(s)) is not None}
    r15_public = r15_public and changed <= shown
    raw_levels = lm.first_by_line(risk.raw_levels)
    banded_levels = lm.first_by_line(risk.banded_levels)
    return PublicRisk(
        base_x=lm.sum_by_line(risk.base_w),
        raw_levels=raw_levels,
        banded_levels=banded_levels,
        raw_x={k: _x(v * unit_w[k]) for k, v in raw_levels.items() if k in unit_w},
        banded_x={k: _x(v * unit_w[k]) for k, v in banded_levels.items() if k in unit_w},
        proposed_x=lm.sum_by_line(risk.proposed_w),
        final_x=lm.sum_by_line(risk.final_w),
        checks=[
            PublicCheck(
                rule_id=_rule_id(c.rule_id), name=clean_text(c.name, 80), passed=c.passed,
                value=_check_value(trace_rules.public_check_value(c.rule_id, c.value, r15_public=r15_public)),
                limit=_check_value(c.limit), kind=c.kind,
            )
            for c in risk.checks
        ],
        gross_x=_x(risk.gross),
        net_x=_x(risk.net),
        margin_use_pct=_pct(risk.margin_use),
        stop_at_risk_pct=_pct(risk.stop_budget_used),
        carry_bp_day=_bp(risk.carry_bps_day),
        ex_ante_vol_pct=_pct(risk.ex_ante_vol),
        hold_reasons=[_code(trace_rules.public_hold_reason(r, value_lines=shown)) for r in risk.hold_reasons],
        compliance=[_code(r) for r in risk.compliance],
    )


def _plan(plan: Plan | None, lm: LineMap) -> PublicPlan | None:
    if plan is None:
        return None
    legs: list[PublicLeg] = []
    for leg in plan.legs:
        line = lm.line(leg.symbol)
        if line is None:
            continue
        legs.append(PublicLeg(
            seq=leg.seq,
            kind=leg.kind,
            line=line,
            direction=leg.direction,
            settlement=leg.settlement,
            leverage=leg.leverage,
            weight_before_x=_x(leg.weight_before),
            weight_after_x=_x(leg.weight_after),
            stop_distance_pct=_pct(leg.stop_distance) if leg.stop_distance is not None else None,
            cost_bp=_bp(leg.cost_bps_nav),
            risk_increasing=leg.risk_increasing,
        ))
    return PublicPlan(
        legs=legs,
        cost_bp_total=_bp(plan.cost_bps_nav),
        carry_bp_day=_bp(plan.carry_bps_day_nav),
        gross_before_x=_x(plan.gross_before),
        gross_after_x=_x(plan.gross_after),
        net_before_x=_x(plan.net_before),
        net_after_x=_x(plan.net_after),
        skipped=[_code(trace_rules.public_plan_skip(s)) for s in plan.skipped],
    )


_OUTCOME = {
    None: "none",
    "awaiting_publication": "pending",
    "proposed": "pending",
    "approved": "approved",
    "executing": "approved",
    "completed": "approved",
    "completed_partial": "approved",
    "blocked": "approved",
    "execution_unknown": "approved",
    "rejected": "rejected",
    "expired": "expired",
    "superseded": "superseded",
    "reviewed_no_action": "no_action",
}


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else None
    return None


def _slot_of(value: Any) -> datetime | None:
    """A timestamp rounded DOWN to its slot; naive or unparseable times are dropped."""
    ts = _as_datetime(value)
    return clock.slot_at_or_before(ts) if ts is not None else None


def _decision(rec: CycleRecord) -> PublicDecision:
    return PublicDecision(
        state=rec.decision_state,
        human_outcome=_OUTCOME.get(rec.decision_state, "none"),
        reason=clean_text(rec.decision_reason, 200),
        approved_slot=_slot_of(rec.approved_at),
    )


_HEX_DIGEST = re.compile(r"^[0-9a-f]{16,}$")
FINGERPRINT_CHARS = 16


def short_fingerprint(value: str | None, key: bytes | None = None) -> str:
    """The material-change fingerprint as a short hash. With the install `key` (the default for a
    cycle once the orchestrator passes it) it is HMAC-SHA256(key, fingerprint) truncated: equal
    fingerprints still give equal hashes across cycles, but low-entropy contents (a rounded
    fundamentals value) cannot be brute-forced from the record. Without a key (older callers): a
    hex digest is truncated, anything else is hashed first. Contents are never published."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if key is not None:
        return keyed_hex(key, raw, FINGERPRINT_CHARS)
    v = raw.lower().removeprefix("sha256:")
    if _HEX_DIGEST.match(v):
        return v[:FINGERPRINT_CHARS]
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:FINGERPRINT_CHARS]


_CALL_STATUSES = set(get_args(CallStatus))
_HTTP = re.compile(r"^http (\d{3})\b")
_SKIP_REASONS = frozenset({"call_budget", "council_unavailable"})


def error_kind(status: str, error: str | None) -> str | None:
    """A fixed code for why a call did not simply succeed, read from the gateway's error text
    (see `council.llm.gateway`). The text itself is never published; unknown texts map to the
    status's generic code. None for a clean call."""
    e = " ".join((error or "").split()).lower()
    if status in ("ok", "cached"):
        return "corrected" if status == "ok" and e.startswith("corrected") else None
    if status == "timeout":
        return "timeout"
    if status == "skipped":
        return e if e in _SKIP_REASONS else "skipped"
    if status == "parse_fail":
        if " | correction " in e:
            return "correction_failed"
        final = e.rsplit("| after correction:", 1)[-1]
        return "not_json" if "not a json object" in final else "schema"
    if status == "transport":
        http = _HTTP.match(e)
        if http:
            code = int(http.group(1))
            return "http_429" if code == 429 else "http_5xx" if code >= 500 else "http_4xx"
        if e.startswith("server error"):
            return "server_error"
        if e.startswith(("non-json", "unexpected response")):
            return "bad_response"
        if e.startswith("unexpected"):
            return "internal_error"
        return "connection"
    return "invalid"


def _calls(rec: CycleRecord) -> list[PublicCall]:
    out = []
    for c in rec.calls:
        status = c.status if c.status in _CALL_STATUSES else "invalid"
        out.append(PublicCall(
            role=_role(c.role),
            replicate=max(0, min(c.replicate, 16)),
            status=status,
            latency_ms=max(0, c.latency_ms),
            tokens_in=max(0, c.tokens_in),
            tokens_out=max(0, c.tokens_out),
            prompt_id=clean_text(c.prompt_id, 64),
            prompt_sha=_sha(c.prompt_sha),
            error_kind=error_kind(status, c.error),
        ))
    return out


_KILL = set(get_args(KillState))
_STATUS = set(get_args(CycleStatus))


def public_cycle(
    rec: CycleRecord,
    pack: FactPack | None,
    *,
    lines: Iterable[LineSpec] | Universe | Mapping[str, LineSpec] | None,
    install_key: bytes | None = None,
    swing_texts: Mapping[str, Sequence[str] | None] | None = None,
    swing_trades: Iterable[Any] = (),
    swing_health: Any = None,
) -> PublicCycleV1:
    """Build the public cycle document from the private record (and the pack, for licensed-text,
    FRED, news-source and cost-source checks). `install_key` (`council.publish.install_key`) keys
    the material-change fingerprint. Raises pydantic.ValidationError if anything falls outside
    the allow-list.

    Swing slot (`rec.extras["swing"]`, `council.swing.record`): `swing_texts` = {cycle id: the
    licensed feed texts its agents saw} for this cycle and every origin of a carried idea
    (`council.swing.record.origin_texts`); `swing_trades` = the ledger's swing trades (the book at
    seal time); `swing_health` = `public_skeptic_health(...)`. Without the record: no swing part."""
    lm = LineMap(lines)
    text = _Text(licensed_texts(pack))
    ev = _Evidence(pack)
    flags = public_flags(rec.flags)

    ref_levels: dict[str, float] = {}
    unit_w: dict[str, float] = {}
    if rec.reference is not None:
        for sym, entry in rec.reference.entries.items():
            line = lm.lookup(sym)
            if line is not None:
                ref_levels[line] = _level(entry.level_ref)
                unit_w[line] = float(entry.unit_weight)

    kill = rec.kill_state.upper()
    if kill not in _KILL:
        flags.append("kill_state_unrecognised")
        kill = "NORMAL"
    single_agent = _single_agent(rec, ref_levels, lm, text, ev)
    facts, facts_dropped, news_mismatch = _facts(pack, lm)
    fields: dict[str, Any] = {
        "cycle_id": rec.cycle_id,
        "slot": rec.slot,
        "mode": "live" if rec.mode == "live" else "rehearsal",
        "status": rec.status if rec.status in _STATUS else "aborted",
        "late_by_min": max(0, round(rec.late_by_s / 60)),
        "input_hash": _sha(rec.input_hash),
        "policy_sha": _sha(rec.policy_sha),
        "prompt_manifest_sha": _sha(rec.prompt_manifest_sha),
        "model": _model_name(rec.model),
        "model_digest": _model_name(rec.model_digest),
        "think": rec.think,
        "why_we_met": [_code(w) for w in rec.why_we_met],
        "kill_state": kill,
        "reference": _reference(rec, lm, day_changes(pack, lm)),
        "cards": [_card(c, lm, text, ev) for c in rec.cards],
        "macro": _macro(rec, text, ev),
        "debate": PublicDebate(
            bull=_advocate(rec.debate.bull_open, lm, text, ev),
            bear=_advocate(rec.debate.bear, lm, text, ev),
            rebuttal=_advocate(rec.debate.bull_rebuttal, lm, text, ev),
        ),
        "pm": _pm_block(rec.pm, rec.medoid_replicate, ref_levels, lm, text, ev,
                        levels=rec.risk.raw_levels if rec.risk is not None else {},
                        agreement=rec.agreement),
        "single_agent": single_agent,
        "material_fingerprint": short_fingerprint(rec.material_fingerprint, install_key),
        "basis": rec.risk.basis if rec.risk is not None else None,
        "bands": {
            line: PublicBand(
                trend=b.trend, ref_level=_level(b.ref_level), lo=_level(b.lo), hi=_level(b.hi),
                reasons=[_code(r) for r in b.reasons],
                qualifying_cards=[c for c in b.qualifying_cards if re.match(r"^K:[a-z_]+:\d+$", c)],
            )
            for sym, b in rec.bands.items()
            if (line := lm.line(sym)) is not None
        },
        "risk": _risk(rec.risk, unit_w, lm, pack),
        "plan": _plan(rec.plan, lm),
        "decision": _decision(rec),
        "calls": _calls(rec),
        "facts": facts,
    }
    swing = rec.extras.get("swing") if isinstance(rec.extras, Mapping) else None
    if isinstance(swing, Mapping) and swing.get("ideas") is not None:
        fields["swing"] = public_swing_section(
            swing, cycle_id=rec.cycle_id, licensed_texts=licensed_texts(pack), origin_texts=swing_texts,
            trades=swing_trades, today=rec.slot.astimezone(clock.NEW_YORK).date(), health=swing_health)
    # Counters are complete only after every field above was built.
    if lm.unmapped:
        flags.append(f"unmapped_symbols_dropped:{lm.unmapped}")
    if ev.dropped:
        flags.append(f"evidence_ids_dropped:{ev.dropped}")
    if facts_dropped:
        flags.append(f"facts_dropped:{facts_dropped}")
    if news_mismatch or ev.mismatch:
        flags.append(f"news_licence_mismatch:{len(news_mismatch | ev.mismatch)}")
    if text.withheld:
        flags.append(f"licensed_overlap_withheld:{text.withheld}")
    if pack is None:
        flags.append("licensed_text_check_skipped")
    return PublicCycleV1(**fields, flags=flags)


# ------------------------------------------------------------------------------ other documents
def public_ops_row(rec: CycleRecord) -> PublicOpsRow:
    """One punctuality/health row per cycle (counts only), plus the decision outcome. Upsert it
    again once the decision is final: the sealed cycle document still says "pending"."""
    duration = None
    if rec.finished_at is not None:
        duration = max(0, int((rec.finished_at - rec.started_at).total_seconds()))
    return PublicOpsRow(
        cycle_id=rec.cycle_id,
        slot=rec.slot,
        status=rec.status if rec.status in _STATUS else "aborted",
        late_by_min=max(0, round(rec.late_by_s / 60)),
        duration_s=duration,
        calls=len(rec.calls),
        calls_ok=sum(1 for c in rec.calls if c.status in ("ok", "cached")),
        parse_fail=sum(1 for c in rec.calls if c.status in ("parse_fail", "invalid")),
        timeouts=sum(1 for c in rec.calls if c.status == "timeout"),
        basis=rec.risk.basis if rec.risk is not None else None,
        legs=len(rec.plan.legs) if rec.plan is not None else 0,
        decision_state=rec.decision_state,
        human_outcome=_OUTCOME.get(rec.decision_state, "none"),
        decision_reason=clean_text(rec.decision_reason, 200),
        approved_slot=_slot_of(rec.approved_at),
        model=_model_name(rec.model),
        model_digest=_model_name(rec.model_digest),
        flags=public_flags(rec.flags),
    )


class PositionLike(Protocol):
    """The fields read from `council.models.broker.Position` (duck-typed). Amounts and rates are
    only numerators and denominators of a percentage; they are never published."""

    symbol: str
    is_buy: bool
    leverage: int
    open_rate: float
    close_rate: float | None
    amount: float
    settlement: str


_SETTLEMENTS = frozenset(get_args(Settlement))


def _position_views(positions: Iterable[PositionLike], lm: LineMap) -> dict[str, dict[str, Any]]:
    """Per line: the largest open position's settlement and leverage, and the P/L % since open
    (price return x leverage, weighted by invested amount). Unknown symbols are skipped."""
    groups: dict[str, list[PositionLike]] = {}
    for p in positions:
        line = lm.lookup(str(p.symbol))
        if line is not None:
            groups.setdefault(line, []).append(p)
    out: dict[str, dict[str, Any]] = {}
    for line, group in groups.items():
        largest = max(group, key=lambda p: _pos(p.amount) or 0.0)
        lev = largest.leverage if isinstance(largest.leverage, int) and 1 <= largest.leverage <= 10 else None
        num = den = 0.0
        for p in group:
            amount, opened, now = _pos(p.amount), _pos(p.open_rate), _pos(p.close_rate)
            if amount is None or opened is None or now is None:
                continue
            leverage = p.leverage if isinstance(p.leverage, int) and p.leverage >= 1 else 1
            num += amount * leverage * (now / opened - 1.0) * (1.0 if p.is_buy else -1.0)
            den += amount
        pnl = _within(round(num / den * 100.0, PCT_DP) + 0.0, _PCT_BOUND) if den > 0 else None
        out[line] = {
            "settlement": largest.settlement if largest.settlement in _SETTLEMENTS else None,
            "leverage": lev,
            "pnl_since_open_pct": pnl,
        }
    return out


def public_book(
    cycle_id: str,
    weights: Mapping[str, float],
    *,
    lines: Iterable[LineSpec] | Universe | Mapping[str, LineSpec] | None,
    reference_weights: Mapping[str, float] | None = None,
    levels: Mapping[str, float] | None = None,
    kill_state: str = "NORMAL",
    positions: Iterable[PositionLike] | None = None,
    pack: FactPack | None = None,
) -> PublicBook:
    """The book by exposure line from signed weights (fractions of NAV, i.e. x).

    Each line also carries its public description (name, asset class, session), and, when given,
    `positions` (the broker snapshot's open positions: settlement, leverage, P/L % since open) and
    `pack` (the 1-day market move of lines with Tiingo / Binance history)."""
    lm = LineMap(lines)
    w = lm.sum_by_line(weights)
    ref = lm.sum_by_line(reference_weights or {})
    lv = lm.first_by_line(levels or {})
    held = _position_views(positions or (), lm)
    day = day_changes(pack, lm)
    book_lines = {}
    for line in lm.ordered({**w, **ref, **held}):
        weight = w.get(line, 0.0)
        spec = lm.specs.get(line)
        view = held.get(line, {})
        book_lines[line] = PublicBookLine(
            direction="long" if weight > 0 else "short" if weight < 0 else "flat",
            weight_x=weight,
            level=lv.get(line),
            reference_weight_x=ref.get(line),
            name=clean_text(spec.name, 48) or None if spec is not None else None,
            asset_class=spec.asset_class if spec is not None else None,
            session=spec.session if spec is not None else None,
            settlement=view.get("settlement"),
            leverage=view.get("leverage"),
            pnl_since_open_pct=view.get("pnl_since_open_pct"),
            day_change_pct=day.get(line),
        )
    gross = _x(sum(abs(v) for v in w.values()))
    net = _x(sum(w.values()))
    kill = kill_state.upper() if kill_state.upper() in _KILL else "NORMAL"
    return PublicBook(
        as_of_cycle_id=cycle_id, lines=book_lines, gross_x=gross, net_x=net,
        cash_x=_x(max(0.0, 1.0 - gross)), kill_state=kill,
    )


# ------------------------------------------------------------------------------ execution
class LegResultLike(Protocol):
    """The fields read from `council.execution.executor.LegResult` (duck-typed: the public-record
    package never imports the executor, the ledger or the broker)."""

    seq: int
    kind: str
    symbol: str
    line: str
    state: str
    units_requested: float | None
    units_filled: float | None
    fill_price: float | None


class ReconcileLike(Protocol):
    drift: float
    achieved_w: dict[str, float]


class ExecutionReportLike(Protocol):
    """The fields read from `council.execution.executor.ExecutionReport`."""

    final_state: str
    legs: Sequence[LegResultLike]
    reconcile: ReconcileLike | None


_LEG_KINDS = set(get_args(LegKind))
_LEG_STATES = set(get_args(LegState))
_DECISION_STATES = set(get_args(DecisionState))
_EXECUTED = frozenset({"filled", "partially_filled", "rejected_partial"})
_MIN_TARGET_X = 1e-4          # below this an exposure error in % is meaningless


def _pos(v: Any) -> float | None:
    """A finite positive float, else None."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if math.isfinite(f) and f > 0 else None


def _within(v: float | None, bound: float) -> float | None:
    return v if v is not None and math.isfinite(v) and abs(v) <= bound else None


def _weight_sign(kind: str, direction: str | None) -> float | None:
    """Sign of a leg's effect on its line's weight: opens add their direction, closes remove it."""
    if direction not in ("long", "short"):
        return None
    base = 1.0 if direction == "long" else -1.0
    if kind == "open":
        return base
    if kind in ("close", "partial_close"):
        return -base
    return 0.0


# M5-N whole-unit fills: a measured weight within the planned tolerance of its target publishes as
# the target. The tolerance is the executor's post-fill exposure tolerance (relative) or the public
# deadband share (absolute, x NAV), whichever is larger: rounding to whole units moves a weight by
# less than one unit, which the size floor keeps under the deadband share whenever gate P2 is green.
FILL_TOLERANCE_REL = 0.05
FILL_TOLERANCE_X = 0.02


def fill_tolerances(policy: Any) -> dict[str, float]:
    """`public_execution`'s tolerance keywords from the policy's risk limits."""
    risk = policy.risk
    return {"tolerance_rel": float(risk["approval"]["post_fill_exposure_tolerance"]),
            "tolerance_x": float(risk["deadband"]["min_nav_share"])}


def _within_tolerance(measured: float, target: float, rel: float, absolute: float) -> bool:
    return abs(measured - target) <= max(rel * abs(target), absolute) + 1e-12


def _fill(r: LegResultLike, line: str, leg: Leg | None, state: str, nav_usd: float,
          tol: tuple[float, float] = (FILL_TOLERANCE_REL, FILL_TOLERANCE_X)) -> PublicFill:
    direction = leg.direction if leg is not None else None
    target = _x(leg.weight_after - leg.weight_before) if leg is not None else None
    filled = slippage = error = fill = None
    units, price = _pos(r.units_filled), _pos(r.fill_price)
    sign = _weight_sign(r.kind, direction)
    if r.kind == "open" and units is not None and price is not None and sign is not None:
        filled = _within(_x(sign * units * price / nav_usd), 5.0)
        if target is not None and filled is not None:
            if _within_tolerance(filled, target, *tol):
                filled, fill = target, "within_tolerance"
            else:
                fill = "outside_tolerance"
        if target is not None and filled is not None and abs(target) >= _MIN_TARGET_X:
            error = _within(round((filled / target - 1.0) * 100.0, PCT_DP) + 0.0, 1000.0)
        amount = _pos(leg.amount_usd) if leg is not None else None
        planned_units = _pos(leg.units) if leg is not None else None
        planned = amount / planned_units if amount and planned_units else None   # notional / units
        if planned:
            adverse = 1.0 if direction == "long" else -1.0
            slippage = _within(_bp(adverse * (price / planned - 1.0) * 1e4), 10000.0)
    return PublicFill(
        seq=r.seq,
        kind=r.kind,
        line=line,
        direction=direction,
        settlement=leg.settlement if leg is not None else None,
        leverage=leg.leverage if leg is not None else None,
        state=state,
        weight_target_x=_within(target, 5.0),
        weight_filled_x=filled,
        exposure_error_pct=error,
        slippage_bp=slippage,
        cost_bp=_bp(leg.cost_bps_nav) if leg is not None else None,
        fill=fill,
    )


def _achieved(achieved: dict[str, float], plan: Plan | None, lm: LineMap, tol: tuple[float, float],
              flags: list[str]) -> tuple[dict[str, float], float | None]:
    """Line weights after reconcile; a planned line within tolerance of its planned weight reads as
    that weight, one outside it keeps the exact value and is flagged `achieved_outside_tolerance`.
    Also the public drift, Σ |published achieved − planned| over the planned lines (the reconcile's
    own definition, `execution.reconcile`), so the exact drift never carries the whole-unit residual
    the snap hides; None without a plan (the caller then keeps the reconcile's drift)."""
    planned: dict[str, float] = {}
    for leg in sorted(plan.legs, key=lambda g: g.seq) if plan is not None else ():
        line = lm.lookup(str(leg.line)) or lm.line(leg.symbol)
        if line is not None:
            planned[line] = _x(leg.weight_after)          # signed line weight after the leg
    out = dict(achieved)
    outside = 0
    for line, target in planned.items():
        if line not in out:
            continue
        if _within_tolerance(out[line], target, *tol):
            out[line] = target + 0.0
        else:
            outside += 1
    if outside:
        flags.append(f"achieved_outside_tolerance:{outside}")
    if plan is None:
        return out, None
    return out, sum(abs(out.get(line, 0.0) - target) for line, target in planned.items())


def _weightless(fill: PublicFill) -> PublicFill:
    """A smoke ticket's fill without anything sized by the NAV (G28): no weights, errors or costs."""
    return fill.model_copy(update={"weight_target_x": None, "weight_filled_x": None, "exposure_error_pct": None,
                                   "cost_bp": None, "fill": None})


def public_execution(
    report: ExecutionReportLike,
    *,
    cycle_id: str | None,
    lines: Iterable[LineSpec] | Universe | Mapping[str, LineSpec] | None,
    nav_usd: float,
    plan: Plan | None = None,
    approved_at: datetime | None = None,
    completed_at: datetime | None = None,
    decision_ref: str | None = None,
    tolerance_rel: float = FILL_TOLERANCE_REL,
    tolerance_x: float = FILL_TOLERANCE_X,
) -> PublicExecution:
    """The public execution record from the PRIVATE `ExecutionReport`.

    Per leg: line, kind, direction, target vs filled weight (x NAV at approval), slippage and
    cost in bp, and state. `nav_usd` is only a denominator and is never published. `plan` (the
    approved plan, joined by leg seq) supplies direction, the approved weight change, the planned
    price and the cost estimate; without it those fields are None and `plan_missing` is flagged.
    Amounts, units, prices, order/position ids, request ids and error strings are never read into
    the output.

    M5-N: a decision without a cycle (a watch flatten, a smoke ticket) passes `cycle_id=None` and
    its `decision_ref`; a smoke ticket's execution is weightless (its size is the broker minimum,
    so any weight would give the NAV). A measured fill or achieved weight within the tolerance of
    its target publishes as the target (see `FILL_TOLERANCE_REL`)."""
    if cycle_id is not None:
        decision_ref = None
    smoke = decision_ref is not None and "-smoke-" in decision_ref
    tol = (tolerance_rel, tolerance_x)
    nav = _pos(nav_usd)
    if nav is None:
        raise ValueError("nav_usd must be a positive finite number")
    lm = LineMap(lines)
    flags: list[str] = []
    plan_legs = {leg.seq: leg for leg in plan.legs} if plan is not None else {}
    if plan is None:
        flags.append("plan_missing")
    fills: list[PublicFill] = []
    bad_kind = bad_state = no_plan_leg = 0
    for r in report.legs:
        line = lm.lookup(str(r.line)) or lm.line(str(r.symbol))
        if line is None:
            continue
        if r.kind not in _LEG_KINDS:
            bad_kind += 1
            continue
        state = r.state if r.state in _LEG_STATES else "unknown"
        if state != r.state:
            bad_state += 1
        leg = plan_legs.get(r.seq)
        if plan is not None and leg is None:
            no_plan_leg += 1
        fill = _fill(r, line, leg, state, nav, tol)
        fills.append(_weightless(fill) if smoke else fill)
    fills.sort(key=lambda f: f.seq)
    final = report.final_state if report.final_state in _DECISION_STATES else "execution_unknown"
    if final != report.final_state:
        flags.append("final_state_unrecognised")
    rec = report.reconcile
    costs = [f.cost_bp for f in fills if f.state in _EXECUTED and f.cost_bp is not None]
    achieved = lm.sum_by_line(rec.achieved_w) if rec is not None else {}
    drift = rec.drift if rec is not None else None
    if smoke:
        achieved, drift = {}, None
    else:
        achieved, public_drift = _achieved(achieved, plan, lm, tol, flags)
        if rec is not None and public_drift is not None:
            drift = public_drift
    for name, count in (("unmapped_symbols_dropped", lm.unmapped), ("leg_kind_unrecognised", bad_kind),
                        ("leg_state_unrecognised", bad_state), ("leg_not_in_plan", no_plan_leg)):
        if count:
            flags.append(f"{name}:{count}")
    return PublicExecution(
        cycle_id=cycle_id,
        decision_ref=decision_ref,
        decision_state=final,
        approved_slot=_slot_of(approved_at),
        completed_slot=_slot_of(completed_at),
        fills=fills,
        achieved_x=achieved,
        achieved_drift_x=_within(_x(drift), 5.0) if drift is not None else None,
        cost_bp_total=_bp(sum(costs)) if plan is not None and not smoke else None,
        flags=flags,
    )


def public_status(
    state: StatusState = "AWAITING_ACCOUNT",
    *,
    last_cycle_id: str | None = None,
    last_cycle_at: datetime | None = None,
    kill_state: str = "NORMAL",
    note: str = "",
) -> PublicStatus:
    kill = kill_state.upper() if kill_state.upper() in _KILL else "NORMAL"
    return PublicStatus(
        state=state, last_cycle_id=last_cycle_id, last_cycle_at=last_cycle_at,
        kill_state=kill, note=clean_text(note, 200),
    )


# ------------------------------------------------------------------------------ swing book (SW-7)
# swing-book.md rev 2, §7.2 / §7.3. The private record is `extras["swing"]` (`council.swing.record`);
# every public value is re-read field by field into the allow-listed swing models:
# - percent-only: distances from the entry, weights x NAV, R and % of the position net of the
#   DECLARED cost (1.25% per leg), bp of NAV. Never a price, rate, unit, amount or id of the broker.
# - the fact card's live layer (`move_*_live_*`, the broker's rate at the slot) is withheld as
#   `broker_data`; short interest, dollar volume and correlations are never listed; Alpaca-derived
#   fields are `unknown_source` until the data-rights row is widened (Q-S10, `alpaca_public`).
# - a reason, claim or argument that cites a live-layer id loses every number (`scrub_numbers`):
#   the model may have quoted the live value.
# - an `N:` catalyst is its id only; a `P:` item keeps its title and its .gov link; `S:` its form
#   and item codes.
# - model text is cleaned (`clean_text`) and withheld on an 8-word overlap with the licensed feed
#   texts of THIS cycle's swing reading and of EVERY cycle a carried-forward idea came from (H9,
#   `leakscan.origin_matcher`); texts that cannot be checked (a purged origin) are withheld.
SWING_WITHHELD = "[withheld: carried from an earlier cycle; its feed text cannot be shown]"
SWING_DECLARED_COST_PCT = 1.25
_SWING_LIVE = re.compile(r"^move_[a-z_]*live[a-z_]*$")
_SWING_NEVER = frozenset({"adv_usd_20d", "short_interest_pct_float", "short_interest_basis", "days_to_cover",
                          "short_interest_settlement", "catalyst_items_feed", "corr_60d_with"})
_SWING_ID = re.compile(r"^(?:X:[A-Z0-9](?:[A-Z0-9_]{0,10}[A-Z0-9])?:[a-z0-9_]{1,48}|N:[0-9a-f]{8}|P:[0-9a-f]{8}"
                       r"|S:[A-Za-z0-9_.:@#+-]{1,80}|M:[A-Za-z0-9_.:@-]{1,60}|[FVCEK]:[A-Za-z0-9_.:@#+-]{1,80})$")
_NUMBER = re.compile(r"[-+±]?\d[\d,]*(?:\.\d+)?\s?(?:%|σ|sigma\b|pct\b|percent\b|bps?\b|x\b)?")
_GOV_LINK = re.compile(r"^https://[A-Za-z0-9.-]+\.gov/\S*$")
_SETUP_RE = re.compile(r"^[a-z][a-z_]{0,31}$")
_FACT_KEY = re.compile(r"^[a-z0-9_]{1,48}$")
_SWING_TRADE_STATES = {
    "open": "open", "partial": "open", "open_tp_missing": "open_tp_missing", "exit_pending": "exit_pending",
    "closed_stop": "closed_stop", "closed_target": "closed_target", "closed_time": "closed_time",
    "closed_exit": "closed_exit", "closed_halt": "closed_halt", "closed_external": "closed_external",
    "closed_unclassified": "closed_external",
}
_EXIT_KIND = {"closed_stop": "stop", "closed_target": "target", "closed_time": "time", "closed_exit": "exit",
              "closed_halt": "halt", "closed_external": "external"}


def scrub_numbers(text: str) -> str:
    """Remove every number (with its %, σ or bp) from a text that cites a live-layer value."""
    return _NUMBER.sub("[value removed]", text)


def swing_line(ticker: str) -> str | None:
    """A swing ticker as a public line id (BRK.B -> BRK_B); None when it does not fit the pattern."""
    line = re.sub(r"[.\-]", "_", str(ticker or "").upper())
    return line if re.match(r"^[A-Z0-9](?:[A-Z0-9_]{0,10}[A-Z0-9])?$", line) else None


def is_live_id(eid: str) -> bool:
    return eid.startswith("X:") and bool(_SWING_LIVE.match(eid.rsplit(":", 1)[-1]))


class _SwingText:
    """Cleans one idea's model text: `clean_text`, then the licensed-overlap check against every
    feed text that applies (this cycle's and the idea's origin cycles'), then (when the text cites
    a live-layer id) the number scrub. `blocked` withholds every text (unverifiable origin)."""

    def __init__(self, matcher: leakscan.LicensedMatcher, blocked: bool = False):
        self.matcher, self.blocked = matcher, blocked
        self.withheld = 0

    def __call__(self, value: Any, max_len: int, *, live: bool = False) -> str:
        if value is None or value == "":
            return ""
        if self.blocked:
            self.withheld += 1
            return SWING_WITHHELD[:max_len]
        text = clean_text(str(value), max_len)
        if self.matcher and self.matcher.hits(text):
            self.withheld += 1
            return WITHHELD_LICENSED[:max_len]
        return _clip(scrub_numbers(text), max_len) if live else text


def _swing_ids(ids: Iterable[Any], dropped: list[int]) -> list[str]:
    out: list[str] = []
    for raw in ids or []:
        eid = str(raw)
        if _SWING_ID.match(eid):
            if eid not in out:
                out.append(eid)
        else:
            dropped[0] += 1
    return out


def _swing_catalyst(row: Mapping[str, Any], text: _SwingText) -> Any:
    from council.publish.public_models import PublicSwingCatalyst

    cid = str(row.get("id") or "")
    if not _SWING_ID.match(cid) or cid[:2] not in ("N:", "P:", "S:", "M:"):
        return None
    if cid.startswith("N:"):
        feed = str(row.get("source") or "")
        if _RSS_FEED.fullmatch(feed):               # a licensed RSS headline: id + feed label only
            return PublicSwingCatalyst(id=cid, kind="licensed_news", source=feed)
        return PublicSwingCatalyst(id=cid, kind="broker_feed")
    if cid.startswith("M:"):
        return PublicSwingCatalyst(id=cid, kind="screen")
    title = clean_text(str(row.get("title") or ""), 160) or None
    if cid.startswith("P:"):
        link = str(row.get("link") or "")
        return PublicSwingCatalyst(id=cid, kind="public_news", title=title,
                                   link=link if _GOV_LINK.match(link) and len(link) <= 300 else None)
    form = clean_text(str(row.get("form") or ""), 16) or None
    items = [clean_text(str(i), 8) for i in (row.get("items") or [])][:8]
    return PublicSwingCatalyst(id=cid, kind="filing", title=title, form=form, items=[i for i in items if i])


def _swing_facts(fields: Mapping[str, Any], *, alpaca_public: bool) -> tuple[dict[str, Any], dict[str, str]]:
    from council.swing.facts import ALPACA_FIELDS

    facts: dict[str, Any] = {}
    withheld: dict[str, str] = {}
    for key in sorted(fields):
        value = fields[key]
        if not _FACT_KEY.match(key) or key in _SWING_NEVER or leakscan.key_denied(key):
            continue
        if _SWING_LIVE.match(key):
            withheld[key] = "broker_data"
            continue
        if key in ALPACA_FIELDS and not alpaca_public:
            withheld[key] = "unknown_source"
            continue
        if key.startswith("vol_ratio") and isinstance(value, int | float) and not isinstance(value, bool):
            v = float(value)                      # §7.3: volume ratios publish as buckets only
            facts[key] = "<1" if v < 1 else "1-2" if v < 2 else "2-4" if v < 4 else ">4"
        elif isinstance(value, bool):
            facts[key] = value
        elif isinstance(value, int | float):
            if math.isfinite(float(value)) and abs(float(value)) < 1_000_000:
                facts[key] = round(float(value), 3)
        elif isinstance(value, str) and value and len(value) <= 40 and literal_ok(value):
            facts[key] = value
    return facts, withheld


def _skeptic_public(ref: str, v: Mapping[str, Any] | None, text: _SwingText, dropped: list[int]) -> Any:
    from council.publish.public_models import PublicSkepticVerdict, PublicSwingReason
    from council.swing.council import model_family

    if not v:
        return None
    status, code = v.get("status"), v.get("code")
    verdict = {"pass": "pass", "wait": "wait", "reject": "reject"}.get(str(status))
    override = v.get("override")
    if verdict is None:
        verdict = "reject" if code == "catalyst_misread" else "failed"
    body = v.get("verdict") or {}
    reasons = []
    for r in body.get("reasons") or []:
        ids = _swing_ids(r.get("evidence_ids"), dropped)
        reasons.append(PublicSwingReason(text=text(r.get("text"), 180, live=any(is_live_id(e) for e in ids)),
                                         evidence=ids[:4]))
    any_live = any(is_live_id(e) for r in reasons for e in r.evidence)
    said = v.get("said") if v.get("said") in ("pass", "wait", "reject") else None
    model = str(v.get("model") or "")
    family = model_family(model) if model else ""
    family = re.sub(r"[^a-z0-9_.-]", "", family.lower())[:32]
    enum = lambda key, allowed: body.get(key) if body.get(key) in allowed else "unknown"  # noqa: E731
    return PublicSkepticVerdict(
        ref=ref, verdict=verdict, said=said if said != verdict else None,
        discounted=enum("priced_in", ("no", "partly", "mostly", "fully")),
        news_status=enum("news_status", ("new", "follow_up", "stale", "restated")),
        regime=enum("regime", ("supports", "neutral", "against")),
        crowding=enum("crowding", ("low", "medium", "high")),
        catalyst_supports_claim=body.get("catalyst_supports_claim") if isinstance(body.get("catalyst_supports_claim"), bool) else None,
        claim_supports_side=body.get("claim_supports_side") if isinstance(body.get("claim_supports_side"), bool) else None,
        code_override=_code(str(override)) if override else None,
        model_family=family, same_family="skeptic_same_model" in (v.get("flags") or []),
        reasons=reasons[:5],
        what_would_change_my_mind=text(body.get("what_would_change_my_mind"), 180, live=any_live),
        second_order=text(body.get("second_order"), 180, live=any_live) or None,
    )


def _swing_case(case: Mapping[str, Any] | None, text: _SwingText, dropped: list[int], refs: Mapping[str, str]) -> Any:
    from council.publish.public_models import PublicSwingCase, PublicSwingClaim, PublicSwingRebuttal

    if not case:
        return None
    claims, live_any = [], False
    for c in case.get("claims") or []:
        ref = refs.get(str(c.get("ref")), str(c.get("ref")))
        if not re.match(r"^(idea:[0-9]{1,2}|trade:[A-Za-z0-9_\-]{1,64})$", ref):
            dropped[0] += 1
            continue
        ids = _swing_ids(c.get("evidence_ids"), dropped)
        live = any(is_live_id(e) for e in ids)
        live_any |= live
        claims.append(PublicSwingClaim(claim_id=str(c.get("claim_id")), ref=ref,
                                       text=text(c.get("text"), 330, live=live), evidence=ids[:6]))
    rebuttals = []
    for r in case.get("rebuttals") or []:
        ids = _swing_ids(r.get("evidence_ids"), dropped)
        live = any(is_live_id(e) for e in ids)
        live_any |= live
        if r.get("verdict") in ("concede", "refute"):
            rebuttals.append(PublicSwingRebuttal(claim_id=str(r.get("claim_id")), verdict=r["verdict"],
                                                 text=text(r.get("text"), 270, live=live), evidence=ids[:4]))
    live_any |= is_live_id(str(case.get("strongest_opposing_fact_id") or ""))
    return PublicSwingCase(argument=text(case.get("argument"), 1600, live=live_any), claims=claims[:8],
                           rebuttals=rebuttals[:8])


def public_swing_section(
    record: Mapping[str, Any],
    *,
    cycle_id: str,
    licensed_texts: Sequence[str] = (),
    origin_texts: Mapping[str, Sequence[str] | None] | None = None,
    trades: Iterable[Any] = (),
    today: Any = None,
    health: Any = None,
    alpaca_public: bool = False,
    declared_cost_pct_per_leg: float = SWING_DECLARED_COST_PCT,
) -> Any:
    """The swing part of one sealed cycle from its private record (`council.swing.record`).
    `licensed_texts`: the cycle pack's licensed texts; `origin_texts`: {cycle id: licensed feed texts
    its agents saw} for THIS cycle's swing reading and every origin of a carried idea (None or a
    missing key = unverifiable, the text is withheld); `trades`: the ledger's swing trades to show
    as the book at seal time (percent-only)."""
    from council.publish.public_models import PublicSwingIdea, PublicSwingSection, PublicSwingVotes

    origin_texts = dict(origin_texts or {})
    flags: list[str] = []
    dropped = [0]
    own = origin_texts.get(cycle_id)
    if own is None:
        flags.append("swing_licensed_texts_unavailable")
    base_texts = list(licensed_texts) + list(own or [])
    ideas_out = []
    refs: dict[str, str] = {}
    counter = [0]
    for row in record.get("ideas") or []:
        ref = str(row.get("ref") or "")
        if not re.match(r"^idea:[0-9]{1,2}$", ref):
            dropped[0] += 1
            continue
        refs[ref] = ref
        line = swing_line(str(row.get("ticker") or ""))
        side = row.get("side")
        if line is None or side not in ("long", "short"):
            dropped[0] += 1
            continue
        origins = [c for c in (row.get("carried_from") or []) if isinstance(c, str) and re.match(CYCLE_ID_RE, c)
                   and c != cycle_id]
        blocked = own is None
        if origins:
            try:
                leakscan.origin_matcher(origin_texts, origins)       # raises when any origin is unreadable
            except leakscan.OriginTextsUnavailable:
                blocked = True
        matcher = leakscan.LicensedMatcher(base_texts + [t for c in origins for t in (origin_texts.get(c) or [])])
        text = _SwingText(matcher, blocked=blocked)
        claim = text(row.get("catalyst_claim"), 135)
        thesis = text(row.get("thesis"), 440)
        verdict = _skeptic_public(ref, row.get("verdict"), text, dropped)
        cats = [c for c in (_swing_catalyst(x, text) for x in (row.get("catalysts") or [])) if c is not None][:4]
        facts, withheld = _swing_facts(row.get("facts") or {}, alpaca_public=alpaca_public)
        votes = row.get("votes")
        setup = str(row.get("setup") or "unknown")
        text_withheld = bool(origins) and text.withheld > 0
        counter[0] += text.withheld
        stage = row.get("stage") if row.get("stage") in (
            "dropped_by_code", "skeptic", "waiting", "debate", "pm", "risk", "planned", "approved", "executed",
            "missed", "expired") else "dropped_by_code"
        ideas_out.append(PublicSwingIdea(
            ref=ref, ticker=line, side=side, setup=setup if _SETUP_RE.match(setup) else "unknown",
            live_setup=bool(row.get("live_setup")), catalysts=cats, catalyst_claim=claim, thesis=thesis,
            stop_pct=_pct(row.get("stop_pct")), target_pct=_pct(row.get("target_pct")),
            time_stop_days=int(row.get("time_stop_days") or 0),
            facts=facts, facts_withheld=withheld, stage_reached=stage,
            drop_code=_code(str(row["drop_code"])) if row.get("drop_code") else None,
            verdict=verdict,
            votes=PublicSwingVotes(enter=int(votes["enter"]), replicates=int(votes["replicates"]),
                                   failed=int(votes.get("failed") or 0)) if isinstance(votes, Mapping) else None,
            carried_from=origins[:8], text_withheld=text_withheld,
        ))
    # the debate cites ideas and trades of this slot: checked against this cycle's texts only, and
    # every carried idea's origins (the advocates saw their facts, not the old feed text, but a
    # model can repeat what it was shown before)
    all_origins = [c for row in record.get("ideas") or [] for c in (row.get("carried_from") or [])
                   if isinstance(c, str) and c != cycle_id]
    try:
        debate_matcher = leakscan.LicensedMatcher(
            base_texts + [t for c in dict.fromkeys(all_origins) for t in (origin_texts.get(c) or [])])
        if all_origins:
            leakscan.origin_matcher(origin_texts, all_origins)
        debate_text = _SwingText(debate_matcher, blocked=own is None)
    except leakscan.OriginTextsUnavailable:
        debate_text = _SwingText(leakscan.LicensedMatcher(base_texts), blocked=True)
    bull = _swing_case(record.get("bull"), debate_text, dropped, refs)
    bear = _swing_case(record.get("bear"), debate_text, dropped, refs)
    counter[0] += debate_text.withheld
    trades_out = [t for t in (public_swing_trade(x, today=today,
                                                 declared_cost_pct_per_leg=declared_cost_pct_per_leg)
                              for x in trades) if t is not None and not t.state.startswith("closed_")]
    if dropped[0]:
        flags.append(f"swing_ids_dropped:{dropped[0]}")
    if counter[0]:
        flags.append(f"swing_text_withheld:{counter[0]}")
    for f in record.get("flags") or []:
        f = str(f)
        if f.startswith(("skeptic_same_model", "swing_error:", "scout_failed:", "skeptic_pass_rate",
                         "debate_claims_dropped:", "budget_")):
            flags.append(_code(f))
    return PublicSwingSection(live=bool(record.get("live")), ideas=ideas_out[:5], bull=bull, bear=bear,
                              trades=trades_out[:12], health=health,
                              declared_cost_pct_per_leg=float(declared_cost_pct_per_leg),
                              flags=list(dict.fromkeys(flags)))


CYCLE_ID_RE = r"^\d{4}-\d{2}-\d{2}T\d{4}Z$"


def _frac(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v)) else None


def public_swing_trade(row: Any, *, today: Any = None,
                       declared_cost_pct_per_leg: float = SWING_DECLARED_COST_PCT) -> Any:
    """One ledger swing trade -> `PublicSwingTrade` (None for a trade that never opened: proposed,
    executing, unknown or missed). Results come from the watch's percent-only outcome in `detail`
    (`r_declared`, `net_ret`) or, failing that, from the entry and exit as a RATIO only, always net
    of the declared cost on both legs. Units, rates, amounts, instrument and position ids are
    never read into the output."""
    from datetime import date as _date

    from council.publish.public_models import PublicSwingTrade
    from council.swing.rules import sessions_until

    state = _SWING_TRADE_STATES.get(str(getattr(row, "state", "")))
    if state is None:
        return None
    line = swing_line(str(row.ticker))
    if line is None or row.side not in ("long", "short"):
        return None
    d = dict(row.detail or {})
    size = _frac(d.get("size_nav")) or 0.0
    stop = _frac(d.get("stop_pct"))
    target = _frac(d.get("target_pct"))
    closed = state.startswith("closed_")
    r = net = None
    if closed:
        r, net = _frac(d.get("r_declared")), _frac(d.get("net_ret"))
        if net is None and row.open_rate and row.close_rate:
            sign = 1.0 if row.side == "long" else -1.0
            net = sign * (float(row.close_rate) / float(row.open_rate) - 1.0) - 2.0 * declared_cost_pct_per_leg / 100.0
        if r is None and net is not None and stop:
            r = net / stop
    held = d.get("days_held")
    if not isinstance(held, int) or isinstance(held, bool):
        start = row.opened_at.date() if getattr(row, "opened_at", None) is not None else None
        end = row.closed_at.date() if closed and getattr(row, "closed_at", None) is not None else today
        held = sessions_until(start, end) if start is not None and isinstance(end, _date) else 0
    tsd = None
    if row.time_stop_date:
        try:
            tsd = _date.fromisoformat(str(row.time_stop_date)[:10])
        except ValueError:
            tsd = None
    origin = row.origin_cycle if isinstance(row.origin_cycle, str) and re.match(CYCLE_ID_RE, row.origin_cycle) else None
    closed_cycle = d.get("closed_cycle") if isinstance(d.get("closed_cycle"), str) and re.match(
        CYCLE_ID_RE, d["closed_cycle"]) else None
    tp_mode = str(d.get("tp_mode") or "none")
    return PublicSwingTrade(
        trade_id=row.trade_id, ticker=line, side=row.side, weight_x=_x(size), opened_cycle=origin,
        closed_cycle=closed_cycle, days_held=max(0, min(400, int(held))),
        stop_pct=_pct(stop), target_pct=_pct(target),
        tp_at_broker=state != "open_tp_missing" and tp_mode not in ("none", ""),
        time_stop_date=tsd, state=state, exit_kind=_EXIT_KIND.get(state),
        r_declared=round(r, 3) if r is not None else None,
        net_declared_pct=_pct(net) if net is not None else None,
        contribution_declared_bp=_bp(size * net * 1e4) if net is not None else None,
        live=bool(d.get("live", True)),
    )


def public_skeptic_health(canary_grades: Sequence[str] = (), model_verdicts: Sequence[str] = (), *,
                          alarm: bool | None = None) -> Any:
    """The Skeptic-health line from the canary grades (oldest first) and the model's own verdicts
    (before code rules, oldest first, canaries excluded)."""
    from council.publish.public_models import PublicSkepticHealth
    from council.swing.canary import pass_rate_alarm

    grades = [g for g in canary_grades if g in ("caught", "missed")]
    verdicts = [v for v in model_verdicts if v in ("pass", "wait", "reject")]
    last20, last10 = verdicts[-20:], verdicts[-10:]
    return PublicSkepticHealth(
        canary_last=grades[-1] if grades else "none_yet",  # type: ignore[arg-type]
        canaries_caught_total=sum(1 for g in grades if g == "caught"),
        canaries_missed_total=sum(1 for g in grades if g == "missed"),
        pass_share_20_pct=round(100.0 * sum(1 for v in last20 if v == "pass") / len(last20), 1) if last20 else None,
        rejects_last_10=sum(1 for v in last10 if v == "reject"),
        alarm=bool(pass_rate_alarm(verdicts)) if alarm is None else bool(alarm),
    )


_EXIT_MIX = {"stop": "stop", "target": "target", "time": "time", "exit": "discretionary",
             "halt": "discretionary", "external": "external"}


def _interval(iv: Any) -> Any:
    from council.publish.public_models import PublicInterval

    r3 = lambda v: round(float(v), 3) if v is not None else None  # noqa: E731
    return PublicInterval(mean=r3(iv.mean), low=r3(iv.low), high=r3(iv.high), n=int(iv.n))


def _base100(rows: Sequence[Mapping[str, Any]], column: str) -> list[float | None]:
    out: list[float | None] = []
    level, seen = 100.0, False
    for r in rows:
        v = r.get(column)
        if isinstance(v, int | float) and math.isfinite(float(v)):
            level *= 1.0 + float(v)
            seen = True
        out.append(round(level, 4) if seen and level > 0 else None)
    return out


def public_swing_book(
    trades: Iterable[Any],
    *,
    as_of: datetime,
    paper_rows: Iterable[Mapping[str, Any]] = (),
    benchmark_days: Sequence[Mapping[str, Any]] = (),
    health: Any = None,
    live: bool = False,
    live_since: Any = None,
    paper_since: Any = None,
    today: Any = None,
    declared_cost_pct_per_leg: float = SWING_DECLARED_COST_PCT,
    resamples: int | None = None,
) -> Any:
    """The swing page's document: open and closed trades, the §8.2 metrics over closed live trades,
    every paper group of the funnel (all eight, empty ones included), the three benchmark curves
    (SQ-8 PAPER, matched index, index hold; base 100) and the Skeptic-health line."""
    from datetime import date as _date

    from council.publish.public_models import (
        PublicBenchmarkPoint,
        PublicFunnelGroup,
        PublicSkepticHealth,
        PublicSwingBook,
        PublicSwingMetrics,
    )
    from council.swing import metrics as M
    from council.swing import paper as P

    kw = {"resamples": resamples} if resamples is not None else {}
    pub = [t for t in (public_swing_trade(x, today=today, declared_cost_pct_per_leg=declared_cost_pct_per_leg)
                       for x in trades) if t is not None]
    open_t = [t for t in pub if not t.state.startswith("closed_")]
    closed_t = [t for t in pub if t.state.startswith("closed_")]
    raw = {x.trade_id: x for x in trades} if isinstance(trades, list) else {}
    closed_live = []
    for t in closed_t:
        if t.r_declared is None or t.net_declared_pct is None or not t.live:
            continue
        d = dict(getattr(raw.get(t.trade_id), "detail", None) or {})
        closed_live.append(M.ClosedTrade(
            r_declared=float(t.r_declared), net_ret=float(t.net_declared_pct) / 100.0, size_nav=float(t.weight_x),
            side=t.side, beta=_frac(d.get("beta")), sector_etf_ret=_frac(d.get("sector_etf_ret")),
            exit_kind=_EXIT_MIX.get(t.exit_kind or "", "external"), days_held=t.days_held))
    s = M.summarize(closed_live, declared_cost_pct_per_leg=declared_cost_pct_per_leg, **kw)
    metrics = PublicSwingMetrics(
        n_closed=s.n, hit_rate_pct=_pct(s.hit_rate) if s.hit_rate is not None else None,
        expectancy_r=_interval(s.expectancy), payoff=round(s.payoff, 3) if s.payoff is not None else None,
        contribution_declared_bp=_bp(s.contribution_bps),
        matched_contribution_declared_bp=_bp(s.matched_contribution_bps) if s.matched_contribution_bps is not None else None,
        vs_matched_pct=_pct(s.vs_matched) if s.vs_matched is not None else None,
        exit_mix_pct={k: _pct(v) for k, v in s.exit_mix.items()},
        avg_days_held=round(s.avg_days_held, 2) if s.avg_days_held is not None else None,
        standard_error_r=(round(se, 3) if (se := M.standard_error([t.r_declared for t in closed_live])) is not None
                          else None),
    )
    rows = list(paper_rows)
    outcomes = P.closed_outcomes(rows)
    by_group = M.funnel(outcomes, **kw)
    funnel = []
    for g in P.GROUPS:
        n_ideas = sum(1 for r in rows if (r.get("record") or {}).get("group", r.get("group")) == g)
        rs = [o["r_declared"] for o in outcomes if o["group"] == g]
        iv = by_group.get(g)
        funnel.append(PublicFunnelGroup(group=g, ideas=n_ideas, closed=len(rs),
                                        r_declared=_interval(iv) if iv is not None else _interval(M.Interval(None, None, None, 0)),
                                        hit_rate_pct=_pct(M.hit_rate(rs)) if rs else None))
    days = sorted(benchmark_days, key=lambda r: str(r.get("day")))
    curves = {c: _base100(days, c) for c in ("sq8_ret", "matched_idx_ret", "idx_hold_ret")}
    bench = []
    for i, r in enumerate(days):
        try:
            day = r["day"] if isinstance(r["day"], _date) else _date.fromisoformat(str(r["day"])[:10])
        except (KeyError, ValueError):
            continue
        bench.append(PublicBenchmarkPoint(day=day, sq8=curves["sq8_ret"][i], matched_index=curves["matched_idx_ret"][i],
                                          index_hold=curves["idx_hold_ret"][i]))
    return PublicSwingBook(
        as_of=as_of, live=bool(live), live_since=live_since, paper_since=paper_since,
        declared_cost_pct_per_leg=float(declared_cost_pct_per_leg), open_trades=open_t[:12],
        closed_trades=closed_t, metrics=metrics, funnel=funnel, benchmarks=bench,
        health=health if health is not None else PublicSkepticHealth(),
    )
