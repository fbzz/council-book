"""The core news / macro JSON fix (2026-10-01) and the Ollama billing flag.

Real runs: `news` and `macro` were parse_fail on most cycles. The replies were valid JSON cut off at
`num_predict` (1200 / 700 tokens); the decoder then picked an INNER card object and reported its
schema errors ("cards: Field required"). Now: a truncated reply is reported as truncated, the
correction turn gets a larger budget and asks for fewer, shorter items, both roles always send their
schema as `format`, and the budgets are 3000 / 2000.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from council.llm.gateway import (
    BILLING_ERROR,
    TRUNCATED,
    TRUNCATION_BOOST,
    correction_message,
    is_billing_error,
    is_truncated,
)
from council.llm.stub import StubFailure, StubGateway
from council.models.cards import MacroAnalystOutput, NewsAnalystOutput

from .factories import stub_responses
from .test_council import Sleeper, council
from .test_gateway import GOOD, Script, chat, complete, gateway

CARD = {"scope": ["NDX"], "card_type": "news_context", "direction": "neutral", "claim": "Yields up.",
        "evidence_ids": ["M:DGS10@2026-09-30"], "horizon_days": 20, "falsifier": "Yields fall.", "novel": True}
NEWS = {"cards": [CARD, CARD]}
CUT = json.dumps(NEWS)[:-40]          # the outer object never closes; inner cards still decode


def test_a_cut_off_reply_is_truncated_not_an_inner_object():
    assert is_truncated(CUT) and is_truncated('{"cards": [{"claim": "unterminated')
    assert not is_truncated(json.dumps(NEWS)) and not is_truncated("prose {\"a\": 1}") and not is_truncated("")


def test_truncation_gets_a_bigger_correction_budget_and_a_shorter_ask():
    script = Script(chat(CUT, tokens=(100, 1200)), chat(json.dumps(NEWS), tokens=(100, 600)))
    gw, _ = gateway(script)
    res = complete(gw, role="news", schema=NewsAnalystOutput, num_predict=1200)
    assert res.call.status == "ok" and len(res.parsed.cards) == 2
    assert res.errors == (TRUNCATED,) and res.call.error == f"corrected: {TRUNCATED}"
    first, second = script.bodies()
    assert first["options"]["num_predict"] == 1200
    assert second["options"]["num_predict"] == int(1200 * TRUNCATION_BOOST)
    assert "FEWER and SHORTER" in second["messages"][-1]["content"]
    assert "Reply with the JSON object only" in second["messages"][-1]["content"]


def test_a_reply_that_used_the_whole_budget_counts_as_truncated():
    """Even when the decoder could find some object, a failed reply at the token limit is cut off."""
    script = Script(chat(CUT, tokens=(100, 700)), chat(CUT, tokens=(100, 1400)))
    gw, _ = gateway(script)
    res = complete(gw, role="macro", schema=MacroAnalystOutput, num_predict=700)
    assert res.call.status == "parse_fail"
    assert res.call.error == f"{TRUNCATED} | after correction: {TRUNCATED}"
    assert "Extra inputs" not in res.call.error


def test_news_and_macro_send_their_schema_as_format_other_roles_json():
    script = Script(chat(json.dumps(NEWS)), chat(json.dumps(GOOD)))
    gw, _ = gateway(script)
    assert complete(gw, role="news", schema=NewsAnalystOutput).call.status == "ok"
    assert complete(gw).call.status == "ok"                              # pm
    news_body, pm_body = script.bodies()
    assert isinstance(news_body["format"], dict) and "cards" in news_body["format"]["properties"]
    assert "$defs" not in json.dumps(news_body["format"])
    assert pm_body["format"] == "json"


def test_correction_message_without_truncation_has_no_shorter_ask():
    assert "FEWER" not in correction_message(["cards: Field required"])


# --------------------------------------------------------------------------------- billing
def test_ollama_403_is_a_billing_error_not_retried_and_alerts_once():
    codes: list[int] = []
    script = Script(chat("payment past due", status=403))
    gw, sleeps = gateway(script, on_billing_error=codes.append)
    res = complete(gw)
    assert res.call.status == "transport" and res.call.error == f"http 403 {BILLING_ERROR}"
    assert is_billing_error(res.call.error) and len(script.requests) == 1 and sleeps == []
    complete(gw)
    assert codes == [403] and gw.billing_error                            # one alert per gateway


def test_401_and_402_are_billing_errors_too_and_a_failing_hook_never_raises():
    for code in (401, 402):
        def boom(_c):
            raise RuntimeError("ntfy down")
        gw, _ = gateway(Script(chat("no", status=code)), on_billing_error=boom)
        assert complete(gw).call.error == f"http {code} {BILLING_ERROR}"


def test_other_4xx_stay_plain():
    gw, _ = gateway(Script(chat("bad", status=400)))
    assert complete(gw).call.error == "http 400"


def test_the_council_flags_a_billing_error_and_does_not_wait_for_it(reg, policy):
    down = StubFailure("transport", f"http 402 {BILLING_ERROR}")
    responses = {k: down for k in stub_responses()}
    sleeper = Sleeper()
    res = council(StubGateway(responses), reg, policy, sleep=sleeper)
    assert sleeper.calls == []                                         # no 3 x 120 s outage waits
    assert BILLING_ERROR in res.flags and "stage_unavailable:specialists" in res.flags
    assert res.basis == "council_unavailable"


def test_the_billing_alert_is_rate_limited(tmp_path, monkeypatch):
    from council import context
    from council.operator import notify

    sent: list[tuple[str, str, str]] = []

    class FakeNotifier:
        def __init__(self, topic, window=None):
            self.topic = topic

        def send(self, title, body, priority="default"):
            sent.append((title, body, priority))

    monkeypatch.setattr(notify, "Notifier", FakeNotifier)

    class S:
        ntfy_topic = "topic-for-tests-only"

    now = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
    assert context.alert_llm_billing(tmp_path, S(), 403, now=now)
    assert not context.alert_llm_billing(tmp_path, S(), 403, now=now + timedelta(hours=1))
    assert context.alert_llm_billing(tmp_path, S(), 402, now=now + timedelta(hours=5))
    assert [p for _t, _b, p in sent] == ["urgent", "urgent"] and "llm_billing_error" in sent[0][1]
    assert "HTTP 403" in sent[0][1]

    class NoTopic:
        ntfy_topic = None

    assert not context.alert_llm_billing(tmp_path / "x", NoTopic(), 403, now=now)
    assert context.billing_alert(S(), None) is None
