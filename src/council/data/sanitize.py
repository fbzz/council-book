"""Untrusted text hygiene for broker feed items (and any other third-party string).

Rule: text that reaches a prompt, a terminal or a page carries no control or escape sequences,
no bidirectional/invisible format characters, no URLs, e-mail addresses or @handles, no HTML tags,
and is at most `max_len` characters. Removal happens in an order that stops one layer from hiding
another (entities are decoded and width variants folded before patterns are matched)."""

from __future__ import annotations

import html
import re
import unicodedata

# ANSI/VT escape sequences, 7-bit (ESC-introduced) and 8-bit (C1-introduced).
_OSC = re.compile(r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?")
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?")
_DCS_LIKE = re.compile(r"(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c)?")
_ESC_OTHER = re.compile(r"\x1b[ -/]*[0-~]?")

_MD_LINK = re.compile(r"!?\[([^\]]{0,300})\]\([^)]{0,500}\)")
_URL_SCHEME = re.compile(r"(?i)\b(?:https?|hxxps?|ftp|file|javascript|data|mailto|tg):\S*")
_URL_WWW = re.compile(r"(?i)\bwww\.\S+")
_TLDS = (
    "com|net|org|io|co|app|xyz|info|biz|ly|me|gg|to|ru|cn|tk|top|site|online|link|click|live|"
    "finance|money|trade|exchange|capital|shop|club|vip|pro|cc|ws|su|uk|de|eu"
)
_URL_BARE = re.compile(
    rf"(?i)\b[a-z0-9](?:[a-z0-9-]{{0,62}}[a-z0-9])?(?:\.[a-z0-9-]{{1,63}})*\.(?:{_TLDS})\b(?:/\S*)?"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_.]{1,40}")
_TAG = re.compile(r"</?[A-Za-z!][^<>]{0,500}>")
_WS = re.compile(r"\s+")

_LINE_BREAKERS = frozenset({"\t", "\n", "\r", "\v", "\f", "\x85", " ", " "})
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})


def strip_escapes(text: str) -> str:
    """Remove whole terminal escape sequences (OSC 52 clipboard writes, CSI colours, DCS...)."""
    for pattern in (_OSC, _DCS_LIKE, _CSI, _ESC_OTHER):
        text = pattern.sub("", text)
    return text


def strip_controls(text: str) -> str:
    """Whitespace controls become a space; every other control/format/surrogate/private char goes.

    Covers C0, DEL, C1, ESC, bidi overrides and isolates (U+202A-E, U+2066-9), zero-width
    characters, the BOM and lone surrogates (which would break UTF-8 hashing downstream)."""
    out: list[str] = []
    for ch in text:
        if ch in _LINE_BREAKERS:
            out.append(" ")
        elif unicodedata.category(ch) not in _DROP_CATEGORIES:
            out.append(ch)
    return "".join(out)


def strip_links(text: str) -> str:
    """Markdown links keep their label; URLs, bare domains, e-mails and @handles are removed."""
    text = _MD_LINK.sub(r"\1", text)
    for pattern in (_URL_SCHEME, _URL_WWW, _EMAIL, _URL_BARE, _HANDLE):
        text = pattern.sub(" ", text)
    return text


def clean_text(s: object, max_len: int) -> str:
    """Sanitise one untrusted string for prompts and terminals; result length <= max_len."""
    if max_len < 0:
        raise ValueError("max_len must be >= 0")
    if s is None:
        return ""
    text = s if isinstance(s, str) else str(s)
    text = html.unescape(text)                  # "&#27;" must not survive as a hidden ESC
    text = strip_escapes(text)
    text = unicodedata.normalize("NFKC", text)  # fold full-width "＠" / "ｈｔｔｐ" lookalikes
    text = strip_controls(text)                 # zero-width chars can no longer split a handle
    text = _TAG.sub(" ", text)                  # before links, so <a href=...> goes whole
    text = strip_links(text)
    text = _WS.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text
