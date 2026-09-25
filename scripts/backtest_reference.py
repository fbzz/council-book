"""Mechanical reference-book backtest (M1): daily, one-day lag, cost floors, controls.

Real mode loads Tiingo (equity ETFs) and Binance (crypto) daily history through the data layer
and writes:
- private CSVs to <state dir>/backtests/ (NAV index, weights, targets, levels, trades, trend states,
  metrics, soft-kill drill), never inside the repository;
- the public summary docs/reference-backtest.md, PERCENT ONLY and labelled "mechanical, in-sample,
  hindsight-chosen lines; not evidence for the council".

`--synthetic` uses seeded random walks and touches no network; its outputs go to
<state dir>/backtests/synthetic/ (the public docs file is never overwritten by synthetic numbers).

    uv run python scripts/backtest_reference.py --synthetic
    uv run python scripts/backtest_reference.py            # real data; run only on a tagged spec
"""

from __future__ import annotations

import argparse
import importlib
import inspect
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from council import paths
from council.policy import LineSpec, Policy
from council.reference.backtest import BacktestConfig, BacktestRun, run_backtest
from council.reference.metrics import performance, soft_kill_drill
from council.reference.report import render_markdown
from council.reference.synthetic import synthetic_closes

DEFAULT_START = date(2015, 1, 1)
DEFAULT_DOCS = paths.REPO_ROOT / "docs" / "reference-backtest.md"
CONTROL_TICKERS = ("SPY", "QQQ", "IEF")
TIINGO_SECRET = "council-book.tiingo"
SYNTHETIC_CRYPTO_START = date(2017, 8, 17)
MAX_KLINE_PAGES = 20                      # 20 x 1000 daily bars is far more than exists

_ALIASES: dict[str, tuple[str, ...]] = {
    "ticker": ("ticker", "symbol", "pair"),
    "start": ("start", "start_date", "since", "start_time"),
    "end": ("end", "end_date", "until", "end_time"),
    "token": ("token", "api_token", "api_key"),
    "interval": ("interval", "timeframe"),
}


def _date(text: str) -> date:
    return date.fromisoformat(text)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mechanical reference-book backtest (percent-only summary).")
    p.add_argument("--synthetic", action="store_true", help="seeded random-walk data, no network")
    p.add_argument("--seed", type=int, default=7, help="synthetic seed")
    p.add_argument("--start", type=_date, default=DEFAULT_START, help="first decision day (default 2015-01-01)")
    p.add_argument("--end", type=_date, default=None, help="last day (default: latest completed UTC day)")
    p.add_argument("--warmup-days", type=int, default=800, help="calendar days of history before --start")
    p.add_argument(
        "--commission-nav", type=float, default=10_000.0,
        help="NAV (account currency) against which the fixed per-leg commission is expressed",
    )
    p.add_argument("--slippage-bps", type=float, default=0.0, help="extra cost per side, bps")
    p.add_argument("--out-dir", type=Path, default=None, help="private CSV directory (default: state dir)")
    p.add_argument("--docs-path", type=Path, default=None, help="public markdown path")
    return p.parse_args(argv)


# ------------------------------------------------------------------------------------ data loading


def call_flexible(fn: Callable[..., Any], **values: Any) -> Any:
    """Call a data-layer function by keyword, matching each value to the parameter name it uses
    (e.g. ticker/symbol, start/start_date). Values with no matching parameter are dropped, except
    the ticker, which is required."""
    params = inspect.signature(fn).parameters
    kwargs: dict[str, Any] = {}
    for key, value in values.items():
        for alias in _ALIASES.get(key, (key,)):
            if alias in params:
                kwargs[alias] = value
                break
        else:
            if key == "ticker":
                raise TypeError(f"{getattr(fn, '__name__', fn)} takes no ticker/symbol parameter")
    return fn(**kwargs)


_DATE_COLS = ("date", "open_time", "start_time", "timestamp", "datetime", "time", "ts", "at", "day")
_CLOSE_COLS = ("adjClose", "adj_close", "close", "Close", "c")


