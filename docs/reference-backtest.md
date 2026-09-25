# Reference book: mechanical backtest

> **Mechanical, in-sample, hindsight-chosen lines; not evidence for the council.** The lines and parameters were chosen with hindsight over this same window, and language models have read this history. The council itself is never backtested.

- Window: 2015-01-02 to 2026-09-24 (daily, trading at the next close). Generated 2026-09-25.
- Policy SHA-256: `5f69c4be3dfb1f053be05b22ca36b883fea3e5bf100ff21c4fee816ac649e84e`
- History starts: NDX 2012-10-23, SEMIS 2012-10-23, SPX 2012-10-23, GOLD 2012-10-23, BTC 2017-08-17, ETH 2017-08-17. Before a line's trend state exists its weight sits in cash.

## Results

| Book | CAGR | Ann. vol | Sharpe | Max drawdown | Calmar | Turnover / yr | Cost drag / yr | Mean gross | 12-month windows with a -25% drawdown |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Reference book (listed vehicles: real UCITS/ETC, real crypto) | 14.1% | 12.1% | 1.15 | -23.0% | 0.61 | 253% | 1.11% | 63% | 0.0% |
| Reference book (1x CFD vehicles, sensitivity) | 14.3% | 12.1% | 1.16 | -22.9% | 0.63 | 253% | 0.94% | 63% | 0.0% |
| Control: same book, no trend overlay (every line at level 1.0) | 15.9% | 14.9% | 1.06 | -31.8% | 0.50 | 77% | 0.32% | 72% | 7.5% |
| Control: same lines at static base weights (no trend, no vol cap) | 23.2% | 20.3% | 1.13 | -37.9% | 0.61 | 24% | 0.14% | 89% | 19.7% |
| Control: SPY buy and hold | 14.0% | 17.5% | 0.83 | -33.7% | 0.41 | 9% | 0.01% | 100% | 9.2% |
| Control: QQQ buy and hold | 19.4% | 21.9% | 0.92 | -35.1% | 0.55 | 9% | 0.01% | 100% | 17.5% |
| Control: 60/40 SPY/IEF, monthly rebalance | 8.6% | 10.3% | 0.85 | -21.4% | 0.40 | 30% | 0.26% | 100% | 0.0% |

**Trend overlay contribution** (reference minus the no-trend control): CAGR -1.81%, ann. vol -2.79%, max drawdown 8.79% (positive = shallower).

## Book shape

- Ex-ante vol of the target book: median 11.9%, max 30.0%; book scale k median 1.00.
- Reductions applied (share of days): BTC: no trend state 27%, BTC: no vol ratio 27%, ETH: no trend state 27%, ETH: no vol ratio 27%.

## Trend states (on the trading calendar)

| Line | Flips / yr | Up | Mixed | Down | No state |
|---|---:|---:|---:|---:|---:|
| NDX | 7.01 | 65% | 21% | 14% | 0% |
| SEMIS | 8.55 | 61% | 23% | 16% | 0% |
| SPX | 6.07 | 70% | 18% | 12% | 0% |
| GOLD | 5.90 | 49% | 28% | 22% | 0% |
| BTC | 13.23 | 28% | 21% | 24% | 27% |
| ETH | 12.53 | 27% | 21% | 25% | 27% |

## Soft-kill drill (WARN at 80% and HALT at 75% of the lifetime peak)

The mechanical book is not stopped here; this shows what it did in the 60 trading days after the first close of each drawdown episode at or below the line (an episode ends at a new lifetime peak).

WARN episodes: 1. HALT episodes: 0.

| Date | Event | Drawdown | Gross at event | Mean gross next 60d | Turnover next 60d | Return next 60d | Worst further fall | Back above line within 60d | Crossings in episode | Days below line in episode |
|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| 2022-09-21 | WARN | -20.2% | 22% | 33% | 49% | -0.8% | -3.4% | yes | 10 | 63 |

## Method

- Levels: up 1.0, mixed 0.5, down 0.25 (close vs SMA-50 and SMA-200 of completed closes; crypto flips need two consecutive closes). Unit = base weight x min(1, 1 / vol ratio): vol only shrinks a line.
- Scale: gross at most the reference limit, ex-ante vol (90-day EWMA covariance) at most the hard limit, then the line and group caps. Never short, never levered.
- Trading: signals at close t, trades at close t+1. A line trades when its level changes or its weight drifts past the deadband (level 0.25, crypto 0.5, at least 2% of NAV).
- Costs per side on traded weight: NDX 5.0 bps etf_real + 1.00 bps of NAV per leg, SEMIS 5.0 bps etf_real + 1.00 bps of NAV per leg, SPX 5.0 bps etf_real + 1.00 bps of NAV per leg, GOLD 5.0 bps etf_real + 1.00 bps of NAV per leg, BTC 100.0 bps crypto, ETH 100.0 bps crypto (CFD sensitivity: NDX 15.0 bps etf_cfd, SEMIS 15.0 bps etf_cfd, SPX 15.0 bps etf_cfd, GOLD 15.0 bps etf_cfd, BTC 100.0 bps crypto, ETH 100.0 bps crypto). No carry at 1x; cash earns nothing (Sharpe with a zero risk-free rate).
- Controls pay the listed ETF rate. The 60/40 control rebalances at month ends.
