"""Run the PRE-REGISTERED reference variants (policy/variants/reference-variants-v1.yaml) and apply
the frozen selection rule. Writes a percent-only summary to docs/reference-variants.md.

    COUNCIL_MODE=dry_run uv run python scripts/reference_variants.py
"""

from __future__ import annotations

import copy
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from backtest_reference import CONTROL_TICKERS, load_history  # noqa: E402

from council.policy import Policy  # noqa: E402
from council.reference.backtest import BacktestConfig, run_backtest  # noqa: E402
from council.reference.metrics import performance  # noqa: E402
from council.reference.report import assert_public_safe  # noqa: E402

SPEC = REPO / "policy" / "variants" / "reference-variants-v1.yaml"
DOCS = REPO / "docs" / "reference-variants.md"
START = date(2015, 1, 1)


def variant_policy(policy: Policy, v: dict) -> Policy:
    ref = copy.deepcopy(policy.reference)
    ref["trend"]["band_pct"] = float(v["band_pct"])
    ref["trend"]["confirm_closes"] = int(v["confirm_closes"])
    ref["trend"]["levels"] = dict(v["levels"])
    ref["vol"]["line_cap_ratio"] = float(v["line_cap_ratio"])
    return policy.model_copy(update={"reference": ref})


def select(rows: dict[str, dict]) -> tuple[str, str]:
    ok = {k: m for k, m in rows.items() if m["max_drawdown"] > -0.25 and m["turnover_per_year"] <= 3.0}
    if not ok:
        best = max(rows, key=lambda k: rows[k]["max_drawdown"])
        return best, "no variant qualified: shallowest max drawdown"
    top = max(m["cagr"] for m in ok.values())
    tied = [k for k, m in ok.items() if top - m["cagr"] <= 0.0025]
    best = max(tied, key=lambda k: ok[k]["sharpe"])
    return best, f"highest CAGR among {len(ok)} qualifying variants (ties within 0.25 pp by Sharpe)"


def main() -> int:
    spec = yaml.safe_load(SPEC.read_text())
    base = Policy.load()
    end = datetime.now(UTC).date() - timedelta(days=1)
    lines = [ln for ln in base.universe.lines if ln.in_reference]
    by_ticker = load_history(lines, start=START - timedelta(days=400), end=end)
    closes = {ln.symbol: by_ticker[ln.signal.ticker] for ln in lines if ln.signal.ticker in by_ticker}
    controls = {t: by_ticker[t] for t in CONTROL_TICKERS if t in by_ticker}
    rows: dict[str, dict] = {}
    last_year: dict[str, dict] = {}
    for name, v in spec["variants"].items():
        pol = variant_policy(base, v)
        run = run_backtest(closes, controls, pol, BacktestConfig(start=START, end=end), lines=lines)
        rows[name] = performance(run.books["reference"])
        ly = run_backtest(closes, controls, pol, BacktestConfig(start=end - timedelta(days=365), end=end),
                          lines=lines)
        last_year[name] = performance(ly.books["reference"])
        print(name, {k: round(rows[name][k], 4) for k in ("cagr", "ann_vol", "sharpe", "max_drawdown",
                                                           "turnover_per_year", "mean_gross")})
    winner, why = select(rows)
    out = ["# Reference-book variants (pre-registered)", "",
           "> **Mechanical, in-sample, hindsight-chosen lines; not evidence for the council.** The "
           "variants and the selection rule were committed and tagged `reference-variants-spec` before "
           "this run.", "",
           f"- Window: {START.isoformat()} to {end.isoformat()}; the last-year columns cover the final 365 days.",
           "- Selection rule: highest CAGR among variants with a max drawdown shallower than -25% and "
           "turnover at most 300% a year; CAGRs within 0.25 pp tie and the higher Sharpe wins.", "",
           "| Variant | Band | Confirm | Levels up/mixed/down | Vol cap ratio | CAGR | Vol | Sharpe | Max DD | "
           "Turnover/yr | Cost drag/yr | Mean gross | Last-year return | Last-year max DD |",
           "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, v in spec["variants"].items():
        m, ly = rows[name], last_year[name]
        lv = v["levels"]
        mark = " **(selected)**" if name == winner else ""
        ly_ret = (1 + ly["cagr"]) ** ly.get("years", 1.0) - 1 if ly.get("cagr") is not None else None
        out.append(
            f"| {name}{mark} | {v['band_pct']:.0f}% | {v['confirm_closes']} | {lv['up']}/{lv['mixed']}/{lv['down']} | "
            f"{v['line_cap_ratio']} | {m['cagr']:.1%} | {m['ann_vol']:.1%} | {m['sharpe']:.2f} | "
            f"{m['max_drawdown']:.1%} | {m['turnover_per_year']:.0%} | {m['cost_drag_per_year']:.2%} | "
            f"{m['mean_gross']:.0%} | {ly_ret:.1%} | {ly['max_drawdown']:.1%} |"
            if ly_ret is not None else f"| {name}{mark} | … |")
    out += ["", f"**Selected: {winner}** — {why}.", ""]
    text = "\n".join(out) + "\n"
    assert_public_safe(text)
    DOCS.write_text(text)
    print(f"selected {winner}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
