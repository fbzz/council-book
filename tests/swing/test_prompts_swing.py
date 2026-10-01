"""SW-3: swing prompts render, their examples satisfy the schemas, no fee number (D17), and the
Skeptic's rendered input is blind to the thesis, why_not_priced_in, the setup and the Scout's levels."""

from __future__ import annotations

import json
import re

import pytest

from council.deliberation.common import prompt_context
from council.llm.prompts import PromptRegistry
from council.paths import PROMPTS_DIR
from council.swing.council import SWING_ROLES, skeptic_input, swing_prompt_context
from council.swing.models import (
    ScoutOutput,
    SkepticVerdict,
    SwingBearCase,
    SwingCase,
    SwingPMDecision,
)
from council.swing.roles import SwingIdea, catalyst_index
from tests.swing import stubs as s

SCHEMAS = {"scout": ScoutOutput, "skeptic": SkepticVerdict, "swing_bull": SwingCase,
           "swing_bear": SwingBearCase, "swing_pm": SwingPMDecision}
SETUPS = ("news_continuation", "post_earnings_drift", "second_order", "gap_fade", "breakout",
          "mean_reversion", "event_run_up")


@pytest.fixture
def reg():
    return PromptRegistry()


@pytest.fixture
def ctx(policy):
    return {**prompt_context(policy), **swing_prompt_context(policy)}


def test_swing_roles_render_with_the_swing_brief(reg, ctx):
    for role in SWING_ROLES:
        text = reg.render(role, **ctx)
        assert "THE SWING BOOK" in text and "ONE JSON object" in text and "JSON FIELDS" in text
        assert "THE BOOK\n" not in text                    # not the core desk brief
        assert "{{" not in text and "{%" not in text
        assert reg.prompt_id(role) == f"council-{role}/v{ {'scout': 2, 'skeptic': 3}.get(role, 1)}"


def test_examples_satisfy_the_schemas(reg, ctx):
    for role, schema in SCHEMAS.items():
        lines = reg.render(role, **ctx).splitlines()
        examples = [lines[i + 1] for i, ln in enumerate(lines) if ln.startswith("EXAMPLE")]
        assert examples, role
        for ex in examples:
            schema.model_validate(json.loads(ex), strict=True)


FEE_NUMBER = [
    re.compile(r"\d\s*(bps|basis point)", re.I),
    re.compile(r"[$€£]\s*\d"),
    re.compile(r"(fee|spread|carry|commission)[^.\n]{0,40}\d", re.I),
    re.compile(r"\d[^.\n]{0,20}(fee|spread|commission)", re.I),
]


@pytest.mark.parametrize("path", sorted(PROMPTS_DIR.glob("*.md")), ids=lambda p: p.name)
def test_no_fee_number_in_swing_prompts(path, reg, ctx):
    name = path.stem
    if name not in (*SWING_ROLES, "_swing_brief"):
        pytest.skip("core prompt")
    texts = [path.read_text(encoding="utf-8")]
    if not name.startswith("_"):
        texts.append(reg.render(name, **ctx))
    for text in texts:
        for pat in FEE_NUMBER:
            assert not pat.search(text), (name, pat.pattern, pat.search(text))


def test_fee_lint_catches_a_fee_number():
    assert any(p.search("the fee is 1 per trade") for p in FEE_NUMBER)
    assert any(p.search("costs 15 bps") for p in FEE_NUMBER)


def test_skeptic_rendered_input_is_blind(reg, ctx):
    idea = ScoutOutput.model_validate(s.scout(s.idea(setup="post_earnings_drift"))).ideas[0]
    swing = SwingIdea(ref="idea:1", idea=idea, line_id="ACME", card=s.card("ACME"))
    inputs = s.inputs(open_trades=[s.trade()])
    sections, admissible = skeptic_input(swing, catalysts=catalyst_index(s.reading(), slot=s.SLOT), inputs=inputs)
    user = "".join(sec.text() for sec in sections)
    whole = reg.render("skeptic", **ctx) + user
    for banned in (s.THESIS, "Zebra-quill", s.WHY_NOT, "Octarine", idea.invalidation,
                   "why_not_priced_in", "thesis:", "stop_pct", "target_pct", "time_stop"):
        assert banned not in whole, banned
    system = reg.render("skeptic", **ctx)
    for setup in SETUPS:
        assert setup not in user, setup
        if setup != "second_order":                        # also the name of one of its output fields
            assert setup not in system, setup
    for level in ("0.0733", "7.33", "0.1777", "17.77"):
        assert level not in whole, level
    assert f"time stop {s.TSTOP}" not in whole
    # what it must see
    assert "ACME" in user and "long" in user and idea.catalyst_claim in user
    assert "Results of Operations" in user and s.P_ACME in user and "X:ACME:rev_yoy" in user
    assert {s.P_ACME, "X:ACME:rev_yoy", "F:SPX:ret_5d"} <= admissible


def test_skeptic_input_has_no_money_amount():
    idea = ScoutOutput.model_validate(s.scout(s.idea())).ideas[0]
    swing = SwingIdea(ref="idea:1", idea=idea, line_id="ACME", card=s.card("ACME", adv_usd_20d=123456789.0))
    sections, admissible = skeptic_input(swing, catalysts=catalyst_index(s.reading(), slot=s.SLOT), inputs=s.inputs())
    user = "".join(sec.text() for sec in sections)
    assert "adv_usd_20d" not in user and "123456789" not in user and "X:ACME:adv_usd_20d" not in admissible
