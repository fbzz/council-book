"""Public-domain content for the Scout's SEC items (swing reading list; SW-5c follow-up).

The current-filings feed gives only a form and item codes ("8-K Item 8.01 Other Events"), which says
nothing a reader can judge. For a bounded number of filings per slot this module reads the filing
itself from EDGAR (U.S. government work, public domain) and extracts, as plain text:

- `item_text`: for an 8-K / 8-K/A, the opening of the first substantive item's text (not 9.01);
- `headline` + `excerpt`: the first EX-99 exhibit's headline and its first EXCERPT_CHARS characters
  (the press release), or, for a 6-K without an EX-99, the report's own first headline;
- `english`: whether the headline and excerpt read as English (a 6-K without an English headline is
  left out of the reading list by `swing.intake`).

Requests: the filing index (`<accession>-index.htm`), then at most the main document and the first
EX-99, i.e. <= MAX_REQUESTS_PER_FILING, all through the caller's `get(url) -> str` (production: the
shared, paced `SecClient` session with its user agent, SEC <= 7 requests a second). Only EDGAR
Archives paths are followed. Results are cached per accession (filings are immutable; an empty
result is cached too, so a filing is fetched at most once per TTL). Text is cleaned and leak-scanned
by `swing.intake` before it reaches a prompt; nothing here publishes.
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from council.stocks.sec_news import Filing

ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{acc}-index.htm"
SEC_HOST = "https://www.sec.gov"
NAMESPACE = "sec-filing-text"
CACHE_VERSION = 1
TTL_S = 30 * 24 * 3600
MAX_REQUESTS_PER_FILING = 3
HTML_MAX = 250_000                  # characters of a document parsed (headlines sit at the top)
HEADLINE_MAX = 200
EXCERPT_CHARS = 600
ITEM_TEXT_MAX = 300
COVER_SCAN = 120                    # blocks of a 6-K searched for the end of its cover page

_BLOCK_END = re.compile(r"(?i)<br\s*/?>|</(?:p|div|tr|h[1-6]|li|td|th|table|center)\s*>")
_HIDDEN = re.compile(r"(?is)<(script|style|ix:header|head)\b[^>]*>.*?</\1\s*>")
_DISPLAY_NONE = re.compile(r"(?is)<div[^>]*display:\s*none[^>]*>.*?</div\s*>")
_SGML_HEAD = re.compile(r"(?is)^.*?<TEXT>")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ROW = re.compile(r"(?is)<tr[^>]*>(.*?)</tr\s*>")
_CELL = re.compile(r"(?is)<td[^>]*>(.*?)</td\s*>")
_HREF = re.compile(r'(?i)href="([^"]+)"')
_ITEM_HEAD = re.compile(r"(?i)^item\s*(\d\.\d{2})\b\.?")
_BOILERPLATE = re.compile(
    r"(?i)^(?:exhibit\s*\d|ex-\d|for immediate release|press release|news release|media release|"
    r"united states|information contained in this report|under the securities|of foreign private issuer|for the month of|\(?translation|securities and exchange commission|washington|form\s*(?:6|8)-k|current report|"
    r"report of (?:a )?foreign (?:private )?issuer|pursuant to|commission file|indicate by check|"
    r"\(?address of|\(?exact name|signatures?\b|exhibit index|table of contents|contacts?:?$|"
    r"investor (?:relations|contact)|media contact|date of report|check the appropriate|"
    r"securities registered|incorporation by reference|forward[- ]looking|safe harbor|"
    r"\(?registrant|\(?commission|\(?state or other|\(?i\.r\.s\.|n/a$)")
_STOPWORDS = (" the ", " and ", " of ", " to ", " in ", " for ", " with ", " on ")


@dataclass(frozen=True)
class IndexDoc:
    seq: str
    description: str
    href: str
    type: str


@dataclass(frozen=True)
class FilingText:
    headline: str = ""
    excerpt: str = ""
    item_text: str = ""
    english: bool = False
    exhibit: str = ""                # the EX-99 type read ("EX-99.1"), "" when the main document

    @property
    def empty(self) -> bool:
        return not (self.headline or self.excerpt or self.item_text)


# ------------------------------------------------------------------------------------ parse
def text_blocks(raw: str, *, limit: int = HTML_MAX) -> list[str]:
    """An HTML (or SGML-wrapped) document as its non-empty text blocks, whitespace collapsed."""
    doc = raw[:limit]
    if "<TEXT>" in doc[:5000]:
        doc = _SGML_HEAD.sub("", doc, count=1)
    doc = _HIDDEN.sub(" ", doc)
    doc = _DISPLAY_NONE.sub(" ", doc)
    doc = _BLOCK_END.sub("\x00", _WS.sub(" ", doc))
    doc = _html.unescape(_TAG.sub(" ", doc)).replace("\xa0", " ")
    return [b for b in (_WS.sub(" ", part).strip() for part in doc.split("\x00")) if b]


def parse_index(raw: str) -> list[IndexDoc]:
    """The filing index's document rows (Seq, Description, Document, Type), EDGAR Archives only."""
    out: list[IndexDoc] = []
    for row in _ROW.findall(raw):
        cells = _CELL.findall(row)
        if len(cells) < 4:
            continue
        m = _HREF.search(cells[2])
        if m is None:
            continue
        href = m.group(1).removeprefix("/ix?doc=")
        if not href.startswith("/Archives/edgar/data/") or not re.search(r"(?i)\.(?:htm|html|txt)$", href):
            continue
        clean = [_WS.sub(" ", _html.unescape(_TAG.sub(" ", c))).strip() for c in cells]
        out.append(IndexDoc(seq=clean[0], description=clean[1], href=SEC_HOST + href, type=clean[3].upper()))
    return out


