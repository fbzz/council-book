"""The wider movers-screen universe (user decision 2026-10-01): S&P 500 + Nasdaq-100 + S&P MidCap 400
+ S&P SmallCap 600 (~1,520 names) through the existing Wikipedia membership fetcher; the stock sleeve
keeps its two indexes; a universe cached before the change is rebuilt."""

from __future__ import annotations

import json

from council.stocks import universe as U
from council.swing import screen as S

from .test_scout_inputs import SLOT, Recorder, _real


def test_the_swing_universe_adds_the_midcap_and_smallcap_lists():
    assert U.INDEXES == ("sp500", "nasdaq100")                       # the stock sleeve is unchanged
    assert U.SWING_INDEXES == ("sp500", "nasdaq100", "sp400", "sp600")
    for index in U.SWING_INDEXES:
        assert index in U.MEMBERSHIP_PAGES and index in U.MEMBERSHIP_COUNTS
    assert U.MEMBERSHIP_PAGES["sp400"] == "List of S&P 400 companies"
    assert U.MEMBERSHIP_COUNTS["sp600"][0] <= 600 <= U.MEMBERSHIP_COUNTS["sp600"][1]
    assert S.UNIVERSE_MAX >= 1_600


def _members(index, **_k):
    sizes = {"sp500": 503, "nasdaq100": 101, "sp400": 400, "sp600": 601}
    shared = [f"S{i:03d}" for i in range(80)]                        # NDX names also in the S&P 500
    letter = {"sp500": "A", "nasdaq100": "B", "sp400": "C", "sp600": "D"}[index]
    own = [f"{letter}{i:04d}" for i in range(sizes[index])]
    syms = shared + own[:sizes[index] - 80] if index in ("sp500", "nasdaq100") else own
    return type("M", (), {"symbols": tuple(syms)})()


def test_screen_universe_reads_all_four_indexes_and_rebuilds_a_narrow_cache(policy, tmp_path, monkeypatch):
    asked: list[str] = []

    def members(index, **k):
        asked.append(index)
        return _members(index, **k)

    monkeypatch.setattr(U, "fetch_membership", members)
    monkeypatch.setattr(U, "load_ai_list", lambda *a, **k: ())
    path = tmp_path / "swing" / "universe.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"asof": SLOT.isoformat(), "tickers": ["CVS"]}))   # old: no "indexes"
    state = _real(policy, tmp_path, Recorder()).gate.__self__
    names = state.screen_universe(SLOT)
    assert asked == list(U.SWING_INDEXES)
    assert 1_400 <= len(names) <= S.UNIVERSE_MAX
    cached = json.loads(path.read_text())
    assert cached["indexes"] == list(U.SWING_INDEXES)
    asked.clear()
    assert len(state.screen_universe(SLOT)) == len(names) and asked == []     # the new cache is used
