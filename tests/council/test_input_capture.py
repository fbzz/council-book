"""Private capture of every model call's exact input (transparency-v2 §2.2, T1)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import stat
from datetime import timedelta

import httpx
import pytest

from council import paths
from council.deliberation.capture import (
    CAPTURE_ERROR,
    InputSink,
    calls_path,
    commit,
    exact_messages,
    licensed_path,
    load_inputs,
    load_licensed,
    section_text,
    user_text,
    verify,
    write_cycle_inputs,
    write_inputs,
)
from council.deliberation.common import call_role, prompt_context
from council.deliberation.council import run_council
from council.deliberation.segments import literal
from council.llm.gateway import OllamaGateway, correction_message
from council.llm.stub import StubFailure, StubGateway, StubTurns
from council.models.cards import NewsAnalystOutput

from .factories import (
    REF_LEVELS,
    SLOT,
    build_bands,
    build_pack,
    build_ref,
    clip_enforce,
    news_reply,
    stub_responses,
)

CYCLE = "2026-10-01T1440Z"


async def _instant(_s: float) -> None:
    return None


def _council(gw, reg, policy, sink, **kw):
    lines = list(policy.universe.lines)
    hints = {ln.symbol: {"per_side_bps": 5.0, "carry_bps_day": 0.0} for ln in lines}
    return run_council(
        gw=gw, reg=reg, pack=build_pack(), ref=build_ref(), bands=build_bands(),
        current_levels={**REF_LEVELS, "OIL": -0.25}, cost_hints=hints, lines=lines, policy=policy,
        enforce=clip_enforce, now=SLOT, sleep=_instant, input_sink=sink, **kw,
    )


def _salts():
    counter = iter(range(10_000))
    return lambda: f"{next(counter):064x}"


# ----------------------------------------------------------------------------- a stub council
def test_every_call_is_captured_with_the_ledger_hash_and_the_exact_text(reg, policy):
    gw = StubGateway(stub_responses())
    sink = InputSink()
    res = asyncio.run(_council(gw, reg, policy, sink, run_macro=True))
    assert [c.call_key for c in sink.calls] == [
        "news:0:0", "macro:0:0", "bull_open:0:0", "bear:0:0", "bull_rebuttal:0:0",
        "pm:0:0", "pm:1:0", "pm:2:0", "single_agent:0:0", "single_agent:1:0", "single_agent:2:0",
    ]
    assert [c.input_hash for c in sink.calls] == [c.input_hash for c in res.calls]
    assert all(c.status == "ok" for c in sink.calls)
    # the section texts join to exactly what the gateway was sent
    assert [sink.user_of(i) for i in range(len(sink.calls))] == [e.user for e in gw.log]
    for call, entry in zip(sink.calls, gw.log, strict=True):
        assert call.system == entry.system
        text = "".join(sink.sections[k].text() for k in call.sections)
        assert text == entry.user
        assert hashlib.sha256(f"{call.system}\0{text}".encode()).hexdigest() == call.input_hash
    pm = [c for c in sink.calls if c.role == "pm"]
    assert len({tuple(c.sections) for c in pm}) == 1          # the same input for all three
    assert pm[0].sections[-3:] == ["sep.pm", "transcript", "tail.pm"]
    news = sink.calls[0]
    assert news.sections[-3:] == ["sep.news", "news_detail", "tail.news"]
    assert all(k.startswith("desk.code.") for k in news.sections[:-3])
    bear = next(c for c in sink.calls if c.role == "bear")
    assert bear.sections[-3:] == ["lit.bear.bull_opening", "case.bull_open.plain", "tail.bear"]
    assert all(k.startswith("desk.full.") for k in bear.sections[:-3])
    single = next(c for c in sink.calls if c.role == "single_agent")
    assert single.sections[-1] == "tail.single_agent"
    assert news.ctx["grid"] and news.prompt_id == res.calls[0].prompt_id


def test_the_sink_never_changes_the_council_result(reg, policy):
    a = asyncio.run(_council(StubGateway(stub_responses()), reg, policy, None)).model_dump_json()
    b = asyncio.run(_council(StubGateway(stub_responses()), reg, policy, InputSink())).model_dump_json()
    assert a == b


def test_a_stage_retry_is_captured_with_its_attempt(reg, policy):
    tries = {"n": 0}

    def news(user, replicate):
        tries["n"] += 1
        return StubFailure("transport", "down") if tries["n"] == 1 else news_reply()

    sink = InputSink()
    res = asyncio.run(_council(StubGateway({**stub_responses(), "news": news}), reg, policy, sink))
    keys = [c.call_key for c in sink.calls if c.role == "news"]
    assert keys == ["news:0:0", "news:0:1"]
    first, second = (c for c in sink.calls if c.role == "news")
    assert first.status == "transport" and second.status == "ok"
    assert first.sections == second.sections and first.input_hash == second.input_hash
    assert first.salt != second.salt and first.input_commit != second.input_commit
    assert "outage:specialists:retry1" in res.flags


def test_a_corrected_call_keeps_both_replies_the_errors_and_the_turn_as_sent(reg, policy):
    bad = "x" * 9000
    sink = InputSink()
    turns = StubTurns(first=bad, second=news_reply())
    asyncio.run(_council(StubGateway({**stub_responses(), "news": turns}), reg, policy, sink))
    news = sink.calls[0]
    assert news.status == "ok"
    assert news.replies == [bad, json.dumps(news_reply(), sort_keys=True)]
    assert news.errors == ["reply is not a JSON object"]
    assert news.correction == correction_message(["reply is not a JSON object"])
    assert news.sent_assistant == bad[:8000] and len(news.sent_assistant) == 8000
    inputs, _ = sink.build(cycle_id=CYCLE, captured_at=SLOT)
    msgs = exact_messages(inputs, inputs.calls[0], sink.build(cycle_id=CYCLE, captured_at=SLOT)[1])
    assert msgs is not None and len(msgs) == 4
    assert msgs[2] == {"role": "assistant", "content": bad[:8000]}
    assert msgs[3]["content"] == news.correction


def test_the_real_gateway_reports_what_it_sent_back():
    bodies: list[dict] = []
    replies = iter(["not json " * 1200, json.dumps({"cards": []})])

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": next(replies)}})

    gw = OllamaGateway("http://ollama.test", "m", transport=httpx.MockTransport(handler),
                       sleep=_instant, backoff_s=0)
    res = asyncio.run(gw.complete(role="news", system="S", user="U", schema=NewsAnalystOutput,
                                  seed=1, num_predict=10, prompt_id="p", prompt_sha="s",
                                  input_hash="h"))
    assert res.call.status == "ok" and len(res.turns) == 2
    assert res.sent_assistant == res.turns[0][:8000] and len(res.sent_assistant) == 8000
    assert res.errors == ("reply is not a JSON object",)
    assert bodies[1]["messages"][2] == {"role": "assistant", "content": res.sent_assistant}
    assert bodies[1]["messages"][3] == {"role": "user", "content": res.correction}
    assert res.raw == res.turns[1]


def test_a_cancelled_council_keeps_the_inputs_of_calls_that_started(reg, policy):
    class HangingBear(StubGateway):
        async def complete(self, **kw):
            if kw["role"] == "bear":
                await asyncio.sleep(60)
            return await super().complete(**kw)

    sink = InputSink()

    async def go():
        await asyncio.wait_for(_council(HangingBear(stub_responses()), reg, policy, sink), 0.5)

    with pytest.raises(TimeoutError):
        asyncio.run(go())
    assert [(c.call_key, c.status) for c in sink.calls] == [
        ("news:0:0", "ok"), ("bull_open:0:0", "ok"), ("bear:0:0", "sent"),
    ]
    assert sink.calls[-1].input_hash and sink.calls[-1].replies == []


def test_a_capture_failure_never_stops_the_council(reg, policy, tmp_path):
    class Broken(InputSink):
        def register(self, section):
            raise RuntimeError("boom")

    sink = Broken()
    res = asyncio.run(_council(StubGateway(stub_responses()), reg, policy, sink))
    base = asyncio.run(_council(StubGateway(stub_responses()), reg, policy, None))
    assert res.model_dump_json() == base.model_dump_json()
    assert sink.flags == [f"{CAPTURE_ERROR}:RuntimeError"] and sink.calls == []
    assert write_cycle_inputs(tmp_path, sink, cycle_id=CYCLE, captured_at=SLOT) == sink.flags


# ------------------------------------------------------------------------------- the files
def test_the_capture_is_private_0600_under_the_state_dir(reg, policy):
    sink = InputSink()
    asyncio.run(_council(StubGateway(stub_responses()), reg, policy, sink))
    root = paths.state_dir()
    assert write_cycle_inputs(root, sink, cycle_id=CYCLE, captured_at=SLOT) == []
    main, lic = calls_path(root, CYCLE), licensed_path(root, CYCLE)
    assert main == root / "calls" / "2026" / "10" / f"{CYCLE}.json.gz"
    assert lic == root / "licensed" / "calls" / "2026" / "10" / f"{CYCLE}.json.gz"
    for path in (main, lic):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert paths.REPO_ROOT not in path.resolve().parents
    for folder in (root / "calls", main.parent, lic.parent):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    inputs = load_inputs(root, CYCLE)
    # two headlines in each desk variant, two titles and two summaries in news_detail
    assert len(inputs.calls) == len(sink.calls) and inputs.licensed_items == 8


def test_the_writer_refuses_a_path_inside_the_repository():
    inputs, lic = InputSink().build(cycle_id=CYCLE, captured_at=SLOT)
    target = paths.REPO_ROOT / "journal" / "never-created"
    with pytest.raises(RuntimeError, match="inside the public repo"):
        write_inputs(target, inputs, lic)
    assert not target.exists()


def test_broker_licensed_text_is_held_apart_from_the_main_capture(reg, policy):
    sink = InputSink()
    asyncio.run(_council(StubGateway(stub_responses()), reg, policy, sink))
    root = paths.state_dir()
    write_cycle_inputs(root, sink, cycle_id=CYCLE, captured_at=SLOT)
    main_bytes = gzip.decompress(calls_path(root, CYCLE).read_bytes()).decode()
    assert "Chip export limits announced" not in main_bytes
    assert "Officials outlined new limits" not in main_bytes
    lic = load_licensed(root, CYCLE)
    assert lic is not None
    held = [t for texts in lic.texts.values() for t in texts.values()]
    assert "Chip export limits announced for advanced parts" in held
    inputs = load_inputs(root, CYCLE)
    for call, entry in zip(inputs.calls, sink.calls, strict=True):
        text, complete = user_text(inputs, call, lic)
        assert complete and text == sink.user_of(sink.calls.index(entry))
    # without the licensed file the text is not rebuildable, but the commits still verify nothing wrong
    problems, notes = verify(inputs, None)
    assert problems == [] and any("purged" in n for n in notes)


def test_commits_round_trip_and_verify_catches_tampering(reg, policy):
    sink = InputSink(salt=_salts())
    res = asyncio.run(_council(StubGateway(stub_responses()), reg, policy, sink))
    inputs, lic = sink.build(cycle_id=CYCLE, captured_at=SLOT)
    for key, sec in inputs.sections.items():
        text, _ = section_text(sec, lic)
        assert sec.commit == hashlib.sha256(bytes.fromhex(sec.salt) + text.encode()).hexdigest()
        assert sec.commit != commit("ff" * 32, text), key
    assert verify(inputs, lic, [c.model_dump() for c in res.calls]) == ([], [])
    key = "desk.full.lines"
    sec = inputs.sections[key]
    items = list(sec.items)
    i = next(n for n, it in enumerate(items) if it.field == "vs_sma50")
    items[i] = items[i].model_copy(update={"text": "+9.9%"})
    tampered = inputs.model_copy(update={"sections": {**inputs.sections, key: sec.model_copy(update={"items": items})}})
    problems, _ = verify(tampered, lic)
    assert f"section {key}: sha256 mismatch" in problems
    assert any("input hash mismatch" in p for p in problems)
    stray = [{"role": "pm", "replicate": 0, "input_hash": "0" * 64}]
    assert "ledger call pm:0: input not captured" in verify(inputs, lic, stray)[0]


def test_a_key_that_comes_back_with_other_content_is_stored_apart():
    sink = InputSink()
    assert sink.register(literal("case.bull_open.plain", "one")) == "case.bull_open.plain"
    assert sink.register(literal("case.bull_open.plain", "one")) == "case.bull_open.plain"
    assert sink.register(literal("case.bull_open.plain", "two")) == "case.bull_open.plain#2"
    assert sink.sections["case.bull_open.plain#2"].text() == "two"


def test_nothing_reads_a_clock_the_caller_stamps_the_capture():
    inputs, _ = InputSink().build(cycle_id=CYCLE, captured_at=SLOT + timedelta(minutes=3))
    assert inputs.captured_at == SLOT + timedelta(minutes=3)
    assert os.environ.get("COUNCIL_STATE_DIR")


# ------------------------------------------------------------------------- transport retries
def test_timeout_ladder_retries_are_recorded_with_the_call(reg, policy):
    """A failed HTTP attempt that resent the same messages is part of the call's record."""
    statuses = iter([503, 200, 200])
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        code = next(statuses)
        if code != 200:
            return httpx.Response(code, json={})
        return httpx.Response(200, json={"message": {"content": json.dumps({"cards": []})}})

    gw = OllamaGateway("http://ollama.test", "m", transport=httpx.MockTransport(handler),
                       sleep=_instant, backoff_s=0)
    sink = InputSink()
    res = asyncio.run(call_role(gw, reg, role="news", ctx=prompt_context(policy), schema=NewsAnalystOutput,
                                seed=1, num_predict=10, sections=[literal("tail.news", "U")],
                                sink=sink))
    assert res.call.status == "ok" and res.retries == ("first 1: http 503",)
    assert bodies[0] == bodies[1]                           # the retry resent the same messages
    assert sink.calls[0].retries == ["first 1: http 503"] and sink.calls[0].status == "ok"


