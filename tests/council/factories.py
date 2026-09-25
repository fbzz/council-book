"""Builders for the council tests: a synthetic percent-only pack, reference book, bands, stub replies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from council.models.common import snap_level
from council.models.facts import EventItem, Fact, FactPack, MarketState, NewsItem
from council.models.reference import ReferenceBook, ReferenceEntry
from council.models.risk import Band

SLOT = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
DOLLAR = chr(36)  # built at runtime so no literal currency amount sits in the repo

# symbol: (trend, dist50, dist200, mom10d, mom63d, dd52, vol_ratio_1y, ewma5_60)
STATES = {
    "NDX": ("up", 3.2, 8.1, 1.2, 6.0, -2.1, 1.05, 1.10),
    "SEMIS": ("up", 1.5, 12.0, -2.5, 9.0, -6.0, 1.60, 2.30),
    "SPX": ("up", 2.0, 6.5, 0.8, 4.0, -1.5, 0.95, 1.00),
    "GOLD": ("mixed", -1.0, 4.0, -0.5, 2.0, -4.0, 0.90, 0.90),
    "BTC": ("up", 5.0, 20.0, 3.0, 15.0, -10.0, 1.20, 1.20),
    "ETH": ("down", -8.0, -3.0, -4.0, -12.0, -35.0, 1.30, 1.40),
    "OIL": ("down", -5.0, -9.0, -3.0, -11.0, -25.0, 1.10, 1.30),
    "EURUSD": ("mixed", 0.4, -0.3, 0.2, 1.0, -3.0, 0.80, 1.00),
    "GBPUSD": ("up", 0.6, 1.2, 0.3, 1.5, -1.0, 0.85, 1.00),
}
REF_LEVELS = {
    "NDX": 1.0, "SEMIS": 1.0, "SPX": 1.0, "GOLD": 0.5, "BTC": 1.0, "ETH": 0.25,
    "OIL": 0.0, "EURUSD": 0.0, "GBPUSD": 0.0,
}
BANDS = {
    "NDX": (1.0, 1.0), "SEMIS": (0.5, 1.0), "SPX": (1.0, 1.0), "GOLD": (0.0, 1.0),
    "BTC": (1.0, 1.0), "ETH": (0.25, 0.25), "OIL": (-0.5, 0.0), "EURUSD": (-0.25, 0.25),
    "GBPUSD": (0.0, 0.5),
}


def build_pack(
    *,
    ewma: dict[str, float] | None = None,
    admitted: list[str] | None = None,
    events: list[EventItem] | None = None,
) -> FactPack:
    ewma = ewma or {}
    avail = SLOT - timedelta(hours=1)
    states: dict[str, MarketState] = {}
    facts: list[Fact] = []
    for sym, (trend, d50, d200, m10, m63, dd, vr, ew) in STATES.items():
        ew = ewma.get(sym, ew)
        states[sym] = MarketState(
            symbol=sym, asset_class="x", trend=trend, dist_sma50_pct=d50, dist_sma200_pct=d200,
            mom10d_pct=m10, mom63d_pct=m63, dd52_pct=dd, vol_ratio_1y=vr, ewma5_60_ratio=ew,
        )
        facts += [
            Fact(id=f"F:{sym}:trend", kind="market", symbol=sym, value=trend, unit="state",
                 available_at=avail, source="test"),
            Fact(id=f"F:{sym}:dist_sma50", kind="market", symbol=sym, value=d50, unit="pct",
                 available_at=avail, source="test"),
            Fact(id=f"F:{sym}:mom63d", kind="market", symbol=sym, value=m63, unit="pct",
                 available_at=avail, source="test"),
            Fact(id=f"V:{sym}:vol_ratio", kind="vol", symbol=sym, value=vr, unit="ratio",
                 available_at=avail, source="test"),
            Fact(id=f"V:{sym}:ewma5_60", kind="vol", symbol=sym, value=ew, unit="ratio",
                 available_at=avail, source="test"),
            Fact(id=f"C:{sym}:bps_side", kind="cost", symbol=sym, value=5.0, unit="bps",
                 available_at=avail, source="test"),
        ]
    facts += [
        Fact(id="M:DGS10@2026-09-30", kind="macro", value=4.1, unit="pct", available_at=avail,
             source="test"),
        Fact(id="M:DGS2@2026-09-30", kind="macro", value=3.6, unit="pct", available_at=avail,
             source="test"),
    ]
    news = [
        NewsItem(id="N:1a2b3c4d", title="Chip export limits announced for advanced parts",
                 summary="Officials outlined new limits on advanced chip exports.",
                 symbols=["SEMIS"], published_at=avail, available_at=avail),
        NewsItem(id="N:5e6f7a8b",
                 title=f"Central bank minutes see patience; fund took {DOLLAR}5bn stake https://x.example.com/a @trader",
                 summary="Minutes signal no hurry.\x1b[31m", symbols=[],
                 published_at=avail - timedelta(hours=2), available_at=avail),
    ]
    if events is None:
        events = [
            EventItem(id="E:fomc@2026-10-02", kind="fomc", at_utc=SLOT + timedelta(hours=20),
                      symbols=[], binary=True, severity=3, source="calendar"),
        ]
    return FactPack(
        cycle_id="2026-10-01T1440Z", slot=SLOT, created_at=SLOT,
        admitted=admitted if admitted is not None else list(STATES),
        states=states, facts=facts, news=news, events=events,
    )


def build_ref() -> ReferenceBook:
    entries = {
        sym: ReferenceEntry(
            symbol=sym, sleeve="core", asset_class="x", in_reference=REF_LEVELS[sym] > 0,
            trend=STATES[sym][0], level_ref=REF_LEVELS[sym], unit_weight=0.1,
            weight_ref=REF_LEVELS[sym] * 0.1, sigma_ann=0.2,
        )
        for sym in STATES
    }
    return ReferenceBook(cycle_id="2026-10-01T1440Z", entries=entries, k=1.0, target_vol=0.22,
                         ex_ante_vol=0.18, gross=0.7)


def build_bands() -> dict[str, Band]:
    return {
        sym: Band(symbol=sym, trend=STATES[sym][0], ref_level=REF_LEVELS[sym], lo=lo, hi=hi,
                  qualifying_cards=["K:vol:1"] if sym == "SEMIS" else [])
        for sym, (lo, hi) in BANDS.items()
    }


def clip_enforce(levels: dict[str, float], bands: dict[str, Band]) -> tuple[dict[str, float], list[str]]:
    """Test stand-in for the risk agent's enforce(): clip to the band, snap to the grid."""
    out, notes = {}, []
    for sym, lvl in levels.items():
        band = bands.get(sym)
        new = lvl if band is None else snap_level(min(max(lvl, band.lo), band.hi))
        if new != lvl:
            notes.append(f"{sym}: clipped {lvl:+.2f} -> {new:+.2f}")
        out[sym] = new
    return out, notes


