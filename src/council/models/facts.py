"""The FactPack: everything the council may see, percentage-only, stamped with availability times."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from council.models.common import Strict, TrendState
from council.publish.leakscan import VALUE_PATTERNS

FactKind = Literal["market", "cost", "vol", "macro", "event", "news", "filing", "fundamental"]
FactUnit = Literal["pct", "x", "ratio", "bps", "bps_day", "days", "hours", "sigma", "state", "text"]


class Fact(Strict):
    id: str = Field(pattern=r"^[FNPSMECVK]:")
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


# ------------------------------------------------------------------------------------ news items
# Where a news item comes from, and under what licence (transparency design §3.1-3.2). The broker's
# feed is eToro Licensed Content: agents may read it, but its text is never published. The other
# six are U.S. federal sources; which of their fields may be published is decided at publication by
# three facts together (the `P:` prefix, a public source, a public-domain licence), never by one.
NewsSource = Literal["etoro_feed", "sec", "fed_board", "bls", "bea", "treasury", "eia"]
NewsLicence = Literal["broker_licensed", "public_domain", "federal_work_unverified"]
BROKER_NEWS_SOURCE = "etoro_feed"
PUBLIC_NEWS_SOURCES: frozenset[str] = frozenset({"sec", "fed_board", "bls", "bea", "treasury", "eia"})
# The licence each source's items carry unless a caller states otherwise. Treasury is a federal work
# (17 U.S.C. §105) but no explicit site statement was found, so it is recorded as unverified.
DEFAULT_LICENCE: Mapping[str, str] = MappingProxyType({
    "etoro_feed": "broker_licensed",
    "sec": "public_domain",
    "fed_board": "public_domain",
    "bls": "public_domain",
    "bea": "public_domain",
    "treasury": "federal_work_unverified",
    "eia": "public_domain",
})
FilingForm = Literal["8-K", "8-K/A", "6-K"]
ItemCode = Annotated[str, Field(pattern=r"^\d\.\d{2}$")]

# Links are a typed field, never text: https only, one of these hosts, no user info or port, printable
# ASCII without spaces, and no run of 7+ digits by the leak scan's own `long_number` rule (so an
# EDGAR `Archives/.../<CIK>/...` path is refused while `.../monetary20260917a.htm` passes).
NEWS_LINK_HOSTS: frozenset[str] = frozenset({
    "www.sec.gov", "www.federalreserve.gov", "www.bls.gov", "www.bea.gov", "apps.bea.gov",
    "home.treasury.gov", "www.treasurydirect.gov", "www.eia.gov",
})
NEWS_LINK_MAX = 300
_LONG_NUMBER = dict(VALUE_PATTERNS)["long_number"]


def check_https_link(value: str) -> str:
    """`value` when it is an allowed public link (see NEWS_LINK_HOSTS); otherwise ValueError, whose
    message never echoes the link."""
    text = value if isinstance(value, str) else ""
    ok = 0 < len(text) <= NEWS_LINK_MAX and all(33 <= ord(ch) < 127 for ch in text)
    if ok:
        try:
            parts = urlsplit(text)
            port = parts.port
        except ValueError:
            ok = False
        else:
            ok = (parts.scheme == "https" and parts.hostname in NEWS_LINK_HOSTS and port is None
                  and parts.username is None and parts.password is None and parts.netloc == parts.hostname
                  and not parts.fragment and _LONG_NUMBER.search(text) is None)
    if not ok:
        raise ValueError("link is not an allowed https link (host allow-list, no 7+ digit run)")
    return text


HttpsLink = Annotated[str, AfterValidator(check_https_link)]


def public_news_id(source: str, key: str) -> str:
    """A public-domain item's id: "P:" + sha256(source NUL stable key)[:8]. The stable key is the
    accession number or the feed's guid, else feed label + link + time + title
    (`council.data.gov_news.stable_key`). The inputs are public, so no salt is needed (anyone can
    recompute the id from the item)."""
    if source not in PUBLIC_NEWS_SOURCES:
        raise ValueError(f"{source!r} is not a public news source")
    if not key:
        raise ValueError("public news id needs a non-empty key")
    blob = f"{source}\0{key}".encode("utf-8", "surrogatepass")
    return "P:" + hashlib.sha256(blob).hexdigest()[:8]


def broker_news_id(post_id: str, install_key: bytes) -> str:
    """A broker feed item's id: "N:" + HMAC-SHA256(install key, post id)[:8] (design T-D15). The
    key is private, so a published id cannot be matched back to the feed post by brute force over
    post ids and minutes."""
    if not post_id:
        raise ValueError("broker news id needs a non-empty post id")
    if not isinstance(install_key, bytes | bytearray) or len(install_key) < 16:
        raise ValueError("broker news id needs an install key of at least 16 bytes")
    digest = hmac.new(bytes(install_key), post_id.encode("utf-8", "surrogatepass"), hashlib.sha256)
    return "N:" + digest.hexdigest()[:8]


class NewsItem(Strict):
    """A news item the news role may read.

    - Broker feed (`N:` id, source `etoro_feed`, licence `broker_licensed`): agents may read it;
      its text is NEVER published.
    - Public-domain (`P:` id, a U.S. federal source): title and summary were cleaned and leak-scanned
      at fetch time (`council.data.gov_news`); what may be published is decided at publication.
    `licence` defaults from `source` when absent. The fields added for public-domain items are left
    out of a dump while they hold their default (`link`, `form`, `items`; `licence` on a broker item),
    so a broker item, and the pack hash over it, serialises exactly as it did before they existed."""

    OMIT_WHEN_DEFAULT: ClassVar[frozenset[str]] = frozenset({"link", "form", "items"})

    id: str = Field(pattern=r"^(?:N|P):[0-9a-f]{8}$")
    title: str
    summary: str = ""
    symbols: list[str] = Field(default_factory=list)
    published_at: datetime
    available_at: datetime
    source: NewsSource = "etoro_feed"
    earnings_date: datetime | None = None
    before_market_open: bool | None = None
    licence: NewsLicence
    link: HttpsLink | None = None
    form: FilingForm | None = None                    # SEC items only
    items: list[ItemCode] = Field(default_factory=list)   # 8-K item codes ("2.02"), SEC items only

    @model_validator(mode="before")
    @classmethod
    def _default_licence(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and data.get("licence") is None:
            source = data.get("source", BROKER_NEWS_SOURCE)
            if isinstance(source, str) and source in DEFAULT_LICENCE:
                data = {**data, "licence": DEFAULT_LICENCE[source]}
        return data

    @model_serializer(mode="wrap")
    def _omit_new_defaults(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            fields = type(self).model_fields
            for name in type(self).OMIT_WHEN_DEFAULT:
                if name in data and getattr(self, name) == fields[name].get_default(call_default_factory=True):
                    del data[name]
            if self.source == BROKER_NEWS_SOURCE and self.licence == DEFAULT_LICENCE[BROKER_NEWS_SOURCE]:
                data.pop("licence", None)
        return data


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
