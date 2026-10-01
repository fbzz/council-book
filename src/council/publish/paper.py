"""The PUBLIC record of a paper run (`council cycle --paper [--trace-all] --publish`).

A paper run trades nothing and has no approval, so its public record is revealed at once. It goes
through the SAME public pipeline as a live cycle: the core council part is `redact.public_cycle`
(the allow-listed `PublicCycleV1`), the swing part reuses the swing redaction helpers (licensed
overlap -> withheld, live-layer numbers scrubbed, `N:` items id + feed label only), the document is
sealed (`commit_reveal.seal_bytes`) and revealed in the same write, and every file passes the leak
scan (the cycle's licensed texts + canaries) before anything is written.

Layout, under the target repo (`--publish-dir`, default the repo the code runs from):

  journal/paper/cycles/YYYY/MM/<cycle>.json          the sealed bytes (PublicPaperCycle)
  journal/paper/cycles/YYYY/MM/<cycle>.reveal.json   its salt (the commitment is in the row)
  journal/paper/decisions.jsonl                      one row per decision, #1, #2, ... append-only
  journal/paper/latest.json                          the paper portfolio + the latest decision

Numbering: a new cycle gets max(#) + 1; re-publishing a cycle (`--force`) keeps its number and
replaces its row in place; earlier rows are never rewritten. Nothing is committed or pushed here.

Percent-only: weights and legs in % of the paper NAV, distances in % of the entry, paper P&L in %
of the paper NAV since the first paper decision. The paper NAV itself is never an input.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from council.publish import commit_reveal, leakscan
from council.publish.public_models import (
    Code,
    CycleId,
    Hex64,
    Line,
    Pct,
    PublicCycleV1,
    PublicModel,
    PublicSwingCase,
    PublicSwingCatalyst,
    PublicSwingIdea,
    ReasonText,
    ShortText,
    SwingEvidenceId,
    SwingSide,
    UtcDatetime,
)

PAPER_DIR = "journal/paper"
DECISIONS_PATH = f"{PAPER_DIR}/decisions.jsonl"
LATEST_PATH = f"{PAPER_DIR}/latest.json"
DECLARED_COST_PCT_PER_LEG = 1.25
Stage = Literal["scout", "gate", "skeptic", "pm", "rules", "leg", "unknown"]
Share = Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
WhyText = Annotated[str, Field(max_length=240)]
_CYCLE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}Z$")
_IDEA = re.compile(r"^idea:[0-9]{1,2}$")
_CTX_ID = re.compile(r"^[FVCESMP]:[A-Za-z0-9_.:@#+-]{1,80}$")
_STAGES = frozenset(Stage.__args__)  # type: ignore[attr-defined]


class PaperPublishError(RuntimeError):
    """The paper record could not be published (nothing was written)."""


# ------------------------------------------------------------------------------------- models
class PaperOutcome(PublicModel):
    stage: Stage = "unknown"
    code: Code | None = None
    note: ShortText = ""


class PaperLeg(PublicModel):
    """A would-be paper leg after the S-rules (SWING_BOOK_LIVE is False: nothing is placed). The
    result is net of the DECLARED cost per leg, never the actual one (which depends on the NAV)."""

    ok: bool
    code: Code | None = None
    rule: Code | None = None
    size_nav_pct: Share | None = None
    stop_pct: Pct | None = None
    target_pct: Pct | None = None
    time_stop_days: int | None = Field(default=None, ge=0, le=30)
    time_stop_date: date | None = None


class PaperIdea(PublicModel):
    idea: PublicSwingIdea
    batch: int | None = Field(default=None, ge=0, le=10)
    real_outcome: PaperOutcome = Field(default_factory=PaperOutcome)
    traced_outcome: PaperOutcome = Field(default_factory=PaperOutcome)
    same: bool = True
    chosen: bool = False
    leg: PaperLeg | None = None              # the real pipeline's leg (what we chose)
    traced_leg: PaperLeg | None = None       # the leg had nothing blocked (trace-all)
    why: WhyText = ""


class PaperReading(PublicModel):
    """One reading-list item: its catalyst view (N: id + feed label only; P: title + .gov link;
    S: form + items), its age and its tickers."""

    item: PublicSwingCatalyst
    age_h: Annotated[float, Field(ge=0.0, le=10000.0)]
    tickers: list[Line] = Field(default_factory=list, max_length=12)


class PaperScreenRow(PublicModel):
    id: SwingEvidenceId
    ticker: Line | None = None
    move_pct: Pct | None = None
    move_sigma: Annotated[float, Field(ge=-100.0, le=100.0, allow_inf_nan=False)] | None = None
    volume: Literal["<1", "1-2", "2-4", ">4"] | None = None
    sector: Annotated[str, Field(max_length=40)] | None = None


class PaperContext(PublicModel):
    id: Annotated[str, Field(pattern=r"^[FVCESMP]:[A-Za-z0-9_.:@#+-]{1,80}$")]
    text: ShortText


class PaperOpenTrade(PublicModel):
    ref: Annotated[str, Field(pattern=r"^trade:[A-Za-z0-9_\-]{1,64}$")]
    ticker: Line
    side: SwingSide
    days_held: int = Field(ge=0, le=400)
    to_stop_pct: Pct | None = None
    to_target_pct: Pct | None = None


class PaperInputs(PublicModel):
    reading: list[PaperReading] = Field(default_factory=list, max_length=200)
    screen: list[PaperScreenRow] = Field(default_factory=list, max_length=200)
    context: list[PaperContext] = Field(default_factory=list, max_length=60)
    open_trades: list[PaperOpenTrade] = Field(default_factory=list, max_length=12)
    carried: list[Line] = Field(default_factory=list, max_length=20)


class PaperAction(PublicModel):
    ref: Annotated[str, Field(pattern=r"^(idea:[0-9]{1,2}|trade:[A-Za-z0-9_\-]{1,64}|budget)$")]
    action: Annotated[str, Field(pattern=r"^[a-z_]{1,24}$")]
    stop_pct: Pct | None = None
    target_pct: Pct | None = None
    time_stop_days: int | None = Field(default=None, ge=0, le=30)
    reason: ReasonText = ""


class PaperReplicate(PublicModel):
    replicate: int = Field(ge=0, le=5)
    valid: bool
    actions: list[PaperAction] = Field(default_factory=list, max_length=12)
    budget_pct: Share | None = None
    budget_vote_valid: bool = False
    budget_reason: ReasonText = ""


class PaperTally(PublicModel):
    ref: Annotated[str, Field(pattern=r"^(idea:[0-9]{1,2}|trade:[A-Za-z0-9_\-]{1,64})$")]
    action: Annotated[str, Field(pattern=r"^[a-z_]{1,24}$")]
    votes_for: int = Field(ge=0, le=3)
    replicates: int = Field(ge=0, le=3)


class PaperBatch(PublicModel):
    refs: list[Annotated[str, Field(pattern=r"^idea:[0-9]{1,2}$")]] = Field(default_factory=list, max_length=10)
    bull: PublicSwingCase | None = None
    bear: PublicSwingCase | None = None
    replicates: list[PaperReplicate] = Field(default_factory=list, max_length=5)
    tally: list[PaperTally] = Field(default_factory=list, max_length=20)


class PaperBudget(PublicModel):
    swing_pct: Share
    core_pct: Share
    median_pct: Share | None = None
    votes: int = Field(default=0, ge=0, le=5)
    fallback: bool = False
    open_pct: Share = 0.0


class PaperSwing(PublicModel):
    trace_all: bool = False
    skeptic_model_family: Annotated[str, Field(pattern=r"^[a-z0-9_.-]{0,32}$")] = ""
    inputs: PaperInputs = Field(default_factory=PaperInputs)
    ideas: list[PaperIdea] = Field(default_factory=list, max_length=40)
    scout_passed: list[Line] = Field(default_factory=list, max_length=40)
    batches: list[PaperBatch] = Field(default_factory=list, max_length=10)
    budget: PaperBudget | None = None
    real_budget_flags: list[Code] = Field(default_factory=list, max_length=12)
    declared_cost_pct_per_leg: float = DECLARED_COST_PCT_PER_LEG
    flags: list[Code] = Field(default_factory=list)


class PublicPaperCycle(PublicModel):
    """One paper run, revealed at once (no approval). `core` is the core council's public cycle
    built by the live pipeline (`redact.public_cycle`); `swing` the paper swing slot."""

    schema_id: Literal["council-book/paper-cycle/v1"] = "council-book/paper-cycle/v1"
    paper: Literal[True] = True
    decision_no: int = Field(ge=1)
    cycle_id: CycleId
    slot: UtcDatetime
    core: PublicCycleV1
    swing: PaperSwing | None = None
    flags: list[Code] = Field(default_factory=list)


class PaperChosen(PublicModel):
    ticker: Line
    side: SwingSide
    size_nav_pct: Share | None = None
    stop_pct: Pct | None = None
    target_pct: Pct | None = None
    time_stop_date: date | None = None


class PublicPaperDecisionRow(PublicModel):
    schema_id: Literal["council-book/paper-decision/v1"] = "council-book/paper-decision/v1"
    decision_no: int = Field(ge=1)
    cycle_id: CycleId
    slot: UtcDatetime
    mode: Literal["paper"] = "paper"
    trace_all: bool = False
    ideas: int = Field(ge=0, le=40)
    chosen: list[PaperChosen] = Field(default_factory=list, max_length=12)
    core_moves: int = Field(default=0, ge=0, le=64)
    why: WhyText = ""
    commitment_sha256: Hex64
    path: Annotated[str, Field(pattern=r"^journal/paper/cycles/\d{4}/\d{2}/\d{4}-\d{2}-\d{2}T\d{4}Z\.json$")]


class PaperReveal(PublicModel):
    schema_id: Literal["council-book/paper-reveal/v1"] = "council-book/paper-reveal/v1"
    cycle_id: CycleId
    salt: Hex64
    commitment_sha256: Hex64
    sealed_at: UtcDatetime
    code_commit: Annotated[str, Field(pattern=r"^([0-9a-f]{7,40})?$")] = ""
    algo: Literal["sha256(salt||canonical_json)"] = "sha256(salt||canonical_json)"


class PaperHolding(PublicModel):
    line: Line
    weight_pct: Pct


class PaperSwingHolding(PublicModel):
    ticker: Line
    side: SwingSide
    size_nav_pct: Share
    stop_pct: Pct | None = None
    target_pct: Pct | None = None
    decision_no: int = Field(ge=1)
    opened_cycle: CycleId
    time_stop_date: date | None = None


class PaperPerformance(PublicModel):
    """Paper P&L in % of the paper NAV since `since`: the closed paper swing legs (size x net
    return after the declared cost, simple sum). The core has no paper fills: not tracked yet."""

    since: date | None = None
    swing_closed_legs: int = Field(default=0, ge=0)
    swing_return_pct: Pct = 0.0
    core_return_pct: Pct | None = None
    note: ShortText = "core: weights only (no paper fills); swing: closed paper legs, net of the declared cost"


class PaperBookLine(PublicModel):
    line: Line
    weight_pct: Pct


class PaperBookTrade(PublicModel):
    ticker: Line
    side: SwingSide
    setup: Code | None = None
    status: Literal["open", "closed"]
    weight_pct: Pct = 0.0
    stop_pct: Pct
    target_pct: Pct
    entry_day: date
    days_held: int = Field(ge=0, le=400)
    exit_reason: Code | None = None
    return_net_pct: Pct                   # net of the declared 1.25% per leg (both legs)


class PaperBookView(PublicModel):
    """The paper BOOK (`council.paperbook.paper_book_public`): what the paper broker holds after the
    paper fills, marked to market. Percent only; the paper NAV is never an input."""

    paper_return_pct: Pct
    started: date | None = None
    core: list[PaperBookLine] = Field(default_factory=list, max_length=64)
    swing_trades: list[PaperBookTrade] = Field(default_factory=list, max_length=200)
    core_pct: Pct = 0.0
    swing_pct: Pct = 0.0
    cash_pct: Pct = 0.0


def paper_book_view(pub: Mapping[str, Any] | None) -> PaperBookView | None:
    """`paper_book_public(state)` as the public model, or None (no book / unusable)."""
    from council.publish.redact import swing_line

    if not pub:
        return None
    try:
        trades = []
        for t in pub.get("swing_trades") or []:
            line = swing_line(str(t.get("ticker") or ""))
            if line is None:
                continue
            trades.append(PaperBookTrade(ticker=line, side=t["side"], setup=t.get("setup"), status=t["status"],
                                         weight_pct=t.get("weight_pct") or 0.0, stop_pct=t["stop_pct"],
                                         target_pct=t["target_pct"], entry_day=date.fromisoformat(t["entry_day"]),
                                         days_held=int(t.get("days_held") or 0), exit_reason=t.get("exit_reason"),
                                         return_net_pct=t["return_net_pct"]))
        split = pub.get("split_pct") or {}
        return PaperBookView(paper_return_pct=pub["paper_return_pct"], started=_day(pub.get("started_at")),
                             core=[PaperBookLine(line=k, weight_pct=v) for k, v in (pub.get("core_weights_pct") or {}).items()],
                             swing_trades=trades[-200:], core_pct=split.get("core", 0.0),
                             swing_pct=split.get("swing", 0.0), cash_pct=split.get("cash", 0.0))
    except Exception:  # noqa: BLE001 - an unusable book view is left out, never published half-checked
        return None


class PublicPaperLatest(PublicModel):
    schema_id: Literal["council-book/paper-latest/v1"] = "council-book/paper-latest/v1"
    as_of: UtcDatetime
    decision_no: int = Field(ge=1)
    cycle_id: CycleId
    decisions: int = Field(ge=1)
    core: list[PaperHolding] = Field(default_factory=list, max_length=64)
    swing_open: list[PaperSwingHolding] = Field(default_factory=list, max_length=24)
    swing_pct: Share = 0.0
    core_pct: Share = 100.0
    swing_budget_pct: Share | None = None
    performance: PaperPerformance = Field(default_factory=PaperPerformance)
    book: PaperBookView | None = None     # the paper book after its fills (`council.paperbook`)


# ------------------------------------------------------------------------------------- helpers
def cycle_file(cycle_id: str) -> str:
    if not _CYCLE.match(cycle_id):
        raise PaperPublishError(f"not a cycle id: {cycle_id!r}")
    return f"{PAPER_DIR}/cycles/{cycle_id[0:4]}/{cycle_id[5:7]}/{cycle_id}.json"


def reveal_file(cycle_id: str) -> str:
    return cycle_file(cycle_id)[: -len(".json")] + ".reveal.json"


def _num(v: Any, lo: float = -1000.0, hi: float = 1000.0) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return round(f, 3) if f == f and lo <= f <= hi else None


def _frac_pct(v: Any) -> float | None:
    n = _num(v, -10.0, 10.0)
    return None if n is None else round(n * 100.0, 3)


def _bucket(v: Any) -> str | None:
    n = _num(v, 0.0, 1e6)
    if n is None:
        return None
    return "<1" if n < 1 else "1-2" if n < 2 else "2-4" if n < 4 else ">4"


def _outcome(o: Mapping[str, Any] | None, text: Any) -> PaperOutcome:
    o = dict(o or {})
    stage = o.get("stage") if o.get("stage") in _STAGES else "unknown"
    code = o.get("code")
    return PaperOutcome(stage=stage, code=str(code)[:120] if code else None, note=text(o.get("note"), 160))


def _leg(v: Mapping[str, Any] | None) -> PaperLeg | None:
    if not v:
        return None
    tsd = v.get("time_stop_date")
    try:
        when = date.fromisoformat(str(tsd)) if tsd else None
    except ValueError:
        when = None
    ok = bool(v.get("ok"))
    size = _num(v.get("size_nav_pct"), 0.0, 100.0) if ok else None
    return PaperLeg(ok=ok, code=str(v["code"])[:120] if v.get("code") else None,
                    rule=str(v["rule"])[:120] if v.get("rule") else None, size_nav_pct=size,
                    stop_pct=_num(v.get("stop_pct")) if ok else None, target_pct=_num(v.get("target_pct")) if ok else None,
                    time_stop_days=int(v["time_stop_days"]) if ok and isinstance(v.get("time_stop_days"), int) else None,
                    time_stop_date=when if ok else None)


STAGE_WORDS = {"scout": "dropped at the Scout check", "gate": "stopped by the code gate",
               "skeptic": "stopped by the Skeptic", "pm": "the manager did not enter",
               "rules": "stopped by the S-rules", "unknown": "outcome not recorded"}


def why_line(idea: PublicSwingIdea, real: PaperOutcome, leg: PaperLeg | None, pm_reason: str) -> str:
    """One public line: why this idea was or was not chosen (code words + cleaned model text)."""
    if leg is not None and leg.ok:
        head = f"chosen: {leg.size_nav_pct:g}% of NAV" if leg.size_nav_pct is not None else "chosen"
        return f"{head}. {pm_reason}".strip()[:240] if pm_reason else head
    if real.stage == "skeptic" and idea.verdict is not None:
        first = idea.verdict.reasons[0].text if idea.verdict.reasons else ""
        return f"Skeptic {idea.verdict.verdict}: {first}".strip(": ")[:240]
    words = STAGE_WORDS.get(real.stage, real.stage)
    if real.stage == "pm" and real.code == "enter":
        words = "entered by the manager, no paper leg"
    return f"{words}{f' ({real.code})' if real.code and real.code != 'enter' else ''}"[:240]


def _texter(licensed: Sequence[str], blocked: bool = False) -> Any:
    from council.publish.redact import _SwingText

    return _SwingText(leakscan.LicensedMatcher(list(licensed)), blocked=blocked)


def _ids(raw: Iterable[Any]) -> list[str]:
    from council.publish.redact import _swing_ids

    return _swing_ids(raw, [0])


# ------------------------------------------------------------------------------------- builders
def _inputs(inp: Mapping[str, Any], text: Any) -> PaperInputs:
    from council.publish.redact import _swing_catalyst, swing_line

    reading = []
    for r in inp.get("reading") or []:
        rid = str(r.get("id") or "")
        row: dict[str, Any] = {"id": rid}
        if rid.startswith("N:"):
            if r.get("feed"):
                row["source"] = str(r["feed"])
        elif not r.get("licensed"):
            row.update(title=r.get("title"), link=r.get("link"), form=r.get("form"), items=r.get("items") or [])
        cat = _swing_catalyst(row, text)
        if cat is None:
            continue
        tickers = [t for t in (swing_line(s) for s in r.get("symbols") or []) if t][:12]
        reading.append(PaperReading(item=cat, age_h=max(0.0, min(10000.0, float(r.get("age_h") or 0.0))),
                                    tickers=tickers))
    screen = []
    for r in inp.get("screen") or []:
        sid = str(r.get("id") or "")
        if not _ids([sid]):
            continue
        sector = text(r.get("sector"), 40) if r.get("sector") else None
        screen.append(PaperScreenRow(id=sid, ticker=swing_line(str(r.get("line_id") or "")),
                                     move_pct=_num(r.get("move_pct")), move_sigma=_num(r.get("move_sigma"), -100, 100),
                                     volume=_bucket(r.get("vol_ratio")), sector=sector or None))
    context = [PaperContext(id=str(c["id"]), text=text(c.get("text"), 160))
               for c in inp.get("context") or [] if _CTX_ID.match(str(c.get("id") or ""))]
    trades = []
    for t in inp.get("open_trades") or []:
        line = swing_line(str(t.get("ticker") or ""))
        if line is None or t.get("side") not in ("long", "short") or not re.match(
                r"^trade:[A-Za-z0-9_\-]{1,64}$", str(t.get("ref") or "")):
            continue
        trades.append(PaperOpenTrade(ref=t["ref"], ticker=line, side=t["side"], days_held=int(t.get("days_held") or 0),
                                     to_stop_pct=_num(t.get("to_stop_pct")), to_target_pct=_num(t.get("to_target_pct"))))
    carried = [x for x in (swing_line(str(c.get("ticker") or "")) for c in inp.get("carried") or []) if x]
    return PaperInputs(reading=reading[:200], screen=screen[:200], context=context[:60], open_trades=trades[:12],
                       carried=carried[:20])


def _action(a: Mapping[str, Any], text: Any) -> PaperAction | None:
    ref, act = str(a.get("ref") or ""), str(a.get("action") or "")
    if not re.match(r"^(idea:[0-9]{1,2}|trade:[A-Za-z0-9_\-]{1,64})$", ref) or not re.match(r"^[a-z_]{1,24}$", act):
        return None
    from council.publish.redact import is_live_id

    live = any(is_live_id(e) for e in _ids(a.get("evidence_ids") or []))
    days = a.get("time_stop_days")
    return PaperAction(ref=ref, action=act, stop_pct=_frac_pct(a.get("stop_pct")), target_pct=_frac_pct(a.get("target_pct")),
                       time_stop_days=days if isinstance(days, int) and 0 <= days <= 30 else None,
                       reason=text(a.get("reason"), 180, live=live))


def _batch(b: Mapping[str, Any], text: Any) -> PaperBatch:
    from council.publish.redact import _swing_case

    refs = [r for r in b.get("refs") or [] if _IDEA.match(str(r))]
    known = {r: r for r in [*refs, *(b.get("trades") or [])]}
    reps = []
    for rep in b.get("pm_replicates") or []:
        acts = rep.get("accepted_actions")
        dec = rep.get("decision") or {}
        budget = _num(dec.get("swing_budget_pct"), 0.0, 100.0) if isinstance(dec, Mapping) else None
        reps.append(PaperReplicate(
            replicate=int(rep.get("replicate") or 0), valid=acts is not None,
            actions=[x for x in (_action(a, text) for a in acts or []) if x is not None][:12],
            budget_pct=budget, budget_vote_valid=rep.get("budget_vote") is not None,
            budget_reason=text(dec.get("swing_budget_reason"), 180) if isinstance(dec, Mapping) else ""))
    tally = []
    for a in b.get("actions") or []:
        ref, act = str(a.get("ref") or ""), str(a.get("action") or "")
        if re.match(r"^(idea:[0-9]{1,2}|trade:[A-Za-z0-9_\-]{1,64})$", ref) and re.match(r"^[a-z_]{1,24}$", act):
            tally.append(PaperTally(ref=ref, action=act, votes_for=int(a.get("votes_for") or 0),
                                    replicates=int(a.get("replicates") or 0)))
    dropped = [0]
    return PaperBatch(refs=refs[:10], bull=_swing_case(b.get("bull"), text, dropped, known),
                      bear=_swing_case(b.get("bear"), text, dropped, known), replicates=reps[:5], tally=tally[:20])


def _pm_reasons(batches: Sequence[PaperBatch]) -> dict[str, str]:
    out: dict[str, str] = {}
    for b in batches:
        for rep in b.replicates:
            for a in rep.actions:
                if a.action == "enter" and a.reason and a.ref not in out:
                    out[a.ref] = a.reason
    return out


def paper_swing(record: Mapping[str, Any], *, cycle_id: str, licensed_texts: Sequence[str],
                own_texts: Sequence[str] | None) -> PaperSwing:
    """The paper swing part from the private swing record (`extras.swing`, with `trace` when the
    run was `--trace-all`). `own_texts`: the licensed feed texts this cycle's agents saw (None =
    unreadable: every model text is withheld)."""
    from council.publish.redact import public_swing_section
    from council.swing.council import model_family

    tr = record.get("trace") if isinstance(record.get("trace"), Mapping) else None
    texts = [*licensed_texts, *(own_texts or [])]
    text = _texter(texts, blocked=own_texts is None)
    ideas_rec = [r for r in record.get("ideas") or [] if _IDEA.match(str(r.get("ref") or ""))]
    public_ideas: dict[str, PublicSwingIdea] = {}
    flags: list[str] = []
    origins = {cycle_id: list(own_texts) if own_texts is not None else None}
    for k in range(0, len(ideas_rec), 5):                 # the live section holds <= 5 ideas
        sec = public_swing_section({**record, "ideas": ideas_rec[k:k + 5], "bull": None, "bear": None},
                                   cycle_id=cycle_id, licensed_texts=licensed_texts, origin_texts=origins)
        public_ideas.update({i.ref: i for i in sec.ideas})
        flags += [f for f in sec.flags if f not in flags]
    batches = [_batch(b, text) for b in (tr or {}).get("batches") or []][:10]
    if tr is None and (record.get("bull") or record.get("bear")):
        from council.publish.redact import _swing_case

        refs = {r: r for r in public_ideas}
        batches = [PaperBatch(refs=list(public_ideas)[:10], bull=_swing_case(record.get("bull"), text, [0], refs),
                              bear=_swing_case(record.get("bear"), text, [0], refs))]
    reasons = _pm_reasons(batches)
    trace_ideas = {i["ref"]: i for i in (tr or {}).get("ideas") or [] if isinstance(i, Mapping)}
    ideas = []
    for ref, pub in public_ideas.items():
        t = trace_ideas.get(ref)
        if t is not None:
            real, traced = _outcome(t.get("real_gate_outcome"), text), _outcome(t.get("traced_outcome"), text)
            leg, tleg = _leg(t.get("real_leg")), _leg(t.get("paper_leg"))
            batch = t.get("batch") if isinstance(t.get("batch"), int) else None
        else:
            row = next((r for r in ideas_rec if r.get("ref") == ref), {})
            accepted = row.get("stage") in ("risk", "planned") and not row.get("rule_code")
            real = PaperOutcome(stage="leg" if accepted else "rules" if row.get("rule_code") else "unknown",
                                code="paper_leg" if accepted else (row.get("rule_code") or row.get("drop_code")))
            traced, leg, tleg, batch = real, PaperLeg(ok=True) if accepted else None, None, None
        chosen = leg is not None and leg.ok
        ideas.append(PaperIdea(idea=pub, batch=batch, real_outcome=real, traced_outcome=traced,
                               same=(real.stage, real.code) == (traced.stage, traced.code), chosen=chosen,
                               leg=leg, traced_leg=tleg, why=why_line(pub, real, leg, reasons.get(ref, ""))))
    from council.publish.redact import swing_line

    bd = (tr or {}).get("budget")
    budget = None
    if isinstance(bd, Mapping) and _num(bd.get("swing_pct"), 0, 100) is not None:
        budget = PaperBudget(swing_pct=_num(bd["swing_pct"], 0, 100), core_pct=_num(bd.get("core_pct"), 0, 100) or 0.0,
                             median_pct=_num(bd.get("median_pct"), 0, 100), votes=int(bd.get("votes") or 0),
                             fallback=bool(bd.get("fallback")), open_pct=_num(bd.get("open_pct"), 0, 100) or 0.0)
    model = str(record.get("skeptic_model") or "")
    family = re.sub(r"[^a-z0-9_.-]", "", model_family(model).lower())[:32] if model else ""
    if text.withheld:
        flags.append(f"paper_text_withheld:{text.withheld}")
    return PaperSwing(
        trace_all=tr is not None, skeptic_model_family=family,
        # code-written inputs (screen, context, labels) are checked against the feed items' own lines
        inputs=_inputs((tr or {}).get("inputs") or {}, _texter([*licensed_texts, *feed_lines(own_texts or [])])),
        ideas=ideas[:40],
        scout_passed=[x for x in (swing_line(str(s)) for s in (tr or {}).get("scout_passed") or []) if x][:40],
        batches=batches, budget=budget,
        real_budget_flags=[str(f)[:120] for f in (tr or {}).get("real_budget_flags") or []][:12],
        flags=list(dict.fromkeys(flags)))


def build_paper_cycle(rec: Any, pack: Any, *, lines: Any, decision_no: int, install_key: bytes | None = None,
                      own_texts: Sequence[str] | None = None) -> PublicPaperCycle:
    """The paper cycle document: the core part through `redact.public_cycle` (the swing record is
    left out of it and published as the paper swing part instead)."""
    from council.publish import redact

    extras = dict(rec.extras or {})
    swing = extras.pop("swing", None)
    core = redact.public_cycle(rec.model_copy(update={"extras": extras}), pack, lines=lines, install_key=install_key)
    part = None
    if isinstance(swing, Mapping) and swing.get("ideas") is not None:
        part = paper_swing(swing, cycle_id=rec.cycle_id, licensed_texts=redact.licensed_texts(pack), own_texts=own_texts)
    return PublicPaperCycle(decision_no=decision_no, cycle_id=rec.cycle_id, slot=rec.slot, core=core, swing=part,
                            flags=[f for f in core.flags if f.startswith(("licensed_", "news_licence"))])


# ------------------------------------------------------------------------------------- numbering
def read_rows(root: Path) -> list[dict[str, Any]]:
    path = Path(root) / DECISIONS_PATH
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def number_for(rows: Sequence[Mapping[str, Any]], cycle_id: str) -> int:
    """The decision number of `cycle_id`: its existing number, else max + 1 (never reused)."""
    for r in rows:
        if r.get("cycle_id") == cycle_id:
            return int(r["decision_no"])
    return max((int(r["decision_no"]) for r in rows), default=0) + 1


def _line(row: PublicPaperDecisionRow) -> str:
    return json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decisions_bytes(existing: bytes | None, row: PublicPaperDecisionRow) -> bytes:
    """Append `row`, or replace the row of the same cycle IN PLACE (same line, same number).
    Every other line is kept byte for byte."""
    lines = [x for x in (existing or b"").decode("utf-8").splitlines() if x.strip()]
    out, done = [], False
    for x in lines:
        if json.loads(x).get("cycle_id") == row.cycle_id:
            if json.loads(x).get("decision_no") != row.decision_no:
                raise PaperPublishError("a published cycle keeps its decision number")
            out.append(_line(row))
            done = True
        else:
            out.append(x)
    if not done:
        if any(json.loads(x).get("decision_no") == row.decision_no for x in lines):
            raise PaperPublishError(f"decision #{row.decision_no} is already taken")
        out.append(_line(row))
    return ("\n".join(out) + "\n").encode("utf-8")


# ------------------------------------------------------------------------------------- portfolio
def _chosen(doc: PublicPaperCycle) -> list[PaperChosen]:
    out = []
    for i in (doc.swing.ideas if doc.swing else []):
        if i.chosen and i.leg is not None:
            out.append(PaperChosen(ticker=i.idea.ticker, side=i.idea.side, size_nav_pct=i.leg.size_nav_pct,
                                   stop_pct=i.leg.stop_pct, target_pct=i.leg.target_pct,
                                   time_stop_date=i.leg.time_stop_date))
    return out[:12]


def _core_moves(core: PublicCycleV1) -> int:
    r = core.risk
    if r is None:
        return 0
    return sum(1 for k in set(r.final_x) | set(r.base_x) if abs(r.final_x.get(k, 0.0) - r.base_x.get(k, 0.0)) > 1e-9)


def decision_why(doc: PublicPaperCycle) -> str:
    chosen = _chosen(doc)
    n = len(doc.swing.ideas) if doc.swing else 0
    moves = _core_moves(doc.core)
    core = f"core: {moves} line(s) re-weighted" if moves else "core: held"
    if chosen:
        legs = ", ".join(f"{c.ticker} {c.side}" + (f" {c.size_nav_pct:g}%" if c.size_nav_pct is not None else "")
                         for c in chosen)
        return f"entered {legs}; {core}"[:240]
    if n:
        return f"no swing trade ({n} idea{'s' if n != 1 else ''} reviewed); {core}"[:240]
    return f"no swing slot; {core}"[:240]


def _day(v: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date() if v else None
    except ValueError:
        return None


def paper_latest(doc: PublicPaperCycle, rows: Sequence[Mapping[str, Any]], paper_rows: Sequence[Mapping[str, Any]],
                 *, today: date, book: Mapping[str, Any] | None = None) -> PublicPaperLatest:
    """The paper portfolio after `doc`: the core's final weights, the chosen paper legs still open
    (matched to the paper ledger rows by origin cycle + ticker + side; unmatched legs count as open
    until their time stop), and the paper P&L of the closed ones."""
    core = []
    if doc.core.risk is not None:
        core = [PaperHolding(line=k, weight_pct=round(v * 100.0, 3)) for k, v in sorted(doc.core.risk.final_x.items())
                if abs(v) > 1e-9]
    ledger: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for r in paper_rows:
        from council.publish.redact import swing_line

        key = (str(r.get("origin_cycle") or ""), swing_line(str(r.get("ticker") or "")) or "", str(r.get("side") or ""))
        ledger.setdefault(key, r)
    open_, closed, ret = [], 0, 0.0
    for row in rows:
        for c in row.get("chosen") or []:
            size = c.get("size_nav_pct")
            if not isinstance(size, int | float):
                continue
            p = ledger.get((row["cycle_id"], c["ticker"], c["side"]))
            if p is not None and p.get("status") == "closed" and isinstance(p.get("ret_pct"), int | float):
                closed += 1
                ret += float(size) * float(p["ret_pct"]) / 100.0
                continue
            tsd = c.get("time_stop_date")
            expired = bool(tsd) and date.fromisoformat(tsd) < today and p is None
            if not expired:
                open_.append(PaperSwingHolding(ticker=c["ticker"], side=c["side"], size_nav_pct=float(size),
                                               stop_pct=c.get("stop_pct"), target_pct=c.get("target_pct"),
                                               decision_no=int(row["decision_no"]), opened_cycle=row["cycle_id"],
                                               time_stop_date=date.fromisoformat(tsd) if tsd else None))
    swing_pct = round(min(100.0, sum(h.size_nav_pct for h in open_)), 3)
    split = doc.core.split
    budget = doc.swing.budget.swing_pct if doc.swing and doc.swing.budget else (split.swing_budget_pct if split else None)
    since = min((d for d in (_day(r.get("slot")) for r in rows) if d is not None), default=None)
    return PublicPaperLatest(
        as_of=doc.slot, decision_no=doc.decision_no, cycle_id=doc.cycle_id, decisions=max(1, len(rows)),
        core=core[:64], swing_open=open_[:24], swing_pct=swing_pct, core_pct=round(100.0 - swing_pct, 3),
        swing_budget_pct=budget,
        performance=PaperPerformance(since=since, swing_closed_legs=closed, swing_return_pct=round(ret, 4)),
        book=paper_book_view(book))


# ------------------------------------------------------------------------------------- files
def dump(doc: PublicModel) -> bytes:
    return (json.dumps(doc.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def paper_files(doc_for: Any, root: Path, cycle_id: str, *, paper_rows: Sequence[Mapping[str, Any]] = (),
                sealed_at: datetime | None = None, code_commit: str = "",
                today: date | None = None, book: Mapping[str, Any] | None = None,
                ) -> tuple[dict[str, bytes], PublicPaperCycle]:
    """{relpath: bytes} of one paper publish. `doc_for(decision_no)` builds the document."""
    root = Path(root)
    rows = read_rows(root)
    no = number_for(rows, cycle_id)
    doc = doc_for(no)
    commitment, salt, sealed = commit_reveal.seal_bytes(doc, sealed_at=sealed_at, code_commit=code_commit)
    if not commit_reveal.verify_bytes(sealed, salt, commitment.commitment_sha256):
        raise PaperPublishError("the sealed bytes do not open their commitment")
    if commit_reveal.canonical_json(PublicPaperCycle.model_validate_json(sealed)) != sealed:
        raise PaperPublishError("the sealed bytes are not the canonical paper document")
    reveal = PaperReveal(cycle_id=cycle_id, salt=salt, commitment_sha256=commitment.commitment_sha256,
                         sealed_at=commitment.sealed_at, code_commit=code_commit)
    row = PublicPaperDecisionRow(decision_no=no, cycle_id=cycle_id, slot=doc.slot,
                                 trace_all=bool(doc.swing and doc.swing.trace_all),
                                 ideas=len(doc.swing.ideas) if doc.swing else 0, chosen=_chosen(doc),
                                 core_moves=min(64, _core_moves(doc.core)), why=decision_why(doc),
                                 commitment_sha256=commitment.commitment_sha256, path=cycle_file(cycle_id))
    existing = (root / DECISIONS_PATH).read_bytes() if (root / DECISIONS_PATH).exists() else None
    dec = decisions_bytes(existing, row)
    all_rows = [json.loads(x) for x in dec.decode().splitlines() if x.strip()]
    latest = paper_latest(doc, all_rows, paper_rows, today=today or doc.slot.date(), book=book)
    files = {cycle_file(cycle_id): sealed, reveal_file(cycle_id): dump(reveal), DECISIONS_PATH: dec,
             LATEST_PATH: dump(latest)}
    return files, doc


def scan_files(files: Mapping[str, bytes], *, licensed_texts: Sequence[str] = (),
               canaries: Sequence[str | float] = ()) -> None:
    findings = []
    for name, data in files.items():
        findings += leakscan.scan_bytes(name, data, canaries=canaries, licensed_texts=licensed_texts)
    if findings:
        raise PaperPublishError("paper record failed the leak scan: " + "; ".join(str(f) for f in findings[:10]))


_ID_LINE = re.compile(r"^\s*-\s*[A-Z]:[0-9A-Za-z_.:@#+-]+:")
_N_LINE = re.compile(r"^\s*-\s*N:[0-9a-f]{8}:")


def feed_lines(texts: Iterable[str]) -> list[str]:
    """The licensed part of captured segments for the final scan: a segment that lists evidence
    lines (`- N:...: ...`, `- P:...: ...`) keeps its `N:` lines only (a public `P:` / `S:` title next to
    a feed item is not licensed); any other text is kept whole."""
    out: list[str] = []
    for t in texts:
        lines = str(t).splitlines()
        if any(_ID_LINE.match(x) for x in lines):
            out += [x for x in lines if _N_LINE.match(x)]
        else:
            out.append(str(t))
    return out


def write(root: Path, files: Mapping[str, bytes]) -> list[Path]:
    from council.publish.journal import write_files

    return write_files(Path(root), files)


def publish_paper(rec: Any, pack: Any, *, state_dir: Path, root: Path, lines: Any, install_key: bytes | None,
                  paper_rows: Sequence[Mapping[str, Any]] = (), canaries: Sequence[str | float] = (),
                  code_commit: str = "", now: datetime | None = None) -> tuple[int, list[Path]]:
    """Build, seal, reveal, leak-scan and write one paper cycle's public record under `root`.
    Returns (decision number, files written). Raises (nothing written) on any failure."""
    from council.publish import redact
    from council.swing.record import origin_texts

    own: list[str] | None
    try:
        own = origin_texts(Path(state_dir), [rec.cycle_id]).get(rec.cycle_id)
    except Exception:  # noqa: BLE001 - unreadable: every swing model text is withheld
        own = None
    swing = (rec.extras or {}).get("swing")
    if not isinstance(swing, Mapping):
        own = own or []
    code_commit = code_commit if re.fullmatch(r"[0-9a-f]{7,40}", code_commit or "") else ""
    from council.paperbook import paper_book_public

    try:
        book = paper_book_public(Path(state_dir))
    except Exception:  # noqa: BLE001 - no readable paper book: latest.json carries none
        book = None
    files, doc = paper_files(
        lambda no: build_paper_cycle(rec, pack, lines=lines, decision_no=no, install_key=install_key, own_texts=own),
        root, rec.cycle_id, paper_rows=paper_rows, sealed_at=now or datetime.now(UTC), code_commit=code_commit,
        book=book)
    scan_files(files, licensed_texts=[*redact.licensed_texts(pack), *feed_lines(own or [])], canaries=canaries)
    return doc.decision_no, write(root, files)


__all__ = ["DECISIONS_PATH", "LATEST_PATH", "PAPER_DIR", "PaperPublishError", "PublicPaperCycle",
           "PublicPaperDecisionRow", "PublicPaperLatest", "build_paper_cycle", "decisions_bytes", "number_for",
           "paper_files", "paper_latest", "paper_swing", "publish_paper", "read_rows", "scan_files"]
