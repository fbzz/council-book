"""Broker data is recognised under the labels the live pipeline really gives it.

`facts.features` labels a state's history `"<source>:<ticker>"` ("etoro:OIL", "tiingo:SPY") and the
market facts of that state carry the same label; a cost fact a broker what-if took part in says
`costs:whatif`. The capture must hold every such value apart as eToro Licensed Content (7-day
retention, `council purge-licensed`), not only the bare labels the golden fixtures use. The rendered
text must not change (the golden tests pin it); only the licence metadata does.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from council.deliberation import desk as live
from council.deliberation.capture import InputSink
from council.models.facts import Fact

from .golden.cases import build
from .golden.make_golden import code_cards, desk_inputs


def _items(sections, **match):
    return [it for s in sections for it in s.items if all(getattr(it, k) == v for k, v in match.items())]


def _labelled(case):
    """The case with production labels: `etoro:<line>` where the fixture wrote `etoro`,
    `tiingo:<line>` elsewhere, and market facts carrying their state's label."""
    states = {s: st.model_copy(update={"history_source": f"{'etoro' if st.history_source == 'etoro' else 'tiingo'}:{s}"})
              for s, st in case.pack.states.items()}
    facts = [f.model_copy(update={"source": states[f.symbol].history_source})
             if f.symbol in states and f.id[:2] in ("F:", "V:") else f for f in case.pack.facts]
    return case.pack.model_copy(update={"states": states, "facts": facts})


@pytest.mark.parametrize("label, broker", [
    ("etoro", True), ("etoro:OIL", True), ("ETORO:oil", True), ("broker_quote", True), ("broker", True),
    ("etoro_feed", True), ("costs:whatif", True), ("costs", True),
    ("tiingo", False), ("tiingo:SPY", False), ("binance:BTCUSDT", False), ("costs:floor", False),
    ("policy_calendar", False), ("sec_estimate", False), ("", False), (None, False),
])
def test_broker_labels_are_recognised_with_or_without_a_ticker(label, broker):
    assert live.is_broker_source(label) is broker


def test_labelled_broker_candles_are_held_as_licensed_and_the_text_is_unchanged(policy):
    case = build("broker_candles")
    before = live.desk_sections(cards=code_cards(case, policy), **desk_inputs(case, policy))
    pack = _labelled(case)
    inputs = desk_inputs(case, policy) | {"pack": pack}
    after = live.desk_sections(cards=code_cards(case, policy), **inputs)
    assert [s.text() for s in after] == [s.text() for s in before]

    semis = _items(after, line="SEMIS", field="vs_sma50")[0]
    assert semis.sources == ("etoro:SEMIS",) and semis.licence == "broker_licensed"
    assert _items(after, line="NDX", field="vs_sma50")[0].licence == "public"
    fact = _items(after, kind="fact", ref="F:SEMIS:trend")[0]
    assert fact.sources == ("etoro:SEMIS",) and fact.licence == "broker_licensed"
    assert _items(after, kind="fact", ref="F:NDX:trend")[0].licence == "public"

    sink = InputSink(salt=lambda: "00" * 32)
    for section in after:
        sink.register(section)
    captured, licensed = sink.build(cycle_id=pack.cycle_id, captured_at=datetime(2026, 10, 1, 15, tzinfo=UTC))
    assert licensed is not None
    held = [t for texts in licensed.texts.values() for t in texts.values()]
    assert semis.text in held and "F:SEMIS:trend=up" in held
    lines = next(s for k, s in captured.sections.items() if k.endswith("lines"))
    kept = [it.text for i, it in enumerate(lines.items) if i not in lines.licensed]
    assert "F:SEMIS:trend=up" not in kept and "F:NDX:trend=up" in kept


def test_a_cost_fact_priced_with_a_broker_what_if_is_licensed():
    at = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)

    def cost(source: str) -> Fact:
        return Fact(id="C:NDX:per_side_bps", kind="cost", symbol="NDX", value=6.1, unit="bps",
                    available_at=at, source=source)

    assert live.fact_licence(cost("costs:whatif")) == "broker_licensed"
    assert live.fact_licence(cost("costs")) == "broker_licensed"          # unknown pricing: strictest
    assert live.fact_licence(cost("costs:floor")) == "public"
