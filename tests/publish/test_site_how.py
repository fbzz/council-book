"""The "How it works" page: two books, one portfolio. The split diagram (accessible inline SVG with a
text twin), the core and swing flows (the decision page's journey steps), the swing seats, the
folded rosters, the glossary linking rule anchors; no JavaScript, the fixed CSP, nothing in money
terms. Built from an empty journal: no LLM or network call."""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import UTC, datetime

import pytest

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish import leakscan

SITE_BUILD = REPO_ROOT / "site" / "build.py"
NOW = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
CSP = ("default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "script-src 'none'; base-uri 'none'; form-action 'none'")


@pytest.fixture(scope="module")
def how(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("council_site_build_how", SITE_BUILD)
    site = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build_how"] = site
    spec.loader.exec_module(site)
    root = tmp_path_factory.mktemp("how_site")
    (root / "journal").mkdir()
    out = root / "out"
    site.build(root / "journal", PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    return out, (out / "how.html").read_text(), (out / "rules.html").read_text()


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_how_page_is_csp_safe_and_scriptless(how):
    out, html, _ = how
    assert f'content="{CSP}"' in html
    assert "<script" not in html and " style=" not in html and "onclick" not in html
    assert leakscan.scan_paths([out]) == []


def test_split_diagram_is_accessible_with_a_text_twin(how):
    _, html, _ = how
    svg = re.search(r'<svg class="split-svg"[^>]*>', html).group(0)
    assert 'role="img"' in svg and 'aria-labelledby="split-t split-d"' in svg
    assert '<title id="split-t">' in html and '<desc id="split-d">' in html
    text = _text(html)
    for words in ("Two books, one portfolio.", "Core book", "Swing book", "0–50% of the portfolio",
                  "02:40, 06:40, 10:40, 14:40, 18:40, 22:40 UTC", "LSE hours", "invested in the core",
                  "Paper for now: real data, real agents, nothing traded."):
        assert words in text, words
    assert "PAPER</span>" in html


def test_two_parallel_flows_reuse_the_journey_steps(how):
    _, html, _ = how
    core = re.search(r'<ol class="jp jp-how jp-n7".*?</ol>', html, re.S).group(0)
    swing = re.search(r'<ol class="jp jp-how jp-n8".*?</ol>', html, re.S).group(0)
    labels = lambda block: re.findall(r'<span class="jp-label">([^<]+)</span>', block)  # noqa: E731
    assert labels(core) == ["Data", "News · macro", "Bull · bear", "Manager ×3", "Risk rules", "Plan", "You approve"]
    assert labels(swing) == ["Screen", "Scout", "Gate", "Skeptic", "Debate", "PM ×3", "Rules", "You approve"]
    for seat in ("scout", "risk", "skeptic", "bull", "pm", "human"):     # the decision page's seat colours
        assert f"accent-{seat}" in swing
    assert swing.count("Dies here:") == 6
    text = _text(html)
    assert "Day-2 confirmation:" in text and "1.25% of the position per leg" in text
    assert html.count('class="jp jp-how') == 2                          # the flow appears once per book


def test_seats_rosters_and_glossary(how):
    _, html, rules = how
    seats = re.search(r'<ol class="swing-seats".*?</ol>', html, re.S).group(0)
    for slug, name in (("scout", "Scout"), ("skeptic", "Skeptic"), ("bull", "Bull · swing"),
                       ("bear", "Bear · swing"), ("pm", "Manager · swing")):
        assert f'class="accent-{slug}"' in seats and name in seats
    roster = re.search(r'<details class="jd rule-more" id="roster">.*?</details>', html, re.S).group(0)
    assert "Full roster and prompt hashes" in roster
    for words in ("Core · language models", "Swing · language models", "Portfolio manager", "Manager · swing",
                  "council-scout/", "council-skeptic/", "deepseek-v4.1-flash:cloud", "Code gate", "Human operator"):
        assert words in roster, words
    for term in ("Medoid", "Deadband", "Anti-chase", "Net-of-cost gate", "Commitment and salt", "Catastrophe stop"):
        assert f"<dt>{term}</dt>" in html, term
    for anchor in set(re.findall(r'href="rules\.html#([A-Za-z0-9-]+)"', html)):
        assert f'id="{anchor}"' in rules, anchor                         # every rule link lands on a card
    assert "0.22 R" in _text(html) and "#7-honest-power-statement" in html


def test_percent_only(how):
    _, html, _ = how
    text = _text(html)
    assert "$" not in text and "/Users" not in html
    assert not re.search(r"\b\d+(\.\d+)?\s?(USD|EUR|GBP)\b", text)


def test_prereg_names_the_skeptics_model_consistently():
    doc = (REPO_ROOT / "docs" / "swing-book-prereg.md").read_text()
    section1 = doc.split("## 1.")[1].split("## 2.")[0]
    assert "another model" not in section1.replace("\n", " ")
    assert "deepseek-v4.1-flash:cloud" in doc
