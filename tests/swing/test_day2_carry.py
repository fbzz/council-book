"""Day-2 catalyst carry: a carried (pending) idea's cited news item rejoins the slot's reading list
from the private catalyst cache even after the 48 h window cut it; a purged item stays gone."""
from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

from council.cycle import with_carried_catalysts
from council.models.facts import NewsItem
from council.swing import council as sc
from council.swing import sources as ss
from council.swing.models import ScoutIdea
from council.swing.roles import catalyst_index
from tests.swing import stubs as s
from tests.swing.test_scout_inputs import ALL, SLOT, FakeSec, Recorder

DAY1 = SLOT - timedelta(days=3)
ITEM = NewsItem(id="N:0a1b2c3d", title="ACME wins a large contract", symbols=["ACME"],
                published_at=DAY1 - timedelta(hours=2), available_at=DAY1 - timedelta(hours=2),
                source="etoro_feed", licence="broker_licensed")


def _real(policy, tmp_path, clock, news):
    return ss.real_swing_sources(policy, state_dir=tmp_path, keys_loader=lambda: object(),
                                 sec_user_agent=lambda: "Council Test ops@example.org", sec_factory=FakeSec,
                                 fetch_bars=lambda *a, **k: {}, fetch_short_interest=lambda *a, **k: {},
                                 wall=lambda: clock["now"], sec_text_get=Recorder(), sec_fetch=lambda _c, _f: ALL,
                                 news=news)


def _carried():
    idea = ScoutIdea.model_validate({**s.idea(), "setup": sc.DAY2_SETUP, "catalyst_ids": [ITEM.id]})
    return sc.CarriedWait(idea_id="idea:old_1", idea=idea, wait_day="2026-09-25")


def test_a_carried_catalyst_outside_the_window_comes_back_from_the_private_cache(policy, tmp_path):
    clock = {"now": DAY1}
    feed = {"items": [ITEM]}
    src = _real(policy, tmp_path, clock, lambda _slot: SimpleNamespace(items=list(feed["items"])))
    day1 = src.inputs(DAY1, [], [])
    assert ITEM.id in {i.id for i in day1.reading}
    files = list((tmp_path / "licensed" / "swing_catalysts").glob("*.json"))
    assert len(files) == 1 and oct(os.stat(files[0]).st_mode & 0o777) == "0o600"

    feed["items"] = []                                   # the feed no longer returns it (72 h old)
    clock["now"] = SLOT
    inputs = src.inputs(SLOT, [], [])
    assert ITEM.id not in {i.id for i in inputs.reading}
    reading = with_carried_catalysts(src, inputs, [_carried()])
    assert [i.id for i in reading if i.id == ITEM.id] == [ITEM.id]
    assert ITEM.id in catalyst_index(reading, slot=SLOT)

    clock["now"] = DAY1 + timedelta(days=8)              # past the licensed retention: gone
    later = SLOT + timedelta(days=5)
    inputs = src.inputs(later, [], [])
    assert ITEM.id not in {i.id for i in with_carried_catalysts(src, inputs, [_carried()])}


def test_without_the_hook_or_a_carry_the_reading_list_is_unchanged():
    inputs = SimpleNamespace(reading=[ITEM], slot=SLOT)
    assert with_carried_catalysts(SimpleNamespace(), inputs, [_carried()]) == [ITEM]
    boom = SimpleNamespace(carry_catalysts=lambda *_: 1 / 0)
    empty = SimpleNamespace(reading=[], slot=SLOT)
    assert with_carried_catalysts(boom, empty, [_carried()]) == []
