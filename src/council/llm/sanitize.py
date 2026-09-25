"""Sanitization of every string an LLM returns (and of feed text before a model reads it).

Rule: model output can be steered by prompt injection from feed posts, and its strings end up in
logs, terminals and (percent-only) public pages. So every string is cleaned before it is trusted:
  - ANSI escape sequences are removed whole; remaining C0 controls (incl. ESC), DEL and C1
    controls are removed; tabs/newlines become spaces;
  - bidi overrides/isolates and zero-width characters are removed (they can disguise text);
  - URLs, e-mail addresses and @handles are removed (no links or people in the record);
  - whitespace is collapsed.
Sanitizing never lengthens a string, so it cannot push a valid field over its max length.
"""

from __future__ import annotations

import re
from typing import Any

# C0 (0x00-0x1F) incl. ESC 0x1B, DEL 0x7F, C1 (0x80-0x9F). Tab/newline/CR handled separately.
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_WHITESPACE_CONTROLS = re.compile(r"[\t\n\r\v\f]")
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")
# Bidi embeddings/overrides/isolates, LRM/RLM/ALM, zero-width space/joiners, BOM, word joiner.
_INVISIBLE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u200b-\u200d\u2060\ufeff]")
_URL = re.compile(r"(?i)\b(?:https?|ftp|file)://\S+|\bwww\.\S+")
_BARE_DOMAIN = re.compile(
    r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
    r"(?:com|net|org|io|co|ai|app|xyz|info|biz|me|ly|gg|to|link|site|online|finance|news)\b(?:/\S*)?"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
_HANDLE = re.compile(r"(?<![\w.@])@[A-Za-z0-9_]{1,30}\b")
_SPACES = re.compile(r"[ ]{2,}")

# Dollar/euro/pound amounts: the council's text must stay percent-only.
_AMOUNT = re.compile(
    r"(?i)(?:US)?[$\u20ac\u00a3]\s?\d[\d,.]*(?:\s?(?:k|m|bn|b|mm|million|billion|trillion)\b)?"
)


def sanitize_text(value: str) -> str:
    """Clean one string (see module docstring). Idempotent and never longer than the input."""
    text = _ANSI.sub("", value)
    text = _WHITESPACE_CONTROLS.sub(" ", text)
    text = _CONTROLS.sub("", text)
    text = _INVISIBLE.sub("", text)
    text = _URL.sub("", text)
    text = _EMAIL.sub("", text)
    text = _BARE_DOMAIN.sub("", text)
    text = _HANDLE.sub("", text)
    text = _SPACES.sub(" ", text)
    return text.strip()


def scrub_amounts(value: str) -> str:
    """Replace currency amounts (e.g. a dollar sign followed by digits) with `[amount]`."""
    return _AMOUNT.sub("[amount]", value)


def sanitize_obj(obj: Any) -> Any:
    """Recursively sanitize every string in a decoded JSON value, including dict keys."""
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, dict):
        return {
            (sanitize_text(k) if isinstance(k, str) else k): sanitize_obj(v) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [sanitize_obj(v) for v in obj]
    return obj
