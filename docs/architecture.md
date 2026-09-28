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

## News
The news analyst reads public-domain items every cycle, in rehearsal and live: Federal Reserve Board,
BLS, BEA, TreasuryDirect and EIA releases, and SEC 8-K / 6-K metadata for the held and shortlisted
stocks (`data/gov_news.py`, `stocks/sec_news.py`). Each source has a 5 s / 10 s timeout, the whole
fetch a 30 s budget, and a failing source is a flag, never a stopped cycle. Items are cleaned and
leak-scanned when fetched, admitted only if available before the slot, and capped per source
(`policy/council.yaml` `news`). When an Agent Portfolio is connected, the broker's feed (eToro
Licensed Content) is added under a switch that is on by default (`invariants.BROKER_FEED_ENABLED`,
`news.broker_feed`): its text may reach the news prompt but is never published, and private copies
are purged within 7 days. A card that rests on 8-K metadata alone can never unlock a cut, because
the model never sees the filing's content. With no news item the news analyst makes no call.

## Transparency
Every model call's exact input (the desk it read, the earlier turns, the news items, the instruction
tail) is recorded privately before the call is sent, with every raw reply and any correction turn
(`state_dir/calls/`, never inside the repository). The operator reads it in the operator terminal:
`council inputs show <cycle> [--role bear] [--html]` prints or renders what each agent saw and the news
reading list (for every item: made into a card, cited, or not used), and `council inputs verify`
re-checks every hash. These commands, and `council purge-licensed`, refuse any agent context,
because their output can hold broker feed text. The public record publishes salted commitments,
never the private text.

## Operator boundary and onboarding
Every operator command carries one decorator (`@operator_command`) that refuses unless it runs in
the operator's own terminal: a TTY, `COUNCIL_ROLE=operator`, no agent variables or ancestors, not
under launchd. Commands that touch the broker, the write keychain or private attestations also
refuse unless they run from the installed release, a clean checkout of a tag verified on `origin`
(`ops/install.sh`). The unattended runner (`cycle`, `watch` under launchd) holds the READ token
only and never imports the broker writer; a test enforces it.

`council doctor --ready` reports every readiness gate (owned by the agents, the user or the token)
with a code and the next command; `ops/install.sh --load` refuses until the token
gates are green. Before any real order the operator runs minimum-size **smoke tickets** (S1–S7):
each goes through the normal `approve` path, its record stays private, and only a weightless ops
row is published, because a weight would reveal the NAV. Each passed step switches on one
**capability** (real ETF, stop-loss modify, partial close, real crypto, CFD short, …); the planner
skips a leg whose capability is not verified (`capability_missing:<name>`). Token day is rehearsed twice against a loopback fake
broker in a marked throwaway sandbox: automatically in CI (`tests/rehearsal`,
`council rehearse onboarding`) and by the operator (`ops/rehearse-onboarding.sh`), followed by a
48-hour launchd soak on a local remote. `council why` renders the per-line decision trail from the
public record ([data rights](data-rights.md#decision-trail)); the steps are in the
[runbook](runbook.md).

## Why so much code around the models
The research that preceded this repo found that rules written only in prompts were overridden often,
while the same rules enforced in code held; that averaging several agents was worse than one
accountable decision; that the model gives different answers to identical prompts about a fifth of
the time; and that fees decided every result. The architecture follows from those findings.
