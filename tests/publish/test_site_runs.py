"""The Runs page (cycles.html): one merged list of every council meeting (paper, rehearsal, live),
newest first, as compact cards with plain labels, the council's health folded into one line, and
links to each meeting's decision page or transcript. Synthetic fixtures only."""

from __future__ import annotations

import re

from council.publish import leakscan
from tests.publish.test_site_paper import CSP, _text, built  # noqa: F401 - the fixture
from tests.swing.test_paper_publish import published  # noqa: F401 - the fixture


def _cards(html: str) -> list[str]:
    start = html.index('class="mtg-list"')
    body = html[start:html.index("</ol>", start)]
    return re.findall(r'<li class="mtg" id="[^"]+">(.*?)\n</li>', body, re.S)


def test_one_merged_list_newest_first(built):  # noqa: F811
    _, _, pages = built
    html = pages["cycles.html"]
    assert f'content="{CSP}"' in html and "<script" not in html.lower()
    cards = _cards(html)
    kinds = [re.search(r">(PAPER|LIVE|REHEARSAL)<", c).group(1) for c in cards]
    assert kinds.count("PAPER") == 2 and len(cards) >= 3
    assert cards[0].index("Decision #2") and "Decision #1" in cards[1]          # newest first
    assert "meetings so far: 2 paper" in _text(html)


def test_cards_say_the_verdict_and_link_the_decision(built):  # noqa: F811
    _, _, pages = built
    html = pages["cycles.html"]
    cards = _cards(html)
    one = _text(next(c for c in cards if "Decision #1" in c))
    two = _text(next(c for c in cards if "Decision #2" in c))
    assert "Entered ACME long 8%" in one and "core:" in one
    assert "Core:" in two and "Entered" not in two and "late." in two          # no swing slot: core only
    assert 'href="decisions/1/index.html">Open decision #1' in html
    assert 'href="decisions/2/index.html">Open decision #2' in html
    assert "ideas reviewed" in one or "idea reviewed" in one
    for c in cards:
        assert "Open decision" in c or "Open transcript" in c                   # every card opens somewhere


def test_plain_labels_and_council_health(built):  # noqa: F811
    _, _, pages = built
    html = pages["cycles.html"]
    t = _text(html)
    assert "Manager attempts agreeing" in t and "Solo agent agreed?" in t
    assert re.search(r"Manager attempts agreeing (\d/\d|\d+%)", t)
    assert re.search(r"\d+ agents · (every reply usable|\d+ (unreadable repl|timeout|service error))", t)
    assert '<details class="jd mtg-health' in html                               # details, no script
    for jargon in ("parse fail", "parse_fail", ".00%", "Agreement</th>"):
        assert jargon not in html, jargon


def test_how_to_read_explains_both_flows(built):  # noqa: F811
    _, _, pages = built
    t = _text(pages["cycles.html"])
    assert "Decision page" in t and "Transcript" in t and "Every idea's journey" in t
    assert "nine steps" not in t


def test_decisions_page_links_all_meetings(built):  # noqa: F811
    _, _, pages = built
    assert 'href="../cycles.html">All meetings' in pages["decisions/index.html"]


def test_runs_page_is_percent_only(built):  # noqa: F811
    _, dest, pages = built
    html = pages["cycles.html"]
    assert "$" not in _text(html) and "/Users/" not in html
    assert leakscan.scan_paths([dest / "cycles.html"]) == []
