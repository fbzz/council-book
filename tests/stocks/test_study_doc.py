"""docs/stock-sleeve-study.md is public: percent-only, and no two of its numbers disclose the funding.

The broker's fixed fee per real trade is public (`policy/costs.yaml`). A book's fee drag (a share of its
capital a year) divided by its trade count a year is that fee over the book's capital, so publishing
both for the same book, or a per-trade share directly, gives away how much real money funds the
account. The fee drag itself is implied whenever a book appears both net of the fixed fee and before
it, which the gate requires (G1, G3). So the page must never carry a trade count, a turnover (turnover
divided by the size of a trade estimates the count) or a per-trade share.

Checks:
- the leak scan is clean;
- no table column, row label, cell or sentence that names a trade count, turnover, fee or cost drag,
  a per-trade share, the fee arithmetic or feasibility, a share price or an account size carries a
  number (so the parser below cannot miss a quantity it does not classify);
- the inference test: per book, derive what a reader can compute from the published pairs and fail if
  any book gives the fee drag and the trade count, or any number is a per-trade share. Its own unit
  tests show it catching each known pair;
- operator-only (skipped without the private run folder): no private per-book trade count, turnover or
  drag appears in the page, and no pair of numbers attributable to one book (a table row, a sentence)
  divides to that book's private per-trade share.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from council.paths import REPO_ROOT
from council.publish.leakscan import scan
from council.stocks import adopted

DOC = REPO_ROOT / "docs" / "stock-sleeve-study.md"
PRIVATE_RUN = (Path.home() / "Library" / "Application Support" / "council-book" / "backtests"
               / "stock-sleeve" / "results" / adopted.RUN_ID)

LEGS = re.compile(
    r"\blegs?\b(?!\s+only)|\btrade counts?\b|\bnumber of (?:trades|orders|legs)\b|\bturnover\b"
    r"|\b(?:trades|orders|stop (?:hits|exits))\s+(?:a|per)\s+(?:year|yr|quarter)\b|\bstop hits\b",
    re.I)
FEE_DRAG = re.compile(r"\bfee drag\b|\bcost drag\b|\bother costs?\b|\bvariable[- ]cost drag\b"
                      r"|\bfixed[- ]fee (?:drag|cost)\b|\bfees paid\b", re.I)
PER_LEG = re.compile(r"\b(?:fee|cost) per (?:leg|trade|order)\b|\bper[- ](?:leg|trade|order) (?:fee|share|cost)\b"
                     r"|\bfee arithmetic\b|\bfee[- ]feasibility\b|\bshare price\b|\breal amount\b"
                     r"|\baccount size\b|\bNAV of\b|\btotal return\b|\bfunded (?:with|at|by)\b", re.I)
SENSITIVE = (LEGS, FEE_DRAG, PER_LEG)
NO_FEE = re.compile(r"before the fixed fee|without the fixed fee|no fixed fee|fixed fee removed"
                    r"|zero fixed fee|before fees", re.I)

_CODE = re.compile(r"`[^`]*`")
_LINK = re.compile(r"\]\([^)]*\)")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_YEARS = re.compile(r"\b(?:19|20)\d{2}-(?:19|20)\d{2}\b")
# a number, not part of an identifier (SQ-8, R14, L1-L14) or a compound unit (63-day, 12-month)
_NUM = re.compile(r"(?<![\w.\-−])([-−]?)(\d+(?:,\d{3})*(?:\.\d+)?)(%?)(?![\w]|-[A-Za-z])")
_CELL_ID = re.compile(r"^(SC|SQ|GC)-\d+\b")


@dataclass(frozen=True)
class Num:
    value: float
    pct: bool
    decimals: int

    @property
    def half_unit(self) -> float:
        return 0.5 * 10 ** -self.decimals

    def precise(self, rel: float = 0.10) -> bool:
        """Precise enough to pin a quantity: rounding error at most `rel` of the value."""
        return self.value != 0 and self.half_unit / abs(self.value) <= rel


def numbers(text: str) -> list[Num]:
    text = _YEARS.sub(" ", _DATE.sub(" ", _LINK.sub("]", _CODE.sub(" ", text))))
    out = []
    for sign, digits, pct in _NUM.findall(text):
        value = float(digits.replace(",", ""))
        if not pct and "." not in digits and 1990 <= value <= 2100:
            continue                                        # a calendar year
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        out.append(Num(-value if sign else value, bool(pct), decimals))
    return out


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]

    @property
    def transposed(self) -> bool:
        """Books are the columns (the calendar-year table)."""
        return self.header[0].strip().lower() == "year"


@dataclass
class Doc:
    tables: list[Table] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse(markdown: str) -> Doc:
    doc = Doc()
    paragraphs: list[str] = []
    current: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            rows = [_cells(b) for b in block if not re.fullmatch(r"\|?[\s:|-]+\|?", b.strip())]
            doc.tables.append(Table(rows[0], rows[1:]))
            continue
        line = line.lstrip("> ").strip()
        if not line or line.startswith("#") or line.startswith("- "):
            if current:
                paragraphs.append(" ".join(current))
            current = [line[2:]] if line.startswith("- ") else []
        else:
            current.append(line)
        i += 1
    if current:
        paragraphs.append(" ".join(current))
    for para in paragraphs:
        doc.sentences += [s for s in re.split(r"(?<=[.;!?])\s+(?=[A-Z*(`\"])", para) if s.strip()]
    return doc


def _sensitive(text: str) -> bool:
    return any(p.search(text) for p in SENSITIVE)


def label_violations(doc: Doc) -> list[str]:
    """Every place where a sensitive label sits with a number."""
    found = []
    for t in doc.tables:
        for j, head in enumerate(t.header):
            if _sensitive(head) and any(numbers(r[j]) for r in t.rows if j < len(r)):
                found.append(f"column {head!r}")
        for r in t.rows:
            if _sensitive(r[0]) and any(numbers(c) for c in r[1:]):
                found.append(f"row {r[0]!r}")
            found += [f"cell {c!r}" for c in r if _sensitive(c) and numbers(c)]
    found += [f"sentence {s[:60]!r}" for s in doc.sentences if _sensitive(s) and numbers(s)]
    return found


# ------------------------------------------------------------------------ the inference engine
def book_of(label: str) -> tuple[str, bool]:
    """(book key, before-fee?) for a row or column label."""
    text = re.sub(r"[*_`]", "", label).strip()
    no_fee = bool(NO_FEE.search(text))
    text = re.sub(r"\(selected\)", "", NO_FEE.sub("", text), flags=re.I).strip()
    match = _CELL_ID.match(text)
    if match:
        return match.group(0), no_fee
    text = re.sub(r"\(.*?\)", "", text).split(",")[0]
    return re.sub(r"\s+", " ", text).strip(" .").lower(), no_fee


def _kind(label: str) -> str:
    if PER_LEG.search(label):
        return "per_leg"
    if re.search(r"\bturnover\b", label, re.I):
        return "turnover"
    if LEGS.search(label):
        return "legs"
    if re.search(r"variable[- ]cost drag|other costs?", label, re.I):
        return "variable_cost"
    if re.search(r"cost drag", label, re.I):
        return "cost_drag"
    if FEE_DRAG.search(label):
        return "fee_drag"
    return "perf"


def book_facts(doc: Doc) -> dict[str, set[str]]:
    """book → the kinds of numbers the page publishes for it (`perf_net`, `perf_nofee`, `legs`, ...)."""
    facts: dict[str, set[str]] = {}
    for t in doc.tables:
        for r in t.rows:
            for j, cell in enumerate(r[1:], start=1):
                if j >= len(t.header) or not numbers(cell):
                    continue
                book_label, kind_label = (t.header[j], r[0]) if t.transposed else (r[0], t.header[j])
                book, no_fee = book_of(book_label)
                kind = _kind(kind_label)
                if kind == "perf":
                    kind = "perf_nofee" if no_fee else "perf_net"
                facts.setdefault(book, set()).add(kind)
    return facts


def disclosures(doc: Doc) -> list[str]:
    """What a reader can compute the funding from, pairing only numbers published for the same book."""
    out = []
    for book, kinds in book_facts(doc).items():
        if "per_leg" in kinds:
            out.append(f"{book}: a per-trade share")
        fee_drag = ("fee_drag" in kinds or {"perf_net", "perf_nofee"} <= kinds
                    or ("cost_drag" in kinds and bool(kinds & {"variable_cost", "turnover"})))
        legs = bool(kinds & {"legs", "turnover"})
        if fee_drag and legs:
            out.append(f"{book}: fee drag and trade count")
    return out


# ------------------------------------------------------------------------------------ the page
@pytest.fixture(scope="module")
def page() -> str:
    return DOC.read_text()


def test_the_page_passes_the_leak_scan(page):
    assert scan(page, where=str(DOC.relative_to(REPO_ROOT))) == []


def test_no_sensitive_label_carries_a_number(page):
    assert label_violations(parse(page)) == []


def test_no_two_published_numbers_disclose_the_funding(page):
    doc = parse(page)
    facts = book_facts(doc)
    assert {"perf_net", "perf_nofee"} <= facts["SQ-8"]        # the gate's before-fee figures are here
    assert disclosures(doc) == []


def test_the_page_records_the_verdict_and_the_override(page):
    assert "NOT ADOPTED" in page and "recorded override" in page
    assert "User decision (2026-09-25): adopted anyway as a recorded override" in page
    assert "hindsight-flattered" in page
    for i in range(1, 15):
        assert f"| L{i} |" in page, f"L{i}"
    assert "https://github.com/fbzz/council-book/tree/stock-sleeve-spec" in page


# --------------------------------------------------------------- the engine catches known leaks
_BASE = """
| Book | CAGR | Sharpe |
|---|---:|---:|
| SQ-8, net of every cost | 12.2% | 0.60 |
| SQ-8 before the fixed fee | 18.7% | 0.84 |
"""


@pytest.mark.parametrize(("snippet", "expected"), [
    ("| Cell | Fee drag/yr | Legs/yr |\n|---|---:|---:|\n| SC-8 | 5.8% | 58 |\n", "SC-8: fee drag and trade count"),
    (_BASE + "\n| Cell | Legs a year |\n|---|---:|\n| SQ-8 | 55 |\n", "SQ-8: fee drag and trade count"),
    (_BASE + "\n| Cell | Turnover/yr |\n|---|---:|\n| SQ-8 | 575% |\n", "SQ-8: fee drag and trade count"),
    ("| Book | Cost drag | Turnover |\n|---|---:|---:|\n| Current reference book | 2.9% | 258% |\n",
     "current reference book: fee drag and trade count"),
    ("| Cell | Fee per trade share |\n|---|---:|\n| SQ-8 | 0.10% |\n", "SQ-8: a per-trade share"),
    ("| Year | SQ-8 | SQ-8 before the fixed fee |\n|---|---:|---:|\n| 2020 | 1.1% | 7.5% |\n"
     "| Legs | 55 | 55 |\n", "SQ-8: fee drag and trade count"),
])
def test_the_inference_catches_each_known_pair(snippet, expected):
    assert expected in disclosures(parse(snippet))


def test_the_inference_allows_what_does_not_disclose():
    snippet = _BASE + "\n| Cell | Turnover/yr |\n|---|---:|\n| SC-8 | 599% |\n"   # turnover alone, other book
    assert disclosures(parse(snippet)) == []
    assert disclosures(parse(_BASE)) == []


def test_the_label_rule_catches_numbers_next_to_sensitive_words():
    assert label_violations(parse("The selected cell traded 55 legs a year.\n"))
    assert label_violations(parse("| Cell | Activity |\n|---|---:|\n| Legs a year | 55 |\n"))
    assert label_violations(parse("| Check | Values |\n|---|---|\n| X | fee drag 5.5% |\n"))
    assert not label_violations(parse("Trade counts and turnover stay operator-only.\n"))


def test_numbers_ignore_identifiers_dates_and_code():
    got = numbers("SQ-8 and R14 on 2016-05-20, 2000-2003, `run-20260926T012203Z`, L1-L14, a 63-day "
                  "window, -25.9% and 1,000")
    assert [(n.value, n.pct) for n in got] == [(-25.9, True), (1000.0, False)]


# ---------------------------------------------------------------- operator-only: private values
def _private_books(result: dict) -> dict[str, list[dict]]:
    """Row/column label pattern → the private metric dicts of that book (full window and sub-periods)."""
    wb, sens, sub = result["whole_book"], result["sensitivities"], result["subperiods"]

    def with_sub(full: dict, key: str) -> list[dict]:
        return [full, *sub.get(key, {}).values()]

    books = {rf"^{cell}\b(?!.*before the fixed fee)": with_sub(m, cell) for cell, m in result["cells"].items()}
    books.update({
        r"^QQQ\b": with_sub(result["controls"]["qqq_bh"], "qqq_bh"),
        r"^SPY buy|^SPY$": with_sub(result["controls"]["spy_bh"], "spy_bh"),
        r"^current reference": with_sub(wb["current_reference"], "book_current_reference"),
        r"^re-based(?: book)?, no overlay|^none$": with_sub(wb["rebased_none"], "book_rebased_none"),
        r"^re-based(?: book)?, down-only|^down-only$": with_sub(wb["rebased_down_only"], "book_rebased_down_only"),
        r"^re-based(?: book)?, reference|^reference levels$": with_sub(wb["rebased_reference"],
                                                                        "book_rebased_reference"),
        r"^index-sleeve book": with_sub(wb["index_book_none"], "book_index_book_none"),
        r"doubled$|^variable costs and the fixed fee doubled": [sens["costs_x2"]],
        r"^no hold buffer": [sens["no_hold_buffer"]],
        r"verbatim data layer": [sens["lab_resolver"]],
        r"^ai list": [sens["ai_list"]],
        r"^five times": [sens["nav_x5"]],
        r"^catastrophe stops": [sens["stock_stops"]],
        r"^executed five sessions": [sens["execution_lag"]],
        r"spy-only": [sens["index_sleeve_spy_only"]],
    })
    return books


def _secrets(metrics: list[dict]) -> list[tuple[str, float, bool]]:
    """(name, value in display units, is a percentage) for one book's private quantities."""
    out = []
    for m in metrics:
        legs, fee = m.get("legs_per_year", 0.0), m.get("fee_drag_per_year", 0.0)
        out += [("trades a year", legs, False), ("fee drag", fee * 100, True),
                ("cost drag", m.get("cost_drag_per_year", 0.0) * 100, True),
                ("variable cost drag", m.get("variable_cost_drag_per_year", 0.0) * 100, True),
                ("turnover", m.get("turnover_per_year", 0.0) * 100, True)]
        if legs > 0 and fee > 0:
            out.append(("per-trade share", fee / legs * 100, True))
    return [s for s in out if s[1] > 0]


