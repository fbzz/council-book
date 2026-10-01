"""The record page, "How it's doing, and what went wrong" (scoreboard first): the paper headline
with its sample size, the controls (table below 10 daily points, an inline SVG from 10), the idea
funnel and the Skeptic's health over every paper decision, then the honesty log. No JavaScript, the
fixed CSP, PAPER badged, percent only. Synthetic fixtures, plus one build of the repo's own journal."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from tests.publish.test_site_paper import (  # noqa: F401 - fixtures
    CSP,
    _load_site,
    _text,
    built,
    built_book,
)
from tests.swing.test_paper_publish import published  # noqa: F401 - the fixture


def _record(pages: dict[str, str]) -> tuple[str, str]:
    html = pages["record.html"]
    return html, _text(html)


def test_record_page_is_scoreboard_first_with_the_fixed_csp(built):  # noqa: F811
    _, _, pages = built
    html, t = _record(pages)
    assert f'content="{CSP}"' in html and "<script" not in html.lower() and 'style="' not in html
    assert "How it's doing, and what went wrong" in t
    assert "PAPER" in html and "far too early to judge anything" in t
    assert re.search(r"n = \d+ decisions?: far too early", t)
    # order: scoreboard, controls, funnel, Skeptic, then the honesty log
    keys = ['id="score"', 'id="controls"', 'id="funnel"', 'id="skeptic"', 'id="incidents"', 'id="smaller"',
            'id="withdrawn"', 'id="disclaimer"']
    assert [html.index(k) for k in keys] == sorted(html.index(k) for k in keys)
    assert "nothing has run for real yet" not in t


def test_metrics_below_their_minimum_are_greyed_with_n_needed(built):  # noqa: F811
    _, _, pages = built
    html, t = _record(pages)
    assert "st-thin" in html and "n = 0 of 20 needed" in t


def test_funnel_counts_where_ideas_really_stopped_and_links_them(built):  # noqa: F811
    _, _, pages = built
    html, t = _record(pages)
    assert "Ideas pitched" in t and "Past the Skeptic" in t and "Passed the rules · entered" in t
    assert 'class="rf-bar"' in html and re.search(r'rf-seg rf-[a-z]+ gw-\d+', html)
    assert re.search(r'href="decisions/\d+/index.html#sw-idea-\d+"', html)


def test_skeptic_health_shows_shares_and_unusable_replies(built):  # noqa: F811
    _, _, pages = built
    _, t = _record(pages)
    assert "Skeptic health" in t and "Unusable replies" in t and "Skeptic calls" in t


def test_return_splits_into_market_and_costs_and_controls_wait_for_data(built_book):  # noqa: F811
    _, pages = built_book
    html, t = _record(pages)
    assert "market" in t and "costs" in t and "Paper return since start" in t
    assert "Paper book (PAPER)" in t and "of 10 daily points" in t           # a small table, not a chart
    ctl = html[html.index('id="controls"'):html.index('id="funnel"')]
    assert "<polyline" not in ctl and "rec-controls" in ctl
    assert "$" not in html and not re.search(r"\b(usd|units)\b", t, re.I)


def test_controls_chart_from_ten_daily_points():
    site = _load_site()
    start = datetime(2026, 10, 1, 18, 40, tzinfo=UTC)
    books = {n: SimpleNamespace(as_of=start + timedelta(days=n), book=SimpleNamespace(paper_return_pct=0.5 * n))
             for n in range(1, 12)}
    view = SimpleNamespace(paper_books=books, paper_latest=None, swing=None)
    c = site.record_controls(view)
    assert c["n"] == 11 and c["chart"] is not None and c["chart_narrow"] is not None
    assert [s["key"] for s in c["chart"]["series"]] == ["paper"]
    few = site.record_controls(SimpleNamespace(paper_books=dict(list(books.items())[:3]), paper_latest=None, swing=None))
    assert few["chart"] is None and len(few["rows"]) == 3


def test_empty_record_has_honest_empty_states(tmp_path):
    site = _load_site()
    journal = tmp_path / "journal"
    journal.mkdir()
    dest = tmp_path / "site"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, dest)
    t = _text((dest / "record.html").read_text())
    assert "No paper decision yet" in t and "Nothing to compare yet" in t and "No swing idea yet" in t


def test_repo_journal_record_lists_incidents_and_smaller_problems(tmp_path):
    site = _load_site()
    dest = tmp_path / "site"
    site.build(REPO_ROOT / "journal", PROMPTS_DIR, POLICY_DIR, dest)   # the build runs the leak scan
    html = (dest / "record.html").read_text()
    t = _text(html)
    assert "started 47 min late" in t                                       # the 25 Sep run, a minor row
    assert "GLM" in t and "DeepSeek" in t and "billing" in t               # the model swap and the outage
    assert "INC-0001" in t and "INC-0003" in t
    assert "$" not in html and "/Users/" not in html
