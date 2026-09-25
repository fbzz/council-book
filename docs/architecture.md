# Architecture

council-book is three layers with hard boundaries between them:

1. **Deterministic core (code).** Data, the reference book, officers (events, volatility, costs),
   the risk engine, planning, the ledger and publishing. Every number that can move money is decided
   here, and every rule has a test.
2. **Council (language models).** Analysts write evidence cards; a bull and a bear debate; a portfolio
   manager proposes at most three deviations from the reference book. Nothing the council says is
   executed directly — the core clips it, prices it and may throw it away.
3. **Human gate.** A person approves every order in a separate operator terminal. The unattended runner
   holds a read-only broker token and cannot import the order-placing code.

```
 launchd (hourly :40, read-only token)            operator terminal (write keychain, TTY, typed nonce)
        │                                                      │
        ▼                                                      ▼
  council cycle ──► data steward ──► fact pack (%, available_at ≤ slot)      council approve <id>
        │                 │                                         re-snapshot · re-eligibility
        │                 ▼                                         re-risk · re-cost · commitment check
        │          reference book (trend × vol cap, ≈1× gross)                 │
        │                 │                                                      ▼
        │     officers: vol shock · macro events · costs               broker writes (close → open,
        │                 │                                             stop-loss on every open)
        │                 ▼                                                      │
        │     analysts (news, macro) ─► cards                                   ▼
        │     bull ─► bear ─► bull rebuttal                             reconcile · ledger
        │     PM ×3 (≤3 deviations each) ─► audit ─► bands ─► medoid
        │                 │
        │                 ▼
        │     risk engine (gross, net, caps, margin, vol breakers, deadband,
        │                  minimum hold, churn, net-of-cost gate, kill switch)
        │                 │
        │                 ▼
        │     plan (legs) ─► ledger ─► seal commitment ─► git push ─► proposal ─► notification
        ▼
  council watch (every 15 min, read-only): NAV/peak, kill switch, stop presence, drift, publishes executions
```

## The reference book
Each **line** (Nasdaq-100, semiconductors, S&P 500, gold, BTC, ETH; oil, EUR/USD and GBP/USD as
council-only overlays) has a signal history and an ordered list of tradable vehicles. The reference
level is 1.0 in an uptrend (price above both its 50- and 200-day averages), 0.5 when mixed and 0.25
in a downtrend; each line's unit weight is capped when its volatility is above its one-year median.
The book is long-only and never levered (about 1× gross). It is the default position, the fallback
when the council fails, and the benchmark the council is scored against.

## What the council may do
The PM may move up to three lines per cycle, inside **bands** set by code:

| Trend | Reference lines | Overlay lines |
|---|---|---|
| Up | cut to reference − 0.5 only with a qualifying card; +0.5 leverage only if the leveraged leg passes the cost gate | 0 … +0.5 |
| Mixed | 0 … 1.0 | −0.25 … +0.25 |
| Down | −0.5 (short, cost-gated, needs a risk-down card) … +0.25 | −0.5 … 0 |

BTC and ETH are reference-only in v1: at about 1% per side, council tilts cannot pay for themselves.

## Evidence and timing
Every fact carries the time it became available. A cycle may only use facts available at its slot
start, and only completed bars. A lookahead test mutates everything at or after the slot and checks
that the pack's hash does not change.

## Why so much code around the models
The research that preceded this repo found that rules written only in prompts were overridden often,
while the same rules enforced in code held; that averaging several agents was worse than one
accountable decision; that the model gives different answers to identical prompts about a fifth of
the time; and that fees decided every result. The architecture follows from those findings.
