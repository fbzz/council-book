"""Deterministic evidence IDs. The auditor rejects any cited ID that is not in the cycle's pack.

Formats (models/common.py): F:<line>:<field> market (and fundamental, kind "fundamental", with a
field from FUNDAMENTAL_FIELDS) · V:<line>:<field> volatility ·
C:<line>:<field> cost · M:<series>@<YYYY-MM-DD> macro · E:<kind>[:<symbol>]@<YYYY-MM-DD> event ·
N:<8 hex> broker feed news · P:<8 hex> public-domain news. Rule: every component is non-empty and
uses [A-Za-z0-9_.-] only, so an ID can never smuggle a separator, whitespace or markup into a prompt
or the journal.

News ids (transparency-v2 §3.2, T-D15):
- `P:` (public-domain items: SEC, Federal Reserve Board, BLS, BEA, Treasury, EIA) =
  "P:" + sha256(source NUL stable key)[:8] (`public_news_id`); the inputs are public, so anyone can
  recompute the id.
- `N:` (the broker's feed, eToro Licensed Content) = "N:" + HMAC-SHA256(install key, post id)[:8]
  (`broker_news_id`), keyed by the private install key (`council.publish.install_key`), so a
  published id cannot be matched back to a post by brute force. The cycle always passes the key;
  `news_id` (an unkeyed sha256) remains only for offline tests and old records."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

from council.models.facts import broker_news_id, public_news_id

_PART = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")

# Field names used in F:/V: IDs (a stable vocabulary other modules and prompts may cite).
MARKET_FIELDS: tuple[str, ...] = (
    "trend", "dist_sma50", "dist_sma200", "mom10d", "mom63d", "dd52", "ret1d_sigma",
    "data_age_h", "market_open",
)
VOL_FIELDS: tuple[str, ...] = ("sigma_ann", "vol_ratio", "ewma5_60")
# Stock lines' fundamentals (design §11.2; kind "fundamental", available at the SEC acceptance time of
# the filing they come from): revenue growth year on year, its acceleration, the gross and operating
# margin changes (percentage points), the rank's within-sector and global scores, the age of the
# latest filing in days, and the FF12 sector.
FUNDAMENTAL_FIELDS: tuple[str, ...] = (
    "rev_yoy", "rev_accel", "gm_chg", "om_chg", "sector_pct", "composite", "filing_age_d", "sector",
)
# Fundamental fields that move with the calendar, not with new evidence (left out of the per-line
# material fingerprint, runtime.material_fingerprints).
FUNDAMENTAL_NOT_MATERIAL: frozenset[str] = frozenset({"filing_age_d"})
if set(FUNDAMENTAL_FIELDS) & set(MARKET_FIELDS):
    raise AssertionError("fundamental and market fields share the F: namespace; they must not overlap")


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


def fundamental_id(line: str, field: str) -> str:
    """Fundamental fact of a stock line, e.g. F:BRK_B:rev_yoy (field from FUNDAMENTAL_FIELDS)."""
    if field not in FUNDAMENTAL_FIELDS:
        raise ValueError(f"unknown fundamental field {field!r}")
    return fact_id(line, field)


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
    """LEGACY unkeyed broker news id, e.g. N:1a2b3c4d — first 8 hex chars of SHA-256 of the item's
    stable key. Offline tests and old records only: a cycle keys `N:` ids (`feed_news_id`)."""
    if not key:
        raise ValueError("news id needs a non-empty key")
    return "N:" + hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def feed_news_id(post_key: str, install_key: bytes | None = None) -> str:
    """A broker feed item's id: the keyed `broker_news_id` under the install key, or the legacy
    unkeyed `news_id` when no key is given (offline tests)."""
    if install_key is None:
        return news_id(post_key)
    return broker_news_id(post_key, install_key)


__all__ = [
    "FUNDAMENTAL_FIELDS", "FUNDAMENTAL_NOT_MATERIAL", "MARKET_FIELDS", "VOL_FIELDS", "broker_news_id",
    "cost_id", "event_id", "fact_id", "feed_news_id", "fundamental_id", "macro_id", "news_id",
    "public_news_id", "vol_id",
]
