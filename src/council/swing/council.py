"""The swing council of one US swing slot (design swing-book.md rev 2, §1 steps ②-⑥, §1.5, §5; SW-3).

    Scout (1) -> accept_ideas (H3/H4/H10, paper-only setups leave here: no call) -> code gate
    (resolve + fact card, injected; chase > hard sigma dropped; best <= max_skeptic_calls) ->
    Skeptic (1 per idea, BLIND, its own model family) -> accept_verdict (§1.5 rules) ->
    bull (1) -> bear (1) -> PM (replicates, 2 of 3) -> aggregate.

- BUDGET: at most `llm.max_calls_per_slot` (9) first attempts per slot, separate from the core's
  budget. Over budget, the drop order is: Skeptic calls beyond the first 2 ideas (those ideas are
  dropped, never sent on without a verdict) -> PM replicates 3 -> 1 -> skip the debate. 0 calls
  after the Scout when nothing survives and no open trade is under review.
- DEADLINE: `run_swing_stage` bounds the whole stage with `asyncio.wait_for` (`llm.deadline_s`,
  360 s). On expiry -> flag `swing_error:timeout`, no new entries, holds unchanged; code-made exits
  (time stops, earnings), computed before any LLM stage, stay in the result.
- NEVER RAISES (from `run_swing_stage`): a failure becomes `swing_error:<stage>:<type>` and the core
  cycle continues untouched. `CanaryLeak` (H11) is the one exception: it is a code bug and raises.
- SKEPTIC MODEL: its own gateway on `llm.skeptic_model` (another family). Unavailable (no gateway,
  `verify_model` false, or a transport failure on the call) -> the Scout's gateway, flag
  `skeptic_same_model` (published). The same family on both gateways also sets the flag.
- BLIND SKEPTIC: `skeptic_input` builds its text only from the ticker, the side, the cited
  catalyst items with the code-attached form/items/titles, the one-line factual claim, the fact card,
  the market context and the open book. The thesis, why_not_priced_in, the setup and the Scout's
  levels are never passed to it.

No broker, no ledger, no network of its own: the gateways and the gate are injected.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from council.deliberation.common import call_role, pm_seeds, role_cfg
from council.deliberation.segments import Item, Licence, Segmented
from council.llm.gateway import Gateway, LLMResult
from council.llm.prompts import PromptRegistry
from council.models.cycle import RoleCall
from council.policy import Policy
from council.swing.aggregate import SwingAggregate, aggregate_actions
from council.swing.canary import PastEvent, build_canary, grade_canary
from council.swing.facts import FactCard
from council.swing.models import (
    AggregatedSwingAction,
    ScoutOutput,
    SkepticVerdict,
    SwingBearCase,
    SwingCase,
    SwingPMDecision,
)
from council.swing.policy import SwingPolicy
from council.swing.roles import (
    CanaryLeak,
    CatalystMeta,
    Drop,
    SwingIdea,
    VerdictOutcome,
    accept_actions,
    accept_case,
    accept_ideas,
    accept_verdict,
    catalyst_index,
    directional_sigma,
    guard_no_canary,
)

if TYPE_CHECKING:
    from council.deliberation.capture import InputSink

SWING_ROLES = ("scout", "skeptic", "swing_bull", "swing_bear", "swing_pm")
NUM_PREDICT = {"scout": 2400, "skeptic": 3000, "swing_bull": 1600, "swing_bear": 1800, "swing_pm": 1400}
SEEDS = {"scout": 42, "skeptic": 42, "swing_bull": 42, "swing_bear": 43}
SKEPTIC_FIRST_IDEAS = 2            # the drop order keeps the Skeptic for the first 2 ideas
MAX_SCOUT_IDEAS = 5
READING_SUMMARY_MAX = 1200          # characters of a public item's summary shown to the Scout
LIVE_FIELDS = ("move_since_news_live_pct", "move_since_news_live_sigma", "move_today_live_pct",
               "move_today_live_sigma")
# Never prompted: the feed copy of the catalyst items, and a money amount (percent-only reasoning;
# the prompt reads `adv_bucket`).
_SKIP_CARD_FIELDS = frozenset({"catalyst_items_feed", "adv_usd_20d"})


class BudgetExceeded(RuntimeError):
    pass


# ----------------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class ContextRow:
    """One line of market / macro / regime context with its evidence id (from the core pack)."""

    id: str
    text: str


@dataclass(frozen=True)
class OpenTrade:
    ref: str                                   # "trade:<id>"
    ticker: str
    side: str
    days_held: int
    to_stop_pct: float | None = None           # distance from the current rate to the stop, %
    to_target_pct: float | None = None
    triggers: tuple[str, ...] = ()             # §1.8 review triggers; empty = not under review
    facts: tuple[ContextRow, ...] = ()

    @property
    def under_review(self) -> bool:
        return bool(self.triggers)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str | None = None
    card: FactCard | None = None


Gate = Callable[[list[SwingIdea]], Awaitable[Mapping[str, GateResult]]]


@dataclass
class SwingInputs:
    slot: datetime
    reading: Sequence[Any] = ()                          # NewsItem: the slot's reading list
    screen_rows: Sequence[Mapping[str, Any]] = ()        # movers-screen rows (M: ids)
    screen_available_at: datetime | None = None
    open_trades: Sequence[OpenTrade] = ()
    recent_ideas: Sequence[str] = ()                     # code-written lines: last 5 sessions + outcome
    core_summary: str = ""                               # one code-written line on the core book
    context: Sequence[ContextRow] = ()                   # gov / macro / regime rows (SPX, NDX, sector)
    recent_rejections: Mapping[str, datetime] = field(default_factory=dict)
    code_exits: Sequence[str] = ()                       # trade refs exited by code (no LLM)
    max_entries: int | None = None                       # room S1-S4 leave
    screened: frozenset[str] | None = None               # the movers-screen universe (line ids); None = unknown


# ---------------------------------------------------------------------------------- results
@dataclass
class IdeaOutcome:
    ref: str
    ticker: str
    side: str
    setup: str
    stage: str                   # scout | gate | skeptic | pm
    code: str | None = None      # drop / park code
    flags: tuple[str, ...] = ()


@dataclass
class BudgetPlan:
    skeptic_ideas: int
    pm_replicates: int
    debate: bool
    flags: tuple[str, ...] = ()

    def calls(self, *, scout: bool = True) -> int:
        later = self.skeptic_ideas + (2 if self.debate else 0) + self.pm_replicates
        return int(scout) + later


@dataclass
class SwingCouncilResult:
    slot: str
    ideas: dict[str, SwingIdea] = field(default_factory=dict)
    outcomes: list[IdeaOutcome] = field(default_factory=list)
    paper_only: list[SwingIdea] = field(default_factory=list)
    aggregate: SwingAggregate | None = None
    code_exits: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    calls: list[RoleCall] = field(default_factory=list)
    skeptic_model: str = ""
    bull: SwingCase | None = None
    bear: SwingBearCase | None = None

    @property
    def calls_used(self) -> int:
        return len(self.calls)

    def entries(self) -> list[AggregatedSwingAction]:
        return self.aggregate.entries() if self.aggregate is not None else []

    def exits(self) -> list[str]:
        llm = [a.ref for a in self.aggregate.exits()] if self.aggregate is not None else []
        return list(dict.fromkeys([*self.code_exits, *llm]))

    def verdicts(self) -> list[VerdictOutcome]:
        return [i.verdict for i in self.ideas.values() if i.verdict is not None]


# --------------------------------------------------------------------------------- policy view
def _pct(x: float) -> str:
    return f"{x * 100:g}"


def swing_prompt_context(policy: Policy) -> dict[str, Any]:
    """Numbers the swing prompts EXPLAIN (code enforces each independently). No fee number (D17)."""
    sw = _swing(policy)
    return {
        "valid_minutes": sw.entry_guard.valid_minutes,
        "target_nav_pct": _pct(sw.size.target_nav),
        "max_open": sw.capacity.max_open,
        "max_short": sw.capacity.max_short,
        "max_new_7d": sw.capacity.max_new_7d,
        "min_net_rr": f"{sw.targets.min_net_rr:g}",
        "max_ideas": MAX_SCOUT_IDEAS,
        "chase_sigma": f"{sw.chase.max_move_since_news_sigma:g}",
        "prior_wait_sigma": f"{sw.chase.prior_wait_sigma:g}",
        "stop_min": f"{sw.stops.min_pct:g}",
        "stop_max_long": f"{sw.stops.max_long_pct:g}",
        "stop_max_short": f"{sw.stops.max_short_pct:g}",
        "target_min": "0.03",
        "target_max": f"{sw.targets.max_pct:g}",
        "time_min": sw.time_stop.min_sessions,
        "time_max": sw.time_stop.max_sessions,
        "pm_replicates": sw.llm.pm_replicates,
        "pm_entry_votes": sw.llm.pm_entry_votes,
    }


def _swing(policy: Policy) -> SwingPolicy:
    if policy.swing is None:
        raise ValueError("policy/swing.yaml is not loaded")
    return policy.swing


def plan_budget(n_ideas: int, n_review: int, *, max_calls: int, max_skeptic: int,
                pm_replicates: int) -> BudgetPlan:
    """The §5 drop order over one slot's calls after the Scout's one call."""
    ideas = min(n_ideas, max_skeptic)
    if ideas == 0 and n_review == 0:
        return BudgetPlan(0, 0, False)
    flags: list[str] = []
    plan = BudgetPlan(ideas, pm_replicates, True)
    room = max_calls - 1
    if plan.calls(scout=False) > room and plan.skeptic_ideas > SKEPTIC_FIRST_IDEAS:
        plan.skeptic_ideas = SKEPTIC_FIRST_IDEAS
        flags.append("budget_skeptic_2")
    if plan.calls(scout=False) > room and plan.pm_replicates > 1:
        plan.pm_replicates = 1
        flags.append("budget_pm_1")
    if plan.calls(scout=False) > room:
        plan.debate = False
        flags.append("budget_no_debate")
    while plan.calls(scout=False) > room and plan.skeptic_ideas > 0:
        plan.skeptic_ideas -= 1                     # an idea without a Skeptic call never goes on
        flags.append("budget_skeptic_less")
    if plan.calls(scout=False) > room:
        plan.pm_replicates = 0
        flags.append("budget_no_pm")
    plan.flags = tuple(flags)
    return plan


def model_family(model: str) -> str:
    return re.split(r"[-:._\d]", model.strip().lower(), maxsplit=1)[0]


async def choose_skeptic(gw: Gateway, skeptic_gw: Gateway | None, policy: Policy) -> tuple[Gateway, list[str]]:
    """The Skeptic's gateway and flags (Q-S9). Never raises."""
    if skeptic_gw is None or _swing(policy).llm.skeptic_model_family == "same":
        return gw, ["skeptic_same_model"]
    verify = getattr(skeptic_gw, "verify_model", None)
    try:
        ok = bool(await verify()) if verify is not None else True
    except Exception:
        ok = False
    if not ok:
        return gw, ["skeptic_same_model", "skeptic_model_unavailable"]
    if model_family(skeptic_gw.model) == model_family(gw.model):
        return skeptic_gw, ["skeptic_same_model"]
    return skeptic_gw, []


