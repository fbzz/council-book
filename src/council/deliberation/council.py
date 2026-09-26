"""One council run: officers -> analysts -> debate -> PM x3 -> audit -> enforce -> medoid.

Rules:
  - Code officers (vol, event) run first; LLM analysts see their cards. The orchestrator normally
    builds them once (with `now = slot`) for its bands and passes them as `code_cards`; the
    council rebuilds them only when `code_cards` is None. Card IDs are per cycle: there is no
    vol-card hysteresis in v1 and no card is carried forward from an earlier cycle (news_material
    corroboration uses this cycle's vol cards only).
  - Bands: `bands` are the bands for the code cards. They drive the specialists' desk and the
    single-agent control (whose information set is the code cards). After the specialists,
    `bands_fn(all cards)` (code + news + macro) recomputes the bands, so a qualifying news card
    can unlock a cut; those FINAL bands drive the council's desk pack, debate, PM, audit and
    enforce, and are returned as `CouncilResult.bands`. Without `bands_fn` (or if it raises,
    flagged `bands_fn_error`) the given bands are final.
  - Call budget: the planned role calls must fit `council.max_calls_per_cycle`; optional stages are
    dropped in `council.drop_order_when_over_budget` order (single-agent control, macro, news).
    The debate and the PM always run. Dropped stages are recorded as `skipped` calls.
  - Macro runs only when asked (`run_macro`, set by the caller on the first cycle of the UTC day).
  - Outage guard: when EVERY call of a stage fails with transport/timeout, wait
    `council.outage_guard.stage_retry_wait_s` and rerun the stage, up to `stage_retries` times.
    A stage still down after that makes the whole council `council_unavailable` (reference levels)
    and the remaining stages are skipped; a single-agent outage only flags the control.
    A retry follows a stage that got no model response at all, so it never adds model responses
    beyond the planned budget.
  - Each replicate is audited, then clipped by the caller's `enforce(levels, bands)`, then the
    medoid with per-line agreement decides (`aggregate.py`).
  - Deterministic: for a deterministic gateway, identical inputs give byte-identical results.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from pydantic import Field

from council.deliberation import pm as pm_mod
from council.deliberation import roles as roles_mod
from council.deliberation.aggregate import AggregateResult, aggregate
from council.deliberation.audit import audit
from council.deliberation.common import (
    deadbands,
    num_predict,
    pm_seeds,
    prompt_context,
    reference_levels,
    role_enabled,
    seed_for,
)
from council.deliberation.debate import DebateRun, run_debate, transcript
from council.deliberation.desk import desk_pack
from council.deliberation.officers import event_cards, vol_cards
from council.deliberation.pm import PMRun, run_pm
from council.deliberation.roles import MacroRun, NewsRun, run_news
from council.llm.gateway import Gateway
from council.llm.prompts import PromptRegistry
from council.models.cards import EvidenceCard, MacroAnalystOutput
from council.models.common import Strict
from council.models.cycle import Debate, PMReplicate, RoleCall
from council.models.facts import FactPack
from council.models.reference import ReferenceBook
from council.models.risk import Band, DecisionBasis
from council.policy import LineSpec, Policy

Enforce = Callable[[dict[str, float], dict[str, Band]], tuple[dict[str, float], list[str]]]
BandsFn = Callable[[list[EvidenceCard]], dict[str, Band]]
Sleep = Callable[[float], Awaitable[None]]
OUTAGE_STATUSES = frozenset({"transport", "timeout"})
DEFAULT_DROP_ORDER = ("single_agent_control", "macro", "news")


class CouncilResult(Strict):
    cards: list[EvidenceCard]
    macro: MacroAnalystOutput | None = None
    debate: Debate = Field(default_factory=Debate)
    pm: list[PMReplicate] = Field(default_factory=list)
    single_agent: list[PMReplicate] = Field(default_factory=list)
    aggregate: AggregateResult
    single_agent_aggregate: AggregateResult | None = None
    calls: list[RoleCall] = Field(default_factory=list)
    basis: DecisionBasis
    dropped: list[str] = Field(default_factory=list)       # card drops, debate normalisation notes
    flags: list[str] = Field(default_factory=list)         # budget drops, outages
    enforce_notes: dict[str, list[str]] = Field(default_factory=dict)
    bands: dict[str, Band] = Field(default_factory=dict)   # final bands the council actually used
    prompt_manifest_sha: str = ""
    desk_sha: str = ""
    raw: dict[str, str] = Field(default_factory=dict)      # PRIVATE transcripts (never published)


class _SpecialistRun(NamedTuple):
    news: NewsRun | None
    macro: MacroRun | None
    calls: list[RoleCall]


class _Stage(NamedTuple):
    result: Any
    calls: list[RoleCall]
    outage: bool


def plan_stages(
    policy: Policy, *, run_macro: bool, run_single_agent: bool
) -> tuple[set[str], list[str]]:
    """Which optional stages run under the call budget. Returns (kept optional stages, flags)."""
    n_pm = len(pm_seeds(policy))
    core = 3 + n_pm  # bull open + bear + bull rebuttal + PM replicates: always run
    optional: dict[str, int] = {}
    if role_enabled(policy, "news"):
        optional["news"] = 1
    if run_macro and role_enabled(policy, "macro"):
        optional["macro"] = 1
    if run_single_agent and role_enabled(policy, "single_agent_control"):
        optional["single_agent_control"] = n_pm
    budget = int(policy.council.get("max_calls_per_cycle", 16))
    total = core + sum(optional.values())
    flags: list[str] = []
    for name in policy.council.get("drop_order_when_over_budget", DEFAULT_DROP_ORDER):
        if total <= budget:
            break
        if name in optional:
            total -= optional.pop(name)
            flags.append(f"budget_dropped:{name}")
    if total > budget:
        flags.append(f"budget_exceeded_by_core:{total}>{budget}")
    return set(optional), flags


def _skipped(reg: PromptRegistry, role: str, reason: str, replicate: int = 0) -> RoleCall:
    return RoleCall(
        role=role, replicate=replicate, prompt_id=reg.prompt_id(role), prompt_sha=reg.sha256(role),
        input_hash="", status="skipped", error=reason,
    )


async def run_council(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    pack: FactPack,
    ref: ReferenceBook,
    bands: Mapping[str, Band],
    current_levels: Mapping[str, float],
    cost_hints: Mapping[str, Mapping[str, Any]],
    lines: Sequence[LineSpec],
    policy: Policy,
    enforce: Enforce,
    now: datetime,
    run_single_agent: bool = True,
    run_macro: bool = False,
    sleep: Sleep = asyncio.sleep,
    code_cards: list[EvidenceCard] | None = None,
    bands_fn: BandsFn | None = None,
    call_log: list[RoleCall] | None = None,
) -> CouncilResult:
    """Run the council for one cycle. Never raises on LLM failures (they become statuses/flags).

    `code_cards`: this cycle's vol/event officer cards (built once by the orchestrator with
    `now = slot`); None rebuilds them here from `pack` and `now`.
    `bands_fn`: called once after the specialists with ALL cards; its bands are the ones the
    desk pack, debate, PM, audit and enforce use (see the module rules).
    `call_log`: a list the calls are appended to as each stage finishes (the same list is
    returned in `calls`), so a caller that cancels the council still has the calls that ran."""
    ctx = prompt_context(policy)
    ref_levels = reference_levels(ref, lines)
    current = {ln.symbol: float(current_levels.get(ln.symbol, 0.0)) for ln in lines}
    seeds = pm_seeds(policy)
    guard = policy.council.get("outage_guard", {})
    wait_s = float(guard.get("stage_retry_wait_s", 120))
    retries = int(guard.get("stage_retries", 3))

    calls: list[RoleCall] = call_log if call_log is not None else []
    flags: list[str] = []
    dropped: list[str] = []
    raw: dict[str, str] = {}
    kept, budget_flags = plan_stages(policy, run_macro=run_macro, run_single_agent=run_single_agent)
    flags += budget_flags
    for name, role in (("news", "news"), ("macro", "macro")):
        if f"budget_dropped:{name}" in budget_flags:
            calls.append(_skipped(reg, role, "call_budget"))
    if "budget_dropped:single_agent_control" in budget_flags:
        calls += [_skipped(reg, "single_agent", "call_budget", i) for i in range(len(seeds))]

    async def guarded(stage: str, fn: Callable[[], Awaitable[Any]]) -> _Stage:
        stage_calls: list[RoleCall] = []
        result: Any = None
        for attempt in range(retries + 1):
            result = await fn()
            stage_calls += result.calls
            down = bool(result.calls) and all(c.status in OUTAGE_STATUSES for c in result.calls)
            if not down:
                return _Stage(result, stage_calls, False)
            if attempt < retries:
                flags.append(f"outage:{stage}:retry{attempt + 1}")
                await sleep(wait_s)
        flags.append(f"stage_unavailable:{stage}")
        return _Stage(result, stage_calls, True)

    # 1. code officers (this cycle only: no card is carried forward, IDs are per cycle)
    if code_cards is None:
        code_cards = vol_cards(pack, policy) + event_cards(pack, now, policy)
    code_cards = list(code_cards)
    corroborators = [(c, now) for c in code_cards if c.card_type == "vol_shock"]
    code_bands: dict[str, Band] = dict(bands)

    def desk(cards: Sequence[EvidenceCard], use_bands: Mapping[str, Band]) -> str:
        return desk_pack(
            pack=pack, ref=ref, bands=use_bands, current_levels=current, cost_hints=cost_hints,
            cards=cards, lines=lines,
        )

    code_desk = desk(code_cards, code_bands)
    outage = False

    # 2. specialists
    news_cards: list[EvidenceCard] = []
    macro_out: MacroAnalystOutput | None = None
    macro_cards: list[EvidenceCard] = []
    if kept & {"news", "macro"}:

        async def specialists() -> _SpecialistRun:
            jobs: list[Awaitable[Any]] = []
            if "news" in kept:
                jobs.append(run_news(
                    gw=gw, reg=reg, pack=pack, desk_text=code_desk, ctx=ctx, lines=lines,
                    policy=policy, corroborators=corroborators, now=now,
                    seed=seed_for(policy, "news", 42), num_predict=num_predict(policy, "news"),
                ))
            if "macro" in kept:
                jobs.append(roles_mod.run_macro(
                    gw=gw, reg=reg, pack=pack, desk_text=code_desk, ctx=ctx, lines=lines,
                    policy=policy, now=now,
                    seed=seed_for(policy, "macro", 42), num_predict=num_predict(policy, "macro"),
                ))
            results = await asyncio.gather(*jobs)
            news = next((r for r in results if isinstance(r, NewsRun)), None)
            macro = next((r for r in results if isinstance(r, MacroRun)), None)
            return _SpecialistRun(news, macro, [c for r in results for c in r.calls])

        stage = await guarded("specialists", specialists)
        calls += stage.calls
        outage = stage.outage
        spec: _SpecialistRun = stage.result
        if spec.news is not None:
            news_cards, raw["news"] = spec.news.cards, spec.news.raw
            dropped += spec.news.dropped
        if spec.macro is not None:
            macro_out, macro_cards, raw["macro"] = spec.macro.output, spec.macro.cards, spec.macro.raw
            dropped += spec.macro.dropped

    all_cards = code_cards + news_cards + macro_cards

    # 2b. bands for ALL cards (a qualifying news card can unlock a cut)
    final_bands: dict[str, Band] = code_bands
    if bands_fn is not None:
        try:
            final_bands = dict(bands_fn(list(all_cards)))
        except Exception as exc:  # a broken band builder keeps the code-card bands, flagged
            flags.append(f"bands_fn_error {type(exc).__name__}")
            final_bands = code_bands
    full_desk = desk(all_cards, final_bands)

    # 3. debate
    debate = Debate()
    if not outage:
        stage = await guarded("debate", lambda: run_debate(
            gw=gw, reg=reg, desk_text=full_desk, ctx=ctx, pack=pack, lines=lines,
            bull_seed=seed_for(policy, "bull", 42), bear_seed=seed_for(policy, "bear", 43),
            bull_num_predict=num_predict(policy, "bull", 900),
            bear_num_predict=num_predict(policy, "bear", 900),
        ))
        calls += stage.calls
        outage = stage.outage
        drun: DebateRun = stage.result
        debate = drun.debate
        dropped += drun.notes
        raw.update(drun.raw)
    else:
        calls += [_skipped(reg, r, "council_unavailable") for r in ("bull_open", "bear")]

    # 4. PM replicates
    pm_reps: list[PMReplicate] = []
    if not outage:
        debate_text = transcript(debate)
        stage = await guarded("pm", lambda: run_pm(
            gw=gw, reg=reg, desk_text=full_desk, debate_text=debate_text, ctx=ctx,
            seeds=seeds, num_predict=num_predict(policy, "pm", 1200),
        ))
        calls += stage.calls
        outage = stage.outage
        prun: PMRun = stage.result
        pm_reps = prun.replicates
        raw.update(prun.raw)
    else:
        calls += [_skipped(reg, "pm", "council_unavailable", i) for i in range(len(seeds))]

    # 5. single-agent control (desk pack with code cards only; no debate)
    sa_reps: list[PMReplicate] = []
    if "single_agent_control" in kept:
        if outage:
            calls += [_skipped(reg, "single_agent", "council_unavailable", i) for i in range(len(seeds))]
        else:
            stage = await guarded("single_agent", lambda: pm_mod.run_single_agent(
                gw=gw, reg=reg, desk_text=code_desk, ctx=ctx, seeds=seeds,
                num_predict=num_predict(policy, "single_agent_control", 1200),
            ))
            calls += stage.calls
            srun: PMRun = stage.result
            sa_reps = srun.replicates
            raw.update(srun.raw)

    # 6. audit -> enforce -> aggregate
    enforce_notes: dict[str, list[str]] = {}
    dbands = deadbands(policy, lines)

    def finish(
        role: str,
        reps: list[PMReplicate],
        cards: Sequence[EvidenceCard],
        use_bands: Mapping[str, Band],
    ) -> list[PMReplicate]:
        out = []
        for rep in reps:
            res = audit(
                rep.decision, pack=pack, cards=cards, bands=use_bands, ref_levels=ref_levels,
                current_levels=current, lines=lines, policy=policy,
            )
            valid = res.valid
            try:
                clipped, notes = enforce(dict(res.levels), dict(use_bands))
                # a line the enforcer drops (no band) goes to its reference, never unclipped
                enforced = {s: float(clipped.get(s, ref_levels.get(s, 0.0))) for s in res.levels}
            except Exception as exc:  # a broken enforcer must not promote an unclipped book
                enforced, notes, valid = dict(ref_levels), [f"enforce_error {type(exc).__name__}"], False
            if notes:
                enforce_notes[f"{role}:{rep.replicate}"] = list(notes)
            out.append(rep.model_copy(update={
                "valid": valid, "audit_violations": res.violations, "reverted": res.reverted,
                "enforced_levels": enforced,
            }))
        return out

    pm_reps = finish("pm", pm_reps, all_cards, final_bands)
    sa_reps = finish("single_agent", sa_reps, code_cards, code_bands)

    if outage:
        agg = AggregateResult(levels=dict(ref_levels), medoid_index=None, basis="council_unavailable")
    else:
        agg = aggregate(pm_reps, ref_levels, current, dbands)
    sa_agg = aggregate(sa_reps, ref_levels, current, dbands) if sa_reps else None

    return CouncilResult(
        cards=all_cards,
        macro=macro_out,
        debate=debate,
        pm=pm_reps,
        single_agent=sa_reps,
        aggregate=agg,
        single_agent_aggregate=sa_agg,
        calls=calls,
        basis=agg.basis,
        dropped=dropped,
        flags=flags,
        enforce_notes=enforce_notes,
        bands=final_bands,
        prompt_manifest_sha=reg.manifest_sha(),
        desk_sha=hashlib.sha256(full_desk.encode()).hexdigest(),
        raw=raw,
    )

