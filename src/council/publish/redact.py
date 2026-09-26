"""Private cycle record -> public document, built FIELD BY FIELD from allow-listed models.

Rules:
- Nothing is produced by deleting fields from a private object: every public value is read from a
  named private field, converted to a percentage unit and rounded (x 0.001, % 0.01, bp 0.1).
- Private execution fields (amounts, units, stop rates, position/instrument ids, request ids,
  error strings) are never read.
- Model text is untrusted: control/bidi characters, URLs, @handles, e-mails, paths, money amounts
  and long numbers are removed, and any text sharing an 8-word n-gram with a licensed feed item
  is withheld.
- Evidence ids become typed refs: N: -> broker_feed id only; M: -> FRED series (value only when
  the series is in `council.data.fred.PUBLISHABLE`, the single publishability list, and the
  pack's fact is not marked unpublishable); F:/V:/C:/E:/S:/K: -> ids.
- Symbols are published only as exposure LINES; unknown symbols are dropped and counted, and
  `UNMAPPED_<instrument id>` is scrubbed to `UNMAPPED` in any published text.
- Approval timestamps are rounded down to their slot; the material-change fingerprint is
  published as a short hash only.
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
from council.publish import labels, leakscan
from council.publish.public_models import (
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
_LONG_NUMBER = re.compile(r"(?<![\w.])(?<!\b[NSMECFVK]:)\d{7,}(?!\w)")
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
_ID_KIND = {"F": "market", "V": "vol", "C": "cost", "E": "event", "S": "filing", "K": "card"}
_ID_OK = re.compile(r"^[FVCESK]:[A-Za-z0-9_.:@#+-]{1,80}$")
_NEWS_OK = re.compile(r"^N:[0-9a-f]{8}$")
# M:<SERIES>[.<measure>][@YYYY-MM-DD], e.g. M:DGS10@2026-09-24 or M:DGS10.chg20@2026-09-24.
_MACRO = re.compile(r"^M:([A-Z0-9_]{1,32})(?:\.([a-z0-9_]{1,16}))?(?:@(\d{4}-\d{2}-\d{2}))?$")
_FRED_UNITS = frozenset({"pct", "bps", "x", "ratio"})
_NO_PUBLISH_SOURCES = frozenset({"fred:no_publish"})


def _fred_publishable(series: str, fact: Fact | None) -> bool:
    """One list decides (council.data.fred.PUBLISHABLE); a pack fact can only make it stricter."""
    if not fred.is_publishable(series):
        return False
    return fact is None or (fact.publishable and fact.source not in _NO_PUBLISH_SOURCES)


class _Evidence:
    def __init__(self, pack: FactPack | None):
        self.facts: dict[str, Fact] = {}
        if pack is not None:
            self.facts = {f.id: f for f in pack.facts if f.id.startswith("M:")}
        self.dropped = 0

    def ref(self, evidence_id: str | None) -> dict[str, Any] | None:
        eid = (evidence_id or "").strip()
        if _NEWS_OK.match(eid):
            return {"kind": "broker_feed", "id": eid}
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


def _facts(pack: FactPack | None, lm: LineMap) -> tuple[list[PublicFact], int]:
    """The evidence table: one entry per fact, event, news item and filing sentence of the pack.
    Returns (entries, count dropped: a symbol that is not a line, or an id or kind the table
    does not know)."""
    if pack is None:
        return [], 0
    rows: list[tuple[tuple[int, int, int, str], PublicFact]] = []
    dropped = 0

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
        source = "calendar" if event.source.startswith(("policy_calendar", "fred_release")) else "broker_feed"
        rows.append((order("event", line, event.id), PublicFact(
            id=event.id, kind="event", label=labels.fact_label(event.id), line=line, source=source,
        )))
    for item in pack.news:
        mapped = {m for m in (lm.lookup(s) for s in item.symbols) if m is not None}
        line = next(iter(mapped)) if len(mapped) == 1 else None
        rows.append((order("news", line, item.id), PublicFact(
            id=item.id, kind="news", label=labels.NEWS_LABEL, line=line, source="broker_feed",
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
    return [row for _, row in sorted(unique.values(), key=lambda kr: kr[0])], dropped


def _check_value(v: float | str | None) -> float | str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return round(float(v), 4)
    return clean_text(v, 40)


def _rule_id(v: str) -> str:
    rid = re.sub(r"[^A-Za-z0-9_]", "", v or "")[:16]
    return rid if rid and rid[0].isupper() else "R_unknown"


def _risk(risk: RiskDecision | None, unit_w: dict[str, float], lm: LineMap) -> PublicRisk | None:
    if risk is None:
        return None
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
                value=_check_value(c.value), limit=_check_value(c.limit), kind=c.kind,
            )
            for c in risk.checks
        ],
        gross_x=_x(risk.gross),
        net_x=_x(risk.net),
        margin_use_pct=_pct(risk.margin_use),
        stop_at_risk_pct=_pct(risk.stop_budget_used),
        carry_bp_day=_bp(risk.carry_bps_day),
        ex_ante_vol_pct=_pct(risk.ex_ante_vol),
        hold_reasons=[_code(r) for r in risk.hold_reasons],
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
        skipped=[_code(s) for s in plan.skipped],
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


def short_fingerprint(value: str | None) -> str:
    """The material-change fingerprint as a short hash: a hex digest is truncated, anything else
    is hashed first, so the fingerprint's contents are never published."""
    raw = (value or "").strip()
    if not raw:
        return ""
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
) -> PublicCycleV1:
    """Build the public cycle document from the private record (and the pack, for licensed-text
    and FRED checks). Raises pydantic.ValidationError if anything falls outside the allow-list."""
    lm = LineMap(lines)
    licensed = [f"{n.title} {n.summary}" for n in pack.news] if pack is not None else []
    text = _Text(licensed)
    ev = _Evidence(pack)
    flags = [_code(f) for f in rec.flags]

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
    facts, facts_dropped = _facts(pack, lm)
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
        "material_fingerprint": short_fingerprint(rec.material_fingerprint),
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
        "risk": _risk(rec.risk, unit_w, lm),
        "plan": _plan(rec.plan, lm),
        "decision": _decision(rec),
        "calls": _calls(rec),
        "facts": facts,
    }
    # Counters are complete only after every field above was built.
    if lm.unmapped:
        flags.append(f"unmapped_symbols_dropped:{lm.unmapped}")
    if ev.dropped:
        flags.append(f"evidence_ids_dropped:{ev.dropped}")
    if facts_dropped:
        flags.append(f"facts_dropped:{facts_dropped}")
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
        flags=[_code(f) for f in rec.flags],
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


