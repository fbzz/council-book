"""Deterministic stub gateway for tests and dry runs. No network, no clock, no randomness.

Responses come from a mapping `{role: response}` where a response is:
  - a dict (the JSON object the model would return),
  - a str (raw model text, to exercise the decoder),
  - a `StubFailure` (simulates timeout / transport),
  - a `StubTurns(first, second)`: the first reply, then (only when the first fails the checker) the
    reply to the correction turn, exactly as the real gateway's single correction retry,
  - or a callable `(user_text, replicate) -> any of the above`.
The reply goes through the same decode -> sanitize -> strict validation path as the real gateway,
and the result carries the same capture fields (`turns`, `errors`, `correction`, `sent_assistant`).
A role with no entry returns `parse_fail` ("no stub response"). Calls are recorded in `.log`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from council.llm.gateway import LLMResult, correction_message, decode_reply
from council.models.cycle import CallStatus, RoleCall


@dataclass(frozen=True)
class StubFailure:
    """Simulated failure: status is `timeout` or `transport`."""

    status: CallStatus = "transport"
    error: str = "stub failure"


@dataclass(frozen=True)
class StubTurns:
    """A first reply and the reply to the correction turn (used only if the first fails)."""

    first: dict[str, Any] | str | StubFailure
    second: dict[str, Any] | str | StubFailure


type StubResponse = dict[str, Any] | str | StubFailure | StubTurns
type StubEntry = StubResponse | Callable[[str, int], StubResponse]


@dataclass(frozen=True)
class StubCall:
    role: str
    replicate: int
    seed: int
    user: str
    system: str = ""


@dataclass
class StubGateway:
    responses: Mapping[str, StubEntry]
    model: str = "stub"
    log: list[StubCall] = field(default_factory=list)

    async def complete(
        self,
        *,
        role: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        seed: int,
        num_predict: int,
        prompt_id: str,
        prompt_sha: str,
        input_hash: str,
        replicate: int = 0,
    ) -> LLMResult:
        """Same contract as `OllamaGateway.complete`: never raises, latency 0."""
        self.log.append(StubCall(role=role, replicate=replicate, seed=seed, user=user, system=system))
        turns: list[str] = []
        extra: dict[str, Any] = {}

        def result(parsed: BaseModel | None, status: CallStatus, raw: str, error: str = "") -> LLMResult:
            call = RoleCall(
                role=role,
                replicate=replicate,
                seed=seed,
                think=False,
                prompt_id=prompt_id,
                prompt_sha=prompt_sha,
                input_hash=input_hash,
                status=status,
                error=error[:300],
            )
            return LLMResult(parsed=parsed, call=call, raw=raw, turns=tuple(turns), **extra)

        def text_of(response: dict[str, Any] | str) -> str:
            return response if isinstance(response, str) else json.dumps(response, sort_keys=True)

        try:
            entry = self.responses.get(role)
            if entry is None:
                return result(None, "parse_fail", "", "no stub response")
            response = entry(user, replicate) if callable(entry) else entry
            second: dict[str, Any] | str | StubFailure | None = None
            if isinstance(response, StubTurns):
                response, second = response.first, response.second
            if isinstance(response, StubFailure):
                return result(None, response.status, "", response.error)
            raw = text_of(response)
            turns.append(raw)
            parsed, errors = decode_reply(raw, schema)
            if parsed is not None:
                return result(parsed, "ok", raw)
            if second is None:
                return result(None, "parse_fail", raw, "; ".join(errors))
            first_errors = "; ".join(errors)
            extra.update(correction=correction_message(errors), sent_assistant=raw[:8000],
                         errors=tuple(errors))
            if isinstance(second, StubFailure):
                return result(None, "parse_fail", raw,
                              f"{first_errors} | correction {second.status}: {second.error}")
            raw2 = text_of(second)
            turns.append(raw2)
            parsed, errors2 = decode_reply(raw2, schema)
            if parsed is not None:
                return result(parsed, "ok", raw2, f"corrected: {first_errors}")
            return result(None, "parse_fail", raw2,
                          f"{first_errors} | after correction: {'; '.join(errors2)}")
        except Exception as exc:
            return result(None, "transport", "", f"stub raised {type(exc).__name__}")

    async def model_digest(self) -> str:
        return "stub"

    async def verify_model(self) -> bool:
        return True
