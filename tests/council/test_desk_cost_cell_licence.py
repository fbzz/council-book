"""Stage-1 review: the licence metadata of the desk's cost cells and news items fails closed.

- The cost/side and carry cells take their source from the pack's own cost fact for the line when the
  hints do not declare one (production hints never do: `runtime.cost_hints`), so a value a broker
  what-if priced (`costs:whatif`) is held apart as eToro Licensed Content (7-day purge) instead of
  staying in the main capture as "public".
- A news item's licence follows the three facts together: an `N:` id is broker-licensed whatever its
  other fields say; public-domain text needs a `P:` id and a public-domain licence.
Only metadata changes; the rendered text is pinned by the golden tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from council.deliberation import desk as live
from council.models.facts import Fact, NewsItem

from .golden.cases import build
from .golden.make_golden import code_cards, desk_inputs

AT = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)


def _cells(sections, line, field):
    return [it for s in sections for it in s.items if it.line == line and it.field == field]


def _with_costs(case, source: str):
    facts = [f for f in case.pack.facts if not f.id.startswith("C:NDX:")] + [
        Fact(id="C:NDX:per_side_bps", kind="cost", symbol="NDX", value=6.1, unit="bps", available_at=AT,
             source=source),
        Fact(id="C:NDX:carry_bps_day", kind="cost", symbol="NDX", value=0.4, unit="bps_day", available_at=AT,
             source=source),
    ]
    return case.pack.model_copy(update={"facts": facts})


@pytest.mark.parametrize("source, licence", [
    ("costs:whatif", "broker_licensed"), ("costs", "broker_licensed"), ("costs:floor", "public"),
])
def test_cost_cells_take_the_source_of_the_packs_cost_fact(policy, source, licence):
    case = build("core_only")
    before = live.desk_sections(cards=code_cards(case, policy), **desk_inputs(case, policy))
    after = live.desk_sections(cards=code_cards(case, policy),
                               **(desk_inputs(case, policy) | {"pack": _with_costs(case, source)}))
    for field in ("cost_side", "carry"):
        (cell,) = _cells(after, "NDX", field)
        assert cell.sources == (source,) and cell.licence == licence
        (old,) = _cells(before, "NDX", field)
        assert cell.text == old.text                                   # the model's text is unchanged


def test_a_declared_hint_source_still_wins_and_unknown_stays_unknown(policy):
    case = build("broker_cost")
    sections = live.desk_sections(cards=[], **(desk_inputs(case, policy) | {"pack": _with_costs(case, "costs:floor")}))
    (side,) = _cells(sections, "NDX", "cost_side")
    assert side.sources == ("broker_quote",) and side.licence == "broker_licensed"
    (spx,) = _cells(sections, "SPX", "cost_side")
    assert spx.sources == ("cost_quote",)


def _news(item_id: str, **kw) -> NewsItem:
    return NewsItem(id=item_id, title="A release title", published_at=AT, available_at=AT, **kw)


@pytest.mark.parametrize("item, licence, source", [
    (_news("N:0badc0de"), "broker_licensed", "broker_feed"),
    (_news("N:0badc0de", source="sec", licence="public_domain"), "broker_licensed", "broker_feed"),
    (_news("P:0badc0de", source="fed_board"), "public_domain", "fed_board"),
    (_news("P:0badc0de", source="treasury"), "public_domain", "treasury"),
    (_news("P:0badc0de", source="etoro_feed"), "broker_licensed", "broker_feed"),
    (_news("P:0badc0de", source="sec", licence="broker_licensed"), "broker_licensed", "sec"),
])
def test_news_licences_fail_closed(item, licence, source):
    assert live.news_licence(item) == licence
    assert live.news_source(item) == source
