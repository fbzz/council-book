"""PM replicates and the single-agent control.

Rules:
  - The PM runs as independent replicates (seeds from `council.seeds.pm`, default 42/43/44) on the
    SAME desk pack and debate transcript; each returns a sparse `PMDecision` or nothing.
  - The single-agent control (C10) uses the same schema and seeds but sees the desk pack only:
    no debate, and no LLM analyst cards (code cards stay; they are data, not council work).
  - Replicates run concurrently; the gateway's limiter and concurrency cap still apply. Results
    keep replicate order, so the run is deterministic for a deterministic gateway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from council.deliberation.common import call_role
from council.llm.gateway import Gateway, LLMResult
from council.llm.prompts import PromptRegistry
from council.models.cycle import PMReplicate, RoleCall
from council.models.pm import PMDecision


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
    gw: Gateway, reg: PromptRegistry, *, role: str, ctx: Mapping[str, Any], user: str,
    seeds: Sequence[int], num_predict: int,
) -> PMRun:
    results = await asyncio.gather(
        *(
            call_role(
                gw, reg, role=role, ctx=ctx, user=user, schema=PMDecision,
                seed=int(seed), num_predict=num_predict, replicate=i,
            )
            for i, seed in enumerate(seeds)
        )
    )
    return _to_run(role, seeds, results)


async def run_pm(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    desk_text: str,
    debate_text: str,
    ctx: Mapping[str, Any],
    seeds: Sequence[int] = (42, 43, 44),
    num_predict: int = 1200,
) -> PMRun:
    """The council PM: desk pack + debate transcript, one call per seed."""
    user = f"{desk_text}\n{debate_text}\nDecide. Reply with the JSON object only."
    return await _replicates(
        gw, reg, role="pm", ctx=ctx, user=user, seeds=seeds, num_predict=num_predict
    )


async def run_single_agent(
    *,
    gw: Gateway,
    reg: PromptRegistry,
    desk_text: str,
    ctx: Mapping[str, Any],
    seeds: Sequence[int] = (42, 43, 44),
    num_predict: int = 1200,
) -> PMRun:
    """Control C10: one well-prompted call per seed on the desk pack only."""
    user = f"{desk_text}\nDecide. Reply with the JSON object only."
    return await _replicates(
        gw, reg, role="single_agent", ctx=ctx, user=user, seeds=seeds, num_predict=num_predict
    )