def _fill(r: LegResultLike, line: str, leg: Leg | None, state: str, nav_usd: float) -> PublicFill:
    direction = leg.direction if leg is not None else None
    target = _x(leg.weight_after - leg.weight_before) if leg is not None else None
    filled = slippage = error = None
    units, price = _pos(r.units_filled), _pos(r.fill_price)
    sign = _weight_sign(r.kind, direction)
    if r.kind == "open" and units is not None and price is not None and sign is not None:
        filled = _within(_x(sign * units * price / nav_usd), 5.0)
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
    )


def public_execution(
    report: ExecutionReportLike,
    *,
    cycle_id: str,
    lines: Iterable[LineSpec] | Universe | Mapping[str, LineSpec] | None,
    nav_usd: float,
    plan: Plan | None = None,
    approved_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> PublicExecution:
    """The public execution record from the PRIVATE `ExecutionReport`.

    Per leg: line, kind, direction, target vs filled weight (x NAV at approval), slippage and
    cost in bp, and state. `nav_usd` is only a denominator and is never published. `plan` (the
    approved plan, joined by leg seq) supplies direction, the approved weight change, the planned
    price and the cost estimate; without it those fields are None and `plan_missing` is flagged.
    Amounts, units, prices, order/position ids, request ids and error strings are never read into
    the output."""
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
        fills.append(_fill(r, line, leg, state, nav))
    fills.sort(key=lambda f: f.seq)
    final = report.final_state if report.final_state in _DECISION_STATES else "execution_unknown"
    if final != report.final_state:
        flags.append("final_state_unrecognised")
    rec = report.reconcile
    costs = [f.cost_bp for f in fills if f.state in _EXECUTED and f.cost_bp is not None]
    achieved = lm.sum_by_line(rec.achieved_w) if rec is not None else {}
    for name, count in (("unmapped_symbols_dropped", lm.unmapped), ("leg_kind_unrecognised", bad_kind),
                        ("leg_state_unrecognised", bad_state), ("leg_not_in_plan", no_plan_leg)):
        if count:
            flags.append(f"{name}:{count}")
    return PublicExecution(
        cycle_id=cycle_id,
        decision_state=final,
        approved_slot=_slot_of(approved_at),
        completed_slot=_slot_of(completed_at),
        fills=fills,
        achieved_x=achieved,
        achieved_drift_x=_within(_x(rec.drift), 5.0) if rec is not None else None,
        cost_bp_total=_bp(sum(costs)) if plan is not None else None,
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
