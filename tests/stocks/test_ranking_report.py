"""The public ranking document (design §4.4, D18, docs/data-rights.md): SEC-derived percentages only,
the rule in words from the rank's own config, sources with attribution, counts and exclusions, per
sector the top names with their roles; it passes `assert_public_safe` and the leak scan, and refuses
the operator's private values (NAV, mirror figures, COUNCIL_LEAK_CANARIES)."""

from __future__ import annotations

import re
from datetime import date

import pytest

from council import paths
from council.operator.mirror import set_mirror
from council.publish.leakscan import scan
from council.reference.report import UnsafePublicText, assert_public_safe
from council.stocks import commands, report
from council.stocks.rank import RankConfig, rank
from tests.stocks import cli_support as cs


@pytest.fixture(scope="module")
def result():
    return rank(cs.D, cs.sector_book().inputs(), RankConfig())


def render(result, **kw):
    roles = {**{k: "selected" for k in result.selected}, **{k: "shortlist" for k in result.shortlist}}
    args = {"quarter": cs.Q, "roles": roles, "counts": {"in": 8, "out": 0, "retiring": 0, "pruned": 0},
            "sources": [report.mediawiki_source("List of S&P 500 companies", cs.D)], "rank_config_sha256": "e" * 64,
            "rule_cell": "SQ-8", "sector_cap": 3, "replaced": 0, "generated": date(2026, 8, 20)}
    return report.render(result, **{**args, **kw})


def test_the_document_is_percent_only_and_public_safe(result):
    doc = render(result)
    assert_public_safe(doc)
    assert scan(doc) == []
    assert "sector score, sector quotas by largest remainder with each at most 3, 8 names" in doc
    assert "not investment advice" in doc and "recorded override" in doc
    assert 'Wikipedia, "List of S&P 500 companies", revision of 2026-08-20, CC BY-SA 4.0' in doc
    assert "SEC EDGAR (US public domain)" in doc and "Kenneth R. French" in doc
    assert "| none | 0 |" in doc                                           # no exclusion in this book
    for sector in ("BusEq", "Hlth", "Shops"):
        assert f"### {sector} (" in doc
    rows = [line for line in doc.splitlines() if re.match(r"^\| \d+ \| ", line)]
    assert len(rows) == 15                                                 # the top 5 of each sector
    assert all(("selected" in r) or ("shortlist" in r) or ("|  |" in r) for r in rows)
    for private in ("revenue_L", "1000000000", "e+09", "cik", "latest_available_at"):
        assert private not in doc


def test_the_rule_text_follows_the_config():
    assert report.rule_text(RankConfig()).startswith("sector score, sector quotas")
    cap = RankConfig(variant={"score": "global", "constraint": "cap", "cap": 2}, n=10)
    assert report.rule_text(cap) == ("overall score, at most 2 names per sector, 10 names at equal weight, held names "
                                     "kept while inside the top 2 times 10")


def test_a_private_value_in_the_document_is_refused(result):
    doc = render(result)
    value = re.search(r"\| (\d+\.\d)% \|", doc).group(1)                   # a figure the document shows
    with pytest.raises(UnsafePublicText):
        render(result, canaries=[value])
    for bad in ("costs $12", "see /Users/someone/x", "mail me@example.org"):
        with pytest.raises(UnsafePublicText):
            report.check_public(bad)


def test_the_private_canaries_are_the_nav_the_mirror_and_the_environment(monkeypatch):
    state = paths.state_dir()
    assert commands.private_canaries(state, None) == []
    assert commands.private_canaries(state, 999.0) == []                  # small numbers would match counts
    set_mirror(state, funding_usd=2_500.0, virtual_nav_usd=12_000.0)
    monkeypatch.setenv("COUNCIL_LEAK_CANARIES", "sealed-word")
    assert commands.private_canaries(state, 11_000.0) == [11_000.0, 2_500.0, 12_000.0, "sealed-word"]


def test_the_ai_list_is_named_only_when_it_was_ranked(result):
    assert "plus the AI-adjacent list ranked by the same rule" in render(result)
    without = render(result, ai_list=False)
    assert "- Universe: S&P 500 and Nasdaq-100 members." in without and "AI-adjacent" not in without