def _words(text: str) -> int:
    return len(text.split())


def _cut(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head = text[:cap].rsplit(" ", 1)[0]
    return head.rstrip(" ,;:") + "..."


def lead(blocks: list[str]) -> tuple[str, str]:
    """(headline, excerpt): the first non-boilerplate block of >= 4 words, and the text after it."""
    for i, block in enumerate(blocks):
        short = _words(block) < 4 and (block.isascii() or len(block) < 12)   # CJK text has no spaces
        if short or _BOILERPLATE.match(block) or not re.search(r"[^\W\d_]{3}", block):
            continue
        if len(block) > HEADLINE_MAX * 2:       # no separate headline: its first sentence
            first = re.split(r"(?<=[.!?])\s", block, maxsplit=1)
            head = _cut(first[0], HEADLINE_MAX)
            rest = first[1] if len(first) > 1 else ""
            return head, _cut(" ".join([rest, *blocks[i + 1:i + 12]]).strip(), EXCERPT_CHARS)
        return _cut(block, HEADLINE_MAX), _cut(" ".join(blocks[i + 1:i + 12]).strip(), EXCERPT_CHARS)
    return "", ""


def after_cover(blocks: list[str]) -> list[str]:
    """A 6-K's blocks after its cover page (the cover ends with the Form 20-F / 40-F check box);
    none when no cover end is found near the top."""
    ends = [i for i, b in enumerate(blocks[:COVER_SCAN]) if re.search(r"(?i)form\s*40-f", b)]
    return blocks[ends[-1] + 1:] if ends else []


def item_text(blocks: list[str], items: tuple[str, ...]) -> str:
    """The opening of the first substantive 8-K item's text (item headings found in the body)."""
    wanted = [c for c in items if c != "9.01"]
    for i, block in enumerate(blocks):
        m = _ITEM_HEAD.match(block)
        if m is None or m.group(1) not in wanted:
            continue
        body = [block[m.end():].strip()]
        for nxt in blocks[i + 1:i + 12]:
            if _ITEM_HEAD.match(nxt) or re.match(r"(?i)^signatures?\b", nxt):
                break
            body.append(nxt)
        text = " ".join(b for b in body if b)
        # drop the item's own title ("Other Events.") when the body repeats the heading
        text = re.sub(r"^[A-Z][^.]{0,90}\.\s+", "", text, count=1) if _words(text) > 12 else text
        if _words(text) >= 6:
            return _cut(text, ITEM_TEXT_MAX)
    return ""


def is_english(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 20:
        return False
    ascii_share = sum(c.isascii() for c in letters) / len(letters)
    low = f" {text.lower()} "
    return ascii_share >= 0.95 and sum(w in low for w in _STOPWORDS) >= 2


def _exhibit(docs: list[IndexDoc]) -> IndexDoc | None:
    ex = [d for d in docs if d.type.startswith("EX-99")]
    return ex[0] if ex else None


def _main(docs: list[IndexDoc], form: str) -> IndexDoc | None:
    base = form.split("/")[0].upper()
    for d in docs:
        if d.type.startswith(base) and not d.href.endswith(".txt"):
            return d
    return None


def filing_text(filing: Filing, get: Callable[[str], str]) -> FilingText:
    """Read one filing (<= MAX_REQUESTS_PER_FILING requests through `get`). Raises on a request
    failure (the caller flags it and caches nothing)."""
    acc = filing.accession
    docs = parse_index(get(ARCHIVES.format(cik=int(filing.cik), nodash=acc.replace("-", ""), acc=acc)))
    ex, main = _exhibit(docs), _main(docs, filing.form)
    itext = ""
    if filing.form.startswith("8-K") and main is not None and any(c != "9.01" for c in filing.items):
        itext = item_text(text_blocks(get(main.href)), filing.items)
    headline = excerpt = ""
    exhibit = ""
    if ex is not None:
        headline, excerpt = lead(text_blocks(get(ex.href)))
        exhibit = ex.type
    elif filing.form.startswith("6-K") and main is not None:
        headline, excerpt = lead(after_cover(text_blocks(get(main.href))))
    english = is_english(" ".join((headline, excerpt, itext)))
    return FilingText(headline=headline, excerpt=excerpt, item_text=itext, english=english, exhibit=exhibit)


# ------------------------------------------------------------------------------------ cache
class Enricher:
    """Per-slot, bounded, cached reader: `__call__(filing)` -> FilingText | None (None: budget spent
    or the request failed, flagged). At most `max_fetches` uncached filings per instance."""

    def __init__(self, get: Callable[[str], str], *, cache_root: Path | None, max_fetches: int,
                 flags: list[str] | None = None) -> None:
        from council.data.cache import FileCache

        self._get = get
        self._cache = FileCache(NAMESPACE, root=cache_root) if cache_root is not None else None
        self.max_fetches = int(max_fetches)
        self.fetched = 0
        self.flags = flags if flags is not None else []

    def _key(self, filing: Filing) -> str:
        from council.data.cache import request_key

        return request_key({"accession": filing.accession, "v": CACHE_VERSION})

    def cached(self, filing: Filing) -> FilingText | None:
        if self._cache is None:
            return None
        raw = self._cache.get(self._key(filing), TTL_S)
        return FilingText(**raw) if isinstance(raw, dict) else None

    def __call__(self, filing: Filing) -> FilingText | None:
        hit = self.cached(filing)
        if hit is not None:
            return hit
        if self.fetched >= self.max_fetches:
            return None
        self.fetched += 1
        try:
            text = filing_text(filing, self._get)
        except Exception as exc:  # noqa: BLE001 - a failed read leaves the item header-only
            self.flags.append(f"swing_sec_text_error:{type(exc).__name__}")
            return None
        if self._cache is not None:
            self._cache.put(self._key(filing), asdict(text))
        return text


def client_get(sec: Any) -> Callable[[str], str]:
    """`get(url)` through a `SecClient`'s paced session (its user agent and <= 7 requests/s bucket),
    with the news timeouts and at most one retry."""
    from council.data import gov_news
    from council.data.http import get_with_retry
    from council.stocks.sec_news import FEED_TIMEOUT, _Timed

    def get(url: str) -> str:
        if not url.startswith(SEC_HOST + "/Archives/edgar/data/"):
            raise ValueError("only EDGAR Archives documents are read")
        response = get_with_retry(_Timed(sec._http, FEED_TIMEOUT), url, what="sec filing document",
                                  retries=gov_news.RETRIES, backoff_s=gov_news.BACKOFF_S, fail_fast_429=True)
        return response.text

    return get