# ------------------------------------------------------------------------------------ renderers
def _num(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _section(key: str, text: str, *, licence: Licence, sources: Iterable[str] = ("swing",)) -> Segmented:
    item = Item(kind="text", ref=key, field="swing", sources=tuple(sources), licence=licence, text=text)
    return Segmented(key=key, kind="raw", runs=(("i", 0),), items=(item,))


def card_rows(card: FactCard | None) -> list[tuple[str, str]]:
    """(evidence id, "field = value") for every present card field (live layer included: agents
    may read it; it is never published)."""
    if card is None:
        return []
    rows = []
    for key in sorted(card.fields):
        value = card.fields[key]
        if value is None or key in _SKIP_CARD_FIELDS:
            continue
        rows.append((f"X:{card.line_id}:{key}", f"{key} = {_num(value)}"))
    return rows


def catalyst_rows(ids: Sequence[str], catalysts: Mapping[str, CatalystMeta], card: FactCard | None,
                  slot: datetime) -> list[tuple[str, str]]:
    """The cited items with the CODE-attached metadata (form, item codes, official titles) and, for a
    public-domain item, the summary the Scout read."""
    attached = {c["id"]: c for c in (card.catalyst_items if card is not None else [])}
    out = []
    for cid in ids:
        meta = catalysts.get(cid)
        if meta is None:
            continue
        hours = max(0, int((slot - meta.available_at).total_seconds() // 3600))
        parts = [f"available {hours}h before the slot"]
        extra = attached.get(cid)
        if extra is not None:
            parts.append(f"form {extra['form']}")
            parts.append("items " + "; ".join(f"{c} {t}" for c, t in zip(extra["items"], extra["titles"], strict=False)))
        elif meta.form:
            parts.append(f"form {meta.form}, items {', '.join(meta.items)}")
        tagged = ", ".join(sorted(meta.symbols)) or "market-wide"
        # the same cleaned summary the Scout read (fact parity: an SEC item's exhibit headline and
        # excerpt), so a reviewer can check the claim against the text and not a bare form header
        body = f" | {meta.summary[:READING_SUMMARY_MAX]}" if meta.summary else ""
        out.append((cid, f"{meta.title} [{tagged}] ({'; '.join(parts)}){body}"))
    return out


def _licence_for(ids: Iterable[str], card_fields: Iterable[str] = ()) -> Licence:
    if any(i.startswith("N:") for i in ids) or any(f in LIVE_FIELDS for f in card_fields):
        return "broker_licensed"
    return "restricted"


def _block(title: str, rows: Sequence[tuple[str, str]]) -> str:
    body = "\n".join(f"- {rid}: {text}" for rid, text in rows) if rows else "- none"
    return f"{title}\n{body}\n"


def _book_rows(trades: Sequence[OpenTrade]) -> list[tuple[str, str]]:
    rows = []
    for t in trades:
        dist = []
        if t.to_stop_pct is not None:
            dist.append(f"{_num(t.to_stop_pct)}% to stop")
        if t.to_target_pct is not None:
            dist.append(f"{_num(t.to_target_pct)}% to target")
        trig = f"; review: {', '.join(t.triggers)}" if t.triggers else ""
        rows.append((t.ref, f"{t.ticker} {t.side}, {t.days_held} sessions held"
                     + (f", {', '.join(dist)}" if dist else "") + trig))
    return rows


def skeptic_input(idea: SwingIdea, *, catalysts: Mapping[str, CatalystMeta], inputs: SwingInputs) -> tuple[list[Segmented], set[str]]:
    """The Skeptic's user message and its admissible ids. BLIND by construction: only these
    attributes of the idea are read: ref, ticker, side, catalyst_ids, catalyst_claim, card."""
    ref, ticker, side = idea.ref, idea.idea.ticker, idea.idea.side
    cat_ids, claim, card = list(idea.idea.catalyst_ids), idea.idea.catalyst_claim, idea.card
    cats = catalyst_rows(cat_ids, catalysts, card, inputs.slot)
    facts = card_rows(card)
    ctx = [(c.id, c.text) for c in inputs.context]
    text = (
        f"IDEA TO REVIEW: {ref}\nticker: {ticker}\nside: {side}\n\n"
        + _block("CATALYST ITEMS (metadata attached by code)", cats)
        + f"\nCLAIM (one factual line from the proposer; check it against the items):\n{claim}\n\n"
        + _block(f"FACT CARD (X:{idea.line_id}:<field>; percentages)", facts)
        + "\n" + _block("MARKET AND SECTOR CONTEXT", ctx)
        + "\n" + _block("OPEN SWING BOOK", _book_rows(inputs.open_trades))
    )
    admissible = {c[0] for c in cats} | {f[0] for f in facts} | {c[0] for c in ctx}
    licence = _licence_for(admissible, card.fields if card is not None else ())
    return [_section(f"swing.skeptic.{ref}", text, licence=licence)], admissible


def scout_input(inputs: SwingInputs, catalysts: Mapping[str, CatalystMeta]) -> tuple[list[Segmented], set[str]]:
    reading = []
    for item in sorted(inputs.reading, key=lambda i: (i.available_at, i.id), reverse=True):
        if item.id not in catalysts:
            continue
        meta = catalysts[item.id]
        hours = max(0, int((inputs.slot - meta.available_at).total_seconds() // 3600))
        tagged = ", ".join(sorted(meta.symbols)) or "market-wide"
        # a name outside the screen universe can never be on the movers screen: say so, so its
        # absence there is not read as "not traded yet"
        unscreened = sorted(meta.symbols - inputs.screened) if inputs.screened is not None else []
        tagged += f"] [not screened: {', '.join(unscreened)}" if unscreened else ""
        form = f" {meta.form}" if meta.form else ""
        form += f" items {', '.join(meta.items)}" if meta.form and meta.items else ""
        # public-domain items carry their cleaned summary (an SEC item: company + filing text);
        # a broker-feed item stays title only here, as before
        summary = str(getattr(item, "summary", "") or "") if item.id.startswith("P:") else ""
        body = f"{item.title} | {summary[:READING_SUMMARY_MAX]}" if summary else item.title
        reading.append((item.id, f"[{tagged}]{form} {hours}h ago: {body}"))
    screen = [(str(r["id"]), f"move {_num(r.get('move_sigma'))} sigma ({_num(r.get('move_pct'))}%), "
               f"volume x{_num(r.get('vol_ratio'))}, sector {r.get('sector') or 'n/a'}")
              for r in inputs.screen_rows if str(r["id"]) in catalysts]
    ctx = [(c.id, c.text) for c in inputs.context]
    text = (
        _block("READING LIST (newest first)", reading)
        + "\n" + _block("MOVERS SCREEN (completed bars; facts, not picks)", screen)
        + "\n" + _block("MARKET CONTEXT", ctx)
        + "\n" + _block("OPEN SWING TRADES", _book_rows(inputs.open_trades))
        + "\nIDEAS OF THE LAST 5 SESSIONS\n" + ("\n".join(f"- {x}" for x in inputs.recent_ideas) or "- none")
        + f"\n\nCORE BOOK\n{inputs.core_summary or 'n/a'}\n"
    )
    admissible = {r[0] for r in reading} | {s[0] for s in screen} | {c[0] for c in ctx}
    return [_section("swing.scout", text, licence=_licence_for(admissible))], admissible


def full_input(ideas: Sequence[SwingIdea], trades: Sequence[OpenTrade], *, catalysts: Mapping[str, CatalystMeta],
               inputs: SwingInputs, bull: SwingCase | None = None, bear: SwingBearCase | None = None,
               ) -> tuple[list[Segmented], set[str]]:
    """Debate / PM input: the full ideas (thesis, setup, levels), the Skeptic's verdicts, the cards,
    the trades under review and, for later roles, the bull's and bear's cases."""
    guard_no_canary(ideas)
    parts: list[str] = []
    admissible: set[str] = set()
    card_fields: list[str] = []
    for i in ideas:
        x = i.idea
        cats = catalyst_rows(list(x.catalyst_ids), catalysts, i.card, inputs.slot)
        facts = card_rows(i.card)
        admissible |= {c[0] for c in cats} | {f[0] for f in facts}
        card_fields += list(i.card.fields) if i.card is not None else []
        v = i.verdict.verdict if i.verdict is not None else None
        sk = "none"
        if v is not None:
            reasons = "\n".join(f"  - {r.text} [{', '.join(r.evidence_ids)}]" for r in v.reasons)
            sk = (f"{i.verdict.status} (priced_in {v.priced_in}, news {v.news_status}, regime {v.regime}, "
                  f"crowding {v.crowding}; flags {', '.join(i.verdict.flags) or 'none'})\n{reasons}\n"
                  f"  would change its mind: {v.what_would_change_my_mind}"
                  + (f"\n  second order: {v.second_order}" if v.second_order else ""))
        parts.append(
            f"IDEA {i.ref}: {x.ticker} {x.side}, setup {x.setup}, entry {x.entry}\n"
            f"stop {_num(x.stop_pct)}, target {_num(x.target_pct)}, time stop {x.time_stop_days} sessions\n"
            f"claim: {x.catalyst_claim}\nthesis: {x.thesis}\nwhy not priced in: {x.why_not_priced_in}\n"
            f"invalidation: {x.invalidation}\n"
            + _block("catalysts", cats) + _block("fact card", facts) + f"SKEPTIC: {sk}\n")
    for t in trades:
        rows = [(f.id, f.text) for f in t.facts]
        admissible |= {r[0] for r in rows}
        parts.append(_block(f"OPEN TRADE UNDER REVIEW {t.ref}", [(t.ref, _book_rows([t])[0][1]), *rows]))
    ctx = [(c.id, c.text) for c in inputs.context]
    admissible |= {c[0] for c in ctx}
    parts.append(_block("MARKET AND SECTOR CONTEXT", ctx))
    parts.append(_block("OPEN SWING BOOK", _book_rows(inputs.open_trades)))
    if bull is not None:
        parts.append("BULL CASE\n" + bull.model_dump_json() + "\n")
    if bear is not None:
        parts.append("BEAR CASE\n" + bear.model_dump_json() + "\n")
    text = "\n".join(parts)
    return [_section("swing.full", text, licence=_licence_for(admissible, card_fields))], admissible


# ---------------------------------------------------------------------------------------- run
class _Runner:
    def __init__(self, gw: Gateway, reg: PromptRegistry, policy: Policy, result: SwingCouncilResult,
                 *, sink: InputSink | None, max_calls: int, stage: list[str]) -> None:
        self.gw, self.reg, self.policy, self.result = gw, reg, policy, result
        self.sink, self.max_calls, self.stage = sink, max_calls, stage
        self.ctx = swing_prompt_context(policy)

    async def call(self, role: str, schema: type[BaseModel], sections: list[Segmented], *,
                   gw: Gateway | None = None, seed: int | None = None, replicate: int = 0) -> LLMResult:
        if self.result.calls_used >= self.max_calls:
            raise BudgetExceeded(f"{role}: slot budget of {self.max_calls} calls spent")
        placeholder = len(self.result.calls)
        self.result.calls.append(None)  # type: ignore[arg-type]   # reserve before awaiting
        npred = int(role_cfg(self.policy, role).get("max_num_predict", NUM_PREDICT[role]))
        res = await call_role(gw or self.gw, self.reg, role=role, ctx=self.ctx, schema=schema,
                              seed=SEEDS.get(role, 42) if seed is None else seed, num_predict=npred,
                              replicate=replicate, sections=sections, sink=self.sink)
        self.result.calls[placeholder] = res.call
        return res


def _outcome(result: SwingCouncilResult, idea: SwingIdea, stage: str, code: str | None,
             flags: Sequence[str] = ()) -> None:
    x = idea.idea
    result.outcomes.append(IdeaOutcome(idea.ref, x.ticker, x.side, x.setup, stage, code, tuple(flags)))


async def run_swing_council(
    gw: Gateway,
    reg: PromptRegistry,
    policy: Policy,
    inputs: SwingInputs,
    *,
    gate: Gate,
    skeptic_gw: Gateway | None = None,
    sink: InputSink | None = None,
    result: SwingCouncilResult | None = None,
    stage: list[str] | None = None,
) -> SwingCouncilResult:
    """One swing slot's council. May raise; `run_swing_stage` is the never-raising wrapper."""
    sw = _swing(policy)
    result = result if result is not None else SwingCouncilResult(slot=inputs.slot.isoformat())
    stage = stage if stage is not None else ["setup"]
    result.code_exits = list(dict.fromkeys(inputs.code_exits))
    run = _Runner(gw, reg, policy, result, sink=sink, max_calls=sw.llm.max_calls_per_slot, stage=stage)
    catalysts = catalyst_index(inputs.reading, inputs.screen_rows, slot=inputs.slot,
                               screen_available_at=inputs.screen_available_at)

    # ② Scout
    stage[0] = "scout"
    sections, _ = scout_input(inputs, catalysts)
    res = await run.call("scout", ScoutOutput, sections)
    scout = res.parsed if isinstance(res.parsed, ScoutOutput) else None
    if scout is None:
        result.flags.append(f"scout_failed:{res.call.status}")
    check = accept_ideas(scout, catalysts=catalysts, setups_live=sw.setups_live,
                         setups_paper_only=sw.setups_paper_only, recent_rejections=inputs.recent_rejections)
    for d in check.drops:
        result.outcomes.append(IdeaOutcome(d.ref, d.ticker, "", "", "scout", d.code))
    for i in check.paper_only:
        result.ideas[i.ref] = i
        result.paper_only.append(i)
        _outcome(result, i, "scout", "setup_paper_only")

    # ③ code gate: resolve + fact card (injected), chase, best <= max_skeptic_calls
    stage[0] = "gate"
    gated = await gate(list(check.accepted)) if check.accepted else {}
    survivors: list[SwingIdea] = []
    for i in check.accepted:
        result.ideas[i.ref] = i
        g = gated.get(i.ref)
        if g is None or not g.ok or g.card is None or not g.card.ok:
            reason = (g.reason if g is not None and g.reason else None) or (
                g.card.reason if g is not None and g.card is not None else None) or "no_facts"
            _outcome(result, i, "gate", reason)
            continue
        i.card = g.card
        sig = directional_sigma(i.card, i.idea.side)
        if sig is not None and sig > sw.chase.max_move_since_news_sigma:
            _outcome(result, i, "gate", "chased")
            continue
        survivors.append(i)
    for i in survivors[sw.llm.max_skeptic_calls:]:
        _outcome(result, i, "gate", "not_best_3")
    survivors = survivors[:sw.llm.max_skeptic_calls]

    review = [t for t in inputs.open_trades if t.under_review and t.ref not in result.code_exits]
    plan = plan_budget(len(survivors), len(review), max_calls=sw.llm.max_calls_per_slot,
                       max_skeptic=sw.llm.max_skeptic_calls, pm_replicates=sw.llm.pm_replicates)
    result.flags.extend(plan.flags)
    for i in survivors[plan.skeptic_ideas:]:
        _outcome(result, i, "gate", "budget_no_skeptic")
    survivors = survivors[:plan.skeptic_ideas]

    # ④ Skeptic: one BLIND call per idea, its own model family
    stage[0] = "skeptic"
    passed: list[SwingIdea] = []
    if survivors:
        sk_gw, sk_flags = await choose_skeptic(gw, skeptic_gw, policy)
        result.flags.extend(sk_flags)
        result.skeptic_model = sk_gw.model
        # A fallback retry may only use calls the plan left unused: it must never eat a call the
        # debate or a PM replicate was planned on (that would raise BudgetExceeded mid-PM and abort
        # the slot, open-trade exits included).
        spare = [max(0, run.max_calls - plan.calls())]

        async def one(idea: SwingIdea) -> tuple[SwingIdea, VerdictOutcome]:
            secs, admissible = skeptic_input(idea, catalysts=catalysts, inputs=inputs)
            r = await run.call("skeptic", SkepticVerdict, secs, gw=sk_gw)
            if r.parsed is None and r.call.status in ("transport", "timeout") and sk_gw is not gw:
                if "skeptic_same_model" not in result.flags:
                    result.flags.append("skeptic_same_model")
                if spare[0] > 0:
                    spare[0] -= 1
                    r = await run.call("skeptic", SkepticVerdict, secs, gw=gw, replicate=1)
            v = r.parsed if isinstance(r.parsed, SkepticVerdict) else None
            return idea, accept_verdict(v, idea=idea, admissible=admissible,
                                        prior_wait_sigma=sw.chase.prior_wait_sigma)

        for idea, out in await asyncio.gather(*(one(i) for i in survivors)):
            idea.verdict = out
            if out.status == "pass":
                passed.append(idea)
            else:
                _outcome(result, idea, "skeptic", out.code, out.flags)
        passed.sort(key=lambda i: int(i.ref.split(":")[1]))

    if not passed and not review:
        return result                                    # 0 more calls

    # ⑤ debate
    guard_no_canary(passed)
    idea_refs = [i.ref for i in passed]
    trade_refs = [t.ref for t in review]
    refs = set(idea_refs) | set(trade_refs)
    if plan.debate:
        stage[0] = "debate"
        secs, admissible = full_input(passed, review, catalysts=catalysts, inputs=inputs)
        rb = await run.call("swing_bull", SwingCase, secs)
        result.bull, dropped = accept_case(rb.parsed if isinstance(rb.parsed, SwingCase) else None,
                                           refs=refs, admissible=admissible)
        secs, _ = full_input(passed, review, catalysts=catalysts, inputs=inputs, bull=result.bull)
        rr = await run.call("swing_bear", SwingBearCase, secs)
        bear, dropped2 = accept_case(rr.parsed if isinstance(rr.parsed, SwingBearCase) else None,
                                     refs=refs, admissible=admissible)
        result.bear = bear if isinstance(bear, SwingBearCase) else None
        if dropped or dropped2:
            result.flags.append(f"debate_claims_dropped:{dropped + dropped2}")

    # ⑥ PM replicates
    stage[0] = "pm"
    if plan.pm_replicates < 1:
        for i in passed:
            _outcome(result, i, "pm", "budget_no_pm")
        return result
    secs, admissible = full_input(passed, review, catalysts=catalysts, inputs=inputs,
                                  bull=result.bull, bear=result.bear)
    seeds = pm_seeds(policy)[:plan.pm_replicates]

    async def rep(k: int, seed: int) -> list[Any] | None:
        r = await run.call("swing_pm", SwingPMDecision, secs, seed=seed, replicate=k)
        dec = r.parsed if isinstance(r.parsed, SwingPMDecision) else None
        return accept_actions(dec, idea_refs=set(idea_refs), trade_refs=set(trade_refs), admissible=admissible)

    reps = await asyncio.gather(*(rep(k, s) for k, s in enumerate(seeds)))
    result.aggregate = aggregate_actions(reps, idea_refs=idea_refs, trade_refs=trade_refs,
                                         max_entries=inputs.max_entries)
    result.flags.extend(result.aggregate.flags)
    entered = {a.ref for a in result.aggregate.entries()}
    for i in passed:
        _outcome(result, i, "pm", None if i.ref in entered else "pm_pass")
    return result


async def run_swing_stage(
    gw: Gateway,
    reg: PromptRegistry,
    policy: Policy,
    inputs: SwingInputs,
    *,
    gate: Gate,
    skeptic_gw: Gateway | None = None,
    sink: InputSink | None = None,
    deadline_s: float | None = None,
) -> SwingCouncilResult:
    """The swing stage as the cycle calls it: bounded by the wall-clock deadline, never raises
    (except `CanaryLeak`, a code bug). On a timeout or an error no entry survives; code exits do."""
    result = SwingCouncilResult(slot=inputs.slot.isoformat(), code_exits=list(dict.fromkeys(inputs.code_exits)))
    stage = ["setup"]
    timeout = float(deadline_s if deadline_s is not None else _swing(policy).llm.deadline_s)
    try:
        await asyncio.wait_for(run_swing_council(gw, reg, policy, inputs, gate=gate, skeptic_gw=skeptic_gw,
                                                 sink=sink, result=result, stage=stage), timeout)
    except TimeoutError:
        _abort(result, "swing_error:timeout")
    except CanaryLeak:
        raise
    except Exception as exc:
        _abort(result, f"swing_error:{stage[0]}:{type(exc).__name__}")
    result.calls = [c for c in result.calls if c is not None]
    return result


def _abort(result: SwingCouncilResult, flag: str) -> None:
    result.flags.append(flag)
    result.aggregate = None                             # no new entries, no LLM exits: holds unchanged


def drops_of(result: SwingCouncilResult) -> list[Drop]:
    return [Drop(o.ref, o.ticker, o.code) for o in result.outcomes if o.code]


# ------------------------------------------------------------------------------------- canary
@dataclass
class CanaryResult:
    grade: str                                  # caught | missed
    outcome: VerdictOutcome
    calls: list[RoleCall] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


async def run_canary(
    gw: Gateway,
    reg: PromptRegistry,
    policy: Policy,
    event: PastEvent,
    *,
    slot: datetime,
    context: Sequence[ContextRow] = (),
    open_trades: Sequence[OpenTrade] = (),
    skeptic_gw: Gateway | None = None,
    sink: InputSink | None = None,
) -> CanaryResult:
    """The weekly canary: ONE Skeptic call on a planted past event, rendered by the same blind
    `skeptic_input`. Nothing else runs: no debate, no PM, no planner, no public idea list (H11)."""
    sw = _swing(policy)
    idea = build_canary(event)
    result = SwingCouncilResult(slot=slot.isoformat())
    run = _Runner(gw, reg, policy, result, sink=sink, max_calls=1, stage=["canary"])
    sk_gw, flags = await choose_skeptic(gw, skeptic_gw, policy)
    inputs = SwingInputs(slot=slot, context=context, open_trades=open_trades)
    secs, admissible = skeptic_input(idea, catalysts={c.id: c for c in event.catalysts}, inputs=inputs)
    r = await run.call("skeptic", SkepticVerdict, secs, gw=sk_gw)
    v = r.parsed if isinstance(r.parsed, SkepticVerdict) else None
    outcome = accept_verdict(v, idea=idea, admissible=admissible, prior_wait_sigma=sw.chase.prior_wait_sigma)
    return CanaryResult(grade=grade_canary(outcome), outcome=outcome, calls=list(result.calls), flags=flags)


# ---------------------------------------------------------------------------- core desk line
def core_desk_line(open_trades: Sequence[OpenTrade], *, gross_nav_pct: float | None = None,
                   last: SwingCouncilResult | None = None) -> str:
    """ONE code-written line the core desk reads about the swing book (the core desk calls it; it
    is not wired into cycle.py here). Percentages and counts only."""
    n = len(open_trades)
    shorts = sum(1 for t in open_trades if t.side == "short")
    parts = [f"SWING BOOK (separate, bounded by its own rules): {n} open"
             + (f" ({shorts} short)" if n else "")]
    if gross_nav_pct is not None:
        parts.append(f"{gross_nav_pct:g}% of the book gross")
    if last is not None:
        if any(f.startswith("swing_error") for f in last.flags):
            parts.append("swing stage failed this slot")
        else:
            parts.append(f"{len(last.entries())} new entr{'y' if len(last.entries()) == 1 else 'ies'} "
                         f"and {len(last.exits())} exit{'' if len(last.exits()) == 1 else 's'} proposed")
    return "; ".join(parts) + ". Do not size the core lines for it."
