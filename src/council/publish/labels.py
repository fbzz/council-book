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
NEWS_LABEL = "broker news item"
FILING_LABEL = "company filing"

_MACRO = re.compile(r"^M:([A-Z0-9_]{1,32})(?:\.([a-z0-9_]{1,16}))?(?:@\d{4}-\d{2}-\d{2})?$")


def _field(field: str) -> str:
    return field.replace("_", " ")


def fact_label(evidence_id: str) -> str:
    """The plain label of an evidence id, without its line or date: `V:NDX:vol_ratio` ->
    "volatility vs its 1-year norm", `M:DGS10.chg20@2026-09-24` -> "10-year Treasury yield ·
    20-day change", `E:earnings:NVDA@2026-10-29` -> "earnings". Unknown fields fall back to the
    field name with spaces; the result is never longer than 80 characters."""
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
    elif prefix == "N":
        label = NEWS_LABEL
    elif prefix == "S":
        label = FILING_LABEL
    elif prefix == "K":
        role, _, n = rest.partition(":")
        label = f"{CARD_ROLES.get(role, role + ' card')} {n}".strip()
    return label[:80]