def to_close_series(raw: Any, name: str = "close") -> pd.Series:
    """Normalise a data-layer result to a daily close Series on a tz-naive date index.

    Accepts a Series, a DataFrame (date index or a date column; adjusted close preferred), a list of
    dicts or pydantic models, or raw Binance kline rows ([open_time_ms, o, h, l, close, ...])."""
    if isinstance(raw, pd.Series):
        series = raw.astype(float)
    else:
        frame = _to_frame(raw)
        close_col = next((c for c in _CLOSE_COLS if c in frame.columns), None)
        if close_col is None:
            raise ValueError(f"no close column in {list(frame.columns)}")
        date_col = next((c for c in _DATE_COLS if c in frame.columns), None)
        if date_col is not None:
            frame = frame.set_index(date_col)
        series = frame[close_col].astype(float)
    index = series.index
    if pd.api.types.is_numeric_dtype(index):
        idx = pd.to_datetime(index, unit="ms", utc=True)
    else:
        idx = pd.to_datetime(index, utc=True)
    series.index = pd.DatetimeIndex(idx).tz_convert(None).normalize()
    series = series[~series.index.duplicated(keep="last")].sort_index().dropna()
    return series.rename(name)


def _to_frame(raw: Any) -> pd.DataFrame:
    if isinstance(raw, pd.DataFrame):
        return raw
    rows = list(raw)
    if not rows:
        return pd.DataFrame(columns=["date", "close"])
    first = rows[0]
    if hasattr(first, "model_dump"):
        return pd.DataFrame([r.model_dump() for r in rows])
    if isinstance(first, Mapping):
        return pd.DataFrame(rows)
    if isinstance(first, (list, tuple)):
        return pd.DataFrame({"open_time": [int(r[0]) for r in rows], "close": [float(r[4]) for r in rows]})
    raise TypeError(f"unsupported data-layer result: {type(first).__name__}")


def _accepts(fn: Callable[..., Any], key: str) -> bool:
    params = inspect.signature(fn).parameters
    return any(alias in params for alias in _ALIASES.get(key, (key,)))