# ------------------------------------------------------------------ canned model replies (dicts)
def news_reply() -> dict:
    return {"cards": [
        {"scope": ["SEMIS"], "card_type": "news_material", "direction": "risk_down",
         "claim": "Export limits hit the semiconductor line directly.",
         "evidence_ids": ["N:1a2b3c4d", "V:SEMIS:ewma5_60"], "horizon_days": 20,
         "falsifier": "Limits delayed.", "novel": True},
        {"scope": ["NDX"], "card_type": "news_context", "direction": "neutral",
         "claim": "Central bank minutes signal patience.", "evidence_ids": ["N:5e6f7a8b"],
         "horizon_days": 5, "falsifier": "", "novel": True},
        {"scope": ["NDX"], "card_type": "news_material", "direction": "risk_up",
         "claim": "Cites an item that does not exist.", "evidence_ids": ["N:deadbeef"],
         "horizon_days": 5, "falsifier": "", "novel": True},
    ]}


def bull_reply() -> dict:
    return {
        "argument": "Equity lines are in uptrends; hold the reference.",
        "proposal": {"GBPUSD": 0.3, "XYZ": 1.0},
        "claims": [
            {"claim_id": "c1", "text": "Nasdaq-100 uptrend intact.", "evidence_ids": ["F:NDX:trend"]},
            {"claim_id": "c2", "text": "Semis vol is manageable.",
             "evidence_ids": ["V:SEMIS:ewma5_60"]},
        ],
        "strongest_opposing_fact_id": "V:SEMIS:ewma5_60",
        "concessions": [],
    }


