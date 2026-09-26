"""The public ranking document of a quarterly stock rank (design §4.4): `ranking-<quarter>.md`.

Percent only and SEC-derived. It shows the rank date, the rule, the sources with their attribution,
the funnel and the exclusions by reason, and per Fama-French 12 sector the top names by the sector
score with the four features and the two score percentiles, and each name's role in the proposed
sleeve file. It never shows a revenue level, a price, a volume, a dollar amount, a CIK, a broker
symbol or instrument id, or why a broker check failed (a replacement is only counted).

`render` refuses text that fails `council.reference.report.assert_public_safe` or the leak scan
(`council.publish.leakscan.scan`, with the caller's private canaries): the human copies the file to
`docs/stocks/`, which CI scans too.

Data rights: SEC EDGAR data are US public domain; the SIC-to-FF12 table is Kenneth R. French's data
library; index member lists come from Wikipedia (CC BY-SA 4.0) and are used, not republished: only
the ranked names appear, with the attribution.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from council.publish.leakscan import scan
from council.reference.report import UnsafePublicText, assert_public_safe
from council.stocks import score
from council.stocks.rank import FUNNEL, RankResult

TOP_PER_SECTOR = 5
LABEL = ("A mechanical ranking of reported fundamentals inside each sector. It is a rule, not a "
         "forecast, and not investment advice.")
FUNNEL_LABELS = {
    "member": "Index members and AI-list names",
    "mapped": "With a SEC identity (CIK)",
    "common": "Common stock, not an ADR",
    "priced": "Priced within the last 7 days",
    "listed": "Listed at least 290 days",
    "one_per_cik": "One share class per company",
    "sector_known": "Sector known (SIC to FF12)",
    "not_money": "Not in FF12 Money (financials)",
    "companyfacts": "SEC companyfacts available",
    "us_gaap": "Reports in US GAAP",
    "visible": "A quarter filed before the rank date",
    "domestic": "Latest report on a domestic form (10-Q/10-K)",
    "fresh": "Latest filing at most 120 days old",
    "all_features": "All four features computable",
    "revenue_floor": "Quarterly revenue above the floor in all four quarters used",
    "plausible": "Plausible margins",
}
REASON_LABELS = {
    "not_common": "not common stock, or an ADR",
    "not_priced": "no recent price",
    "not_listed": "listed for less than 290 days",
    "no_cik": "no SEC identity",
    "duplicate_cik": "another share class of the same company",
    "no_sector": "sector unknown",
    "excluded_sector": "FF12 Money (financials)",
    "no_companyfacts": "no SEC companyfacts",
    "not_us_gaap": "not US GAAP",
    "no_visible_quarter": "no quarter filed before the rank date",
    "not_domestic_form": "latest report not on a domestic form",
    "stale_filing": "latest filing older than 120 days",
    "missing_feature": "a feature not computable",
    "revenue_floor": "quarterly revenue below the floor",
    "implausible": "implausible margins",
}


@dataclass(frozen=True)
class Source:
    """One membership source as the document credits it (no revision ids: they are long numbers)."""

    name: str
    as_of: date
    licence: str

    def line(self) -> str:
        return f"{self.name}, revision of {self.as_of:%Y-%m-%d}, {self.licence}"


def mediawiki_source(title: str, as_of: date) -> Source:
    return Source(f'Wikipedia, "{title}"', as_of, "CC BY-SA 4.0")


def rule_text(config: Any) -> str:
    """The selection rule in words, from the rank's own config (the frozen `score.select_rule`)."""
    v = config.variant
    score_name = "sector score" if v["score"] == "sector" else "overall score"
    cap, n, mult = int(v["cap"]), int(config.n), float(config.hold_buffer_multiple)
    if v["constraint"] == "quota":
        return (f"{score_name}, sector quotas by largest remainder with each at most {cap}, {n} names at equal "
                f"weight, held names kept while inside {mult:g} times their sector's quota")
    return (f"{score_name}, at most {cap} names per sector, {n} names at equal weight, held names kept "
            f"while inside the top {mult:g} times {n}")


def ticker(line_id: str) -> str:
    """Display form of a line id (class separator "."), as the site shows it."""
    return line_id.replace("_", ".")


def _pct(x: Any) -> str:
    return "n/a" if x is None or not math.isfinite(float(x)) else f"{100.0 * float(x):.1f}%"


def _pp(x: Any) -> str:
    return "n/a" if x is None or not math.isfinite(float(x)) else f"{100.0 * float(x):+.1f} pp"


def _pctile(x: Any) -> str:
    return "n/a" if x is None or not math.isfinite(float(x)) else f"{100.0 * float(x):.0f}"


