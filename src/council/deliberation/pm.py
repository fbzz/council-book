"""PM replicates and the single-agent control.

Rules:
  - The PM runs as independent replicates (seeds from `council.seeds.pm`, default 42/43/44) on the
    SAME desk pack and debate transcript; each returns a sparse `PMDecision` or nothing.
  - The single-agent control (C10) uses the same schema and seeds but sees the desk pack only:
    no debate, and no LLM analyst cards (code cards stay; they are data, not council work).
  - Replicates run concurrently; the gateway's limiter and concurrency cap still apply. Results
    keep replicate order, so the run is deterministic for a deterministic gateway.
  - Every replicate reads the same sections: the PM `desk.full.*`, `sep.pm`, `transcript`,
    `tail.pm`; the control `desk.code.*`, `tail.single_agent` (byte-identical to the old strings).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from council.deliberation.capture import InputSink
from council.deliberation.common import call_role
from council.deliberation.segments import Segmented, literal, raw
from council.llm.gateway import Gateway, LLMResult
from council.llm.prompts import PromptRegistry
from council.models.cycle import PMReplicate, RoleCall
from council.models.pm import PMDecision

TAIL_DECIDE = "\nDecide. Reply with the JSON object only."


class PMRun(NamedTuple):
    replicates: list[PMReplicate]
    calls: list[RoleCall]
    raw: dict[str, str]


def _to_run(role: str, seeds: Sequence[int], results: Sequence[LLMResult]) -> PMRun:
    reps: list[PMReplicate] = []
    raw: dict[str, str] = {}
    for i, (seed, res) in enumerate(zip(seeds, results, strict=True)):
        decision = res.parsed if isinstance(res.parsed, PMDecision) else None
        reps.append(PMReplicate(replicate=i, seed=int(seed), decision=decision, valid=decision is not None))
        raw[f"{role}:{i}"] = res.raw
    return PMRun(reps, [r.call for r in results], raw)


async def _replicates(
    gw: Gateway, reg: PromptRegistry, *, role: str, ctx: Mapping[str, Any],
    sections: Sequence[Segmented], seeds: Sequence[int], num_predict: int,
    sink: InputSink | None = None, attempt: int = 0,
) -> PMRun:
    results = await asyncio.gather(
        *(
            call_role(
                gw, reg, role=role, ctx=ctx, sections=sections, schema=PMDecision,
                seed=int(seed), num_predict=num_predict, replicate=i, sink=sink, attempt=attempt,
            )
            for i, seed in enumerate(seeds)
        )
    )
    return _to_run(role, seeds, results)


def _desk(desk_text: str | None, desk_sections: Sequence[Segmented] | None) -> list[Segmented]:
    return list(desk_sections) if desk_sections is not None else [raw("desk", desk_text or "")]


async def run_pm(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    desk_text: str | None = None,
    debate_text: str | None = None,
    ctx: Mapping[str, Any],
    seeds: Sequence[int] = (42, 43, 44),
    num_predict: int = 1200,
    desk_sections: Sequence[Segmented] | None = None,
    debate_section: Segmented | None = None,
    sink: InputSink | None = None,
    attempt: int = 0,
) -> PMRun:
    """The council PM: desk pack + debate transcript, one call per seed."""
    debate = debate_section if debate_section is not None else raw("transcript", debate_text or "")
    sections = [*_desk(desk_text, desk_sections), literal("sep.pm", "\n"), debate,
                literal("tail.pm", TAIL_DECIDE, "tail")]
    return await _replicates(
        gw, reg, role="pm", ctx=ctx, sections=sections, seeds=seeds, num_predict=num_predict,
        sink=sink, attempt=attempt,
    )


async def run_single_agent(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    desk_text: str | None = None,
    ctx: Mapping[str, Any],
    seeds: Sequence[int] = (42, 43, 44),
    num_predict: int = 1200,
    desk_sections: Sequence[Segmented] | None = None,
    sink: InputSink | None = None,
    attempt: int = 0,
) -> PMRun:
    """Control C10: one well-prompted call per seed on the desk pack only."""
    sections = [*_desk(desk_text, desk_sections), literal("tail.single_agent", TAIL_DECIDE, "tail")]
    return await _replicates(
        gw, reg, role="single_agent", ctx=ctx, sections=sections, seeds=seeds,
        num_predict=num_predict, sink=sink, attempt=attempt,
    )
