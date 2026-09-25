"""OllamaGateway against httpx.MockTransport: no network, fake sleep, fake clock."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from council.llm.gateway import (
    LLMResult,
    OllamaGateway,
    correction_message,
    decode_reply,
    input_hash_of,
    parse_json_object,
)
from council.models.pm import PMDecision

MODEL = "deepseek-v4-flash:cloud"
GOOD = {
    "deviations": [],
    "decisive_fact": {"text": "Trend intact.", "evidence_id": "F:NDX:trend"},
    "sided_with": "reference",
    "dismissed": [],
    "no_change_reason": "Nothing changed.",
}


def chat(content: str, *, status: int = 200, tokens: tuple[int, int] = (100, 20)) -> httpx.Response:
    return httpx.Response(
        status,
        json={"message": {"role": "assistant", "content": content}, "done": True,
              "prompt_eval_count": tokens[0], "eval_count": tokens[1]},
    )


class Script:
    """A MockTransport handler that replays a list of responses/exceptions and records requests."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    def read_timeouts(self) -> list[float]:
        return [r.extensions["timeout"]["read"] for r in self.requests]


def gateway(script: Script, **kw) -> tuple[OllamaGateway, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    kw.setdefault("clock", lambda: 0.0)
    gw = OllamaGateway("http://ollama.test", MODEL, transport=httpx.MockTransport(script),
                       sleep=fake_sleep, **kw)
    return gw, sleeps


def complete(gw: OllamaGateway, **kw) -> LLMResult:
    args = dict(role="pm", system="SYS", user="USER", schema=PMDecision, seed=42, num_predict=500,
                prompt_id="council-pm/v1", prompt_sha="a" * 64, input_hash="b" * 64, replicate=1)
    args.update(kw)
    return asyncio.run(gw.complete(**args))


# ------------------------------------------------------------------------------------ happy path
def test_clean_json_ok_and_request_shape():
    script = Script(chat(json.dumps(GOOD)))
    gw, _ = gateway(script)
    res = complete(gw)
    assert res.call.status == "ok" and isinstance(res.parsed, PMDecision)
    assert res.call.tokens_in == 100 and res.call.tokens_out == 20
    assert res.call.seed == 42 and res.call.replicate == 1 and res.call.think is False
    body = script.bodies()[0]
    assert script.requests[0].url.path == "/api/chat"
    assert body["model"] == MODEL and body["stream"] is False
    assert body["format"] == "json" and body["think"] is False
    assert body["options"] == {"temperature": 0, "seed": 42, "num_ctx": 32768, "num_predict": 500}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_json_wrapped_in_prose_uses_last_object():
    text = ('The bull said {"exposure": 1.0}. My decision:\n```json\n' + json.dumps(GOOD)
            + "\n```\nThanks.")
    gw, _ = gateway(Script(chat(text)))
    res = complete(gw)
    assert res.call.status == "ok" and res.parsed.sided_with == "reference"


# ------------------------------------------------------------------------------------ correction
def test_invalid_then_correction_ok_and_retry_carries_exact_errors():
    bad = {**GOOD, "sided_with": "both", "extra": 1}
    script = Script(chat(json.dumps(bad)), chat(json.dumps(GOOD)))
    gw, _ = gateway(script)
    res = complete(gw)
    assert res.call.status == "ok" and res.parsed is not None
    assert res.call.error.startswith("corrected:")
    assert res.call.tokens_in == 200 and res.call.tokens_out == 40
    retry = script.bodies()[1]
    assert [m["role"] for m in retry["messages"]] == ["system", "user", "assistant", "user"]
    fix = retry["messages"][-1]["content"]
    assert "sided_with:" in fix and "extra:" in fix
    assert retry["options"]["seed"] == 42


def test_invalid_twice_is_parse_fail():
    script = Script(chat("not json at all"), chat('{"deviations": "nope"}'))
    gw, _ = gateway(script)
    res = complete(gw)
    assert res.call.status == "parse_fail" and res.parsed is None
    assert len(script.requests) == 2
    assert "after correction" in res.call.error


def test_one_correction_only():
    script = Script(chat("{}"))
    gw, _ = gateway(script)
    complete(gw)
    assert len(script.requests) == 2  # original + exactly one correction


# ------------------------------------------------------------------------------ transport ladder
def test_timeout_moves_to_next_ladder_step():
    script = Script(httpx.ReadTimeout("slow"), chat(json.dumps(GOOD)))
    gw, sleeps = gateway(script)
    res = complete(gw)
    assert res.call.status == "ok"
    assert script.read_timeouts() == [40.0, 60.0]
    assert sleeps == [2.0]  # backoff between ladder steps


def test_all_timeouts_is_timeout_status():
    script = Script(httpx.ReadTimeout("slow"))
    gw, _ = gateway(script)
    res = complete(gw)
    assert res.call.status == "timeout" and res.parsed is None
    assert script.read_timeouts() == [40.0, 60.0, 90.0]


def test_timeout_then_connection_errors_is_transport():
    script = Script(httpx.ReadTimeout("slow"), httpx.ConnectError("refused"))
    gw, _ = gateway(script)
    res = complete(gw)
    assert res.call.status == "transport"
    assert len(script.requests) == 3


def test_5xx_retried_then_ok():
    script = Script(httpx.Response(502, text="bad gateway"), chat(json.dumps(GOOD)))
    gw, _ = gateway(script)
    assert complete(gw).call.status == "ok"
    assert len(script.requests) == 2


def test_5xx_everywhere_is_transport():
    script = Script(httpx.Response(503, text="down"))
    gw, _ = gateway(script)
    res = complete(gw)
    assert res.call.status == "transport" and "503" in res.call.error
    assert len(script.requests) == 3


def test_4xx_is_not_retried():
    script = Script(httpx.Response(404, json={"error": "model not found"}))
    gw, _ = gateway(script)
    res = complete(gw)
    assert res.call.status == "transport" and len(script.requests) == 1


def test_error_payload_is_transport():
    script = Script(httpx.Response(200, json={"error": "quota"}))
    gw, _ = gateway(script)
    assert complete(gw).call.status == "transport"


def test_never_raises_on_unexpected_exception():
    script = Script(RuntimeError("boom"))
    gw, _ = gateway(script)
    res = complete(gw)
    assert res.call.status == "transport" and res.parsed is None


def test_custom_ladder_and_num_ctx():
    script = Script(httpx.ReadTimeout("slow"))
    gw, _ = gateway(script, timeouts=(5, 7), num_ctx=8192)
    complete(gw)
    assert script.read_timeouts() == [5.0, 7.0]
    assert script.bodies()[0]["options"]["num_ctx"] == 8192


# ---------------------------------------------------------------------------------- sanitization
def test_parsed_strings_are_sanitized():
    dirty = {**GOOD, "no_change_reason": "\x1b[31mCalm‮ https://evil.example/x @someone ok"}
    gw, _ = gateway(Script(chat(json.dumps(dirty))))
    res = complete(gw)
    assert res.call.status == "ok"
    assert res.parsed.no_change_reason == "Calm ok"


def test_errors_are_sanitized_and_short():
    script = Script(httpx.Response(200, json={"error": "see https://status.example/x " + "z" * 500}))
    gw, _ = gateway(script)
    res = complete(gw)
    assert "https://" not in res.call.error and len(res.call.error) <= 300


# --------------------------------------------------------------------------------------- limiter
def test_rate_limiter_waits_when_window_is_full():
    script = Script(chat(json.dumps(GOOD)))
    gw, sleeps = gateway(script, calls_per_min=2)

    async def three():
        for _ in range(3):
            await gw.complete(role="pm", system="S", user="U", schema=PMDecision, seed=1,
                              num_predict=10, prompt_id="p", prompt_sha="s", input_hash="h")

    asyncio.run(three())
    assert sleeps == [60.0]
    assert len(script.requests) == 3


def test_rate_limiter_passes_under_limit():
    script = Script(chat(json.dumps(GOOD)))
    gw, sleeps = gateway(script, calls_per_min=20)
    complete(gw)
    complete(gw)
    assert sleeps == []


# ------------------------------------------------------------------------------ digest and tags
def test_model_digest_prefers_digest_field():
    def handler(request):
        assert request.url.path == "/api/show"
        assert json.loads(request.content) == {"model": MODEL}
        return httpx.Response(200, json={"digest": "6ca9e29c41de", "details": {}})

    gw = OllamaGateway("http://ollama.test", MODEL, transport=httpx.MockTransport(handler))
    assert asyncio.run(gw.model_digest()) == "6ca9e29c41de"


def test_model_digest_hashes_show_payload_and_ignores_modified_at():
    def make(modified):
        def handler(request):
            return httpx.Response(200, json={"modelfile": "FROM x", "modified_at": modified})
        return OllamaGateway("http://o.test", MODEL, transport=httpx.MockTransport(handler))

    a = asyncio.run(make("2026-01-01").model_digest())
    b = asyncio.run(make("2026-02-01").model_digest())
    assert len(a) == 64 and a == b


def test_model_digest_failure_is_empty():
    gw = OllamaGateway("http://o.test", MODEL,
                       transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert asyncio.run(gw.model_digest()) == ""
    gw = OllamaGateway("http://o.test", MODEL,
                       transport=httpx.MockTransport(Script(httpx.ConnectError("x"))))
    assert asyncio.run(gw.model_digest()) == ""


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([MODEL], True),
        (["deepseek-v4-flash"], False),         # missing the :cloud suffix is NOT a match
        (["deepseek-v4-flash:cloud-2"], False),
        ([], False),
    ],
)
def test_verify_model_exact_name(names, expected):
    payload = {"models": [{"name": n, "model": n} for n in names]}
    gw = OllamaGateway("http://o.test", MODEL,
                       transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    assert asyncio.run(gw.verify_model()) is expected


def test_verify_model_failure_is_false():
    gw = OllamaGateway("http://o.test", MODEL,
                       transport=httpx.MockTransport(Script(httpx.ConnectError("x"))))
    assert asyncio.run(gw.verify_model()) is False


# ------------------------------------------------------------------------------------- decoding
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('<think>{"a": 0}</think>{"a": 2}', {"a": 2}),
        ('first {"a": 1} then {"a": {"b": 2}} end', {"a": {"b": 2}}),
        ('broken {"a": } and {"c": 3}', {"c": 3}),
        ("[1, 2]", None),
        ("no json", None),
        ("", None),
    ],
)
def test_parse_json_object(text, expected):
    assert parse_json_object(text) == expected


def test_decode_reply_is_strict():
    bad = {**GOOD, "deviations": [{"symbol": "NDX", "level": "0.5", "direction": "cut",
                                   "evidence_ids": ["F:NDX:trend"], "reason": "x"}]}
    parsed, errors = decode_reply(json.dumps(bad), PMDecision)
    assert parsed is None and any("deviations.0.level" in e for e in errors)
    parsed, errors = decode_reply(json.dumps(GOOD), PMDecision)
    assert parsed is not None and errors == []


def test_correction_message_lists_errors():
    msg = correction_message([f"e{i}" for i in range(20)])
    assert "- e0" in msg and "- e11" in msg and "- e12" not in msg and "8 more" in msg


def test_input_hash_depends_on_both_parts():
    assert input_hash_of("a", "b") != input_hash_of("ab", "")
    assert len(input_hash_of("a", "b")) == 64