def render(
    result: RankResult,
    *,
    quarter: str,
    roles: Mapping[str, str],
    counts: Mapping[str, int],
    sources: Sequence[Source],
    rank_config_sha256: str,
    rule_cell: str,
    sector_cap: int,
    replaced: int,
    generated: date,
    canaries: Sequence[str | float | int] = (),
    ai_list: bool = True,
) -> str:
    """The document. `roles` maps line ids to `selected` / `shortlist` in the proposed sleeve file;
    `counts` gives {"in", "out", "retiring", "pruned"}. Raises UnsafePublicText when the result is
    not public-safe (a currency sign, a path, an e-mail address, a long number or a canary)."""
    elig = result.eligible
    n_names = result.config.n
    out = [
        f"# Stock sleeve ranking, {quarter}",
        "",
        f"_{LABEL}_",
        "",
        f"- Rank date (D): {result.asof:%Y-%m-%d}. Only SEC filings filed before D are used.",
        f"- Rule: cell {rule_cell} ({rule_text(result.config)}). The pre-registered study did not "
        "pass its adoption gate; the rule was adopted anyway as a recorded override "
        "(docs/stock-sleeve-study.md).",
        "- Universe: S&P 500 and Nasdaq-100 members" + (
            ", plus the AI-adjacent list ranked by the same rule and filters (an untested extension of the "
            "studied universe)." if ai_list else "."),
        f"- Rank configuration sha256: `{rank_config_sha256}`.",
        "",
        "## Sources",
        "",
        "- Fundamentals and SIC codes: SEC EDGAR (US public domain).",
        "- Sectors: SIC codes mapped to the Fama-French 12 industries (Kenneth R. French data library).",
        *[f"- Index membership: {s.line()}. Member lists are used, not republished." for s in sources],
        "- Prices: used only for the listing and pricing filters; none are shown.",
        "",
        "## Funnel",
        "",
        "| Step | Names left |",
        "|---|---:|",
        *[f"| {FUNNEL_LABELS.get(k, k)} | {int(result.funnel.get(k, 0))} |" for k in FUNNEL],
        "",
        "## Exclusions",
        "",
        "| Reason | Names |",
        "|---|---:|",
        *([f"| {REASON_LABELS.get(k, k)} | {v} |" for k, v in result.exclusion_counts.items()]
          or ["| none | 0 |"]),
        "",
        "## The proposed sleeve",
        "",
        f"- Selected: {sum(1 for r in roles.values() if r == 'selected')} of {n_names}; shortlisted: "
        f"{sum(1 for r in roles.values() if r == 'shortlist')}.",
        f"- Against the previous quarter: {counts.get('in', 0)} in, {counts.get('out', 0)} out, "
        f"{counts.get('retiring', 0)} retiring (sold, then removed once flat), "
        f"{counts.get('pruned', 0)} removed.",
        *([f"- Selected names that are in the universe only through the AI-adjacent list: "
           f"{len(result.ai_selected)}."] if ai_list else []),
        f"- Names replaced because the broker cannot hold them as real, unlevered shares with a "
        f"stop-loss: {replaced} (the next eligible name takes the slot, from the same sector when there "
        "is one; a divergence from the studied rule).",
    ]
    if not elig.empty:
        order = score.ordered(elig, "sector")
        by_sector = elig["sector"].value_counts().to_dict()
        quotas = score.sector_quotas(by_sector, n_names, int(sector_cap))
        out += ["", f"## By sector (top {TOP_PER_SECTOR} by the sector score)", "",
                "Scores are percentiles (0 to 100) of the average rank of the four features: inside the "
                "sector (sectors with fewer than the minimum peer group are scored together), and "
                "across all eligible names. Growth is year on year; changes are in percentage points."]
        for sector in sorted(by_sector):
            names = [k for k in order if elig.at[k, "sector"] == sector][:TOP_PER_SECTOR]
            out += ["", f"### {sector} ({by_sector[sector]} eligible, quota {quotas.get(sector, 0)})", "",
                    "| # | Name | Role | Revenue growth | Growth acceleration | Gross margin change "
                    "| Operating margin change | Sector score | Overall score |",
                    "|---:|---|---|---:|---:|---:|---:|---:|---:|"]
            for i, key in enumerate(names, 1):
                row = elig.loc[key]
                out.append(
                    f"| {i} | {ticker(str(key))} | {roles.get(str(key), '')} | {_pct(row['revenue_growth_yoy'])} | "
                    f"{_pp(row['revenue_growth_acceleration'])} | {_pp(row['gross_margin_change_yoy'])} | "
                    f"{_pp(row['operating_margin_change_yoy'])} | {_pctile(row['s_score'])} | "
                    f"{_pctile(row['g_score'])} |")
    out += ["", f"Generated {generated:%Y-%m-%d} by `council stocks rank`.", ""]
    text = "\n".join(out)
    check_public(text, canaries=canaries)
    return text


def check_public(text: str, *, canaries: Sequence[str | float | int] = ()) -> None:
    """`assert_public_safe` and the leak scan; raises UnsafePublicText on any finding."""
    assert_public_safe(text)
    findings = scan(text, canaries=canaries)
    if findings:
        raise UnsafePublicText("ranking document fails the leak scan: " + "; ".join(str(f) for f in findings))
