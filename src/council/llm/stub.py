"""Deterministic stub gateway for tests and dry runs. No network, no clock, no randomness.

Responses come from a mapping `{role: response}` where a response is:
  - a dict (the JSON object the model would return),
  - a str (raw model text, to exercise the decoder),
  - a `StubFailure` (simulates timeout / transport),
  - or a callable `(user_text, replicate) -> any of the above`.
The reply goes through the same decode -> sanitize -> strict validation path as the real gateway.
A role with no entry returns `parse_fail` ("no stub response"). Calls are recorded in `.log`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from council.llm.gateway import LLMResult, decode_reply
from council.models.cycle import CallStatus, RoleCall


@dataclass(frozen=True)
class StubFailure:
    """Simulated failure: status is `timeout` or `transport`."""

    status: CallStatus = "transport"
    error: str = "stub failure"


type StubResponse = dict[str, Any] | str | StubFailure
type StubEntry = StubResponse | Callable[[str, int], StubResponse]


@dataclass(frozen=True)
class StubCall:
    role: str
    replicate: int
    seed: int
    user: str


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
        self.log.append(StubCall(role=role, replicate=replicate, seed=seed, user=user))

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
            return LLMResult(parsed=parsed, call=call, raw=raw)

        try:
            entry = self.responses.get(role)
            if entry is None:
                return result(None, "parse_fail", "", "no stub response")
            response = entry(user, replicate) if callable(entry) else entry
            if isinstance(response, StubFailure):
                return result(None, response.status, "", response.error)
            raw = response if isinstance(response, str) else json.dumps(response, sort_keys=True)
            parsed, errors = decode_reply(raw, schema)
            if parsed is None:
                return result(None, "parse_fail", raw, "; ".join(errors))
            return result(parsed, "ok", raw)
        except Exception as exc:
            return result(None, "transport", "", f"stub raised {type(exc).__name__}")

    async def model_digest(self) -> str:
        return "stub"

    async def verify_model(self) -> bool:
        return True
