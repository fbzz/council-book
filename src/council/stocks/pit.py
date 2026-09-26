"""Point-in-time fundamentals and the four-feature rule, PORTED VERBATIM from the lab study.

Source: the private lab repository `stock-runner-research` at commit d569049 (these files last
changed in e7164a0):
- `src/data/fundamentals.py` lines 46-602: companyfacts -> one row per (ticker, period_end), values
  as FIRST REPORTED, `available_at` = the SEC `filed` date of the filing that first carried them.
  Only the pure part is ported; the network driver (`build_fundamentals`, `edgar.py`) is not.
- `src/features/fundamentals.py` (`fundamental_features`, the rule's feature function).
- `src/baselines/scores.py` (`FUNDAMENTAL_COLUMNS`, `rank_average`) without its sklearn models.

The ported code is unchanged apart from the imports and this docstring; the lab's own tests are
ported as tests/research/test_stock_sleeve_pit.py. Council-book additions are at the END of the
file under "council-book additions" and never change the ported functions' behaviour.

CALLER CONTRACT (lab snapshots.py:253, decision D-008): `fundamental_features` keeps rows with
available_at <= asof, so the caller passes only rows with available_at < decision date (`filed` is
a date without a time; a filing dated D counts as after the decision on D).

The lab module's own docstring follows, as comments.
"""
# ruff: noqa: B905, SIM108

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

# ==================================================================================================
# PORTED: stock-runner-research src/data/fundamentals.py (module docstring, then lines 46-602)
# ==================================================================================================
# Point-in-time quarterly fundamentals from the SEC XBRL `companyfacts` API.
#
# Contract: `data/processed/fundamentals_pit.parquet`, one row per (ticker, period_end), values as
# **first reported**, `available_at` = the SEC `filed` date of the filing that first carried them
# (`docs/interfaces.md`, D-002).
#
# The whole module exists to enforce AGENT.md §3 (no lookahead). The rules, in order of importance:
#
# 1. **First report wins.** A companyfacts unit array contains the same (concept, start, end) once per
#    filing that mentioned it: the original 10-Q, every later 10-Q/10-K that repeats it as a
#    comparative, and every restatement. We keep the row with the *earliest* `filed` and discard the
#    rest, so a number can never be replaced by a revision that did not exist at the snapshot date.
# 2. **Quarterly only.** A duration fact is a quarter iff 80 <= (end - start).days <= 100. YTD facts
#    (~180 / ~270 days) and annual facts (~365 days) are never treated as quarters.
# 3. **Q4 is derived, not read.** US filers do not file a Q4 10-Q, so Q4 = FY - (Q1+Q2+Q3) with the
#    Q1..Q3 values *as tagged inside that same 10-K* when present (else the first-reported ones), and
#    `available_at` = the 10-K's filed date, which is when Q4 first became knowable. If any quarter is
#    missing, Q4 stays NaN rather than being guessed.
# 3b. **Cash-flow items are cumulative and get the same treatment.** The cash-flow statement in a 10-Q
#    is year-to-date, so `cfo` and `capex` only ever exist as ~91 / ~182 / ~273 / ~365-day facts. Rule
#    3 generalises: a quarter is `YTD_n - YTD_(n-1)` taken from the filing that reported `YTD_n`, so
#    `available_at` is that filing's date and no later restatement is used. Without this, `cfo` and
#    `capex` would be present in Q1 only (and `fcf_margin` would be ~75% NaN). The sum-of-reported-
#    quarters form of the rule is always tried first, so revenue-style concepts are unaffected.
# 4. **One `available_at` per row.** The row's `available_at`/`fy`/`fp`/`form`/`accession` come from the
#    fact that anchors the row (revenue, else net income, else the next flow that exists). Any field
#    whose own first `filed` is *later* than that anchor date is blanked to NaN, because publishing it
#    on the anchor's date would be lookahead. `fields_masked` counts how often that happens.
#

QUARTER_MIN_DAYS, QUARTER_MAX_DAYS = 80, 100
ANNUAL_MIN_DAYS, ANNUAL_MAX_DAYS = 340, 400
ANNUAL_FORM_PREFIXES = ("10-K", "20-F", "40-F")
# Cumulative (year-to-date) durations, by number of quarters covered. 1 quarter = the plain
# quarterly range above; 2 = half year; 3 = nine months; 4 = full year.
CUMULATIVE_DAY_RANGES: dict[int, tuple[int, int]] = {2: (150, 200), 3: (245, 295), 4: (340, 400)}
START_TOLERANCE = pd.Timedelta(days=5)

US_GAAP = "us-gaap"
IFRS = "ifrs-full"

