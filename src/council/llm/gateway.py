"""Async Ollama gateway: one role call in, one `LLMResult` out. It never raises.

Rules (each one tested):
  - Ollama Cloud ignores JSON schemas, so the request sends `format: "json"`, `think: false`,
    temperature 0 and a fixed seed; the reply is validated STRICTLY after decoding.
  - Decoding: the whole reply as JSON first, then the LAST top-level JSON object in the text
    (a model may quote another agent's JSON before its own).
  - Every string in the decoded object is sanitized before validation (see `sanitize.py`).
  - One correction retry on a parse/validation failure; the retry tells the model the exact errors.
  - Transport failures walk the timeout ladder (40/60/90 s by default): each attempt gets the next
    timeout; timeouts, connection errors, 429 and 5xx move to the next step; other 4xx stop.
  - A rate limiter (calls per minute, sliding 60 s window) and a concurrency cap apply to every
    HTTP attempt.
  - Statuses: ok / parse_fail / timeout / transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from council.llm.sanitize import sanitize_obj, sanitize_text
from council.models.cycle import CallStatus, RoleCall

Sleep = Callable[[float], Awaitable[None]]

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
MAX_ERRORS_IN_CORRECTION = 12
MAX_ERROR_CHARS = 300
RETRYABLE_HTTP = frozenset({408, 409, 425, 429})


@dataclass(frozen=True)
class LLMResult:
    """`parsed` is a validated, sanitized instance of the requested schema, or None."""

    parsed: BaseModel | None
    call: RoleCall
    raw: str


class Gateway(Protocol):
    """What the deliberation code needs from a gateway (Ollama or stub)."""

    model: str

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
    ) -> LLMResult: ...


# --------------------------------------------------------------------------------------- decoding
def parse_json_object(text: str) -> dict[str, Any] | None:
    """Decode a JSON object from model text: the whole text first (after removing think blocks and
    markdown fences), else the LAST top-level `{...}` object that decodes. None if there is none."""
    if not isinstance(text, str):
        return None
    cleaned = _THINK_BLOCK.sub("", text).strip()
    stripped = _FENCE.sub("", cleaned).strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        return obj
    decoder = json.JSONDecoder()
    last: dict[str, Any] | None = None
    i = cleaned.find("{")
    while i != -1:
        try:
            candidate, end = decoder.raw_decode(cleaned, i)
        except (json.JSONDecodeError, ValueError):
            i = cleaned.find("{", i + 1)
            continue
        if isinstance(candidate, dict):
            last = candidate
        i = cleaned.find("{", end)
    return last


def format_validation_errors(exc: ValidationError) -> list[str]:
    """`loc: message` per error, e.g. `cards.0.claim: String should have at most 200 characters`."""
    out = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        out.append(f"{loc}: {err.get('msg', 'invalid')}")
    return out


def decode_reply(raw: str, schema: type[BaseModel]) -> tuple[BaseModel | None, list[str]]:
    """Parse -> sanitize every string -> strict validation. Returns (model, []) or (None, errors)."""
    obj = parse_json_object(raw)
    if obj is None:
        return None, ["reply is not a JSON object"]
    clean = sanitize_obj(obj)
    try:
        return schema.model_validate(clean, strict=True), []
    except ValidationError as exc:
        return None, format_validation_errors(exc)


def correction_message(errors: Sequence[str]) -> str:
    """The single correction turn: the exact validation errors, then the output rule again."""
    shown = list(errors)[:MAX_ERRORS_IN_CORRECTION]
    lines = "\n".join(f"- {e}" for e in shown)
    more = "" if len(errors) <= len(shown) else f"\n- ... and {len(errors) - len(shown)} more"
    return (
        "Your previous reply was not accepted by the checker. Problems:\n"
        f"{lines}{more}\n"
        "Reply again with ONE corrected JSON object containing exactly the fields described in "
        "the instructions. No prose, no markdown fences."
    )


def input_hash_of(system: str, user: str) -> str:
    """Hash of exactly what the model is shown."""
    return hashlib.sha256(system.encode() + b"\0" + user.encode()).hexdigest()


def _short_error(text: str) -> str:
    return sanitize_text(text)[:MAX_ERROR_CHARS]


# ---------------------------------------------------------------------------------------- limiter
class _RateLimiter:
    """Sliding 60 s window: at most `per_min` attempts start in any window. Uses the injected clock
    and sleep, and trusts that `sleep(w)` waited `w` seconds (so a fake sleep never spins)."""

    def __init__(self, per_min: int, clock: Callable[[], float], sleep: Sleep) -> None:
        self._per_min = max(1, int(per_min))
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock, self._loop = asyncio.Lock(), loop
        async with self._lock:
            now = self._clock()
            while self._starts and now - self._starts[0] >= 60.0:
                self._starts.popleft()
            if len(self._starts) >= self._per_min:
                wait = 60.0 - (now - self._starts[0])
                await self._sleep(max(wait, 0.0))
                self._starts.popleft()
                now = max(self._clock(), now + max(wait, 0.0))
            self._starts.append(now)


class _AttemptFailure(Exception):
    def __init__(self, status: CallStatus, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# ---------------------------------------------------------------------------------------- gateway
class OllamaGateway:
    """Async client for Ollama `/api/chat` (local daemon, cloud-backed `:cloud` models)."""

    def __init__(
        self,
        host: str,
        model: str,
        *,
        calls_per_min: int = 20,
        concurrency: int = 3,
        timeouts: Sequence[float] = (40, 60, 90),
        num_ctx: int = 32768,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        backoff_s: float = 2.0,
    ) -> None:
        if not timeouts:
            raise ValueError("timeout ladder must have at least one step")
        self.host = host.rstrip("/")
        self.model = model
        self.think = False
        self.num_ctx = int(num_ctx)
        self.timeouts = tuple(float(t) for t in timeouts)
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._backoff_s = float(backoff_s)
        self._concurrency = max(1, int(concurrency))
        self._limiter = _RateLimiter(calls_per_min, clock, sleep)
        self._sem: asyncio.Semaphore | None = None
        self._sem_loop: asyncio.AbstractEventLoop | None = None

    # -- helpers -------------------------------------------------------------------------------
    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem, self._sem_loop = asyncio.Semaphore(self._concurrency), loop
        return self._sem

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.host,
            transport=self._transport,
            timeout=httpx.Timeout(timeout if timeout is not None else 10.0),
        )

    def _body(self, messages: list[dict[str, str]], seed: int, num_predict: int) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {
                "temperature": 0,
                "seed": int(seed),
                "num_ctx": self.num_ctx,
                "num_predict": int(num_predict),
            },
        }

    async def _attempt(self, body: dict[str, Any], timeout: float) -> tuple[str, int, int]:
        """One HTTP attempt. Returns (content, tokens_in, tokens_out) or raises _AttemptFailure."""
        await self._limiter.acquire()
        async with self._semaphore():
            try:
                async with asyncio.timeout(timeout + 1.0), self._client(timeout) as client:
                    resp = await client.post("/api/chat", json=body)
            except (httpx.TimeoutException, TimeoutError) as exc:
                raise _AttemptFailure("timeout", f"timeout after {timeout:.0f}s", True) from exc
            except httpx.HTTPError as exc:
                raise _AttemptFailure("transport", f"{type(exc).__name__}", True) from exc
        if resp.status_code >= 500 or resp.status_code in RETRYABLE_HTTP:
            raise _AttemptFailure("transport", f"http {resp.status_code}", True)
        if resp.status_code >= 400:
            raise _AttemptFailure("transport", f"http {resp.status_code}", False)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise _AttemptFailure("transport", "non-JSON response body", True) from exc
        if not isinstance(payload, dict):
            raise _AttemptFailure("transport", "unexpected response shape", True)
        if payload.get("error"):
            raise _AttemptFailure("transport", f"server error: {payload.get('error')}", True)
        message = payload.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        tokens_in = int(payload.get("prompt_eval_count") or 0)
        tokens_out = int(payload.get("eval_count") or 0)
        return (content if isinstance(content, str) else ""), tokens_in, tokens_out

    async def _post_with_ladder(self, body: dict[str, Any]) -> tuple[str, int, int]:
        """Walk the timeout ladder. Raises the last _AttemptFailure when every step failed."""
        last: _AttemptFailure | None = None
        for step, timeout in enumerate(self.timeouts):
            if step > 0 and self._backoff_s > 0:
                await self._sleep(self._backoff_s * step)
            try:
                return await self._attempt(body, timeout)
            except _AttemptFailure as exc:
                last = exc
                if not exc.retryable:
                    break
        assert last is not None
        raise last

    # -- public --------------------------------------------------------------------------------
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
        """One role call with at most one correction retry. Never raises."""
        started = self._clock()
        tokens_in = tokens_out = 0
        raw = ""

        def result(parsed: BaseModel | None, status: CallStatus, error: str = "") -> LLMResult:
            call = RoleCall(
                role=role,
                replicate=replicate,
                seed=seed,
                think=False,
                prompt_id=prompt_id,
                prompt_sha=prompt_sha,
                input_hash=input_hash,
                latency_ms=max(0, int(round((self._clock() - started) * 1000))),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                status=status,
                error=_short_error(error),
            )
            return LLMResult(parsed=parsed, call=call, raw=raw)

        try:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            try:
                raw, t_in, t_out = await self._post_with_ladder(self._body(messages, seed, num_predict))
            except _AttemptFailure as exc:
                return result(None, exc.status, str(exc))
            tokens_in, tokens_out = t_in, t_out
            parsed, errors = decode_reply(raw, schema)
            if parsed is not None:
                return result(parsed, "ok")

            retry_messages = [
                *messages,
                {"role": "assistant", "content": raw[:8000]},
                {"role": "user", "content": correction_message(errors)},
            ]
            first_errors = "; ".join(errors)
            try:
                raw2, t_in, t_out = await self._post_with_ladder(
                    self._body(retry_messages, seed, num_predict)
                )
            except _AttemptFailure as exc:
                return result(None, "parse_fail", f"{first_errors} | correction {exc.status}: {exc}")
            tokens_in += t_in
            tokens_out += t_out
            raw = raw2
            parsed, errors2 = decode_reply(raw2, schema)
            if parsed is not None:
                return result(parsed, "ok", f"corrected: {first_errors}")
            return result(None, "parse_fail", f"{first_errors} | after correction: {'; '.join(errors2)}")
        except Exception as exc:  # the gateway never raises
            return result(None, "transport", f"unexpected {type(exc).__name__}")

    async def model_digest(self) -> str:
        """Fingerprint of the model as the local daemon describes it (`POST /api/show`).

        Returns the `digest` field when the daemon provides one, else a SHA-256 over the canonical
        `/api/show` payload (minus `modified_at`); "" on any failure. LIMITATION: for `:cloud` models
        the local entry is only a stub that points at ollama.com, so this digest CANNOT see a model
        swap or re-quantisation on the cloud side. It detects local changes only."""
        try:
            async with self._client(self.timeouts[0]) as client:
                resp = await client.post("/api/show", json={"model": self.model})
            if resp.status_code != 200:
                return ""
            payload = resp.json()
            if not isinstance(payload, dict):
                return ""
            digest = payload.get("digest")
            if isinstance(digest, str) and digest:
                return digest
            stable = {k: v for k, v in payload.items() if k != "modified_at"}
            blob = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
            return hashlib.sha256(blob.encode()).hexdigest()
        except Exception:
            return ""

    async def verify_model(self) -> bool:
        """True only if `/api/tags` lists EXACTLY `self.model` (including a `:cloud` suffix).
        `deepseek-v4-flash` does not match `deepseek-v4-flash:cloud`. Never raises."""
        try:
            async with self._client(self.timeouts[0]) as client:
                resp = await client.get("/api/tags")
            if resp.status_code != 200:
                return False
            models = resp.json().get("models", [])
            names = set()
            for entry in models:
                if isinstance(entry, dict):
                    for key in ("name", "model"):
                        if isinstance(entry.get(key), str):
                            names.add(entry[key])
            return self.model in names
        except Exception:
            return False