def fetch_binance_daily_history(
    ticker: str,
    *,
    start: date,
    end: date,
    client: Any = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Every completed Binance daily kline from `start` (or the listing date) to `end`, paged
    forward 1000 bars at a time. The data layer's `fetch_klines` returns only the most recent 1000
    bars, so this reuses its URL, retry helper and parser (same completed-bars rule) instead."""
    binance = importlib.import_module("council.data.binance")
    http = importlib.import_module("council.data.http")
    asof = now or datetime.now(UTC)
    what = f"binance klines {ticker} 1d history"
    stop = pd.Timestamp(end).tz_localize("UTC")
    cursor = pd.Timestamp(start).tz_localize("UTC")
    frames: list[pd.DataFrame] = []
    with http.client_scope(client) as session:
        for _ in range(MAX_KLINE_PAGES):
            params = {"symbol": ticker, "interval": "1d", "limit": binance.MAX_LIMIT,
                      "startTime": int(cursor.timestamp() * 1000)}
            response = http.get_with_retry(session, binance.KLINES_URL, what=what, params=params)
            payload = http.json_body(response, what=what)
            bars = binance.parse_klines(payload, "1d", asof)
            if bars.empty:
                break
            frames.append(bars)
            last = bars.index.max()
            if last >= stop or len(payload) < binance.MAX_LIMIT:
                break
            cursor = last + pd.Timedelta(days=1)
    if not frames:
        raise RuntimeError(f"no Binance history for {ticker}")
    history = pd.concat(frames)
    return history[~history.index.duplicated(keep="last")].sort_index()


def load_history(lines: Sequence[LineSpec], *, start: date, end: date) -> dict[str, pd.Series]:
    """Daily closes by ticker from the data layer (network). Imported lazily: tests never call it
    without fakes, and the data layer is built separately. Needs the Keychain Tiingo token, which
    stub mode never reads (run with COUNCIL_MODE=dry_run)."""
    tiingo = importlib.import_module("council.data.tiingo")
    binance = importlib.import_module("council.data.binance")
    credentials = importlib.import_module("council.data.credentials")
    token = credentials.secret(TIINGO_SECRET)
    if not token:
        raise SystemExit(
            f"no Tiingo token: the Keychain entry {TIINGO_SECRET} is unset or COUNCIL_MODE is stub "
            "(stub mode never reads the Keychain; use COUNCIL_MODE=dry_run)"
        )
    tiingo_tickers = {ln.signal.ticker for ln in lines if ln.signal.source == "tiingo"} | set(CONTROL_TICKERS)
    crypto_tickers = {ln.signal.ticker for ln in lines if ln.signal.source == "binance"}
    out: dict[str, pd.Series] = {}
    for ticker in sorted(tiingo_tickers):
        raw = call_flexible(tiingo.fetch_daily, ticker=ticker, start=start, end=end, token=token)
        out[ticker] = to_close_series(raw, ticker)
    for ticker in sorted(crypto_tickers):
        if _accepts(binance.fetch_klines, "start"):
            raw = call_flexible(binance.fetch_klines, ticker=ticker, interval="1d", start=start, end=end)
        else:
            raw = fetch_binance_daily_history(ticker, start=start, end=end)
        out[ticker] = to_close_series(raw, ticker)
    return {t: s[s.index <= pd.Timestamp(end)] for t, s in out.items()}


def synthetic_history(lines: Sequence[LineSpec], *, start: date, end: date, seed: int) -> dict[str, pd.Series]:
    equity = sorted({ln.signal.ticker for ln in lines if ln.signal.source != "binance"} | set(CONTROL_TICKERS))
    crypto = sorted({ln.signal.ticker for ln in lines if ln.signal.source == "binance"})
    return synthetic_closes(
        equity, start=start, end=end, crypto_tickers=crypto,
        crypto_start=max(start, SYNTHETIC_CRYPTO_START), seed=seed,
    )


# ------------------------------------------------------------------------------------ outputs


def write_private(run: BacktestRun, out_dir: Path, *, warn_at: float, halt_at: float) -> list[Path]:
    """Private CSVs (NAV indices and weights, no currency). Refuses a directory inside the repo."""
    paths.assert_outside_repo(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = run.books["reference"]
    tables: dict[str, pd.DataFrame] = {
        "nav.csv": pd.DataFrame({name: sim.nav for name, sim in run.books.items()}),
        "reference_weights.csv": ref.weights,
        "reference_targets.csv": run.plan.targets,
        "reference_levels.csv": run.plan.levels,
        "reference_trades.csv": ref.trades,
        "reference_costs.csv": ref.costs.to_frame(),
        "reference_ex_ante_vol.csv": pd.concat([run.detail.ex_ante_vol, run.detail.k], axis=1),
        "trend_states.csv": run.panel.trend,
        "metrics.csv": pd.DataFrame({name: performance(sim) for name, sim in run.books.items()}).T,
        "kill_drill.csv": pd.DataFrame(soft_kill_drill(ref, warn_at=warn_at, halt_at=halt_at)),
    }
    written = []
    for filename, table in tables.items():
        path = out_dir / filename
        table.to_csv(path)
        written.append(path)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    policy = Policy.load()
    today = datetime.now(UTC).date()
    end = args.end or (today - timedelta(days=1))
    if end <= args.start:
        raise SystemExit("--end must be after --start")
    fetch_start = args.start - timedelta(days=args.warmup_days)
    lines = [ln for ln in policy.universe.lines if ln.in_reference]
    if args.synthetic:
        by_ticker = synthetic_history(lines, start=fetch_start, end=end, seed=args.seed)
    else:
        by_ticker = load_history(lines, start=fetch_start, end=end)
    closes = {ln.symbol: by_ticker[ln.signal.ticker] for ln in lines if ln.signal.ticker in by_ticker}
    controls = {t: by_ticker[t] for t in CONTROL_TICKERS if t in by_ticker}
    config = BacktestConfig(
        start=args.start, end=end, commission_nav=args.commission_nav, slippage_bps=args.slippage_bps
    )
    run = run_backtest(closes, controls, policy, config, lines=lines)

    kill = policy.risk["killswitch"]
    warn_at, halt_at = float(kill["warn_at"]), float(kill["halt_at"])
    base_dir = args.out_dir or paths.state_dir() / "backtests"
    out_dir = base_dir / "synthetic" if args.synthetic else base_dir
    written = write_private(run, out_dir, warn_at=warn_at, halt_at=halt_at)
    text = render_markdown(run, generated=today, synthetic=args.synthetic, warn_at=warn_at, halt_at=halt_at)
    docs_path = args.docs_path or (out_dir / "reference-backtest-synthetic.md" if args.synthetic else DEFAULT_DOCS)
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    docs_path.write_text(text)

    m = performance(run.books["reference"])
    cagr = "n/a" if m["cagr"] is None else f"{m['cagr']:.2%}"
    print(f"reference: CAGR {cagr}  vol {m['ann_vol']:.2%}  max drawdown {m['max_drawdown']:.2%}")
    print(f"wrote {len(written)} private CSVs to {out_dir}")
    print(f"wrote summary to {docs_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
