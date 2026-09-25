"""eToro news feed -> NewsItem list. v1 reads the news feed only (no instrument discussion posts).

Rules:
- Keep only: stable id, title, summary (summary, else aiSummary), created time, market symbols,
  and the earnings fields. Authors, handles, avatars, message bodies and links are never read.
- Text is sanitised (sanitize.clean_text) before it can reach a prompt.
- ID = "N:" + sha256(post id, else title + created)[:8], stable across fetches.
- An item without a parseable created time is dropped (its availability cannot be proven); an
  item created after `now` + 5 min is dropped as clock-skewed. available_at = created.
- Field nesting varies between feed versions, so every lookup tolerates the known shapes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from council.data.bars import to_utc
from council.data.sanitize import clean_text
from council.facts.evidence_ids import news_id
from council.models.facts import NewsItem

TITLE_MAX = 200
SUMMARY_MAX = 600
SYMBOLS_MAX = 10
FUTURE_SKEW = timedelta(minutes=5)
SOURCE = "etoro_feed"
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def _dig(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _entries(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, Mapping):
        items = _first(*(payload.get(k) for k in ("discussions", "items", "data", "posts"))) or []
    else:
        items = []
    return [x for x in items if isinstance(x, Mapping)]


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return to_utc(str(value)).to_pydatetime()
    except (TypeError, ValueError):
        return None


def _symbol_of(tag: Any) -> str | None:
    if isinstance(tag, str):
        raw = tag
    elif isinstance(tag, Mapping):
        raw = _first(
            _dig(tag, "market", "symbolName"),
            _dig(tag, "market", "symbol"),
            tag.get("symbolName"),
            tag.get("symbol"),
        )
    else:
        raw = None
    if not isinstance(raw, str):
        return None
    sym = raw.strip().lstrip("$").upper()
    return sym if _SYMBOL.match(sym) else None


def _symbols(post: Mapping[str, Any], entry: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for source in (post, entry):
        for key in ("tags", "markets", "instruments"):
            values = source.get(key)
            if not isinstance(values, list):
                continue
            for tag in values:
                sym = _symbol_of(tag)
                if sym and sym not in out:
                    out.append(sym)
    return out[:SYMBOLS_MAX]


def parse_item(entry: Mapping[str, Any], *, now: datetime) -> NewsItem | None:
    """One feed entry -> NewsItem, or None when it cannot be admitted safely."""
    post = entry.get("post") if isinstance(entry.get("post"), Mapping) else entry
    created = _parse_time(
        _first(post.get("created"), entry.get("created"), post.get("createdAt"), entry.get("createdAt"))
    )
    if created is None or created > to_utc(now).to_pydatetime() + FUTURE_SKEW:
        return None
    title = clean_text(_first(post.get("title"), entry.get("title")), TITLE_MAX)
    summary = clean_text(
        _first(post.get("summary"), entry.get("summary"), post.get("aiSummary"), entry.get("aiSummary")),
        SUMMARY_MAX,
    )
    if not title and not summary:
        return None
    if not title:
        title = summary[:TITLE_MAX]
    raw_id = _first(entry.get("id"), post.get("id"), post.get("postId"), entry.get("postId"))
    key = str(raw_id) if raw_id is not None else title + created.isoformat()
    event = _first(post.get("marketEvent"), entry.get("marketEvent"))
    earnings = _parse_time(_dig(event, "earningsDate")) if event else None
    before_open = _dig(event, "isBeforeMarketOpen") if event else None
    return NewsItem(
        id=news_id(key),
        title=title,
        summary=summary,
        symbols=_symbols(post, entry),
        published_at=created,
        available_at=created,
        source=SOURCE,
        earnings_date=earnings,
        before_market_open=before_open if isinstance(before_open, bool) else None,
    )


def parse_news_feed(payload: Any, *, now: datetime) -> list[NewsItem]:
    """All admissible items, de-duplicated by ID, newest first (ties broken by ID)."""
    seen: dict[str, NewsItem] = {}
    for entry in _entries(payload):
        item = parse_item(entry, now=now)
        if item is not None and item.id not in seen:
            seen[item.id] = item
    return sort_news(seen.values())


def sort_news(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Deterministic order: newest first, then by ID."""
    return sorted(items, key=lambda n: (-n.published_at.timestamp(), n.id))