def _pairs_hit(nums: list[Num], per_leg: float) -> bool:
    """Does some percentage / plain number pair, within rounding, give `per_leg` (in % units)?"""
    for p in nums:
        if not (p.pct and p.precise()):
            continue
        for n in nums:
            if n.pct or not n.precise() or n.value <= 0:
                continue
            lo = (abs(p.value) - p.half_unit) / (n.value + n.half_unit)
            hi = (abs(p.value) + p.half_unit) / (n.value - n.half_unit)
            if lo <= per_leg <= hi:
                return True
    return False


def test_private_values_do_not_reach_the_page(page):
    result_path = PRIVATE_RUN / "result.json"
    if not result_path.is_file():
        pytest.skip("the private study run folder is not on this machine")
    result = json.loads(result_path.read_text())
    books = _private_books(result)
    doc = parse(page)
    problems: list[str] = []          # messages name the place and the kind, never a private value

    # 1. private quantities as the operator summary prints them (drags to 0.01%, turnover to 1%,
    #    trade counts whole or to 0.1): such a number anywhere on the page is a copy, not a coincidence
    tokens = numbers(page)
    for metrics in books.values():
        for name, value, _ in _secrets(metrics):
            if name in ("fee drag", "cost drag", "variable cost drag"):
                hit = any(n.pct and n.decimals == 2 and abs(abs(n.value) - value) <= 0.005 for n in tokens)
            elif name == "turnover":
                hit = value >= 100 and any(n.pct and n.decimals <= 1 and abs(n.value - value) <= n.half_unit
                                           for n in tokens)
            elif name == "trades a year":
                hit = value >= 10 and any(not n.pct and n.decimals <= 1 and abs(n.value - value) <= n.half_unit
                                          for n in tokens)
            else:
                continue
            if hit:
                problems.append(f"a private {name} appears")
    feasibility = result["diagnostics"]["fee_feasibility"]["share_priced_above_one_name"] * 100
    if any(n.pct and n.decimals <= 1 and abs(n.value - feasibility) <= n.half_unit for n in tokens):
        problems.append("the private fee-feasibility share appears")

    # 2. pairs within one book's table row (or column) against that book's private per-trade share
    for t_index, t in enumerate(doc.tables):
        groups: list[tuple[str, list[Num]]] = []
        if t.transposed:
            for j, head in enumerate(t.header[1:], start=1):
                groups.append((head, [n for r in t.rows if j < len(r) for n in numbers(r[j])]))
        else:
            groups += [(r[0], [n for c in r[1:] for n in numbers(c)]) for r in t.rows]
        for label, nums in groups:
            clean = re.sub(r"[*_`]", "", label).strip().lower()
            for pattern, metrics in books.items():
                if not re.search(pattern, clean, re.I):
                    continue
                for name, value, _ in _secrets(metrics):
                    if name == "per-trade share" and _pairs_hit(nums, value):
                        problems.append(f"table {t_index}, row {label!r}: a pair gives the per-trade share")

    # 3. pairs within one sentence or free-text cell, against every book (attribution unknown)
    per_legs = {v for metrics in books.values() for name, v, _ in _secrets(metrics) if name == "per-trade share"}
    texts = doc.sentences + [c for t in doc.tables if t.header[0] in ("Check", "#") for r in t.rows for c in r]
    for text in texts:
        nums = numbers(text)
        if any(_pairs_hit(nums, v) for v in per_legs):
            problems.append(f"sentence {text[:50]!r}: a pair gives a per-trade share")

    if problems:
        pytest.fail("funding-disclosing numbers in the study page:\n" + "\n".join(sorted(set(problems))),
                    pytrace=False)
