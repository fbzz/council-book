"""The live stock universe for the quarterly rank: index membership, the AI list, identity (ticker to
CIK, line ids), sectors, share-class dedupe, price facts, and the assembly of the rank's inputs.

Rules:
- Universe = the current S&P 500 and Nasdaq-100 members (Wikipedia's constituents tables through the
  MediaWiki API: the current page, or the revision as of a date for a historical rank) PLUS the
  AI-adjacent list (user decision, spec L9: ranked mechanically with the index members, same rule
  and filters; a named divergence from the gated universe). Each membership source carries the
  timestamp of its revision; the rank refuses a source older than 120 days or newer than the rank
  date. Wikipedia text is CC BY-SA: member lists are used, never republished (`attribution`).
- `index_constitution` (the study's membership package) is an optional cross-check: loaded only
  when installed, compared by line id (`cross_check`).
- Identity: one line id per ticker, its class separator written `_` (`BRK.B`, `BF-B`, `BRK/B` ->
  `BRK_B`; the public LINE_PATTERN, never `UNMAPPED...`); ticker -> CIK from SEC
  `company_tickers.json`, else the CIK column of the constituents table; one CIK = one company
  (`dedupe_by_cik`: the higher 63-session median dollar volume wins, as the study).
- Sector: the SEC SIC code mapped to Fama-French 12 by the FROZEN `council.stocks.sectors` (no
  overrides); FF12 "Money" is excluded by the rank.
- Foreign filers (latest annual report on 20-F or 40-F: ADRs and other foreign private issuers)
  are flagged `is_adr`, so the rank drops them at the common-stock step. The study drops ADRs there
  and every other 20-F/40-F filer at its domestic-forms step, so the eligible set is the same; only
  the step that counts them differs.
- Fundamentals: SEC companyfacts, always refetched at rank time (forced refresh), run through the
  FROZEN `council.stocks.pit.fundamentals_comparable` (the study's headline data layer).
- Price facts (first and last bar on or before the rank date, 63-session median dollar volume) come
  from the history source through `price_facts_from_bars`; this module never fetches prices.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import yaml

from council.data.cache import FileCache
from council.data.http import DataError, get_with_retry, json_body
from council.paths import POLICY_DIR
from council.policy import LINE_PATTERN, RESERVED_LINE_PREFIX
from council.stocks import pit, sectors
from council.stocks.sec import SecClient, TickerRow

MONEY = "Money"
INDEXES = ("sp500", "nasdaq100")
# The swing movers screen's wider universe (user decision 2026-10-01): + S&P MidCap 400 and S&P
# SmallCap 600, ~1,520 distinct liquid US names. The stock sleeve keeps INDEXES.
SWING_INDEXES = (*INDEXES, "sp400", "sp600")
AI = "ai"
MEMBERSHIP_PAGES = {"sp500": "List of S&P 500 companies", "nasdaq100": "List of NASDAQ-100 companies",
                    "sp400": "List of S&P 400 companies", "sp600": "List of S&P 600 companies"}
MEMBERSHIP_COUNTS = {"sp500": (480, 520), "nasdaq100": (95, 110),   # plausible row counts
                     "sp400": (385, 415), "sp600": (580, 620)}
MEDIAWIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikimedia's user-agent policy wants a contact; a project URL, never the SEC e-mail address.
MEDIAWIKI_USER_AGENT = "council-book/0.1 (https://github.com/fbzz/council-book; index membership, read-only)"
NS_MEDIAWIKI = "mediawiki"
TTL_MEDIAWIKI_CURRENT_S = 6 * 3600
TTL_MEDIAWIKI_ASOF_S = 30 * 24 * 3600     # a past revision never changes
AI_LIST_PATH = POLICY_DIR / "stock-universe-extra.yaml"
DOLLAR_VOLUME_SESSIONS = 63
FOREIGN_ANNUAL_FORMS = ("20-F", "40-F")
DOMESTIC_ANNUAL_FORMS = ("10-K", "10-KT", "10-K405")

_LINE_ID = re.compile(LINE_PATTERN)
_SEPARATORS = re.compile(r"[.\-/ ]")
_TICKER_TEXT = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.\-/][A-Z0-9]{1,2})?$")


class MembershipError(DataError):
    """A membership source could not be read or looks wrong (too few or too many members)."""


# ------------------------------------------------------------------------------------ identity


def normalise_id(symbol: str) -> str:
    """The line id of a ticker: upper case, class separator `.`, `-`, `/` or space written `_`
    (`BRK.B`, `BF-B`, `BRK/B` -> `BRK_B`). Raises ValueError when the result is not a valid line id
    (public LINE_PATTERN) or starts with the reserved `UNMAPPED`."""
    if not isinstance(symbol, str):
        raise ValueError(f"ticker {symbol!r} is not a string")
    text = _SEPARATORS.sub("_", symbol.strip().upper())
    if not _LINE_ID.fullmatch(text) or "__" in text or text.startswith(RESERVED_LINE_PREFIX):
        raise ValueError(f"ticker {symbol!r} does not give a valid line id")
    return text


def try_normalise_id(symbol: Any) -> str | None:
    try:
        return normalise_id(symbol)
    except ValueError:
        return None


def ticker_map(rows: Iterable[TickerRow]) -> dict[str, TickerRow]:
    """SEC tickers keyed by line id. When two rows give the same line id, the first (SEC's order)
    wins; unusable tickers are skipped."""
    out: dict[str, TickerRow] = {}
    for row in rows:
        lid = try_normalise_id(row.ticker)
        if lid is not None:
            out.setdefault(lid, row)
    return out


def sector_of(sic: Any) -> str | None:
    """FF12 sector of a SIC code through the frozen map; None when unknown."""
    return sectors.ff12(sic)


def is_foreign_filer(submissions: Mapping[str, Any]) -> bool:
    """True when the filer's latest annual report in its recent filings is a 20-F or 40-F (an ADR or
    another foreign private issuer). No annual report in the list -> False (the rank's domestic-forms
    step still applies)."""
    recent = ((submissions.get("filings") or {}).get("recent") or {})
    for form in recent.get("form") or []:
        base = str(form).upper().removesuffix("/A")
        if base in FOREIGN_ANNUAL_FORMS:
            return True
        if base in DOMESTIC_ANNUAL_FORMS:
            return False
    return False


# ------------------------------------------------------------------------------------ inputs


@dataclass(frozen=True)
class PriceFacts:
    """What the rank's price filters need: the first and the last bar on or before the rank date and
    the median raw close x volume over the last 63 sessions (share-class dedupe)."""

    first_bar: pd.Timestamp | None = None
    last_bar: pd.Timestamp | None = None
    dollar_volume: float | None = None


@dataclass(frozen=True)
class Candidate:
    """One security the rank considers. `key` is unique in a rank (live: the line id)."""

    key: str
    symbol: str                                 # as the source spells it (BRK.B)
    cik: int | None
    ciks: tuple[int, ...] = ()                  # every CIK the security had (reorganisations)
    sources: frozenset[str] = frozenset()       # sp500, nasdaq100, ai
    security_type: str = "common"
    is_adr: bool = False
    first_bar: pd.Timestamp | None = None
    last_bar: pd.Timestamp | None = None
    dollar_volume: float | None = None
    name: str = ""
    line_id: str | None = None

    @property
    def chain(self) -> tuple[int, ...]:
        """The CIKs whose filings are read, sorted (first report per period across all of them)."""
        base = {int(c) for c in self.ciks}
        if self.cik is not None:
            base.add(int(self.cik))
        return tuple(sorted(base))

    @property
    def ai_only(self) -> bool:
        """In the universe only through the AI list (L9's count)."""
        return self.sources == frozenset({AI})


@dataclass(frozen=True)
class SourceStamp:
    """A membership source and the date of the revision it read."""

    name: str
    as_of: date


@dataclass(frozen=True)
class RankInputs:
    """Everything `council.stocks.rank.rank` reads. Pure data; no network."""

    candidates: tuple[Candidate, ...]
    sic: Mapping[int, Any] = field(default_factory=dict)
    taxonomy: Mapping[int, str] = field(default_factory=dict)     # "us-gaap", "ifrs-full", "none", "no_companyfacts"
    fundamentals: Mapping[int, pd.DataFrame] = field(default_factory=dict)   # pit.COMPARABLE_COLUMNS rows per CIK
    held: tuple[str, ...] = ()                  # the previous rule selection (keys)
    unmapped: tuple[tuple[str, str], ...] = ()  # (symbol, reason) members with no identity
    panel_start: pd.Timestamp | None = None     # a first bar on this day counts as listed earlier
    sources: tuple[SourceStamp, ...] = ()
    notes: tuple[str, ...] = ()                 # identity remarks for the operator (never change the rank)


def dedupe_by_cik(candidates: Sequence[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    """(kept, dropped): one security per CIK, the higher median dollar volume winning; a missing
    volume counts as -1; ties go to the smaller key. Order of first appearance is kept."""
    groups: dict[int, list[Candidate]] = {}
    for c in candidates:
        if c.cik is None:
            raise ValueError(f"candidate {c.key} has no CIK")
        groups.setdefault(int(c.cik), []).append(c)
    kept: list[Candidate] = []
    dropped: list[Candidate] = []
    for group in groups.values():
        best = sorted(group, key=lambda c: (-_liquidity(c), str(c.key)))
        kept.append(best[0])
        dropped += best[1:]
    return kept, dropped


def _liquidity(c: Candidate) -> float:
    v = c.dollar_volume
    return float(v) if v is not None and v == v else -1.0


def price_facts_from_bars(bars: pd.DataFrame | None, asof: date | datetime | str,
                          *, sessions: int = DOLLAR_VOLUME_SESSIONS) -> PriceFacts:
    """Price facts from daily bars (index = the trading date, naive or UTC; columns close, volume)
    using only bars dated on or before `asof`. Dollar volume = close x volume: raw bars give the
    study's figure; split-adjusted bars give nearly the same product (the split factors cancel)."""
    if bars is None or bars.empty:
        return PriceFacts()
    idx = pd.DatetimeIndex(bars.index)
    days = (idx.tz_convert("UTC").tz_localize(None) if idx.tz is not None else idx).normalize()
    mask = days <= pd.Timestamp(asof).normalize()
    if not mask.any():
        return PriceFacts()
    dv = (bars["close"].astype(float) * bars["volume"].astype(float))[mask].tail(sessions).median()
    return PriceFacts(first_bar=days[mask].min(), last_bar=days[mask].max(),
                      dollar_volume=float(dv) if dv == dv else None)


# ------------------------------------------------------------------------------------ membership


@dataclass(frozen=True)
class Membership:
    index: str
    symbols: tuple[str, ...]
    as_of: datetime                              # revision timestamp (UTC)
    source: str                                  # mediawiki | index_constitution
    revision: int | None = None
    title: str = ""
    ciks: Mapping[str, int] = field(default_factory=dict)   # symbol -> CIK when the table has one

    @property
    def attribution(self) -> str:
        if self.source == "mediawiki":
            return (f'Index membership: Wikipedia, "{self.title}", revision {self.revision} '
                    f"({self.as_of:%Y-%m-%d}), CC BY-SA 4.0")
        return f"Index membership: {self.source} ({self.as_of:%Y-%m-%d})"

    def stamp(self) -> SourceStamp:
        return SourceStamp(f"{self.source}:{self.index}", self.as_of.date())


@dataclass(frozen=True)
class MembershipCheck:
    index: str
    only_primary: tuple[str, ...]
    only_secondary: tuple[str, ...]
    overlap: float                               # |both| / |either|, by line id


def cross_check(primary: Membership, secondary: Membership) -> MembershipCheck:
    """Compare two membership sources of one index by line id."""
    a = {try_normalise_id(s) for s in primary.symbols} - {None}
    b = {try_normalise_id(s) for s in secondary.symbols} - {None}
    either = a | b
    return MembershipCheck(primary.index, tuple(sorted(a - b)), tuple(sorted(b - a)),  # type: ignore[arg-type]
                           len(a & b) / len(either) if either else 1.0)


def index_constitution_membership(index: str) -> Membership | None:
    """Current members from the `index_constitution` package when it is installed (optional
    cross-check), else None. `as_of` = its latest recorded membership change."""
    try:
        ic = importlib.import_module("index_constitution")
    except ImportError:
        return None
    latest = ic.latest(index)
    hist = ic.history(index)
    stamps = pd.concat([pd.to_datetime(hist["opt-in"]), pd.to_datetime(hist["opt-out"])]).dropna()
    as_of = stamps.max().to_pydatetime().replace(tzinfo=UTC)
    return Membership(index, tuple(sorted({str(s) for s in latest["symbol"]})), as_of=as_of,
                      source="index_constitution")


def _split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` outside [[...]] and {{...}}."""
    parts: list[str] = []
    depth_link = depth_tpl = 0
    i = start = 0
    while i < len(text):
        two = text[i:i + 2]
        if two == "[[":
            depth_link += 1
            i += 2
        elif two == "]]":
            depth_link = max(0, depth_link - 1)
            i += 2
        elif two == "{{":
            depth_tpl += 1
            i += 2
        elif two == "}}":
            depth_tpl = max(0, depth_tpl - 1)
            i += 2
        elif depth_link == 0 and depth_tpl == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            i += len(sep)
            start = i
        else:
            i += 1
    parts.append(text[start:])
    return parts


def _strip_attrs(cell: str) -> str:
    """`style="..." | content` -> `content`: drop a cell's attribute prefix, which is either empty (a
    row line written `|| content`, as the S&P 500 table does) or holds `name=value` pairs."""
    parts = _split_top(cell, "|")
    head = parts[0].strip()
    if len(parts) > 1 and (not head or ("=" in head and not head.startswith(("[", "{")))):
        return "|".join(parts[1:])
    return cell


def _plain(cell: str) -> str:
    """Display text of a wikitext cell: refs, comments and tags removed; templates -> their first
    argument; links -> their label; bold/italic marks removed."""
    t = re.sub(r"<ref[^>]*/>", "", cell)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.S)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    for _ in range(3):
        t = re.sub(r"\{\{\s*[^|{}]*\|\s*([^|{}]*?)\s*(?:\|[^{}]*)?\}\}", r"\1", t)
        t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[(?:https?:)?//\S+\s+([^\]]*)\]", r"\1", t)
    t = t.replace("'''", "").replace("''", "").replace("&nbsp;", " ")
    return " ".join(t.split())


def _tables(wikitext: str) -> list[tuple[str, list[str]]]:
    """(opening attributes, body lines) of every top-level wikitext table; nested tables skipped."""
    tables: list[tuple[str, list[str]]] = []
    depth, attrs, body = 0, "", []
    for line in wikitext.splitlines():
        s = line.strip()
        if s.startswith("{|"):
            depth += 1
            if depth == 1:
                attrs, body = s[2:], []
            continue
        if s.startswith("|}"):
            depth = max(0, depth - 1)
            if depth == 0:
                tables.append((attrs, body))
            continue
        if depth == 1:
            body.append(line)
    return tables


def _rows(body: Sequence[str]) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    for raw in body:
        s = raw.strip()
        if s.startswith("|-"):
            if cur:
                rows.append(cur)
            cur = []
            continue
        if s.startswith("|+"):
            continue
        if s[:1] in ("!", "|"):
            kind, content = s[0], s[1:]
            cells = _split_top(content, "!!") if kind == "!" else [content]
            for c in cells:
                for piece in _split_top(c, "||"):
                    cur.append((kind, _strip_attrs(piece)))
        elif cur and s:
            kind, text = cur[-1]
            cur[-1] = (kind, text + "\n" + raw)
    if cur:
        rows.append(cur)
    return rows


def _column(header: Sequence[str], names: Sequence[str]) -> int | None:
    for i, h in enumerate(header):
        low = h.lower()
        if any(low == n or low.startswith(n + " ") for n in names):
            return i
    return None


def _symbol(cell: str) -> str | None:
    text = _plain(cell).upper().replace(" ", "")
    return text if _TICKER_TEXT.fullmatch(text) else None


def parse_constituents(wikitext: str) -> tuple[list[str], dict[str, int]]:
    """The ticker column (and the CIK column when present) of a constituents table: the table with
    `id="constituents"`, else the table with a Symbol/Ticker column and the most valid tickers.
    Returns (tickers in table order, without duplicates; {ticker: CIK})."""
    best: tuple[list[str], dict[str, int]] = ([], {})
    for attrs, body in _tables(wikitext):
        rows = _rows(body)
        header_i = next((i for i, r in enumerate(rows) if r and all(k == "!" for k, _ in r)), None)
        if header_i is None:
            continue
        header = [_plain(c) for _, c in rows[header_i]]
        col = _column(header, ("symbol", "ticker", "ticker symbol"))
        if col is None:
            continue
        cik_col = _column(header, ("cik",))
        symbols: list[str] = []
        ciks: dict[str, int] = {}
        for row in rows[header_i + 1:]:
            if len(row) <= col:
                continue
            sym = _symbol(row[col][1])
            if sym is None or sym in symbols:
                continue
            symbols.append(sym)
            if cik_col is not None and len(row) > cik_col:
                digits = re.sub(r"\D", "", _plain(row[cik_col][1]))
                if digits and 0 < int(digits) < 10**10:
                    ciks[sym] = int(digits)
        if 'id="constituents"' in attrs.replace("'", '"').replace(" ", "") or "id=constituents" in attrs:
            return symbols, ciks
        if len(symbols) > len(best[0]):
            best = (symbols, ciks)
    return best


def fetch_membership(index: str, *, asof: date | None = None, transport: httpx.BaseTransport | None = None,
                     refresh: bool = False, cache_root: Path | None = None) -> Membership:
    """Members of `index` from its Wikipedia constituents table: the current revision, or with `asof`
    the last revision on or before the end of that day (UTC). Cached (6 h current, 30 d as-of)."""
    if index not in MEMBERSHIP_PAGES:
        raise ValueError(f"unknown index {index!r}")
    params = {"action": "query", "format": "json", "formatversion": "2", "prop": "revisions",
              "titles": MEMBERSHIP_PAGES[index], "rvprop": "ids|timestamp|content", "rvslots": "main",
              "rvlimit": "1", "redirects": "1"}
    if asof is not None:
        params.update(rvstart=f"{pd.Timestamp(asof):%Y-%m-%d}T23:59:59Z", rvdir="older")
    what = f"mediawiki membership {index}"

    def fetch() -> Any:
        with httpx.Client(transport=transport, timeout=30.0, follow_redirects=True,
                          headers={"User-Agent": MEDIAWIKI_USER_AGENT}) as client:
            return json_body(get_with_retry(client, MEDIAWIKI_API, what=what, params=params), what=what)

    ttl = TTL_MEDIAWIKI_ASOF_S if asof is not None else TTL_MEDIAWIKI_CURRENT_S
    payload = FileCache(NS_MEDIAWIKI, root=cache_root).get_or_fetch(params, ttl, fetch, fmt="json.gz",
                                                                    refresh=refresh)
    try:
        page = payload["query"]["pages"][0]
        rev = page["revisions"][0]
        content = rev["slots"]["main"]["content"]
        stamp = datetime.fromisoformat(str(rev["timestamp"]).replace("Z", "+00:00")).astimezone(UTC)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise MembershipError(f"{what}: unexpected API response") from exc
    symbols, ciks = parse_constituents(content)
    lo, hi = MEMBERSHIP_COUNTS[index]
    if not lo <= len(symbols) <= hi:
        raise MembershipError(f"{what}: {len(symbols)} members parsed, expected {lo}-{hi}")
    return Membership(index, tuple(symbols), as_of=stamp, source="mediawiki", revision=int(rev.get("revid") or 0),
                      title=str(page.get("title") or MEMBERSHIP_PAGES[index]), ciks=ciks)


# ------------------------------------------------------------------------------------ the AI list


class _TickerSafeLoader(yaml.SafeLoader):
    """SafeLoader without the boolean resolver, so a ticker such as ON stays a string. The resolver
    table is a copy: yaml.SafeLoader itself keeps its booleans."""

    yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
        for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


def load_ai_list(path: Path = AI_LIST_PATH) -> tuple[str, ...]:
    """The AI-adjacent tickers, sorted and unique: a top-level `tickers:` list, or the lab's layout
    (`peer_groups` / `groups`: {group: {tickers: [...]}} or {group: [...]}). Every ticker must give a
    valid line id."""
    doc = yaml.load(Path(path).read_text(), Loader=_TickerSafeLoader) or {}
    tickers: list[str] = [str(t) for t in doc.get("tickers") or []]
    for group in (doc.get("peer_groups") or doc.get("groups") or {}).values():
        members = group.get("tickers", group) if isinstance(group, dict) else group
        tickers += [str(t) for t in members or []]
    bad = [t for t in tickers if try_normalise_id(t) is None]
    if bad:
        raise ValueError(f"AI list {Path(path).name}: invalid tickers {sorted(set(bad))}")
    if not tickers:
        raise ValueError(f"AI list {Path(path).name} lists no tickers")
    return tuple(sorted(set(tickers)))


# ------------------------------------------------------------------------------------ assembly


def build_rank_inputs(asof: date, *, memberships: Sequence[Membership], sec: SecClient,
                      price_facts: Mapping[str, PriceFacts], ai_symbols: Sequence[str] = (),
                      held: Sequence[str] = (), refresh: bool = True,
                      predecessors: Mapping[int, Sequence[int]] | None = None) -> RankInputs:
    """The rank's inputs from live sources. Keys are line ids; `price_facts` is keyed by line id.

    Requests: one tickers file, one submissions document per mapped CIK, and companyfacts (forced
    refresh by default) only for CIKs the rank can reach: a known non-Money sector and not a
    foreign filer (the rank excludes the others before it reads any filing)."""
    entries: dict[str, dict[str, Any]] = {}
    unmapped: list[tuple[str, str]] = []

    def add(symbol: str, source: str, wiki_cik: int | None = None) -> None:
        lid = try_normalise_id(symbol)
        if lid is None:
            unmapped.append((str(symbol), "bad_symbol"))
            return
        e = entries.setdefault(lid, {"symbol": str(symbol), "sources": set(), "wiki_cik": None})
        e["sources"].add(source)
        if wiki_cik is not None and e["wiki_cik"] is None:
            e["wiki_cik"] = wiki_cik

    for m in memberships:
        for s in m.symbols:
            add(s, m.index, m.ciks.get(s))
    for s in ai_symbols:
        add(s, AI)

    tickers = ticker_map(sec.company_tickers(refresh=refresh))
    extra = predecessors or {}
    candidates: list[Candidate] = []
    notes: list[str] = []
    for lid in sorted(entries):
        e = entries[lid]
        row = tickers.get(lid)
        cik = row.cik if row is not None else e["wiki_cik"]
        if row is not None and e["wiki_cik"] is not None and e["wiki_cik"] != row.cik:
            notes.append(f"cik_mismatch:{lid}:sec={row.cik}:table={e['wiki_cik']}")
        elif row is None and cik is not None:
            notes.append(f"cik_from_table:{lid}:{cik}")
        if cik is None:
            unmapped.append((e["symbol"], "no_cik"))
            continue
        pf = price_facts.get(lid) or PriceFacts()
        candidates.append(Candidate(
            key=lid, symbol=e["symbol"], cik=int(cik), ciks=tuple(sorted({int(cik), *map(int, extra.get(int(cik), ()))})),
            sources=frozenset(e["sources"]), first_bar=pf.first_bar, last_bar=pf.last_bar,
            dollar_volume=pf.dollar_volume, name=row.title if row is not None else "", line_id=lid))

    sic: dict[int, Any] = {}
    foreign: set[int] = set()
    for cik in sorted({int(c.cik) for c in candidates if c.cik is not None}):
        sub = sec.submissions(cik, refresh=refresh)
        sic[cik] = sub.get("sic")
        if is_foreign_filer(sub):
            foreign.add(cik)
    wanted: set[int] = set()
    for c in candidates:
        if c.cik is not None and c.cik not in foreign and sector_of(sic.get(c.cik)) not in (None, MONEY):
            wanted |= set(c.chain)
    taxonomy: dict[int, str] = {}
    fundamentals: dict[int, pd.DataFrame] = {}
    for cik in sorted(wanted):
        doc = sec.companyfacts(cik, refresh=refresh)
        if doc is None:
            taxonomy[cik] = "no_companyfacts"
            continue
        rows, stats = pit.fundamentals_comparable(str(cik), str(cik), doc)
        taxonomy[cik] = str(stats["taxonomy"])
        fundamentals[cik] = rows
    candidates = [replace(c, is_adr=c.cik in foreign) for c in candidates]
    return RankInputs(candidates=tuple(candidates), sic=sic, taxonomy=taxonomy, fundamentals=fundamentals,
                      held=tuple(held), unmapped=tuple(unmapped), panel_start=None,
                      sources=tuple(m.stamp() for m in memberships), notes=tuple(notes))