def test_a_stub_result_has_no_transport_retries(reg, policy):
    sink = InputSink()
    asyncio.run(_council(StubGateway(stub_responses()), reg, policy, sink))
    assert all(c.retries == [] for c in sink.calls)


# ------------------------------------------------------------------------ evidence licences
def test_evidence_licences_include_the_news_items_a_card_cites():
    from council.deliberation.desk import evidence_sources

    pack = build_pack()
    news_id = pack.news[0].id
    sources, licence = evidence_sources(pack, [news_id])
    assert sources == ("broker_feed",) and licence == "broker_licensed"
    fact = pack.facts[0]
    assert evidence_sources(pack, [fact.id]) == ((fact.source,), "public")
    assert evidence_sources(pack, [fact.id, news_id])[1] == "broker_licensed"   # the strictest wins


# ---------------------------------------------------------------------------- hook boundary
def test_the_modules_the_runner_hooks_import_never_load_the_writer_or_keychain():
    """cycle.py (stage 2) imports capture, reading and purge: none may pull in the broker writer
    or the Keychain (the unattended runner never imports the writer)."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import council.deliberation.capture, council.deliberation.reading, council.deliberation.segments\n"
        "import council.operator.purge, council.models.inputs\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('council.broker.etoro_write', "
        "'council.operator.keychain', 'council.operator.approve')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
