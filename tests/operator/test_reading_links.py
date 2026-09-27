"""The private reading list shows each public-domain item's link (the user's decision of
2026-09-26: every item the agent read is listed with its link), from the ledger record's
`extras.news_fetch.public_items`; a broker item never gets one."""

from __future__ import annotations

from council.deliberation.reading import Reading, public_links, with_links
from council.operator.inputs_cli import reading_text, render_html


def _reading(item_id: str, source: str) -> Reading:
    return Reading(id=item_id, source=source, licence="public_domain", title="Title", summary="",
                   symbols="", age="-1.0h", available=True, read_by=("news",), cited_by=(), cards=(),
                   disposition="not_cited", used=False, why="no card, claim or decision cited it")


RECORD = {"extras": {"news_fetch": {"broker_items": 1, "public_items": [
    {"id": "P:aaaa1111", "source": "eia", "link": "https://www.eia.gov/x?id=1"},
    {"id": "P:bbbb2222", "source": "bls", "link": "javascript:alert(1)"},
    {"id": "N:cccc3333", "source": "etoro_feed", "link": "https://broker.example/n"},
    "not a row",
]}}}


def test_public_links_keep_only_public_http_links():
    assert public_links(RECORD) == {"P:aaaa1111": "https://www.eia.gov/x?id=1"}


def test_public_links_tolerate_old_or_malformed_records():
    assert public_links(None) == {}
    assert public_links({}) == {}
    assert public_links({"extras": {"news_fetch": "bad"}}) == {}


def test_the_terminal_reading_list_prints_the_link_of_a_public_item_only():
    readings = with_links([_reading("P:aaaa1111", "eia"), _reading("N:cccc3333", "etoro_feed")],
                          {**public_links(RECORD), "N:cccc3333": "https://broker.example/n"})
    text = "\n".join(reading_text(readings))
    assert "link: https://www.eia.gov/x?id=1" in text
    assert "broker.example" not in text


def test_the_html_reading_list_links_the_headline(captured):
    from council.operator.inputs_cli import load

    inputs, lic = load(captured.root, captured.cycle_id)
    page = render_html(inputs, lic, readings=with_links([_reading("P:aaaa1111", "eia")], public_links(RECORD)))
    assert '<a href="https://www.eia.gov/x?id=1" rel="noopener noreferrer">Title</a>' in page