def bear_reply() -> dict:
    return {
        "argument": "Semis vol card qualifies a cut.",
        "proposal": {"SEMIS": 0.5},
        "claims": [{"claim_id": "c1", "text": "Vol card on semis.", "evidence_ids": ["K:vol:1"]}],
        "strongest_opposing_fact_id": "F:NDX:trend",
        "concessions": ["Nasdaq trend intact."],
        "rebuttals": [
            {"claim_id": "c1", "verdict": "concede", "text": "Trend is up.", "evidence_ids": []},
            {"claim_id": "c2", "verdict": "refute", "text": "Ratio above threshold.",
             "evidence_ids": ["K:vol:1"]},
            {"claim_id": "c9", "verdict": "refute", "text": "No such claim.", "evidence_ids": []},
        ],
    }


def rebuttal_reply() -> dict:
    return {
        "argument": "Accept a smaller semis line.",
        "proposal": {"SEMIS": 0.75},
        "claims": [{"claim_id": "c1", "text": "Card covers semis only.", "evidence_ids": ["K:vol:1"]}],
        "strongest_opposing_fact_id": "K:vol:1",
        "concessions": ["c1: the card qualifies a cut."],
    }


def pm_decision(deviations: list[dict] | None = None, *, fact: str = "K:vol:1", sided: str = "bear") -> dict:
    return {
        "deviations": deviations or [],
        "decisive_fact": {"text": "Semis vol above threshold.", "evidence_id": fact},
        "sided_with": sided,
        "dismissed": [],
        "no_change_reason": "" if deviations else "Nothing material changed.",
    }


SEMIS_CUT = {"symbol": "SEMIS", "level": 0.5, "direction": "cut",
             "evidence_ids": ["K:vol:1"], "reason": "Vol card."}


def pm_reply(user: str, replicate: int) -> dict:
    """Replicates 0 and 1 cut SEMIS; replicate 2 holds the reference."""
    return pm_decision([SEMIS_CUT] if replicate in (0, 1) else None)


def single_reply(user: str, replicate: int) -> dict:
    return pm_decision(None, fact="F:NDX:trend", sided="reference")


def stub_responses() -> dict:
    return {
        "news": news_reply(),
        "macro": {"regime": "neutral",
                  "drivers": [{"text": "Rates steady.", "evidence_ids": ["M:DGS10@2026-09-30"]}],
                  "sleeve_tilts": {"overlay": 0}, "cards": []},
        "bull_open": bull_reply(),
        "bear": bear_reply(),
        "bull_rebuttal": rebuttal_reply(),
        "pm": pm_reply,
        "single_agent": single_reply,
    }


def with_late_evidence(pack: FactPack) -> FactPack:
    """Add a fact and a news item stamped available AFTER the slot (lookahead bait)."""
    late = SLOT + timedelta(minutes=5)
    fact = Fact(id="F:NDX:late_close", kind="market", symbol="NDX", value=9.9, unit="pct",
                available_at=late, source="test")
    item = NewsItem(id="N:0badc0de", title="Tomorrow's headline", symbols=["NDX"],
                    published_at=late, available_at=late)
    return pack.model_copy(update={"facts": [*pack.facts, fact], "news": [*pack.news, item]})
