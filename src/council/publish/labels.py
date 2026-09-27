"""Plain-language labels for evidence ids. ONE table, used by the public record (the per-cycle
facts table built in `redact`) and by the site (site/build.py), so both say the same thing.

A label names what was measured, never the line: the facts table and the site carry the line
separately (`F:SEMIS:mom63d` -> "3-month change", line SEMIS).
"""

from __future__ import annotations

import re

MARKET_FIELDS: dict[str, str] = {
    "trend": "trend", "dist_sma50": "vs 50-day average", "dist_sma200": "vs 200-day average",
    "dist_sma200_pct": "vs 200-day average", "dist_sma50_pct": "vs 50-day average",
    "mom10d": "10-day change", "mom63d": "3-month change", "dd52": "drop from 1-year high",
    "ret1d_sigma": "last day's move vs normal", "data_age_h": "age of the data", "market_open": "market open or closed",
}
VOL_FIELDS: dict[str, str] = {
    "sigma_ann": "yearly volatility", "vol_ratio": "volatility vs its 1-year norm", "ewma5_60": "volatility shock",
}
COST_FIELDS: dict[str, str] = {
    "per_side_bps": "cost per side", "bps_side": "cost per side", "carry_bps_day": "overnight cost",
}
FRED_SERIES: dict[str, str] = {
    "DGS10": "10-year Treasury yield", "DGS2": "2-year Treasury yield", "T10Y2Y": "10y–2y yield curve",
    "DFF": "Fed funds rate", "DTWEXBGS": "broad US dollar index", "VIXCLS": "VIX",
    "BAMLH0A0HYM2": "high-yield credit spread",
}
FRED_MEASURES: dict[str, str] = {"chg20": "20-day change"}
EVENT_KINDS: dict[str, str] = {
    "fomc": "FOMC", "cpi": "US inflation (CPI)", "nfp": "US jobs report", "pce": "US inflation (PCE)",
    "earnings": "earnings",
}
CARD_ROLES: dict[str, str] = {
    "vol": "volatility card", "news": "news card", "macro": "macro card", "event": "event card",
    "filings": "filing card", "sector": "sector card",
}
BROKER_NEWS_LABEL = "broker news item"
NEWS_LABEL = BROKER_NEWS_LABEL          # older name: an N: item is always a broker feed item
PUBLIC_NEWS_LABEL = "public-source news item"
# A P: item's label by publisher (the item's own `source`); never "broker": these are U.S. federal
# public-domain releases (docs/data-rights.md).
PUBLIC_NEWS_LABELS: dict[str, str] = {
    "sec": "SEC filing notice", "fed_board": "Federal Reserve Board release", "bls": "BLS release",
    "bea": "BEA release", "treasury": "U.S. Treasury release", "eia": "EIA release",
}
# The attribution each public item carries in the journal (fixed text per publisher; "SEC", not
# "EDGAR", which is a registered mark). EIA also asks for the release date (`attribution`).
ATTRIBUTIONS: dict[str, str] = {
    "sec": "Source: U.S. Securities and Exchange Commission",
    "fed_board": "Source: Board of Governors of the Federal Reserve System",
    "bls": "Source: U.S. Bureau of Labor Statistics",
    "bea": "Source: U.S. Bureau of Economic Analysis",
    "treasury": "Source: U.S. Department of the Treasury",
    "eia": "Source: U.S. Energy Information Administration",
}
FILING_LABEL = "company filing"

_MACRO = re.compile(r"^M:([A-Z0-9_]{1,32})(?:\.([a-z0-9_]{1,16}))?(?:@\d{4}-\d{2}-\d{2})?$")


def _field(field: str) -> str:
    return field.replace("_", " ")


def news_label(evidence_id: str, source: str | None = None) -> str:
    """The label of a news item: an `N:` id is a broker news item whatever its source says; a `P:`
    id takes its publisher's label, or the generic public label when the publisher is unknown."""
    eid = (evidence_id or "").strip()
    if eid.startswith("P:"):
        return PUBLIC_NEWS_LABELS.get(source or "", PUBLIC_NEWS_LABEL)
    return BROKER_NEWS_LABEL


def attribution(source: str, release_date: str | None = None) -> str:
    """The fixed attribution of a public publisher ("" for anything else); EIA's names the release
    date when one is given (YYYY-MM-DD)."""
    text = ATTRIBUTIONS.get(source, "")
    if text and source == "eia" and release_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
        text += f" ({release_date})"
    return text


def fact_label(evidence_id: str) -> str:
    """The plain label of an evidence id, without its line or date: `V:NDX:vol_ratio` ->
    "volatility vs its 1-year norm", `M:DGS10.chg20@2026-09-24` -> "10-year Treasury yield ·
    20-day change", `E:earnings:NVDA@2026-10-29` -> "earnings". Unknown fields fall back to the
    field name with spaces; `N:` ids are broker news items and `P:` ids public-source news items
    (`news_label` names the publisher when the source is known); the result is never longer than 80
    characters."""
    eid = (evidence_id or "").strip()
    prefix, _, rest = eid.partition(":")
    label = eid
    if prefix in ("F", "V", "C"):
        field = rest.split(":", 1)[1] if ":" in rest else rest
        table = {"F": MARKET_FIELDS, "V": VOL_FIELDS, "C": COST_FIELDS}[prefix]
        label = table.get(field, _field(field))
    elif prefix == "M":
        m = _MACRO.match(eid)
        if m:
            label = FRED_SERIES.get(m.group(1), m.group(1))
            if m.group(2):
                label += " · " + FRED_MEASURES.get(m.group(2), _field(m.group(2)))
    elif prefix == "E":
        kind = rest.partition("@")[0].partition(":")[0]
        label = EVENT_KINDS.get(kind, kind.upper())
    elif prefix in ("N", "P"):
        label = news_label(eid)
    elif prefix == "S":
        label = FILING_LABEL
    elif prefix == "K":
        role, _, n = rest.partition(":")
        label = f"{CARD_ROLES.get(role, role + ' card')} {n}".strip()
    return label[:80]
