"""Shared helpers: the prompt context built from policy, one role call, per-line policy lookups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from council.llm.gateway import Gateway, LLMResult, input_hash_of
from council.llm.prompts import PromptRegistry
from council.models.common import LEVEL_GRID
from council.models.facts import FactPack
from council.models.reference import ReferenceBook
from council.policy import LineSpec, Policy


def late_ids(pack: FactPack) -> set[str]:
    """Evidence stamped as available AFTER the slot: never admissible (no lookahead)."""
    late = {f.id for f in pack.facts if f.available_at > pack.slot}
    late |= {n.id for n in pack.news if n.available_at > pack.slot}
    late |= {s.id for f in pack.filings if f.available_at > pack.slot for s in f.sentences}
    return late


def admissible_ids(pack: FactPack) -> set[str]:
    """IDs a role may cite: `pack.evidence_ids()` minus anything available after the slot."""
    return pack.evidence_ids() - late_ids(pack)


def _fmt_level(x: float) -> str:
    return f"{x:g}"


def prompt_context(policy: Policy) -> dict[str, Any]:
    """Numbers the prompts EXPLAIN. They come from policy so prompts never restate a stale number;
    code still enforces every one of them independently."""
    risk = policy.risk
    auth = risk["authority"]
    return {
        "grid": ", ".join(_fmt_level(g) for g in LEVEL_GRID),
        "max_deviations": int(auth["max_deviations_per_cycle"]),
        "cut_with_card": _fmt_level(float(auth["up"]["cut_with_qualifying_card"])),
        "leverage_extension": _fmt_level(float(auth["up"]["leverage_extension"])),
        "deadband_level": _fmt_level(float(risk["deadband"]["level"])),
        "deadband_crypto": _fmt_level(float(risk["deadband"]["level_crypto"])),
        "srbe_council": _fmt_level(float(risk["net_of_cost_gate"]["council_max_srbe"])),
        "min_hold_days": int(risk["min_hold_days"]["default"]),
        "min_hold_crypto": int(risk["min_hold_days"]["crypto"]),
        "event_before_h": _fmt_level(float(risk["event_block"]["macro_before_h"])),
        "event_after_h": _fmt_level(float(risk["event_block"]["macro_after_h"])),
        "warn_pct": _fmt_level(round((1.0 - float(risk["killswitch"]["warn_at"])) * 100, 6)),
        "halt_pct": _fmt_level(round((1.0 - float(risk["killswitch"]["halt_at"])) * 100, 6)),
        "max_cards": 8,
        "claim_max": 200,
        "falsifier_max": 160,
        "reason_max": 160,
    }


def role_cfg(policy: Policy, key: str) -> dict[str, Any]:
    return dict(policy.council.get("roles", {}).get(key, {}))


def role_enabled(policy: Policy, key: str) -> bool:
    return bool(role_cfg(policy, key).get("enabled", False))


def num_predict(policy: Policy, key: str, default: int = 1000) -> int:
    return int(role_cfg(policy, key).get("max_num_predict", default))


def pm_seeds(policy: Policy) -> tuple[int, ...]:
    seeds = policy.council.get("seeds", {}).get("pm", [42, 43, 44])
    return tuple(int(s) for s in seeds)


def seed_for(policy: Policy, key: str, default: int = 42) -> int:
    value = policy.council.get("seeds", {}).get(key, default)
    return int(value[0] if isinstance(value, list) else value)


def reference_levels(ref: ReferenceBook, lines: Sequence[LineSpec]) -> dict[str, float]:
    """Reference level per line, in line order; a line missing from the book is 0 (flat)."""
    return {
        ln.symbol: float(ref.entries[ln.symbol].level_ref) if ln.symbol in ref.entries else 0.0
        for ln in lines
    }


def full_levels(levels: Mapping[str, float], lines: Sequence[LineSpec]) -> dict[str, float]:
    return {ln.symbol: float(levels.get(ln.symbol, 0.0)) for ln in lines}


def deadbands(policy: Policy, lines: Sequence[LineSpec]) -> dict[str, float]:
    """Per-line level deadband (R11): crypto lines use the wider crypto deadband."""
    db = policy.risk["deadband"]
    return {
        ln.symbol: float(db["level_crypto"] if ln.asset_class == "crypto" else db["level"])
        for ln in lines
    }


async def call_role(
    gw: Gateway,
    reg: PromptRegistry,
    *,
    role: str,
    ctx: Mapping[str, Any],
    user: str,
    schema: type[BaseModel],
    seed: int,
    num_predict: int,
    replicate: int = 0,
) -> LLMResult:
    """Render the role's system prompt, hash the exact input, and make one gateway call."""
    system = reg.render(role, **ctx)
    return await gw.complete(
        role=role,
        system=system,
        user=user,
        schema=schema,
        seed=seed,
        num_predict=num_predict,
        prompt_id=reg.prompt_id(role),
        prompt_sha=reg.sha256(role),
        input_hash=input_hash_of(system, user),
        replicate=replicate,
    )
