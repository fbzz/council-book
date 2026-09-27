"""Digests of every model input for the ten golden cases.

`uv run python -m tests.council.golden.make_golden` rewrites `renderings.json`. It was generated
at f577534, BEFORE the structured-desk refactor, so the stored digests are what the models read
then; the golden tests recompute them with the live code and require equality. Regenerate only in
a reviewed PR that intends to change model input (and then it is a prompt change for CHANGELOG).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from council.deliberation.council import run_council
from council.deliberation.officers import event_cards, vol_cards
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.models.cycle import Debate
from council.models.debate import AdvocateCase, BearCase
from council.policy import Policy

from ..factories import SLOT, bear_reply, bull_reply, clip_enforce, rebuttal_reply
from . import legacy
from .cases import CASE_NAMES, Case, build

GOLDEN = Path(__file__).with_name("renderings.json")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def debate_cases() -> dict[str, Debate]:
    bull = AdvocateCase.model_validate(bull_reply())
    bear = BearCase.model_validate(bear_reply())
    reb = AdvocateCase.model_validate(rebuttal_reply())
    empty = bull.model_copy(update={"proposal": {}, "claims": [], "concessions": ["a", "b"]})
    return {
        "full": Debate(bull_open=bull, bear=bear, bull_rebuttal=reb),
        "no_bull": Debate(bull_open=None, bear=bear, bull_rebuttal=reb),
        "no_bear": Debate(bull_open=bull, bear=None, bull_rebuttal=None),
        "bear_only_rebuttal_failed": Debate(bull_open=bull, bear=bear, bull_rebuttal=None),
        "empty_proposal": Debate(bull_open=empty, bear=None, bull_rebuttal=empty),
        "none": Debate(),
    }


def code_cards(case: Case, policy: Policy) -> list:
    return vol_cards(case.pack, policy) + event_cards(case.pack, case.pack.slot, policy)


def lines_of(case: Case, policy: Policy) -> list:
    return list(policy.universe.lines)


def desk_inputs(case: Case, policy: Policy) -> dict[str, Any]:
    return dict(pack=case.pack, ref=case.ref, bands=case.bands, current_levels=case.current,
                cost_hints=case.hints, lines=lines_of(case, policy))


def legacy_renderings(case: Case, policy: Policy) -> dict[str, str]:
    """Every rendering of the old code for one case, as text (key -> text)."""
    cc = code_cards(case, policy)
    inputs = desk_inputs(case, policy)
    code = legacy.desk_pack(cards=cc, **inputs)
    full = legacy.desk_pack(cards=cc + case.extra_cards, **inputs)
    out = {
        "desk.code": code,
        "desk.full": full,
        "desk.nocards": legacy.desk_pack(cards=cc, include_cards=False, **inputs),
        "news_detail": legacy.news_detail(case.pack),
        "news_detail.5": legacy.news_detail(case.pack, max_items=5),
        "user.news": legacy.news_user(code, case.pack),
        "user.macro": legacy.macro_user(code),
        "user.bull_open": legacy.bull_open_user(full),
        "user.single_agent": legacy.single_agent_user(code),
    }
    for name, deb in debate_cases().items():
        out[f"transcript.{name}"] = legacy.transcript(deb)
        out[f"user.bear.{name}"] = legacy.bear_user(full, deb.bull_open)
        out[f"user.bull_rebuttal.{name}"] = legacy.bull_rebuttal_user(full, deb.bull_open, deb.bear)
        out[f"user.pm.{name}"] = legacy.pm_user(full, legacy.transcript(deb))
        out[f"case.bull.{name}"] = legacy.format_case("BULL opening", deb.bull_open, prefix="bull_open:")
        out[f"case.bear.{name}"] = legacy.format_case("BEAR reply", deb.bear, prefix="bear:",
                                                      rebut_prefix="bull_open:")
    return out


def live_calls(case: Case, policy: Policy, reg: PromptRegistry, **kw: Any) -> tuple[list[list[Any]], Any]:
    """Run the live council end to end on the stub gateway; [role, replicate, sha(system), sha(user)]
    for every model call, in call order (the gateway sees exactly what a model would read)."""

    class Recorder(StubGateway):
        async def complete(self, **call: Any):
            seen.append([call["role"], call["replicate"], sha(call["system"]), sha(call["user"])])
            return await super().complete(**call)

    seen: list[list[Any]] = []

    async def instant(_s: float) -> None:
        return None

    result = asyncio.run(run_council(
        gw=Recorder(case.responses()), reg=reg, pack=case.pack, ref=case.ref, bands=case.bands,
        current_levels=case.current, cost_hints=case.hints, lines=lines_of(case, policy),
        policy=policy, enforce=clip_enforce, now=SLOT, sleep=instant, run_macro=True, **kw,
    ))
    return seen, result


def golden(core: Policy, sleeve: Policy) -> dict[str, Any]:
    reg = PromptRegistry()
    out: dict[str, Any] = {}
    for name in CASE_NAMES:
        case = build(name)
        policy = case.policy(core, sleeve)
        rend = {k: sha(v) for k, v in legacy_renderings(case, policy).items()}
        calls, _ = live_calls(case, policy, reg)
        out[name] = {"renderings": rend, "calls": calls}
    return out


def main() -> None:  # pragma: no cover - maintenance entry point
    import tempfile

    from council import policy as policy_module
    from tests.conftest import make_sleeve_policy_dir

    core = Policy.load(include_sleeve=False)
    policy_module.install_default_policy(core)
    sleeve = Policy.load(make_sleeve_policy_dir(Path(tempfile.mkdtemp()) / "policy"))
    data = golden(core, sleeve)
    GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN} ({len(data)} cases)")


if __name__ == "__main__":  # pragma: no cover
    main()
