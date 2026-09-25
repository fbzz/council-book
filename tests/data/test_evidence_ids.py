from __future__ import annotations

import hashlib
from datetime import date

import pytest

from council.facts import evidence_ids as ids
from council.models.common import EVIDENCE_PREFIXES
from tests.data.synth import utc


def test_formats():
    assert ids.fact_id("NDX", "dist_sma200") == "F:NDX:dist_sma200"
    assert ids.vol_id("BTC", "vol_ratio") == "V:BTC:vol_ratio"
    assert ids.cost_id("SPX", "bps_side") == "C:SPX:bps_side"
    assert ids.macro_id("DGS10", date(2026, 9, 24)) == "M:DGS10@2026-09-24"
    assert ids.macro_id("DGS10.chg20", utc(2026, 9, 24, 23, 0)) == "M:DGS10.chg20@2026-09-24"
    assert ids.event_id("fomc", utc(2026, 10, 28, 18, 0)) == "E:fomc@2026-10-28"
    assert ids.event_id("earnings", "2026-10-29", symbol="AAPL") == "E:earnings:AAPL@2026-10-29"
    assert ids.news_id("d-1001") == "N:" + hashlib.sha256(b"d-1001").hexdigest()[:8]
    for made in (ids.fact_id("A", "b"), ids.vol_id("A", "b"), ids.cost_id("A", "b"),
                 ids.macro_id("X", "2026-01-01"), ids.event_id("cpi", "2026-01-01"), ids.news_id("k")):
        assert made.startswith(EVIDENCE_PREFIXES)


@pytest.mark.parametrize(
    "bad",
    [lambda: ids.fact_id("ND X", "f"), lambda: ids.fact_id("NDX", "a:b"), lambda: ids.vol_id("", "x"),
     lambda: ids.macro_id("DGS10@", "2026-01-01"), lambda: ids.macro_id("DGS10", "2026-13-01"),
     lambda: ids.event_id("fomc", "2026-10-28", symbol="A<B"), lambda: ids.news_id(""),
     lambda: ids.cost_id("NDX", "x" * 41)],
)
def test_bad_components_rejected(bad):
    with pytest.raises(ValueError):
        bad()
