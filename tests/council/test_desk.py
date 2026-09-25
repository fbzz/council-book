"""Desk pack: one reference line per instrument, evidence IDs, cards, percent-only."""

from __future__ import annotations

import re

from council.deliberation.desk import allowed_directions, desk_pack, news_detail
from council.deliberation.officers import event_cards, vol_cards
from council.models.risk import Band

from .factories import build_pack, with_late_evidence


def render(pack, ref, bands, current, lines, policy, **kw):
    cards = vol_cards(pack, policy) + event_cards(pack, pack.slot, policy)
    hints = {ln.symbol: {"per_side_bps": 5.0, "carry_bps_day": 0.0} for ln in lines}
    hints["BTC"] = {"per_side_bps": 100.0, "carry_bps_day": 0.0}
    return desk_pack(pack=pack, ref=ref, bands=bands, current_levels=current, cost_hints=hints,
                     cards=cards, lines=lines, **kw)


def test_one_reference_row_per_line(pack, ref, bands, current, lines, policy):
    text = render(pack, ref, bands, current, lines, policy)
    rows = [ln for ln in text.splitlines() if " | trend " in ln]
    assert [r.split()[0] for r in rows] == [ln.symbol for ln in lines]
    semis = next(r for r in rows if r.startswith("SEMIS"))
    assert "trend up" in semis and "vs SMA50 +1.5%" in semis and "vs SMA200 +12.0%" in semis
    assert "vol shock 2.30x" in semis and "ref +1.00" in semis and "now +1.00" in semis
    assert "band [+0.50, +1.00] qualifying K:vol:1" in semis and "may: cut" in semis
    assert "cost 5 bps/side, carry 0.00 bps/day" in semis
    btc = next(r for r in rows if r.startswith("BTC"))
    assert "may: none (reference-only line)" in btc and "100 bps/side" in btc


def test_evidence_ids_and_cards_listed(pack, ref, bands, current, lines, policy):
    text = render(pack, ref, bands, current, lines, policy)
    for eid in ("F:NDX:trend", "V:SEMIS:ewma5_60", "C:GOLD:bps_side", "M:DGS10@2026-09-30",
                "E:fomc@2026-10-02", "N:1a2b3c4d"):
        assert eid in text
    assert "K:vol:1 [vol] vol_shock risk_down scope SEMIS" in text
    assert "K:event:1 [event] event_binary" in text


def test_include_cards_false_omits_cards(pack, ref, bands, current, lines, policy):
    text = render(pack, ref, bands, current, lines, policy, include_cards=False)
    assert "EVIDENCE CARDS" not in text and "K:vol:1 [vol]" not in text


def test_percent_only_no_currency_urls_or_handles(pack, ref, bands, current, lines, policy):
    text = render(pack, ref, bands, current, lines, policy) + news_detail(pack)
    assert not re.search(r"[$€£]\s*\d", text)
    assert "[amount]" in text  # the fixture headline's amount was scrubbed
    assert "https://" not in text and "@trader" not in text and "\x1b" not in text


def test_deterministic(pack, ref, bands, current, lines, policy):
    assert render(pack, ref, bands, current, lines, policy) == render(
        build_pack(), ref, bands, current, lines, policy
    )


def test_not_admitted_line_says_so(ref, bands, current, lines, policy):
    pack = build_pack(admitted=["NDX", "SPX"])
    text = render(pack, ref, bands, current, lines, policy)
    gold = next(r for r in text.splitlines() if r.startswith("GOLD"))
    assert "may: none (not admitted this cycle)" in gold


def _band(lo, hi):
    return Band(symbol="X", trend="mixed", ref_level=0.5, lo=lo, hi=hi)


def test_allowed_directions(lines):
    by = {ln.symbol: ln for ln in lines}
    gold, oil, btc = by["GOLD"], by["OIL"], by["BTC"]
    assert allowed_directions(band=_band(0.0, 1.0), ref=0.5, current=0.5, line=gold, admitted=True) == ["cut", "add"]
    assert allowed_directions(band=_band(0.0, 1.5), ref=1.0, current=1.0, line=gold, admitted=True) == ["cut", "lever"]
    assert allowed_directions(band=_band(-0.5, 0.0), ref=0.0, current=-0.25, line=oil, admitted=True) == ["add", "short", "cover"]
    assert allowed_directions(band=_band(1.0, 1.0), ref=1.0, current=1.0, line=gold, admitted=True) == []
    assert allowed_directions(band=_band(0.0, 1.0), ref=1.0, current=1.0, line=btc, admitted=True) == []
    assert allowed_directions(band=_band(0.0, 1.0), ref=0.5, current=0.5, line=gold, admitted=False) == []


def test_late_evidence_never_shown(ref, bands, current, lines, policy):
    pack = with_late_evidence(build_pack())
    text = render(pack, ref, bands, current, lines, policy) + news_detail(pack)
    assert "F:NDX:late_close" not in text and "N:0badc0de" not in text
    assert "Tomorrow" not in text
