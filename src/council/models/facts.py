"""The FactPack: everything the council may see, percentage-only, stamped with availability times."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import Field

from council.models.common import Strict, TrendState

FactKind = Literal["market", "cost", "vol", "macro", "event", "news", "filing", "fundamental"]
FactUnit = Literal["pct", "x", "ratio", "bps", "bps_day", "days", "hours", "sigma", "state", "text"]


class Fact(Strict):
    id: str = Field(pattern=r"^[FNSMECVK]:")
    kind: FactKind
    symbol: str | None = None
    value: float | str | bool | None
    unit: FactUnit
    available_at: datetime
    source: str
    publishable: bool = True        # False for licensed series (e.g. VIXCLS): agents read, never published


class MarketState(Strict):
    """Per-instrument state from COMPLETED bars only."""

    symbol: str
    asset_class: str
    trend: TrendState | None = None
    dist_sma50_pct: float | None = None
    dist_sma200_pct: float | None = None
    sigma_ann: float | None = None          # annualised vol, fraction (0.25 = 25%)
    sigma_daily: float | None = None
    vol_ratio_1y: float | None = None       # current sigma / 1y median sigma
    ewma5_60_ratio: float | None = None     # vol shock ratio (vol officer / breaker)
    mom10d_pct: float | None = None
    mom63d_pct: float | None = None
    dd52_pct: float | None = None           # distance from 52-week high, <= 0
    ret1d_sigma: float | None = None        # last completed daily return in sigma units (anti-chase)
    market_open: bool = True
    data_age_h: float | None = None
    frozen: bool = False
    frozen_reason: str | None = None
    history_source: str | None = None
    bar_available_at: datetime | None = None   # availability time of the last bar used


class NewsItem(Strict):
    """Broker news/discussion item. Agents may read it; its text is NEVER published."""

    id: str = Field(pattern=r"^N:[0-9a-f]{8}$")
    title: str
    summary: str = ""
    symbols: list[str] = Field(default_factory=list)
    published_at: datetime
    available_at: datetime
    source: str = "etoro_feed"
    earnings_date: datetime | None = None
    before_market_open: bool | None = None


class FilingSentence(Strict):
    id: str = Field(pattern=r"^S:[0-9-]+#p\d+$")
    text: str


class FilingItem(Strict):
    accession: str
    cik: str
    symbol: str
    form: str
    items: list[str] = Field(default_factory=list)
    accepted_at: datetime
    available_at: datetime
    url: str
    sentences: list[FilingSentence] = Field(default_factory=list)
    guidance_regex: Literal["raise", "lower", "maintain", "none"] = "none"


class EventItem(Strict):
    id: str = Field(pattern=r"^E:")
    kind: Literal["earnings", "fomc", "cpi", "nfp", "pce"]
    at_utc: datetime
    symbols: list[str] = Field(default_factory=list)   # empty = market-wide
    binary: bool = True
    severity: int = Field(ge=1, le=3)
    source: str
    known_at: datetime | None = None           # when the schedule became public


class FactPack(Strict):
    cycle_id: str
    slot: datetime
    created_at: datetime
    admitted: list[str]                          # instruments the council may act on this cycle
    states: dict[str, MarketState]
    facts: list[Fact] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)
    filings: list[FilingItem] = Field(default_factory=list)
    events: list[EventItem] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    frozen: list[str] = Field(default_factory=list)
    input_hash: str = ""

    def evidence_ids(self) -> set[str]:
        ids = {f.id for f in self.facts}
        ids |= {n.id for n in self.news}
        ids |= {s.id for f in self.filings for s in f.sentences}
        ids |= {e.id for e in self.events}
        return ids

    def compute_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"input_hash", "created_at"})
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()

    def sealed(self) -> FactPack:
        return self.model_copy(update={"input_hash": self.compute_hash()})
