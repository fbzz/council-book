# Stock sleeve: the study result and the user's override

> **The rule failed its pre-registered adoption gate (NOT ADOPTED). The user adopted it anyway, as a
> recorded override.** Everything below is a mechanical, in-sample backtest of a rule that was
> committed and tagged before any return was computed. It is not evidence for the council, which is
> never backtested. Nothing in the live book changes with this page: stock lines reach live cycles
> only in a separate go-live commit.

All figures are percentages, net of every cost unless a row says otherwise. Returns are daily, 252
days a year, zero risk-free rate. CAGR is the compound annual growth rate; max DD is the maximum
drawdown.

## In short

- **What ran.** The pre-registered stock-sleeve study ([the spec](stock-sleeve-spec.md), git tag
  [`stock-sleeve-spec`](https://github.com/fbzz/council-book/tree/stock-sleeve-spec)) ran once, from
  a checkout of that tag, over 42 quarterly rebalances from 2016-05-20 to 2026-09-04.
- **What it chose.** The selection rule picked the cell **SQ-8** (sector score, sector quotas,
  8 names). No overlay option kept the re-based book above the kill switch's HALT line in every
  check, so the overlay rule reported the one with the shallowest worst drawdown: **no overlay**.
- **The gate.** G1 passed; G2, G3, B1 and B2 failed. Verdict: **NOT ADOPTED**.
- **The decision.** On 2026-09-25 the user chose option 1 of the spec's section 12: adopt the rule
  sleeve anyway, as a recorded override. Live will run SQ-8, no overlay, the sleeve at 50% of NAV
  and the core lines re-based to 45%, with the AI-adjacent list ranked mechanically too (an untested
  extension). The override is recorded in
  [`policy/variants/stock-sleeve-adopted.yaml`](../policy/variants/stock-sleeve-adopted.yaml).

## What was pre-registered

The spec, its numbers (`policy/variants/stock-sleeve-variants-v1.yaml`), the study script
(`scripts/stock_sleeve_study.py`) and the four rule modules the live book will import
(`src/council/stocks/pit.py`, `score.py`, `sectors.py` and `src/council/reference/sleeve.py`) were
committed, tagged `stock-sleeve-spec` (commit `fc8fb39`) and pushed before the run. The script
refuses a real run unless it executes from a clean checkout of the tag with the frozen inputs, and it
runs at most once. The run folder is `run-20260926T012203Z`; the SHA-256 of its result and gate files
are in the adoption record.

- **Universe.** S&P 500 and Nasdaq-100 members at each rebalance date, point in time; common stock
  only, one security per company, financials (Fama-French 12 "Money") excluded, and a recent
  domestic quarterly filing with all four features required.
- **Rule.** Four fundamentals features: revenue growth, revenue-growth acceleration, gross-margin
  change and operating-margin change, each against the same quarter a year earlier and as first
  reported. The score is the mean of their percentile ranks.
- **Cells.** SC (sector score, at most 3 names per sector) and SQ (sector score, sector quotas
  proportional to the eligible set), each with 8 and 10 names: four selectable cells. GC (a score over
  the whole universe) is a reported control, never selected. Equal weight, a quarterly refresh with a
  hold buffer, and the book's deadband rule between rebalances.
- **Selection.** The selectable cell with the highest net Sharpe, stand-alone, without an overlay;
  Sharpes within 0.05 tie and the lower total cost wins.
- **Overlay.** None, down-only or the reference's levels, set by the SPX line's trend. An option
  qualifies only if the re-based whole book (sleeve at 50%, core re-based to 45%) stays shallower
  than the HALT line (-25%) in sample, at doubled costs and in three older stress windows.
- **Costs.** Variable costs on every leg of every book, and the broker's fixed fee per real trade at
  the account's sealed funding, which the public record never states.

## The adoption gate

| Check | Must hold | Result | Values |
|---|---|---|---|
| G1 pool | Before the fixed fee, the selected cell's CAGR and Sharpe are at least the equal-weight pool's, at base and doubled variable costs | pass | 18.7% / 0.84 against 13.3% / 0.77; doubled variable costs 17.3% / 0.79 against 13.0% / 0.75 |
| G2 random | Adjusted random-null percentile at or above the 95th | **fail** | 85% adjusted (94% against the cell's own draws) |
| G3 regimes | Before the fixed fee, Sharpe above the pool's in each sub-period | **fail** | before 2023-05-01: 0.63 against 0.70; from 2023-05-01: 1.30 against 0.97 |
| B1 book | An overlay option keeps the re-based book shallower than -25% in every check | **fail** | no option qualified; the shallowest worst case was no overlay, at -39.6% (financial-crisis window) |
| B2 index sleeve | All fees included, the stock-sleeve book's Sharpe is at least the index-sleeve book's and its max DD at most 2 pp deeper | **fail** | Sharpe 0.72 against 1.04; max DD -25.9% against -25.1% |

**Verdict: NOT ADOPTED.** Reported, not gating: net of the fixed fee the selected cell made 12.2% a
year with a Sharpe of 0.60, against 13.3% and 0.77 for the pool (which pays no fixed fee), and it
trailed QQQ (Sharpe 0.98) and SPY (0.90) bought and held.

How to read it:

- **G1** says the selection beat the pool it picks from before the fixed fee.
- **G2** says that edge cannot be told apart, at the 95% level, from random picks made under the same
  sector constraints, with the same trading and the same costs. The spec's power analysis (section
  14) found that G2 misses an edge of 2% to 4% a year about half the time or more, so this failure
  argues against a large edge more than against any edge.
- **G3** fails because the sleeve trailed the pool before May 2023. From May 2023 it led, but that
  period overlaps the years in which the rule was found.
- **B1** fails because a 50% stock sleeve put the re-based book through the HALT line in sample and
  deeper in the dot-com and financial-crisis stress windows.
- **B2** fails because, at this account size, the fixed fee on a concentrated sleeve costs more than
  the selection added over an index sleeve.

## The cells

Stand-alone sleeve, no overlay, net of every cost. "-25% windows" is the share of 12-month windows
that contain a drawdown of 25% or more. Percentiles are against 1,000 random draws; the adjusted
percentile corrects for picking the best of the four selectable cells.

| Cell | Selectable | CAGR | Vol | Sharpe | Max DD | -25% windows | Null percentile (own) | Null percentile (adjusted) | Null median / 95th Sharpe |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SC-8 | yes | 11.6% | 24.6% | 0.57 | -51.7% | 39.5% | 92% | 80% | 0.37 / 0.61 |
| SC-10 | yes | 10.3% | 23.5% | 0.54 | -48.4% | 29.3% | 93% | 82% | 0.34 / 0.56 |
| SQ-8 **(selected)** | yes | 12.2% | 23.8% | 0.60 | -38.2% | 38.7% | 94% | 85% | 0.39 / 0.62 |
| SQ-10 | yes | 7.4% | 22.9% | 0.43 | -38.9% | 41.4% | 72% | 48% | 0.35 / 0.57 |
| GC-8 | control | 14.9% | 24.9% | 0.68 | -45.2% | 35.6% | 98% | 96% | 0.38 / 0.63 |
| GC-10 | control | 13.2% | 23.5% | 0.64 | -45.5% | 30.0% | 99% | 97% | 0.33 / 0.53 |

SQ-8 had the highest net Sharpe. SC-8 was inside the 0.05 tie band, and SQ-8 also had the lower
total cost, so it won either way. The GC controls scored higher percentiles, but they were never
selectable: the user fixed within-sector ranking before the run, and had GC been selectable the
correction would have covered six cells instead of four.

## Baselines

| Book | CAGR | Vol | Sharpe | Max DD |
|---|---:|---:|---:|---:|
| SQ-8, net of every cost | 12.2% | 23.8% | 0.60 | -38.2% |
| SQ-8 before the fixed fee | 18.7% | 23.6% | 0.84 | -35.8% |
| SQ-8 before the fixed fee, doubled variable costs | 17.3% | 23.6% | 0.79 | -36.0% |
| Equal-weight eligible pool (no fixed fee) | 13.3% | 18.5% | 0.77 | -36.3% |
| Equal-weight eligible pool, doubled variable costs | 13.0% | 18.5% | 0.75 | -36.3% |
| QQQ buy and hold | 21.3% | 22.3% | 0.98 | -35.1% |
| SPY buy and hold | 15.6% | 17.8% | 0.90 | -33.7% |

The pool is every eligible name at equal weight, fully rebalanced each quarter. It is what the rule
picks from, and it is not investable at this account size.

## Sub-periods

Sharpe / CAGR, split at 2023-05-01 (the lab found the rule's signal only from May 2023).

| Book | Before 2023-05-01 | From 2023-05-01 |
|---|---:|---:|
| SQ-8, net of every cost | 0.39 / 6.8% | 1.06 / 24.5% |
| SQ-8 before the fixed fee | 0.63 / 13.0% | 1.30 / 31.4% |
| Equal-weight eligible pool | 0.70 / 12.8% | 0.97 / 14.2% |
| QQQ buy and hold | 0.84 / 18.3% | 1.32 / 27.9% |
| SPY buy and hold | 0.72 / 12.7% | 1.39 / 21.8% |
| Current reference book | 1.01 / 12.3% | 1.37 / 19.0% |
| Re-based book, no overlay | 0.51 / 7.0% | 1.14 / 19.1% |
| Re-based book, down-only overlay | 0.47 / 5.6% | 0.97 / 14.8% |
| Re-based book, reference-level overlay | 0.35 / 3.8% | 0.88 / 13.0% |
| Index-sleeve book, no overlay | 0.89 / 12.6% | 1.36 / 20.0% |

## Whole books and the HALT line

The re-based book holds the selected sleeve at 50% of NAV and the core lines, with their trend and
volatility rules unchanged, re-based pro rata to 45%. The HALT line is a 25% drawdown from the
lifetime peak.

**Overlay options** (re-based book with SQ-8):

| Overlay | CAGR | Sharpe | Max DD | Max DD, doubled costs | Stress: 2000-2003 | Stress: 2007-2009 | Stress: 2015-2016 | Worst of the five |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| none | 10.8% | 0.72 | -25.9% | -31.8% | -38.4% | -39.6% | -17.6% | -39.6% |
| down-only | 8.5% | 0.64 | -25.9% | -38.3% | -45.3% | -36.6% | -25.7% | -45.3% |
| reference levels | 6.7% | 0.54 | -27.1% | -42.5% | -39.6% | -32.1% | -24.1% | -42.5% |

No option qualified. In the stress windows the sleeve is a proxy (the equal-weight S&P 500, times
the selected cell's beta on it, 1.02), so they test the book's structure and the overlay, not the
rule's picks.

**Whole books** (full window):

| Book | CAGR | Vol | Sharpe | Max DD | Headroom to HALT | -25% windows | WARN episodes | HALT episodes | Days above R8's volatility line |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Current reference book | 14.5% | 12.7% | 1.13 | -24.3% | 0.7% | 0.0% | 1 | 0 | 0.0% |
| Re-based book, no overlay | 10.8% | 16.1% | 0.72 | -25.9% | -0.9% | 11.0% | 3 | 2 | 2.2% |
| Re-based book, down-only overlay | 8.5% | 14.4% | 0.64 | -25.9% | -0.9% | 0.0% | 1 | 1 | not measured |
| Re-based book, reference-level overlay | 6.7% | 13.9% | 0.54 | -27.1% | -2.1% | 0.0% | 1 | 1 | not measured |
| Index-sleeve book, no overlay | 15.0% | 14.5% | 1.04 | -25.1% | -0.1% | 0.0% | 2 | 1 | not measured |

The current reference book already came within 0.7 pp of the HALT line. The re-based book with the
stock sleeve and no overlay, which is the book the override puts live, crossed it twice in sample
(March 2020 and October 2022).

## Sensitivities (reported only, never used for selection or the gate)

On the selected cell, stand-alone, net of every cost.

| Sensitivity | CAGR | Sharpe | Max DD | Null percentile |
|---|---:|---:|---:|---:|
| Variable costs and the fixed fee doubled | 4.8% | 0.32 | -46.4% | not run |
| No hold buffer | 10.7% | 0.55 | -37.9% | not run |
| The lab's verbatim data layer | 9.4% | 0.50 | -39.3% | not run |
| **AI list added to the universe (hindsight-flattered)** | 22.7% | 0.87 | -40.4% | not run |
| Fixed fee removed | 18.7% | 0.84 | -35.8% | 91% |
| Five times the sealed funding | 17.4% | 0.80 | -36.0% | 92% |
| Catastrophe stops, re-bought after the cool-off | 10.2% | 0.54 | -43.5% | not run |
| Executed five sessions after each rebalance date | 11.6% | 0.58 | -37.5% | not run |
| B2's comparison with an SPY-only index sleeve (whole book) | 12.8% | 1.01 | -23.5% | not run |

- **The AI-list row is flattered by hindsight.** The list of AI-adjacent tickers was assembled in
  2026 around themes that had already boomed, so its higher return is not evidence. Live ranks these
  names anyway (L9 below).
- The overlay sensitivity on the stand-alone sleeve did not apply: the chosen overlay is none.
- With the fixed fee removed or the funding multiplied by five, the cell's own random-null
  percentile stays at 91% to 92%: a larger account would pay much less in fees, but the selection
  would still not clear the 95th percentile.
- The stop version's Sharpe is 0.06 below the headline's, within the 0.10 the spec allows, so the
  spec's pre-committed stop rule applies (L3).

## Diagnostics

- **GC overlap.** On average 72% of SC's picks were also GC's picks (lowest 38% with 8 names, 40%
  with 10).
- **Concentration.** The top contributor, NVDA, made 16% of the selected cell's arithmetic return.
  With its slot held as cash the Sharpe is 0.47, against a random-null median of 0.33 on the same
  statistic (85th percentile).
- **Survivorship.** The price panel tilts toward survivors: an equal weight of the mapped S&P 500
  members made 12.7% a year against 12.4% for RSP (a 0.3 pp gap), and of the mapped Nasdaq-100
  members 16.6% against 15.5% for QQQE (a 1.1 pp gap), both before costs.
- **R8.** Days on which the 63-day realised volatility exceeded R8's line: none for the current
  reference book, 2.2% for the re-based book without an overlay.

## Calendar years

Net of every cost; 2016 starts on 2016-05-20 and 2026 ends on 2026-09-04.

| Year | SQ-8 | Pool | QQQ | SPY | Current reference | Re-based, no overlay | Re-based, down-only | Re-based, reference levels | Index-sleeve book |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2016 | -9.8% | 10.3% | 12.1% | 10.5% | 3.5% | -4.5% | -4.5% | -7.0% | 5.5% |
| 2017 | 38.2% | 22.3% | 32.7% | 21.7% | 16.6% | 22.8% | 22.8% | 22.4% | 21.1% |
| 2018 | 6.5% | -5.4% | -0.1% | -4.6% | -12.9% | -5.7% | -6.1% | -6.4% | -8.9% |
| 2019 | 24.6% | 32.3% | 39.0% | 31.2% | 23.8% | 21.1% | 17.8% | 12.7% | 28.3% |
| 2020 | 1.1% | 23.5% | 48.6% | 18.4% | 42.3% | 14.3% | 14.0% | 12.1% | 37.2% |
| 2021 | 19.4% | 25.7% | 27.4% | 28.7% | 30.8% | 23.6% | 23.0% | 22.6% | 26.7% |
| 2022 | -24.1% | -17.1% | -32.6% | -18.2% | -19.3% | -21.9% | -24.0% | -23.9% | -22.7% |
| 2023 | 11.8% | 18.3% | 54.9% | 26.2% | 37.1% | 21.0% | 14.0% | 10.6% | 35.2% |
| 2024 | 17.7% | 9.7% | 25.6% | 24.9% | 20.5% | 15.0% | 15.1% | 11.3% | 19.9% |
| 2025 | 24.6% | 10.0% | 20.8% | 17.7% | 12.6% | 17.0% | 15.4% | 13.2% | 15.0% |
| 2026 | 31.9% | 15.9% | 17.3% | 13.5% | 9.9% | 19.1% | 10.8% | 11.6% | 12.1% |

## What this page leaves out, and why

- **Trade counts, turnover, fee and cost drag, the fee arithmetic and the fee-feasibility share stay
  operator-only.** The fixed fee per trade is public (`policy/costs.yaml`), so combined with the
  cost figures of the same book these would disclose how much real money funds the account. A test
  (`tests/stocks/test_study_doc.py`) fails if this page ever publishes such a combination, and the
  leak scan runs on it with every other public file.
- **Selections, prices and member lists are not republished** (see [data rights](data-rights.md)).
  The only name on this page is the top contributor of the concentration diagnostic.

## User decision (2026-09-25): adopted anyway as a recorded override

The user chose option 1 of the spec's section 12: **adopt the rule sleeve anyway, as a recorded
override of the gate.** The verdict stands as NOT ADOPTED; the override is a decision taken knowing
it, not a pass.

What the override means:

- **Live runs the selected cell as studied: SQ-8, no overlay.** The sleeve holds 8 names at equal
  weight, 6.25% of NAV each (50% / 8), chosen by the sector score with sector quotas and the hold
  buffer, refreshed at the four quarterly dates. With no overlay, the sleeve stays at full level
  whatever the SPX trend.
- **The sleeve is 50% of NAV and the core is re-based to 45%.** In-reference core base weights are
  scaled pro rata by 0.45 / 0.95; their trend and volatility rules do not change.
- **The AI-adjacent list is ranked mechanically too.** Live ranks those names alongside the index
  members with the same rule and filters, so it runs the universe of the hindsight-flattered `ai_list`
  sensitivity, not the tested one. This is an untested extension (L9). The quarterly report counts how
  many selected names come from that list.
- **What the spec pre-committed for an adoption applies**: no per-name trend or volatility levels
  (the sleeve's level comes from the overlay only, which is none); R4's stock floor becomes the tested
  stop rule (L3); a quarterly rotation may be staged over up to five sessions, with stock legs only in
  US-session slots.
- **The accepted risks are the gate's failures.** The selection's edge is not distinguishable from
  chance at the 95% level (G2); the edge appeared only in the period in which it was discovered (G3);
  a 50% stock sleeve can trip the HALT line, which it did twice in sample and more deeply in the
  stress windows (B1); and after the fixed fee an index sleeve did better on this small book (B2).
- **The rule is frozen for its life.** The four rule modules and the study record are pinned to the
  tag by a test (`tests/stocks/test_frozen_rule.py`); a bug found in them needs a new
  pre-registration, never an in-place edit.
- **Nothing live changes yet.** The adoption record is not loaded by the policy loader, and stock
  lines stay off in every runtime. The re-based core, the reference sleeve section and the switch
  that turns stock lines on arrive together in a separate go-live commit, after the engineering
  (eligibility, costs, market hours, a dedicated stock price-history source) is in place. Each
  quarter's names will be a tagged policy file.

Council latitude on stock names (dropping, swapping or adding a name with evidence) is a separate,
later policy change with its own CHANGELOG entry. It is never covered by this study.

## Live against the study: L1-L14 under the override

The spec numbers the engineering items L1-L14 (to avoid confusion with the risk rules R1-R21). Each
row says what live runs under the override. "As studied" means live uses the studied rule or the same
code; a named divergence is a known difference.

| # | Item | In the study | Live under the override | Status |
|---|---|---|---|---|
| L1 | Sleeve deadband | A name trades back to target when its drift reaches 0.25 of a unit or 2% of NAV; the fixed fee is charged on every drift trade | The same thresholds; no fee-derived threshold, which would disclose the funding | as studied |
| L2 | Trading between rebalances | A drift past the deadband, a level change and the never-borrow trim, decided by `reference/sleeve.py::pending_trades` | The same function decides the rule's own orders; the only live state is the last executed reference level per line | as studied |
| L3 | Catastrophe stops | Not in the headline; the stop sensitivity models them, with the re-buy after the cool-off | Live stop-outs, then the rule re-buys after R4d's cool-off and pays the fee; the stop sensitivity passed the spec's test, so R4's stock floor is the tested stop rule | named divergence |
| L4 | Fee level | The fixed fee per real trade at the sealed funding | Charged at both the virtual and the real level until the first live stock trade shows which applies; the doubled-cost sensitivity covers the difference | named divergence |
| L5 | Volatility scaling and caps | The reference's volatility scaling and cap act on the core only | The same; the engine's whole-book R8 also stays (live is stricter). The single-stock cap (10%) cannot bind on the rule path: a unit of 6.25% plus a 2% drift band stays below it | named divergence (stricter) |
| L6 | Execution | The close of the session after the rebalance date; a five-session lag as a sensitivity | The next US-session slots; R16, R17 and a gap guard can delay a name; tracking error reported quarterly | named divergence |
| L7 | Code | The four rule modules, imported by the study script | The live rank and the reference book import the same modules; `tests/stocks/test_frozen_rule.py` pins their blob hashes to the tag | as studied |
| L8 | The GC cell | A reported control, never selected | Never live | never live |
| L9 | The AI list | A hindsight sensitivity only | Ranked mechanically with the index members, same rule and filters; labelled untested in the public record; the quarterly report counts the selected names it supplies | named divergence (user decision) |
| L10 | Constants | The variants file | `council.stocks.adopted` checks the adoption record and the tagged variants file's SHA-256 and fails closed on any difference; nothing reads git at run time | as studied |
| L11 | Tie-break | `score.ordered`: the variant's score, the global score, the symbol | The same function | as studied |
| L12 | "Held" for the hold buffer | The rule's previous selection | The committed selected names; council decisions and stop-outs do not change it | as studied |
| L13 | Core level steps after the re-base | A level change always trades, whatever its size | The same, through `pending_trades`: a reference level change skips R11's 2% floor | as studied |
| L14 | Fee on core lines | The fixed fee on every core fund trade in every whole book | Charged in the cost budget (R14) and the planner; the net-of-cost gate (R15) on the rule's own orders leaves it out, as today | as studied |

Beyond these, two differences are structural: the gate did not pass, so live carries no claim of
mechanical evidence; and the council, which may later drop, swap or add names, is never backtested.
