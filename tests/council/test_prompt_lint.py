"""PROMPT LINT: no performance or backtest claims in any prompt (raw files and rendered text).

AGENTS.md rule 6: prompts carry no backtest claims. The lab's prompts quoted results ("turned ...
into ...", Sharpe, out-of-sample scores); the council's prompts must not."""

from __future__ import annotations

import re

import pytest

from council.deliberation.common import prompt_context
from council.paths import PROMPTS_DIR

FORBIDDEN = [
    (re.compile(r"backtest", re.I), "backtest"),
    (re.compile(r"sharpe", re.I), "Sharpe"),
    (re.compile(r"\bOOS\b"), "OOS"),
    (re.compile(r"out[- ]of[- ]sample", re.I), "out-of-sample"),
    (re.compile(r"validated", re.I), "validated"),
    (re.compile(r"\$\s*\d"), "dollar amount"),
    (re.compile(r"turned\s+\$", re.I), "turned $"),
    (re.compile(r"[€£]\s*\d"), "currency amount"),
]


def lint(text: str) -> list[str]:
    return [label for pattern, label in FORBIDDEN if pattern.search(text)]


def prompt_files():
    files = sorted(PROMPTS_DIR.glob("*.md"))
    assert files, "no prompts found"
    return files


@pytest.mark.parametrize("path", prompt_files(), ids=lambda p: p.name)
def test_raw_prompt_files_are_clean(path):
    assert lint(path.read_text(encoding="utf-8")) == []


def test_rendered_prompts_are_clean(reg, policy):
    ctx = prompt_context(policy)
    for role in reg.roles():
        assert lint(reg.render(role, **ctx)) == [], role


@pytest.mark.parametrize(
    "bad",
    [
        "This rule was backtested since 2018.",
        "It has a Sharpe of 1.2.",
        "OOS Spearman +0.66",
        "walk-forward validated",
        "turned " + chr(36) + "1,000 into more",
        "worth " + chr(36) + "18,500 today",
    ],
)
def test_lint_catches_performance_claims(bad):
    assert lint(bad) != []


@pytest.mark.parametrize("ok", ["choose the reference", "costs 5 bps per side", "S&P 500 line"])
def test_lint_allows_ordinary_text(ok):
    assert lint(ok) == []
