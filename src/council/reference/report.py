"""Public summary of the mechanical reference backtest. PERCENT ONLY.

Rule: the rendered text carries no currency amounts, no local paths and no e-mail addresses;
`assert_public_safe` enforces it before anything is written for publication.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from council.reference.backtest import BacktestRun, CostModel
from council.reference.metrics import performance, soft_kill_drill, trend_stats

LABEL = "Mechanical, in-sample, hindsight-chosen lines; not evidence for the council."

BOOK_TITLES = {
    "reference": "Reference book (listed vehicles: real UCITS/ETC, real crypto)",
    "reference_cfd": "Reference book (1x CFD vehicles, sensitivity)",
    "no_trend": "Control: same book, no trend overlay (every line at level 1.0)",
    "static": "Control: same lines at static base weights (no trend, no vol cap)",
    "spy_bh": "Control: SPY buy and hold",
    "qqq_bh": "Control: QQQ buy and hold",
    "sixty_forty": "Control: 60/40 SPY/IEF, monthly rebalance",
}

_UNSAFE = [
    (re.compile(r"\$"), "currency sign"),
    (re.compile(r"\b(?:USD|EUR|GBP)\s*\d"), "currency amount"),
    (re.compile(r"/Users/|/home/|\b[A-Za-z]:\\"), "local path"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "e-mail address"),
    (re.compile(r"Application Support"), "private state path"),
]


class UnsafePublicText(ValueError):
    pass


def assert_public_safe(text: str) -> None:
    """Refuse text with a currency sign or amount, a local path or an e-mail address."""
    for pattern, what in _UNSAFE:
        match = pattern.search(text)
        if match:
            raise UnsafePublicText(f"public text contains a {what}: {match.group(0)!r}")


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{100.0 * x:.{digits}f}%"


def ratio(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _metrics_table(rows: dict[str, dict[str, Any]]) -> list[str]:
    out = [
        "| Book | CAGR | Ann. vol | Sharpe | Max drawdown | Calmar | Turnover / yr | Cost drag / yr | Mean gross | 12-month windows with a -25% drawdown |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, m in rows.items():
        out.append(
            f"| {BOOK_TITLES.get(name, name)} | {pct(m['cagr'])} | {pct(m['ann_vol'])} | {ratio(m['sharpe'])} | "
            f"{pct(m['max_drawdown'])} | {ratio(m['calmar'])} | {pct(m['turnover_per_year'], 0)} | "
            f"{pct(m['cost_drag_per_year'], 2)} | {pct(m['mean_gross'], 0)} | {pct(m['dd25_window_share'])} |"
        )
    return out


def render_markdown(
    run: BacktestRun,
    *,
    generated: date,
    synthetic: bool = False,
    warn_at: float = 0.80,
    halt_at: float = 0.75,
) -> str:
    """The public summary (percent only). Raises if the result would not be public-safe."""
    ref = run.books["reference"]
    rows = {name: performance(sim) for name, sim in run.books.items()}
    start, end = ref.nav.index[0].date(), ref.nav.index[-1].date()
    trend = trend_stats(run.panel.trend.loc[ref.nav.index])
    events = soft_kill_drill(ref, warn_at=warn_at, halt_at=halt_at)
    cfd = run.books.get("reference_cfd")
    lines: list[str] = ["# Reference book: mechanical backtest", ""]
    if synthetic:
        lines += ["> **SYNTHETIC DATA (random walks). These numbers describe nothing real.**", ""]
    lines += [
        f"> **{LABEL}** The lines and parameters were chosen with hindsight over this same window, and "
        "language models have read this history. The council itself is never backtested.",
        "",
        f"- Window: {start} to {end} (daily, trading at the next close). Generated {generated}.",
        f"- Policy SHA-256: `{run.policy_sha}`",
        "- History starts: "
        + ", ".join(f"{sym} {d}" if d else f"{sym} none" for sym, d in run.data_start.items())
        + ". Before a line's trend state exists its weight sits in cash.",
        "",
        "## Results",
        "",
        *_metrics_table(rows),
        "",
    ]
    if "no_trend" in rows and rows["reference"]["cagr"] is not None and rows["no_trend"]["cagr"] is not None:
        d_cagr = rows["reference"]["cagr"] - rows["no_trend"]["cagr"]
        d_dd = rows["reference"]["max_drawdown"] - rows["no_trend"]["max_drawdown"]
        d_vol = rows["reference"]["ann_vol"] - rows["no_trend"]["ann_vol"]
        lines += [
            "**Trend overlay contribution** (reference minus the no-trend control): "
            f"CAGR {pct(d_cagr, 2)}, ann. vol {pct(d_vol, 2)}, max drawdown {pct(d_dd, 2)} (positive = shallower).",
            "",
        ]
    ex = run.detail.ex_ante_vol.loc[ref.nav.index]
    held = ex[ex > 0]
    lines += [
        "## Book shape",
        "",
        f"- Ex-ante vol of the target book: median {pct(float(held.median()) if len(held) else None)}, "
        f"max {pct(float(held.max()) if len(held) else None)}; book scale k median {ratio(float(run.detail.k.median()))}.",
        "- Reductions applied (share of days): "
        + (
            ", ".join(
                f"{key} {pct(days / len(ex), 0)}" for key, days in sorted(run.detail.truncation_days.items())
            )
            or "none"
        )
        + ".",
        "",
        "## Trend states (on the trading calendar)",
        "",
        "| Line | Flips / yr | Up | Mixed | Down | No state |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for sym, st in trend.items():
        lines.append(
            f"| {sym} | {ratio(st['flips_per_year'])} | {pct(st['up'], 0)} | {pct(st['mixed'], 0)} | "
            f"{pct(st['down'], 0)} | {pct(st['missing'], 0)} |"
        )
    lines += [
        "",
        f"## Soft-kill drill (WARN at {pct(warn_at, 0)} and HALT at {pct(halt_at, 0)} of the lifetime peak)",
        "",
        "The mechanical book is not stopped here; this shows what it did in the 60 trading days after the first "
        "close of each drawdown episode at or below the line (an episode ends at a new lifetime peak).",
        "",
    ]
    n_warn = sum(1 for e in events if e["kind"] == "WARN")
    n_halt = sum(1 for e in events if e["kind"] == "HALT")
    lines += [f"WARN episodes: {n_warn}. HALT episodes: {n_halt}.", ""]
    if events:
        lines += [
            "| Date | Event | Drawdown | Gross at event | Mean gross next 60d | Turnover next 60d | Return next 60d | Worst further fall | Back above line within 60d | Crossings in episode | Days below line in episode |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|",
        ]
        for e in events:
            lines.append(
                f"| {e['date']} | {e['kind']} | {pct(e['drawdown'])} | {pct(e['gross_at_event'], 0)} | "
                f"{pct(e['mean_gross_next'], 0)} | {pct(e['turnover_next'], 0)} | {pct(e['return_next'])} | "
                f"{pct(e['worst_further'])} | {'yes' if e['recovered_within'] else 'no'} | "
                f"{e['crossings_in_episode']} | {e['days_below_in_episode']} |"
            )
    else:
        lines.append("No WARN or HALT event in this window.")
    lines += [
        "",
        "## Method",
        "",
        "- Levels: up 1.0, mixed 0.5, down 0.25 (close vs SMA-50 and SMA-200 of completed closes; crypto "
        "flips need two consecutive closes). Unit = base weight x min(1, 1 / vol ratio): vol only shrinks a line.",
        "- Scale: gross at most the reference limit, ex-ante vol (90-day EWMA covariance) at most the hard limit, "
        "then the line and group caps. Never short, never levered.",
        "- Trading: signals at close t, trades at close t+1. A line trades when its level changes or its weight "
        "drifts past the deadband (level 0.25, crypto 0.5, at least 2% of NAV).",
        "- Costs per side on traded weight: "
        + _cost_text(ref.cost_model)
        + (f" (CFD sensitivity: {_cost_text(cfd.cost_model)})" if cfd is not None else "")
        + ". No carry at 1x; cash earns nothing (Sharpe with a zero risk-free rate).",
        "- Controls pay the listed ETF rate. The 60/40 control rebalances at month ends.",
        "",
    ]
    text = "\n".join(lines)
    assert_public_safe(text)
    return text


def _cost_text(model: CostModel) -> str:
    """e.g. "NDX 5.0 bps etf_real + 1.00 bps of NAV per leg"."""
    parts = []
    for sym, cls in model.classes.items():
        text = f"{sym} {1e4 * model.per_side[sym]:.1f} bps {cls}"
        if model.fixed.get(sym, 0.0) > 0:
            text += f" + {1e4 * model.fixed[sym]:.2f} bps of NAV per leg"
        parts.append(text)
    return ", ".join(parts)
