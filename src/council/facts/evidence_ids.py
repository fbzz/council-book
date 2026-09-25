"""Deterministic evidence IDs. The auditor rejects any cited ID that is not in the cycle's pack.

Formats (models/common.py): F:<line>:<field> market · V:<line>:<field> volatility ·
C:<line>:<field> cost · M:<series>@<YYYY-MM-DD> macro · E:<kind>[:<symbol>]@<YYYY-MM-DD> event ·
N:<sha256[:8]> news. Rule: every component is non-empty and uses [A-Za-z0-9_.-] only, so an ID
can never smuggle a separator, whitespace or markup into a prompt or the journal."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

_PART = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")

# Field names used in F:/V: IDs (a stable vocabulary other modules and prompts may cite).
MARKET_FIELDS: tuple[str, ...] = (
    "trend", "dist_sma50", "dist_sma200", "mom10d", "mom63d", "dd52", "ret1d_sigma",
    "data_age_h", "market_open",
)
VOL_FIELDS: tuple[str, ...] = ("sigma_ann", "vol_ratio", "ewma5_60")


def _part(value: object, what: str) -> str:
    text = str(value)
    if not _PART.match(text):
        raise ValueError(f"bad evidence-id {what} {text!r}")
    return text


def _day(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)[:10]
    date.fromisoformat(text)
    return text


def fact_id(line: str, field: str) -> str:
    """Market fact, e.g. F:NDX:dist_sma200."""
    return f"F:{_part(line, 'line')}:{_part(field, 'field')}"


def vol_id(line: str, field: str) -> str:
    """Volatility fact, e.g. V:NDX:vol_ratio."""
    return f"V:{_part(line, 'line')}:{_part(field, 'field')}"


def cost_id(line: str, field: str) -> str:
    """Cost fact, e.g. C:NDX:bps_side."""
    return f"C:{_part(line, 'line')}:{_part(field, 'field')}"


def macro_id(series: str, day: date | datetime | str) -> str:
    """Macro value, e.g. M:DGS10@2026-09-24 (the observation date, not the fetch date)."""
    return f"M:{_part(series, 'series')}@{_day(day)}"


def event_id(kind: str, at: date | datetime | str, symbol: str | None = None) -> str:
    """Scheduled event, e.g. E:fomc@2026-10-28 or E:earnings:AAPL@2026-10-29."""
    scope = f":{_part(symbol, 'symbol')}" if symbol else ""
    return f"E:{_part(kind, 'kind')}{scope}@{_day(at)}"


def news_id(key: str) -> str:
    """News item, e.g. N:1a2b3c4d — first 8 hex chars of SHA-256 of the item's stable key."""
    if not key:
        raise ValueError("news id needs a non-empty key")
    return "N:" + hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:8]