# Concept priority: first taxonomy concept with data for a period wins.
FLOW_CONCEPTS: dict[str, dict[str, list[str]]] = {
    "revenue": {
        US_GAAP: [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
        ],
        IFRS: ["Revenue", "RevenueFromContractsWithCustomers"],
    },
    "gross_profit": {US_GAAP: ["GrossProfit"], IFRS: ["GrossProfit"]},
    "operating_income": {
        US_GAAP: ["OperatingIncomeLoss"],
        IFRS: ["ProfitLossFromOperatingActivities"],
    },
    "net_income": {US_GAAP: ["NetIncomeLoss"], IFRS: ["ProfitLoss"]},
    "cfo": {
        US_GAAP: ["NetCashProvidedByUsedInOperatingActivities"],
        IFRS: ["CashFlowsFromUsedInOperatingActivities"],
    },
    # PaymentsToAcquireProductiveAssets is a documented addition to the spec's single capex tag:
    # 13 filers use it, NVDA among them (it switched tags after FY2020 and would otherwise have no
    # capex at all from 2021 on, taking `fcf_margin` with it). The spec tag keeps priority per period.
    "capex": {
        US_GAAP: ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
        IFRS: ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
    },
}
COST_CONCEPTS = {
    US_GAAP: ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    IFRS: ["CostOfSales"],
}
CASH_CONCEPTS = {
    US_GAAP: [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    IFRS: ["CashAndCashEquivalents"],
}
# Debt combinations, tried in order; a combination is used when its first component exists.
DEBT_COMBOS = {
    US_GAAP: [("LongTermDebt", "DebtCurrent"), ("LongTermDebtNoncurrent", "LongTermDebtCurrent")],
    IFRS: [("NoncurrentPortionOfNoncurrentBorrowings", "CurrentPortionOfNoncurrentBorrowings")],
}
SHARES_CONCEPT = ("dei", "EntityCommonStockSharesOutstanding")
# Fallbacks when the cover-page count is absent (multi-class filers such as PLTR/APP tag dei with a class axis, which
# companyfacts drops): the balance-sheet instant count, then the diluted weighted average (a duration concept, ~quarter).
SHARES_FALLBACK_CONCEPTS = (
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
)

ANCHOR_ORDER = ["revenue", "net_income", "operating_income", "cfo", "gross_profit", "capex"]
FLOW_FIELDS = ["revenue", "gross_profit", "operating_income", "net_income", "cfo", "capex"]
INSTANT_FIELDS = ["cash", "debt_total", "shares_outstanding"]

FUNDAMENTALS_COLUMNS = [
    "ticker",
    "cik",
    "period_end",
    "fy",
    "fp",
    "form",
    "accession",
    "available_at",
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "cfo",
    "capex",
    "cash",
    "debt_total",
    "shares_outstanding",
    "is_derived_q4",
]


# --------------------------------------------------------------------------------------
# fact plumbing
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Fact:
    concept: str
    start: pd.Timestamp | None
    end: pd.Timestamp
    val: float
    accn: str
    fy: Any
    fp: Any
    form: str
    filed: pd.Timestamp
    unit: str

    @property
    def days(self) -> int | None:
        if self.start is None:
            return None
        return int((self.end - self.start).days)

    @property
    def is_quarter(self) -> bool:
        d = self.days
        return d is not None and QUARTER_MIN_DAYS <= d <= QUARTER_MAX_DAYS

    @property
    def is_annual(self) -> bool:
        d = self.days
        return d is not None and ANNUAL_MIN_DAYS <= d <= ANNUAL_MAX_DAYS

    @property
    def n_quarters(self) -> int | None:
        """How many quarters this duration covers: 1 (quarter), 2/3 (YTD), 4 (year), else None."""
        d = self.days
        if d is None:
            return None
        if QUARTER_MIN_DAYS <= d <= QUARTER_MAX_DAYS:
            return 1
        for n, (lo, hi) in CUMULATIVE_DAY_RANGES.items():
            if lo <= d <= hi:
                return n
        return None


@dataclass
class Resolved:
    """A single field value plus the provenance that decides `available_at`."""

    value: float
    filed: pd.Timestamp
    accn: str
    form: str
    fy: Any
    fp: Any
    is_derived_q4: bool = False


def _unit_key(units: dict[str, Any], prefer: Sequence[str]) -> str | None:
    for p in prefer:
        if p in units:
            return p
    return next(iter(units), None) if units else None


def iter_facts(
    companyfacts: dict, taxonomy: str, concept: str, prefer_units: Sequence[str] = ("USD",)
) -> list[Fact]:
    """Every non-dimensional fact for one concept, de-duplicated."""
    node = companyfacts.get("facts", {}).get(taxonomy, {}).get(concept)
    if not node:
        return []
    units = node.get("units", {}) or {}
    key = _unit_key(units, prefer_units)
    if key is None:
        return []
    out: list[Fact] = []
    seen: set[tuple] = set()
    for raw in units[key]:
        if raw.get("val") is None or not raw.get("end") or not raw.get("filed"):
            continue
        start = pd.Timestamp(raw["start"]) if raw.get("start") else None
        f = Fact(
            concept=concept,
            start=start,
            end=pd.Timestamp(raw["end"]),
            val=float(raw["val"]),
            accn=str(raw.get("accn", "")),
            fy=raw.get("fy"),
            fp=raw.get("fp"),
            form=str(raw.get("form", "")),
            filed=pd.Timestamp(raw["filed"]),
            unit=key,
        )
        k = (f.start, f.end, f.val, f.accn, f.form, f.filed)
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def _better(a: Fact, b: Fact) -> Fact:
    """Earliest `filed` wins; deterministic tie-break on accession."""
    if b.filed < a.filed:
        return b
    if b.filed == a.filed and b.accn < a.accn:
        return b
    return a


class ConceptIndex:
    """First-reported views of one concept.

    `quarters` / `instants` / `cumulative` all hold the *earliest filed* fact for their key, which is
    the whole point: a value can never come from a filing later than the one that first reported it.
    `quarter_by_accn` additionally keeps the quarters exactly as tagged inside each filing, so a Q4
    derivation can prefer the Q1..Q3 numbers printed in that same 10-K.
    """

    def __init__(self, facts: list[Fact]) -> None:
        self.quarters: dict[pd.Timestamp, Fact] = {}
        self.instants: dict[pd.Timestamp, Fact] = {}
        self.quarter_by_accn: dict[tuple[str, pd.Timestamp], Fact] = {}
        self.cumulative: dict[tuple[pd.Timestamp, int], Fact] = {}  # (period end, n quarters)
        self.by_start: dict[tuple[pd.Timestamp, int], Fact] = {}  # (period start, n quarters)
        for f in facts:
            if f.start is None:
                cur = self.instants.get(f.end)
                self.instants[f.end] = f if cur is None else _better(cur, f)
                continue
            n = f.n_quarters
            if n is None:
                continue  # neither a quarter nor a recognised YTD window: ignore entirely
            if n == 4 and not f.form.upper().startswith(ANNUAL_FORM_PREFIXES):
                continue  # a 365-day duration outside an annual report is not a fiscal year
            if n == 1:
                cur = self.quarters.get(f.end)
                self.quarters[f.end] = f if cur is None else _better(cur, f)
                k = (f.accn, f.end)
                cur2 = self.quarter_by_accn.get(k)
                self.quarter_by_accn[k] = f if cur2 is None else _better(cur2, f)
            else:
                cur = self.cumulative.get((f.end, n))
                self.cumulative[(f.end, n)] = f if cur is None else _better(cur, f)
            cur3 = self.by_start.get((f.start, n))
            self.by_start[(f.start, n)] = f if cur3 is None else _better(cur3, f)

    @property
    def annuals(self) -> dict[pd.Timestamp, Fact]:
        return {end: f for (end, n), f in self.cumulative.items() if n == 4}

    def prior_ytd(self, start: pd.Timestamp, n: int) -> Fact | None:
        """The YTD fact for the same fiscal year covering one quarter less (n-1)."""
        f = self.by_start.get((start, n - 1))
        if f is not None:
            return f
        # 52/53-week filers shift the fiscal-year start by a day or two between filings; take the
        # closest start within tolerance (sorted, so the choice is deterministic).
        near = sorted(
            (
                (abs((s - start).days), s, cand)
                for (s, k), cand in self.by_start.items()
                if k == n - 1 and abs((s - start).days) <= START_TOLERANCE.days
            ),
            key=lambda t: (t[0], t[1]),
        )
        return near[0][2] if near else None


def build_indexes(companyfacts: dict, taxonomy: str, concepts: Iterable[str]) -> dict[str, ConceptIndex]:
    return {c: ConceptIndex(iter_facts(companyfacts, taxonomy, c)) for c in concepts}


def detect_taxonomy(companyfacts: dict) -> str | None:
    """us-gaap / ifrs-full / None, chosen by how many mapped concepts actually carry facts."""
    facts = companyfacts.get("facts", {}) or {}
    scores = {}
    for tax in (US_GAAP, IFRS):
        node = facts.get(tax)
        if not node:
            continue
        wanted = [c for m in FLOW_CONCEPTS.values() for c in m.get(tax, [])]
        scores[tax] = sum(1 for c in wanted if node.get(c))
    if not scores:
        return None
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else (US_GAAP if US_GAAP in facts else (IFRS if IFRS in facts else None))


# --------------------------------------------------------------------------------------
# quarter resolution
# --------------------------------------------------------------------------------------
def _subtract_reported_quarters(idx: ConceptIndex, cum: Fact, n: int) -> Resolved | None:
    """`cum` minus the n-1 quarters reported inside its window (same filing preferred)."""
    lo, hi = cum.start, cum.end
    ends: set[pd.Timestamp] = set()
    for (accn, end), f in idx.quarter_by_accn.items():
        if accn == cum.accn and lo < end < hi and f.start is not None and f.start >= lo - START_TOLERANCE:
            ends.add(end)
    for end, f in idx.quarters.items():
        if lo < end < hi and f.start is not None and f.start >= lo - START_TOLERANCE:
            ends.add(end)
    if len(ends) != n - 1:
        return None
    total, filed = 0.0, cum.filed
    for end in sorted(ends):
        f = idx.quarter_by_accn.get((cum.accn, end)) or idx.quarters.get(end)
        if f is None:
            return None
        total += f.val
        filed = max(filed, f.filed)
    return Resolved(cum.val - total, filed, cum.accn, cum.form, cum.fy, "Q4" if n == 4 else cum.fp, n == 4)


def derive_quarter(idx: ConceptIndex, period_end: pd.Timestamp) -> Resolved | None:
    """Back out the quarter ending `period_end` from a cumulative (YTD or annual) fact.

    Rule 1 (the spec's Q4 rule, generalised): cumulative minus the quarters reported inside it,
    preferring the versions tagged in the same filing. Rule 2 (needed for cash-flow concepts, which
    are never tagged as bare quarters after Q1): cumulative minus the previous cumulative of the same
    fiscal year. Both take `available_at` from the filing that reported the cumulative value.
    """
    for n in (2, 3, 4):
        cum = idx.cumulative.get((period_end, n))
        if cum is None or cum.start is None:
            continue
        direct = _subtract_reported_quarters(idx, cum, n)
        if direct is not None:
            return direct
        prev = idx.prior_ytd(cum.start, n)
        if prev is not None and prev.end < cum.end:
            return Resolved(
                value=cum.val - prev.val,
                filed=max(cum.filed, prev.filed),
                accn=cum.accn,
                form=cum.form,
                fy=cum.fy,
                fp="Q4" if n == 4 else cum.fp,
                is_derived_q4=(n == 4),
            )
    return None


def resolve_concept_list(
    indexes: dict[str, ConceptIndex], concepts: Sequence[str], period_end: pd.Timestamp
) -> Resolved | None:
    """Directly reported quarter first (concepts in priority order), then derived quarters."""
    for c in concepts:
        idx = indexes.get(c)
        if idx is None:
            continue
        f = idx.quarters.get(period_end)
        if f is not None:
            return Resolved(f.val, f.filed, f.accn, f.form, f.fy, f.fp, False)
    for c in concepts:
        idx = indexes.get(c)
        if idx is None:
            continue
        d = derive_quarter(idx, period_end)
        if d is not None:
            return d
    return None


def quarter_grid(indexes: dict[str, ConceptIndex], flow_concepts: Sequence[str]) -> list[pd.Timestamp]:
    """Every period_end we can populate: reported quarters, plus period ends we can derive."""
    ends: set[pd.Timestamp] = set()
    for c in flow_concepts:
        idx = indexes.get(c)
        if idx is None:
            continue
        ends.update(idx.quarters.keys())
        for end, _n in idx.cumulative:
            if end not in ends and derive_quarter(idx, end) is not None:
                ends.add(end)
    return sorted(ends)


# --------------------------------------------------------------------------------------
# instants
# --------------------------------------------------------------------------------------
def resolve_instant(indexes: dict[str, ConceptIndex], concepts: Sequence[str], end: pd.Timestamp) -> Resolved | None:
    for c in concepts:
        idx = indexes.get(c)
        if idx is None:
            continue
        f = idx.instants.get(end)
        if f is not None:
            return Resolved(f.val, f.filed, f.accn, f.form, f.fy, f.fp, False)
    return None


def resolve_debt(indexes: dict[str, ConceptIndex], combos: Sequence[tuple[str, str]], end: pd.Timestamp):
    """LongTermDebt + DebtCurrent, else the noncurrent/current pair, else whichever single tag exists."""
    for lead, second in combos:
        a = indexes.get(lead, ConceptIndex([])).instants.get(end)
        if a is None:
            continue
        b = indexes.get(second, ConceptIndex([])).instants.get(end)
        val = a.val + (b.val if b is not None else 0.0)
        filed = max(a.filed, b.filed) if b is not None else a.filed
        return Resolved(val, filed, a.accn, a.form, a.fy, a.fp, False)
    for _, second in combos:  # only the current-debt tag exists
        b = indexes.get(second, ConceptIndex([])).instants.get(end)
        if b is not None:
            return Resolved(b.val, b.filed, b.accn, b.form, b.fy, b.fp, False)
    return None


def shares_indexes(companyfacts: dict) -> tuple[dict[pd.Timestamp, Resolved], dict[str, Resolved]]:
    """dei cover-page share counts, summed across share classes.

    Returns (by period-end date, by accession). Multi-class filers tag one fact per class with the
    same `end` and `accn`; identical (accn, val) pairs are collapsed so a fact repeated across
    contexts is not double counted.
    """
    facts = iter_facts(companyfacts, SHARES_CONCEPT[0], SHARES_CONCEPT[1], prefer_units=("shares",))
    for tax, concept in SHARES_FALLBACK_CONCEPTS:
        if facts:
            break
        facts = iter_facts(companyfacts, tax, concept, prefer_units=("shares",))
    by_end: dict[pd.Timestamp, list[Fact]] = defaultdict(list)
    by_accn: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        by_end[f.end].append(f)
        by_accn[f.accn].append(f)

    def _sum(group: list[Fact], only_first_filing: bool) -> Resolved:
        if only_first_filing:
            first = min(f.filed for f in group)
            group = [f for f in group if f.filed == first]
            accns = sorted({f.accn for f in group})
            group = [f for f in group if f.accn == accns[0]]
        seen: set[tuple[str, float]] = set()
        total = 0.0
        for f in group:
            k = (f.accn, f.val)
            if k in seen:
                continue
            seen.add(k)
            total += f.val
        ref = group[0]
        return Resolved(total, min(f.filed for f in group), ref.accn, ref.form, ref.fy, ref.fp, False)

    return (
        {end: _sum(g, True) for end, g in by_end.items() if g},
        {accn: _sum(g, False) for accn, g in by_accn.items() if g},
    )


# --------------------------------------------------------------------------------------
# per-company assembly
# --------------------------------------------------------------------------------------
def fundamentals_for_company(ticker: str, cik: str, companyfacts: dict) -> tuple[pd.DataFrame, dict]:
    """Build the PIT rows for one company. `stats` carries the coverage numbers for the report."""
    taxonomy = detect_taxonomy(companyfacts)
    stats: dict[str, Any] = {
        "ticker": ticker,
        "cik": cik,
        "taxonomy": taxonomy or "none",
        "fields_masked": 0,
        "n_annual_only_periods": 0,
    }
    if taxonomy is None:
        return pd.DataFrame(columns=FUNDAMENTALS_COLUMNS), stats

    flow_map = {field: FLOW_CONCEPTS[field].get(taxonomy, []) for field in FLOW_FIELDS}
    cost_list = COST_CONCEPTS.get(taxonomy, [])
    cash_list = CASH_CONCEPTS.get(taxonomy, [])
    debt_combos = DEBT_COMBOS.get(taxonomy, [])
    all_concepts = (
        [c for lst in flow_map.values() for c in lst]
        + cost_list
        + cash_list
        + [c for combo in debt_combos for c in combo]
    )
    indexes = build_indexes(companyfacts, taxonomy, sorted(set(all_concepts)))
    shares_by_end, shares_by_accn = shares_indexes(companyfacts)

    flow_concepts_all = [c for lst in flow_map.values() for c in lst]
    grid = quarter_grid(indexes, flow_concepts_all)

    # how many fiscal years exist as annual facts but produced no quarterly rows (IFRS filers)
    annual_ends = {e for c in flow_concepts_all for e in indexes.get(c, ConceptIndex([])).annuals}
    stats["n_annual_only_periods"] = len([e for e in annual_ends if e not in set(grid)])

    rows = []
    for period_end in grid:
        fields: dict[str, Resolved | None] = {
            field: resolve_concept_list(indexes, flow_map[field], period_end) for field in FLOW_FIELDS
        }
        # gross_profit fallback: revenue - cost of revenue, both for the same quarter
        if fields["gross_profit"] is None and fields["revenue"] is not None:
            cost = resolve_concept_list(indexes, cost_list, period_end)
            if cost is not None:
                rev = fields["revenue"]
                fields["gross_profit"] = Resolved(
                    value=rev.value - cost.value,
                    filed=max(rev.filed, cost.filed),
                    accn=cost.accn,
                    form=cost.form,
                    fy=cost.fy,
                    fp=cost.fp,
                    is_derived_q4=rev.is_derived_q4 or cost.is_derived_q4,
                )
        anchor_field = next((f for f in ANCHOR_ORDER if fields.get(f) is not None), None)
        if anchor_field is None:
            continue
        anchor = fields[anchor_field]
        available_at = anchor.filed

        row: dict[str, Any] = {
            "ticker": ticker,
            "cik": cik,
            "period_end": period_end,
            "fy": anchor.fy,
            "fp": "Q4" if anchor.is_derived_q4 else anchor.fp,
            "form": anchor.form,
            "accession": anchor.accn,
            "available_at": available_at,
            "is_derived_q4": bool(anchor.is_derived_q4),
        }
        for field in FLOW_FIELDS:
            r = fields[field]
            if r is None:
                row[field] = np.nan
            elif r.filed > available_at:  # would be lookahead on this row's available_at
                row[field] = np.nan
                stats["fields_masked"] += 1
            else:
                row[field] = r.value

        instants = {
            "cash": resolve_instant(indexes, cash_list, period_end),
            "debt_total": resolve_debt(indexes, debt_combos, period_end),
            "shares_outstanding": shares_by_end.get(period_end) or shares_by_accn.get(anchor.accn),
        }
        for field in INSTANT_FIELDS:
            r = instants[field]
            if r is None:
                row[field] = np.nan
            elif r.filed > available_at:
                row[field] = np.nan
                stats["fields_masked"] += 1
            else:
                row[field] = r.value
        rows.append(row)

    df = pd.DataFrame(rows, columns=FUNDAMENTALS_COLUMNS)
    return _coerce(df), stats


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        df = pd.DataFrame(columns=FUNDAMENTALS_COLUMNS)
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["available_at"] = pd.to_datetime(df["available_at"])
    df["fy"] = pd.to_numeric(df["fy"], errors="coerce").astype("Int64")
    for c in ("ticker", "cik", "fp", "form", "accession"):
        df[c] = df[c].astype("object").where(df[c].notna(), None)
    for c in FLOW_FIELDS + INSTANT_FIELDS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df["is_derived_q4"] = df["is_derived_q4"].astype(bool)
    return df[FUNDAMENTALS_COLUMNS]


# ==================================================================================================
# PORTED: stock-runner-research src/features/fundamentals.py (after its imports)
# ==================================================================================================
FUNDAMENTAL_FEATURE_KEYS: tuple[str, ...] = (
    "revenue_growth_yoy",
    "revenue_growth_qoq",
    "revenue_growth_yoy_prev",
    "revenue_growth_acceleration",
    "gross_margin",
    "gross_margin_change_yoy",
    "operating_margin",
    "operating_margin_change_yoy",
    "fcf_margin",
    "cash",
    "debt_total",
    "net_cash",
    "quarters_of_history",
    "days_since_last_filing",
    "latest_period_end",
    "latest_available_at",
)


def fundamental_features(fund: pd.DataFrame, ticker: str, asof: pd.Timestamp) -> dict:
    """
    Compute point-in-time fundamental features for a ticker as of a given timestamp.

    Uses only rows with available_at <= asof. Returns a dict with exactly the keys
    in FUNDAMENTAL_FEATURE_KEYS.
    """
    # Normalize dates and avoid mutating the input
    fund = fund.copy()
    fund["period_end"] = pd.to_datetime(fund["period_end"])
    fund["available_at"] = pd.to_datetime(fund["available_at"])
    asof = pd.Timestamp(asof)

    # Filter visible rows
    visible = fund[(fund["ticker"] == ticker) & (fund["available_at"] <= asof)]
    if visible.empty:
        return {
            "revenue_growth_yoy": float("nan"),
            "revenue_growth_qoq": float("nan"),
            "revenue_growth_yoy_prev": float("nan"),
            "revenue_growth_acceleration": float("nan"),
            "gross_margin": float("nan"),
            "gross_margin_change_yoy": float("nan"),
            "operating_margin": float("nan"),
            "operating_margin_change_yoy": float("nan"),
            "fcf_margin": float("nan"),
            "cash": float("nan"),
            "debt_total": float("nan"),
            "net_cash": float("nan"),
            "quarters_of_history": 0,
            "days_since_last_filing": float("nan"),
            "latest_period_end": pd.NaT,
            "latest_available_at": pd.NaT,
        }

    # Deduplicate by period_end, keep earliest available_at (first reported)
    visible = visible.sort_values(["available_at", "period_end"]).drop_duplicates(
        subset=["period_end"], keep="first"
    )
    visible = visible.sort_values("period_end").reset_index(drop=True)

    # Latest row
    latest_idx = visible["period_end"].idxmax()
    latest = visible.loc[latest_idx]

    def match(target: pd.Timestamp) -> pd.Series | None:
        """Return the visible row whose period_end is closest to target within 20 days."""
        if pd.isna(target):
            return None
        diffs = (visible["period_end"] - target).abs().dt.days
        min_diff = diffs.min()
        if min_diff > 20:
            return None
        candidates = visible[diffs == min_diff]
        if len(candidates) > 1:
            candidates = candidates.sort_values("period_end", ascending=False)
        return candidates.iloc[0]

    prev = match(latest["period_end"] - pd.Timedelta(days=91))
    yearago = match(latest["period_end"] - pd.Timedelta(days=365))
    prev_yearago = match(prev["period_end"] - pd.Timedelta(days=365)) if prev is not None else None

    def growth(a: float, b: float) -> float:
        if pd.isna(a) or pd.isna(b):
            return float("nan")
        if b > 0:
            return a / b - 1
        return float("nan")

    def margin(x: float, rev: float) -> float:
        if pd.isna(x) or pd.isna(rev):
            return float("nan")
        if rev > 0:
            return x / rev
        return float("nan")

    # Revenue growth
    revenue_growth_yoy = growth(latest["revenue"], yearago["revenue"]) if yearago is not None else float("nan")
    revenue_growth_qoq = growth(latest["revenue"], prev["revenue"]) if prev is not None else float("nan")
    revenue_growth_yoy_prev = (
        growth(prev["revenue"], prev_yearago["revenue"])
        if prev is not None and prev_yearago is not None
        else float("nan")
    )
    if pd.isna(revenue_growth_yoy) or pd.isna(revenue_growth_yoy_prev):
        revenue_growth_acceleration = float("nan")
    else:
        revenue_growth_acceleration = revenue_growth_yoy - revenue_growth_yoy_prev

    # Margins
    gross_margin = margin(latest["gross_profit"], latest["revenue"])
    if yearago is not None:
        gross_margin_change_yoy = gross_margin - margin(yearago["gross_profit"], yearago["revenue"])
    else:
        gross_margin_change_yoy = float("nan")

    operating_margin = margin(latest["operating_income"], latest["revenue"])
    if yearago is not None:
        operating_margin_change_yoy = operating_margin - margin(yearago["operating_income"], yearago["revenue"])
    else:
        operating_margin_change_yoy = float("nan")

    if pd.isna(latest["cfo"]) or pd.isna(latest["capex"]):
        fcf_margin = float("nan")
    else:
        fcf_margin = margin(latest["cfo"] - latest["capex"], latest["revenue"])

    # Balance sheet
    cash = latest["cash"] if not pd.isna(latest["cash"]) else float("nan")
    debt_total = latest["debt_total"] if not pd.isna(latest["debt_total"]) else float("nan")
    if pd.isna(cash) or pd.isna(debt_total):
        net_cash = float("nan")
    else:
        net_cash = cash - debt_total

    # History and filing recency
    quarters_of_history = int(visible["period_end"].nunique())
    days_since_last_filing = int((asof - visible["available_at"].max()).days)

    latest_period_end = pd.Timestamp(latest["period_end"])
    latest_available_at = pd.Timestamp(latest["available_at"])

    return {
        "revenue_growth_yoy": revenue_growth_yoy,
        "revenue_growth_qoq": revenue_growth_qoq,
        "revenue_growth_yoy_prev": revenue_growth_yoy_prev,
        "revenue_growth_acceleration": revenue_growth_acceleration,
        "gross_margin": gross_margin,
        "gross_margin_change_yoy": gross_margin_change_yoy,
        "operating_margin": operating_margin,
        "operating_margin_change_yoy": operating_margin_change_yoy,
        "fcf_margin": fcf_margin,
        "cash": cash,
        "debt_total": debt_total,
        "net_cash": net_cash,
        "quarters_of_history": quarters_of_history,
        "days_since_last_filing": days_since_last_filing,
        "latest_period_end": latest_period_end,
        "latest_available_at": latest_available_at,
    }


# ==================================================================================================
# PORTED: stock-runner-research src/baselines/scores.py (FUNDAMENTAL_COLUMNS, rank_average)
# ==================================================================================================
FUNDAMENTAL_COLUMNS: tuple[str, ...] = (
    "revenue_growth_yoy",
    "revenue_growth_acceleration",
    "gross_margin_change_yoy",
    "operating_margin_change_yoy",
)


def rank_average(
    df: pd.DataFrame,
    columns: Sequence[str],
    signs: Sequence[float] | None = None,
) -> np.ndarray:
    """Return the row-wise average of percentile ranks for the given columns.

    Missing columns raise a KeyError naming every absent column.  NaN values in a
    column are replaced by the median of that column's non-NaN ranks (or 0.5 if the
    column is entirely NaN).  The result is a float64 array in [0, 1].
    """
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise KeyError(f"Missing columns: {', '.join(missing)}")

    if signs is None:
        signs = [1.0] * len(columns)
    if len(signs) != len(columns):
        raise ValueError("signs must have the same length as columns")

    sign_arr = np.asarray(signs, dtype=float)
    rank_arrays: list[np.ndarray] = []

    for col, sign in zip(columns, sign_arr):
        series = df[col].astype(float) * sign
        ranks = series.rank(pct=True, method="average")
        median_rank = ranks.median()
        if pd.isna(median_rank):
            median_rank = 0.5
        filled = ranks.fillna(median_rank)
        rank_arrays.append(filled.to_numpy(dtype=float))

    return np.mean(np.column_stack(rank_arrays), axis=1)



# ==================================================================================================
# council-book additions (not in the lab code; they reuse the ported selection logic unchanged)
# ==================================================================================================
DOMESTIC_FORMS: frozenset[str] = frozenset({"10-Q", "10-K", "10-Q/A", "10-K/A", "10-QT", "10-KT"})


def rule_quarters(fund: pd.DataFrame, ticker: str, asof: pd.Timestamp) -> dict:
    """The four quarters the rule compares, chosen EXACTLY as `fundamental_features` chooses them.

    Returns the revenue of L (latest visible period_end), P (nearest L-91d), Y (nearest L-365d) and
    PY (nearest P-365d), each within 20 days or NaN, plus the form of L's anchoring filing. The study
    uses them for the revenue floor and the domestic-filer check; a test pins that
    revenue_L / revenue_Y - 1 equals `fundamental_features`' revenue_growth_yoy."""
    nan = float("nan")
    out = {"revenue_L": nan, "revenue_P": nan, "revenue_Y": nan, "revenue_PY": nan, "form_L": None}
    fund = fund.copy()
    fund["period_end"] = pd.to_datetime(fund["period_end"])
    fund["available_at"] = pd.to_datetime(fund["available_at"])
    visible = fund[(fund["ticker"] == ticker) & (fund["available_at"] <= pd.Timestamp(asof))]
    if visible.empty:
        return out
    visible = visible.sort_values(["available_at", "period_end"]).drop_duplicates(
        subset=["period_end"], keep="first"
    )
    visible = visible.sort_values("period_end").reset_index(drop=True)
    latest = visible.loc[visible["period_end"].idxmax()]

    def match(target: pd.Timestamp) -> pd.Series | None:
        if pd.isna(target):
            return None
        diffs = (visible["period_end"] - target).abs().dt.days
        if diffs.min() > 20:
            return None
        candidates = visible[diffs == diffs.min()]
        if len(candidates) > 1:
            candidates = candidates.sort_values("period_end", ascending=False)
        return candidates.iloc[0]

    prev = match(latest["period_end"] - pd.Timedelta(days=91))
    yearago = match(latest["period_end"] - pd.Timedelta(days=365))
    prev_yearago = match(prev["period_end"] - pd.Timedelta(days=365)) if prev is not None else None
    out["revenue_L"] = float(latest["revenue"])
    out["revenue_P"] = float(prev["revenue"]) if prev is not None else nan
    out["revenue_Y"] = float(yearago["revenue"]) if yearago is not None else nan
    out["revenue_PY"] = float(prev_yearago["revenue"]) if prev_yearago is not None else nan
    out["form_L"] = latest["form"]
    return out


def resolve_earliest_tagged(
    indexes: dict[str, ConceptIndex], concepts: Sequence[str], period_end: pd.Timestamp
) -> tuple[str, Resolved] | None:
    """`resolve_concept_list_earliest`, also returning the concept that won (used by change (c))."""
    candidates: list[tuple[pd.Timestamp, int, int, str, Resolved]] = []
    for rank, c in enumerate(concepts):
        idx = indexes.get(c)
        if idx is None:
            continue
        f = idx.quarters.get(period_end)
        if f is not None:
            candidates.append((f.filed, 0, rank, c, Resolved(f.val, f.filed, f.accn, f.form, f.fy, f.fp, False)))
        d = derive_quarter(idx, period_end)
        if d is not None:
            candidates.append((d.filed, 1, rank, c, d))
    if not candidates:
        return None
    best = min(candidates, key=lambda t: (t[0], t[1], t[2]))
    return best[3], best[4]


def resolve_concept_list_earliest(
    indexes: dict[str, ConceptIndex], concepts: Sequence[str], period_end: pd.Timestamp
) -> Resolved | None:
    """First report wins ACROSS the concept list (the lab resolver applies it within one concept).

    Candidates: every concept's directly reported quarter and every concept's derived quarter for
    `period_end`. The earliest `filed` wins; ties go to a direct quarter over a derived one, then to
    the lab's concept priority. Why: the lab resolver takes the highest-priority concept that has ANY
    fact for the period, so when a later filing re-tags old quarters (the 2018 revenue-standard
    change), those quarters are dated at the re-tagging filing and look unpublished until then."""
    hit = resolve_earliest_tagged(indexes, concepts, period_end)
    return None if hit is None else hit[1]


_LAB_RESOLVER = resolve_concept_list
_LAB_COST_CONCEPTS = COST_CONCEPTS
# CostOfGoodsSold is the pre-2018 us-gaap tag that CostOfGoodsAndServicesSold replaced; it is added as
# the LAST gross-profit fallback so older filings are read with the same economic item.
COUNCIL_COST_CONCEPTS = {**COST_CONCEPTS, US_GAAP: [*COST_CONCEPTS[US_GAAP], "CostOfGoodsSold"]}


def fundamentals_first_reported(ticker: str, cik: str, companyfacts: dict) -> tuple[pd.DataFrame, dict]:
    """`fundamentals_for_company` (unchanged) run with two council-book changes, both swapped in only
    for this call: `resolve_concept_list_earliest` in place of the lab resolver, and
    `COUNCIL_COST_CONCEPTS` in place of the lab's cost list. Every other rule applies as is."""
    global resolve_concept_list, COST_CONCEPTS
    resolve_concept_list = resolve_concept_list_earliest
    COST_CONCEPTS = COUNCIL_COST_CONCEPTS
    try:
        return fundamentals_for_company(ticker, cik, companyfacts)
    finally:
        resolve_concept_list = _LAB_RESOLVER
        COST_CONCEPTS = _LAB_COST_CONCEPTS


# --------------------------------------------------------------------------------------------------
# change (c): comparable year-ago values (same concept, as printed with the compared quarter)
# --------------------------------------------------------------------------------------------------
# The lab resolves every quarter on its own, so the two sides of a year-on-year comparison can come
# from different revenue tags or from different revenue standards (ASC 605 as first reported against
# ASC 606). Change (c) keeps each quarter's own value (first report, changes (a) and (b)) and adds,
# per row, the year-ago quarter's values resolved with the SAME concepts as that row and taken AS
# PRINTED no later than the row's own filing date: the row's own filing first (the comparative column
# of the same 10-Q or 10-K), else the latest earlier filing of the same concept. A derived quarter
# (Q4 = year minus Q1..Q3) is derived from the same filings' figures. Everything used was filed on or
# before the row's `available_at`, which is before the decision date, so there is no lookahead.

YA_COLUMNS = ["ya_period_end", "revenue_ya", "gross_profit_ya", "operating_income_ya"]
COMPARABLE_COLUMNS = [*FUNDAMENTALS_COLUMNS, *YA_COLUMNS]
MATCH_DAYS = 20          # the lab's `match` tolerance, reused for the year-ago row


class PrintedIndex:
    """Every quarterly and cumulative fact of one concept, with all its filings (not only the first)."""

    def __init__(self, facts: list[Fact]) -> None:
        self.quarters: dict[pd.Timestamp, list[Fact]] = defaultdict(list)
        self.cumulative: dict[tuple[pd.Timestamp, int], list[Fact]] = defaultdict(list)
        self.by_start: dict[tuple[pd.Timestamp, int], list[Fact]] = defaultdict(list)
        for f in facts:
            n = f.n_quarters
            if f.start is None or n is None:
                continue
            if n == 4 and not f.form.upper().startswith(ANNUAL_FORM_PREFIXES):
                continue
            if n == 1:
                self.quarters[f.end].append(f)
            else:
                self.cumulative[(f.end, n)].append(f)
            self.by_start[(f.start, n)].append(f)


def _as_printed(cands: Sequence[Fact], asof: pd.Timestamp, prefer_accn: str | None) -> Fact | None:
    """The fact printed in `prefer_accn` if there is one, else the latest filed on or before `asof`."""
    ok = [f for f in cands if f.filed <= asof]
    if not ok:
        return None
    own = [f for f in ok if f.accn == prefer_accn]
    pool = own or ok
    return max(pool, key=lambda f: (f.filed, f.accn, f.start or pd.Timestamp(0)))


def value_as_printed(idx: PrintedIndex | None, end: pd.Timestamp, asof: pd.Timestamp,
                     prefer_accn: str | None) -> float | None:
    """One concept's quarter ending `end`, as printed on or before `asof` (see `_as_printed`):
    a directly tagged quarter first, else cumulative minus the quarters inside it, else cumulative
    minus the previous cumulative of the same fiscal year (the lab's two derivation rules)."""
    if idx is None:
        return None
    f = _as_printed(idx.quarters.get(end, ()), asof, prefer_accn)
    if f is not None:
        return f.val
    for n in (2, 3, 4):
        cum = _as_printed(idx.cumulative.get((end, n), ()), asof, prefer_accn)
        if cum is None or cum.start is None:
            continue
        lo, hi = cum.start, cum.end
        inner_ends = sorted({e for e, fs in idx.quarters.items() if lo < e < hi
                             and any(x.start is not None and x.start >= lo - START_TOLERANCE for x in fs)})
        if len(inner_ends) == n - 1:
            inner = [_as_printed([x for x in idx.quarters[e] if x.start >= lo - START_TOLERANCE], asof, cum.accn)
                     for e in inner_ends]
            if all(x is not None for x in inner):
                return cum.val - sum(x.val for x in inner)  # type: ignore[union-attr]
        prev_c = list(idx.by_start.get((lo, n - 1), ()))
        if not prev_c:
            prev_c = [x for (s, k), fs in idx.by_start.items() if k == n - 1
                      and abs((s - lo).days) <= START_TOLERANCE.days for x in fs]
        prev = _as_printed([x for x in prev_c if x.end < hi], asof, cum.accn)
        if prev is not None:
            return cum.val - prev.val
    return None


def _same(a: float, b: float) -> bool:
    return bool(np.isfinite(a) and np.isfinite(b) and abs(a - b) <= 1e-6 * max(1.0, abs(b)))


def year_ago_match(rows: pd.DataFrame, period_end: pd.Timestamp, asof: pd.Timestamp) -> pd.Timestamp | None:
    """The lab's `match(L - 365 days)` over the rows filed on or before `asof`."""
    vis = rows[rows["available_at"] <= asof]
    if vis.empty:
        return None
    target = period_end - pd.Timedelta(days=365)
    diffs = (vis["period_end"] - target).abs().dt.days
    if diffs.min() > MATCH_DAYS:
        return None
    return pd.Timestamp(vis.loc[diffs == diffs.min(), "period_end"].max())


def add_year_ago_as_printed(rows: pd.DataFrame, companyfacts: dict) -> pd.DataFrame:
    """Change (c): add YA_COLUMNS to one company's first-reported rows (the change (a)/(b) build).

    For each row R (quarter ending e, filed t) and its year-ago row Y (the lab's match, among rows
    filed by t): each of revenue, gross profit and operating income is re-resolved at e to find the
    concept R's value came from (it must reproduce R's value, else the field stays NaN), and Y's
    value of that same concept is taken as printed by t, preferring R's own filing. A gross profit
    computed as revenue minus a cost concept is compared as the same difference."""
    out = rows.copy()
    for c in YA_COLUMNS:
        out[c] = pd.NaT if c == "ya_period_end" else np.nan
    taxonomy = detect_taxonomy(companyfacts)
    if taxonomy is None or out.empty:
        return out[COMPARABLE_COLUMNS]
    rev_c = FLOW_CONCEPTS["revenue"].get(taxonomy, [])
    gp_c = FLOW_CONCEPTS["gross_profit"].get(taxonomy, [])
    oi_c = FLOW_CONCEPTS["operating_income"].get(taxonomy, [])
    cost_c = COUNCIL_COST_CONCEPTS.get(taxonomy, [])
    concepts = sorted(set(rev_c + gp_c + oi_c + cost_c))
    first = build_indexes(companyfacts, taxonomy, concepts)
    printed = {c: PrintedIndex(iter_facts(companyfacts, taxonomy, c)) for c in concepts}
    frame = out.assign(period_end=pd.to_datetime(out["period_end"]), available_at=pd.to_datetime(out["available_at"]))

    def printed_value(hit: tuple[str, Resolved] | None, ye: pd.Timestamp, t: pd.Timestamp) -> float:
        if hit is None:
            return float("nan")
        v = value_as_printed(printed.get(hit[0]), ye, t, hit[1].accn)
        return float("nan") if v is None else float(v)

    for i, r in frame.iterrows():
        e, t = r["period_end"], r["available_at"]
        ye = year_ago_match(frame, e, t)
        if ye is None:
            continue
        out.at[i, "ya_period_end"] = ye
        rev_hit = resolve_earliest_tagged(first, rev_c, e)
        rev_ok = rev_hit is not None and _same(rev_hit[1].value, float(r["revenue"]))
        rev_ya = printed_value(rev_hit, ye, t) if rev_ok else float("nan")
        out.at[i, "revenue_ya"] = rev_ya
        gp = float(r["gross_profit"])
        if np.isfinite(gp):
            gp_hit = resolve_earliest_tagged(first, gp_c, e)
            if gp_hit is not None and _same(gp_hit[1].value, gp):
                out.at[i, "gross_profit_ya"] = printed_value(gp_hit, ye, t)
            elif gp_hit is None and rev_ok:
                cost_hit = resolve_earliest_tagged(first, cost_c, e)
                if cost_hit is not None and _same(rev_hit[1].value - cost_hit[1].value, gp):  # type: ignore[index]
                    out.at[i, "gross_profit_ya"] = rev_ya - printed_value(cost_hit, ye, t)
        oi = float(r["operating_income"])
        if np.isfinite(oi):
            oi_hit = resolve_earliest_tagged(first, oi_c, e)
            if oi_hit is not None and _same(oi_hit[1].value, oi):
                out.at[i, "operating_income_ya"] = printed_value(oi_hit, ye, t)
    out["ya_period_end"] = pd.to_datetime(out["ya_period_end"])
    return out[COMPARABLE_COLUMNS]


def fundamentals_comparable(ticker: str, cik: str, companyfacts: dict) -> tuple[pd.DataFrame, dict]:
    """The headline build: changes (a) and (b) (`fundamentals_first_reported`) plus change (c)."""
    df, stats = fundamentals_first_reported(ticker, cik, companyfacts)
    return add_year_ago_as_printed(df, companyfacts), stats


def year_ago_from_rows(fund: pd.DataFrame, by: pd.Series | None = None) -> pd.DataFrame:
    """YA_COLUMNS filled from each company's own first-reported rows (no concept information). With
    these, `comparable_features` equals the lab's `fundamental_features` (a test pins it); the
    synthetic fixture uses it. `by` groups rows (default: the ticker)."""
    parts = []
    for _, g in fund.groupby(fund["ticker"] if by is None else by, sort=False):
        g = g.copy()
        g["period_end"] = pd.to_datetime(g["period_end"])
        g["available_at"] = pd.to_datetime(g["available_at"])
        first = g.sort_values(["available_at", "period_end"]).drop_duplicates("period_end", keep="first")
        by_end = first.set_index("period_end")
        ya = {c: [] for c in YA_COLUMNS}
        for _, r in g.iterrows():
            ye = year_ago_match(first, r["period_end"], r["available_at"])
            ya["ya_period_end"].append(ye if ye is not None else pd.NaT)
            for c, src in (("revenue_ya", "revenue"), ("gross_profit_ya", "gross_profit"),
                           ("operating_income_ya", "operating_income")):
                ya[c].append(float(by_end.at[ye, src]) if ye is not None else float("nan"))
        for c in YA_COLUMNS:
            g[c] = ya[c]
        parts.append(g)
    out = pd.concat(parts) if parts else fund.assign(**dict.fromkeys(YA_COLUMNS, np.nan))
    out["ya_period_end"] = pd.to_datetime(out["ya_period_end"])
    return out[COMPARABLE_COLUMNS]


def plausible_quarter(revenue: float, gross_profit: float, operating_income: float, *, need_margins: bool) -> bool:
    """Guards: revenue > 0; 0 <= gross profit <= revenue; |operating income| <= revenue. Margins that
    are missing fail only when `need_margins` (the L and Y quarters, whose margins the rule uses)."""
    if not (np.isfinite(revenue) and revenue > 0):
        return False
    for value, ok in ((gross_profit, lambda v: 0.0 <= v <= revenue),
                      (operating_income, lambda v: abs(v) <= revenue)):
        if np.isfinite(value):
            if not ok(value):
                return False
        elif need_margins:
            return False
    return True


def comparable_features(fund: pd.DataFrame, ticker: str, asof: pd.Timestamp) -> dict:
    """The four rule features on comparable pairs (change (c)), with the quarters they compare.

    L, P and the visibility rule are exactly the lab's (`fundamental_features`); Y and PY are the
    year-ago values printed with L and with P (YA_COLUMNS). Returns the four features, revenue_L,
    revenue_P, revenue_Y, revenue_PY, form_L, plausible (the guards on all four quarters),
    quarters_of_history, latest_period_end and latest_available_at."""
    nan = float("nan")
    out: dict[str, Any] = {c: nan for c in FUNDAMENTAL_COLUMNS}
    out.update({"revenue_L": nan, "revenue_P": nan, "revenue_Y": nan, "revenue_PY": nan, "form_L": None,
                "plausible": False, "quarters_of_history": 0, "latest_period_end": pd.NaT,
                "latest_available_at": pd.NaT})
    fund = fund.copy()
    fund["period_end"] = pd.to_datetime(fund["period_end"])
    fund["available_at"] = pd.to_datetime(fund["available_at"])
    visible = fund[(fund["ticker"] == ticker) & (fund["available_at"] <= pd.Timestamp(asof))]
    if visible.empty:
        return out
    visible = visible.sort_values(["available_at", "period_end"]).drop_duplicates(subset=["period_end"], keep="first")
    visible = visible.sort_values("period_end").reset_index(drop=True)
    latest = visible.loc[visible["period_end"].idxmax()]
    diffs = (visible["period_end"] - (latest["period_end"] - pd.Timedelta(days=91))).abs().dt.days
    prev = None
    if diffs.min() <= MATCH_DAYS:
        prev = visible[diffs == diffs.min()].sort_values("period_end", ascending=False).iloc[0]

    def growth(a: float, b: float) -> float:
        return a / b - 1 if np.isfinite(a) and np.isfinite(b) and b > 0 else nan

    def margin(x: float, rev: float) -> float:
        return x / rev if np.isfinite(x) and np.isfinite(rev) and rev > 0 else nan

    L = {k: float(latest[k]) for k in ("revenue", "gross_profit", "operating_income")}
    Y = {k: float(latest[f"{k}_ya"]) for k in ("revenue", "gross_profit", "operating_income")}
    g_now = growth(L["revenue"], Y["revenue"])
    out["revenue_growth_yoy"] = g_now
    out["gross_margin_change_yoy"] = margin(L["gross_profit"], L["revenue"]) - margin(Y["gross_profit"], Y["revenue"])
    out["operating_margin_change_yoy"] = (margin(L["operating_income"], L["revenue"])
                                          - margin(Y["operating_income"], Y["revenue"]))
    plausible = (plausible_quarter(L["revenue"], L["gross_profit"], L["operating_income"], need_margins=True)
                 and plausible_quarter(Y["revenue"], Y["gross_profit"], Y["operating_income"], need_margins=True))
    if prev is not None:
        P = {k: float(prev[k]) for k in ("revenue", "gross_profit", "operating_income")}
        PY = {k: float(prev[f"{k}_ya"]) for k in ("revenue", "gross_profit", "operating_income")}
        out["revenue_growth_acceleration"] = g_now - growth(P["revenue"], PY["revenue"])
        out["revenue_P"], out["revenue_PY"] = P["revenue"], PY["revenue"]
        plausible = (plausible and plausible_quarter(P["revenue"], P["gross_profit"], P["operating_income"],
                                                     need_margins=False)
                     and plausible_quarter(PY["revenue"], PY["gross_profit"], PY["operating_income"],
                                           need_margins=False))
    else:
        plausible = False
    out["revenue_L"], out["revenue_Y"] = L["revenue"], Y["revenue"]
    out["form_L"] = latest["form"]
    out["plausible"] = bool(plausible)
    out["quarters_of_history"] = int(visible["period_end"].nunique())
    out["latest_period_end"] = pd.Timestamp(latest["period_end"])
    out["latest_available_at"] = pd.Timestamp(latest["available_at"])
    return out
