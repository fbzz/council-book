# Stock sleeve: pre-registered study (spec v1)

> **Pre-registered.** This text, the numbers in `policy/variants/stock-sleeve-variants-v1.yaml`, the
> study script `scripts/stock_sleeve_study.py` and the four rule modules it imports were committed,
> tagged `stock-sleeve-spec` and pushed to `origin` before any return was computed. The rule modules
> are the code the live book will run, so the study tests that code, not a copy of it:
>
> - `src/council/stocks/pit.py`: the point-in-time fundamentals and the four features (section 4);
> - `src/council/stocks/score.py`: the global and sector scores and the choice of names (sections 4
>   and 5);
> - `src/council/stocks/sectors.py`: the SIC to Fama-French 12 sector map (section 3);
> - `src/council/reference/sleeve.py`: equal weight, the overlay level, the deadband and the
>   never-borrow rule (sections 6 and 11).
>
> The universe filters of section 3 stay in the study script. The live rank must reproduce them, and
> a test must pin its eligible set to the tagged script's on the same inputs.
>
> Editing any of these seven files after the tag voids the study. The script refuses to run on real
> data when:
>
> - the tag is missing, is not on `origin`, or points elsewhere on `origin`;
> - HEAD is not the tagged commit, or the checkout is not the tagged tree: every tracked file must
>   equal its tagged blob, hashed from the file's own bytes (so git's index flags cannot hide an
>   edit), and no untracked file may sit beside them. This pins every module the script imports and
>   the locked library versions, whether or not a list names them;
> - any of the seven files, or a code or policy file the numbers depend on (the reference-book
>   modules, the policy loader, the reference backtest script, the stock package's init file, the
>   universe, reference, risk and cost policies, `pyproject.toml` and `uv.lock`), differs from its
>   tagged version;
> - the `council` package it imports is not the one in its own checkout (the real run executes from
>   a worktree checked out at the tag, section 16);
> - the input bundle is not byte-for-byte the one frozen in the variants file (sha256 of the
>   bundle, of every lab source file it was built from, and of the study script and the four rule
>   modules);
> - `COUNCIL_STATE_DIR` is set, or `HOME` is not the account's home: a real run uses the default
>   private state folder only, so a copied state folder cannot start a second run;
> - another run holds `results/LOCK` (created exclusively, removed when the run ends);
> - a real run already reached its gate: a run folder holds `gate.json`, which is written before the
>   verdict is shown, or `result.json`. The study runs once, and a verdict in `gate.json` stands even
>   if the reported sections never finished. Every attempt writes to its own folder and nothing is
>   overwritten; the market data are fetched before the folder exists, so a failed fetch is not an
>   attempt.
>
> Only data coverage (row counts, date ranges, symbol coverage) and point-in-time feature values
> (never a return) were inspected beforehand. Sections 14 and 15 report them, with the gate's power
> measured on synthetic data.

## 1. What is fixed and what is tested

**Fixed by the user (not tested here).** The book adds single US stocks as a sleeve of about half of
NAV. The sleeve holds 8 to 10 names at equal weight and is refreshed quarterly. Names come from the
lab's mechanical four-feature fundamentals rule, **ranked within sector**. The user's live universe
is the S&P 500, the Nasdaq-100 and the lab's AI-adjacent list. The book holds real long 1x shares
only: no CFD, no short and no leverage on stocks. A human approves every trade. The council may
drop, swap or add names from the ranked shortlist, with evidence. The reference keeps its core
lines and is re-based to make room.

**What this study decides:**

- which of four mechanical sleeve cells (section 5) becomes the reference sleeve. A ranking over
  the whole universe (GC) is run as a reported control only, because the user fixed within-sector
  ranking;
- whether a market-trend overlay is used;
- whether the result passes the adoption gate (section 12).

**What an ADOPT covers.** The gate is judged on the headline universe only: S&P 500 and
Nasdaq-100 members. The AI-list names cannot be tested fairly (they are a hindsight list, section
3). **User decision (2026-09-25, before this pre-registration was tagged): after an ADOPT the live
rule ranks the AI-list names mechanically alongside the index members.** Live therefore runs the
`ai_list` sensitivity's universe, not the gated one. This is a named divergence (L9): the ADOPT
evidence does not cover it, the `ai_list` row is reported but is flattered by hindsight, and the
public record labels the live sleeve as running an untested universe extension.

It says nothing about the council. The council is never backtested.

## 2. Data (point in time)

Paths are relative to the private lab working copy. The script reads it from the `COUNCIL_LAB_ROOT`
environment variable; the default is the operator's local lab checkout. Every path is listed in the
variants file. Outputs go to `<state dir>/backtests/stock-sleeve/`, never into this repository. The
subfolders are `inputs/` (the point-in-time bundle and its manifest), `coverage.csv`, `power/` and
`results/run-<UTC time>/`.

| Role | Source | File (in the lab) | Rights |
|---|---|---|---|
| Index membership | `index_constitution` 1.0.0: S&P 500 history (883 rows, 857 symbols) and Nasdaq-100 history (313 rows); ticker-change events | `spikes/001-timesfm-finance/.venv/.../index_constitution/_data/history/{sp500,nasdaq100}.pkl`, `.../event/us.pkl` | MIT code; data from Wikipedia (CC BY-SA). Member lists are not republished. |
| Identity | Finetune data contract, keyed by CIK, with one listed share class per issuer, recycled-symbol handling and predecessor CIKs | `finetune/data/processed/contract/{symbol_intervals,issuer_intervals,securities,issuers}.parquet` | Derived from SEC and Alpaca metadata |
| Prices | Alpaca SIP daily bars (open, low, close; the adjusted close includes splits and dividends). 1,173 securities, 2016-01-04 to 2026-09-04 | `finetune/data/processed/contract/bars_daily.parquet` | Personal use. Never published; only percentages are. |
| Fundamentals | SEC XBRL companyfacts, bulk snapshot of 2026-09-06 | `finetune/data/raw/sec/bulk/companyfacts.zip` | Public domain |
| Sector | SEC SIC code per issuer (current), mapped to Fama-French 12 | `issuers.parquet`; the map is `finetune/data/raw/french/Siccodes12.zip`, embedded in `src/council/stocks/sectors.py` and checked by a test | Public (SEC, Ken French library). GICS is not used. |
| Core lines, index ETFs, stress and bias proxies | Tiingo (QQQ, SOXX, SPY, GLD, RSP, QQQE, from 1998-01-02) and Binance (BTC, ETH), fetched at run time through the data layer of the reference study | network | Derived percentages only |
| AI list (sensitivity only) | The 108 AI-adjacent tickers in the lab's config (the lab's evaluation used 104 of them). CIK and SIC come from the lab's EDGAR table. Prices come from the panel, or else from the lab's own file (Tiingo, with a yfinance fallback). | `stock-runner-research/config/universe.yaml`, `data/processed/{edgar_companies,prices}.parquet` | Research only |

The `prepare` step writes every source file's size, modification time and sha256, and the sha256
of the code that built the bundle, into `inputs/manifest.json`. The `freeze` step copies those
hashes and the bundle's own sha256 into `frozen_inputs` in the variants file. `prepare` refuses to
run once the tag exists. The run records a hash of every fetched market series (the crypto lines
included) in its folder before any return is computed, and again in its result.

## 3. Universe at a rebalance date D

The filters apply in this order. The script records how many names survive each step (the
funnel), and writes every member it cannot map, with the reason, into `coverage.csv`.

1. **Member** of the S&P 500 or the Nasdaq-100 at D: opt-in ≤ D < opt-out.
2. **Mapped** to a security in the price panel. The membership symbol, or a symbol it replaced
   through a ticker change after D, must be valid at D in the identity table. An unmapped member is
   `no_identity` (the symbol is not in the identity table) or `identity_later` (its identity
   history starts after D).
3. **Common stock.** The REIT security type and ADRs are excluded.
4. **Priced**: a bar within 7 calendar days up to D.
5. **Listed** at least 290 calendar days (about 200 sessions) before D. A first bar on the panel's
   first session counts as listed earlier. The live cycle needs 200 closes before a line has trend
   facts.
6. **One security per CIK.** Share classes are deduplicated; the higher 63-session median dollar
   volume wins.
7. **Sector known.** The SIC code maps to Fama-French 12. **FF12 "Money" is excluded** (banks,
   insurers, brokers, REITs): their growth and margin lines are not comparable.
8. **Taxonomy `us-gaap`** in companyfacts.
9. **A visible quarter**, meaning it was filed before D. The filing that carries the latest quarter
   must be a domestic 10-Q or 10-K (amendments and transition forms included). This excludes
   20-F, 40-F and 6-K filers, which have no quarterly us-gaap XBRL.
10. **Fresh**: at most 120 days between D and the filing date of the latest visible quarter.
11. **All four features finite.** There is no median fill: a name with a missing feature is not
    ranked.
12. **Revenue floor**: at least 50 million US dollars of revenue, as filed, in each of the four
    compared quarters (L, P, Y, PY below). Pre-revenue names therefore cannot top the growth ranks.
13. **Plausible**: in each of the four quarters, revenue is positive, 0 ≤ gross profit ≤ revenue
    and |operating income| ≤ revenue. Gross profit and operating income must exist in L and Y,
    whose margins the rule compares.

**Fundamentals over the CIK chain.** A security keeps its fundamentals through a holding-company
reorganisation: the features read every CIK the security had up to D (its issuer intervals, which
include predecessor CIKs), first report per period across them. Without this, a reorganised issuer
has no visible history under its new CIK and is sold and bought back for a bookkeeping reason.

**The AI list is not in the headline universe.** It was assembled in 2026 around themes that boomed,
so including it would be hindsight. It appears only in a labelled sensitivity (section 13).

## 4. The rule (ported and cited)

`src/council/stocks/pit.py` is a verbatim port from the lab repository `stock-runner-research` at
commit d569049:

- `src/data/fundamentals.py`, lines 46-602: the point-in-time quarterly table. First report wins; a
  quarter is an 80-100 day duration; Q4 is the fiscal year minus Q1-Q3 from the same 10-K;
  `available_at` is the SEC filed date.
- `src/features/fundamentals.py` (`fundamental_features`).
- `src/baselines/scores.py` (`rank_average`).

The lab's own 33 tests for this code are ported unchanged in
`tests/research/test_stock_sleeve_pit.py` and pass.

The quarters are chosen from the rows visible at D, meaning filed strictly before D:

- **L**: the latest period end.
- **P**: the period end nearest to L minus 91 days.
- **Y**: the period end nearest to L minus 365 days.
- **PY**: the period end nearest to P minus 365 days.

Each match must fall within 20 days. With g(a, b) = a / b − 1 when b > 0, and m(x, rev) = x / rev
when rev > 0:

- f1 revenue growth = g(rev L, rev Y)
- f2 revenue growth acceleration = g(rev L, rev Y) − g(rev P, rev PY)
- f3 gross-margin change = m(gross profit L, rev L) − m(gross profit Y, rev Y)
- f4 operating-margin change = m(operating income L, rev L) − m(operating income Y, rev Y)

The score is the plain mean of the four percentile ranks (pandas `rank(pct=True, method="average")`)
within the ranked set. Higher is better. The universe filter lets no missing value reach the ranks,
so the lab's median fill never applies.

- **Global score**: the rule applied to the whole eligible set at D.
- **Sector score**: the rule applied inside each peer group. A peer group is an FF12 sector. Sectors
  with fewer than 5 eligible names at D are ranked together in one pooled group.

**Three documented changes to the data layer.** The formula itself is untouched. All three changes
are council-book additions at the end of the ported module and are covered by their own tests.

- **(a) First report across tags.** The lab resolver takes the highest-priority revenue (or cost)
  tag that has *any* fact for a quarter. When a later filing re-tags old quarters, as many did under
  the 2018 revenue standard, those quarters are dated at the re-tagging filing. They then look
  unpublished until that filing.
  - Measured on 2018-03-20: with the lab resolver, 198 of 387 non-financial us-gaap members had no
    quarter filed in the previous 120 days. One mega-cap's quarters from late 2016 to late 2018 all
    carried a November 2018 date.
  - The change picks the earliest-filed fact across the lab's own tag list. Ties go to a directly
    reported quarter, then to the lab's priority. With it, 335 names were fresh on that date.
- **(b) One extra cost tag.** The pre-2018 tag `CostOfGoodsSold`, which `CostOfGoodsAndServicesSold`
  replaced, is added as the last gross-profit fallback. Eligible names at the first rebalance rose
  from 140 to 162.
- **(c) Comparable year-ago values.** The lab resolves every quarter on its own, so the two sides of a
  comparison can come from different revenue tags, from different revenue standards (ASC 605 as
  first reported against ASC 606), or from before and after a spin-off. Change (c) keeps each
  quarter's own value (first report, as in (a) and (b)) and takes the year-ago side of each
  comparison **as printed with the compared quarter**:
  - Y is read from L's own filing, with the same concept L's value came from (the comparative
    column of the same 10-Q or 10-K). PY is read from P's own filing the same way.
  - When that filing does not carry it, the latest filing of the same concept filed on or before
    L's (P's) own filing date is used. A derived quarter (Q4 = year minus Q1-Q3) is derived from
    those same filings' figures.
  - A gross profit computed as revenue minus a cost concept is compared as the same difference.
  - Everything used was filed on or before the compared quarter's own filing, which is before D,
    so there is no lookahead. If the concept cannot be traced, the feature is missing and the name
    is not ranked.
  - Measured before the tag (features only, never returns): over the 42 dates, 358 of 9,253
    eligible pairs change revenue growth by more than 5 points, 69 of them in the top or bottom
    decile of the sector score. Tag or standard mixes: HAS on 2016-05-20 (172% becomes 16.5%),
    MSFT on 2018-08-20 (29.0% becomes 17.5%), PM on 2018-08-20 (−60.0% becomes +11.7%). Spin-off
    recasts: BAX, EBAY, DHR, CTXS in 2016-2018. Full retrospective ASC 606: AMD, IPG in 2018.
    Regression tests pin HAS, MSFT and PM on a trimmed public-domain SEC fixture.

The verbatim lab data layer (no changes (a), (b) or (c), no plausibility guards, no CIK chain)
runs as a sensitivity (`lab_resolver`).

## 5. From ranks to names: the variants

| Variant | Score used for the order | Sector constraint | Role |
|---|---|---|---|
| **SC** | sector score | at most 3 names per FF12 sector | selectable |
| **SQ** | sector score | sector quotas proportional to the sector's share of the eligible set (largest remainder, each quota at most min(3, eligible count)) | selectable |
| **GC** | global score | at most 3 names per FF12 sector | reported control, never selected |

Each variant runs with N = 8 and N = 10. That gives **four selectable cells** (SC-8, SC-10, SQ-8
and SQ-10) and two control cells (GC-8 and GC-10). The share of SC's picks that GC also picks is
reported at every date.

- **Order.** Names are sorted best first by the variant's score, then by the global score, then by
  symbol (`score.ordered`, which the live rank calls too).
- **Hold buffer.** A held name that is still eligible stays while it ranks inside the top 2N of the
  order. For SQ, it stays while it ranks inside the top 2 × quota of its own sector, keeping at most
  the quota. Names kept this way count toward the cap or quota. "Held" means the rule's previous
  selection, not the book's positions: live, council drops, swaps and stop-outs do not change it.
- **Cap variants.** Walk the order and take each name unless its sector already has 3 names. Fill
  without the cap only if fewer than N names fit under it.
- **Quota variant.** Fill each sector up to its quota with the sector's best names that are not
  already held.
- **Weights.** Every name gets an equal target of sleeve share / N.

## 6. Rebalance dates and execution

- **Dates.** D is the first trading session on or after each anchor, written month-day:
  03-20, 05-20, 08-20 and 11-20.
  - These follow the 10-K deadline (60 days) and the 10-Q deadline (40 days) for large accelerated
    filers.
  - The first decision is 2016-05-20; the last session is the price panel's last (2026-09-04). That
    gives 42 rebalances.
  - A filing counts as visible only if it was **filed before D**.
- **Timing.** The decision is taken at the close of D and executed at the close of the next session.
  This is the same one-day lag as the reference backtest. A five-session lag runs as a sensitivity.
- **Between rebalances (R11 deadband).** A name trades back to its target when
  |weight − target| ≥ max(0.25 × unit weight, 2% of NAV).
  - In the stand-alone sleeve, the 2% floor is 4% of the sleeve's own capital.
  - An overlay level change forces trades.
  - The same rule applies at D itself. A name the rule keeps is not reset to its equal weight: it
    trades only when its drift reaches the deadband, or when the never-borrow rule below trims it.
    Only added and dropped names are forced to trade, through their level change.
- **A rebalance never borrows.** If the orders would lift the sleeve above its share, every held
  name above its target is trimmed to target first.
- **Delisted names.** The last close is carried forward at zero return. The name is sold at the
  next rebalance, and that sale pays a fee, which is conservative.

## 7. Costs

- **Variable cost**, per side on the traded amount: a 10 bps stock spread floor plus the 10 bps
  slippage buffer from `policy/costs.yaml`, so 20 bps. **The slippage buffer applies to every leg
  in this study**: stocks, the core lines, the index sleeve, the current reference and the ETF
  comparisons. (The published reference backtest charged none on its ETF legs; charging it on only
  one side would tilt every comparison against the sleeve.)
- **Fixed fee.**
  - Every traded leg pays the broker's fixed fee per real trade (`fixed_commission_usd.real`)
    divided by the funded real NAV. In basis points of the leg's real amount, that is
    fee / (|Δw| × real NAV).
  - In the stand-alone sleeve, the fee is expressed on the sleeve's capital, which is half of NAV.
  - The funded NAV is held constant over the window. The public record never states account
    amounts, so it is **sealed**: `private-params.json` in the private study folder holds the fee
    and the NAV with a random salt.
  - The variants file publishes only `private_params_sha256` = sha256(salt ‖ canonical JSON). The
    script refuses a real run when the file does not match that commitment, or when the sealed fee
    differs from `policy/costs.yaml`.
  - **Assumption: one fixed fee per real leg.** The lab's one live stock trade suggests the fee is
    also charged on the agent's virtual book and then mirrored at the copy ratio, which would add
    the copy ratio × the fee per leg. The doubled-cost sensitivity covers any copy ratio up to 1.
  - **The run's outputs are operator-only.** `summary.md` and `result.json` hold percentages only,
    but the fee is public (`policy/costs.yaml`) and a book's fee drag divided by its legs a year is
    the fee over the sealed funding. So the fee drag and the leg count of the same book, published
    together, disclose the funding. The summary does not state the fee per leg; a public write-up
    after the run is a separate document that never shows both numbers for the same book.
- **Whole books.** Core ETF legs pay the same sealed fee per leg in every whole book: the current
  one, the re-based one and the index-sleeve book. The published reference backtest expressed this
  fee on the virtual book's NAV instead. Crypto costs 100 bps per side.
- **Sensitivities.** Both the variable cost and the fixed fee doubled; the fixed fee removed; five
  times the sealed funding. The last two come with their own random null.
- **Not modelled:** dividend withholding tax (adjusted closes reinvest gross dividends), FX and
  taxes.

## 8. Baselines

- **Equal-weight eligible pool.** Every eligible name at D, at equal weight, fully rebalanced at
  every D, paying 20 bps per side and **no fixed fee**. This is the pool the rule picks from. It is
  not investable at this account size.
- **Index-sleeve book** (the counterfactual of check B2). The same re-based book with the sleeve's
  50% held in the index ETFs of the NDX and SPX lines (QQQ and SPY) in the ratio of their base
  weights (0.35 : 0.15), under the same overlay option, the same deadband rule and the same fee per
  leg. This is option 3 of section 12 made concrete. A SPY-only version is reported.
- **QQQ and SPY buy and hold** (reported, not gating). Tiingo adjusted closes, with the listed-ETF
  cost plus one fee leg.
- **Random null: 1,000 draws**, with the seed fixed in the variants file, built with **common
  random numbers**:
  - For each draw index and each D, one random order of the eligible set is drawn and **shared by
    every cell**. So a draw's N = 8 picks are nested in its N = 10 picks, and its SC and SQ picks
    overlap, as the rule cells do.
  - At each D, a cell's draw first keeps, in the draw's order, as many of its own eligible held
    names as the rule cell kept at that D. It then fills the remaining slots in the draw's order,
    under the cell's sector constraint (the same cap, or the same quotas).
  - It uses the same costs and the same simulator. The draws therefore match the rule's turnover
    and fee load; only the choice of names differs.
  - A cell's percentile is the share of its own draws with a lower net Sharpe, with ties counting
    half.
  - **The gate corrects for picking the best of four cells** (a single-step max-T correction on
    percentiles). For each draw index, the highest own-cell percentile among the four selectable
    cells is recorded. The selected cell's own percentile is then ranked against that distribution
    (the adjusted percentile). Percentiles, not raw Sharpes, are compared, because random N = 10
    sleeves have higher Sharpes than N = 8 ones through diversification alone. Both percentiles
    are reported for every cell, the GC controls included.

## 9. Metrics

All figures are net of every cost. Returns are daily, with 252 days a year and a zero risk-free
rate, as in the reference study.

- CAGR, volatility, Sharpe, maximum drawdown and Calmar.
- Turnover per year (sum of |Δw|).
- Fee drag per year (fixed fees) and other cost drag per year.
- Legs per year.
- The share of 12-month windows that contain a −25% drawdown.
- For whole books: headroom to the HALT line, WARN and HALT episodes from the soft-kill drill, and
  the share of days whose 63-day realised volatility exceeds R8's hard line.
- A calendar-year table.
- Two sub-periods, split at 2023-05-01: before, and from that date. The lab found the rule's signal
  only from May 2023.

## 10. Selection rule (frozen)

The four selectable cells are compared stand-alone, without an overlay, at base costs over the full
window. The cell with the **highest net Sharpe** wins. Cells within 0.05 of the best Sharpe tie, and
the one with the lowest total cost drag wins the tie. Every cell is reported, the controls
included, whatever the result.

## 11. Overlay and the whole-book check (frozen)

**Re-based reference:**

- The in-reference core lines keep their V3 trend and volatility rules. Their base weights are
  scaled by 0.45 / 0.95, and the reference gross limit is set to 0.45.
- The selected sleeve sits at 0.50 of NAV. Its level comes from the SPX overlay only (below).
- The reference's volatility scaling applies to the core only, as it would live (section 12).

**Current reference:** the policy as it stands (V3, 0.95 gross). All books use the same window,
calendar, slippage and fee.

**Overlay options** set the sleeve level from the SPX line's trend state. The trend uses SPY closes
with the V3 parameters: 50- and 200-day averages with a 2% band.

| Option | up | mixed | down |
|---|---|---|---|
| none | 1.0 | 1.0 | 1.0 |
| down only | 1.0 | 1.0 | 0.25 |
| reference levels | 1.0 | 0.75 | 0.25 |

**Proxy stress windows.** The window from 2016 contains only two equity bear markets, and the
overlay would be both chosen and checked on them. So each option is also run over three older
windows: 2000-01-03 to 2003-12-31, 2007-01-03 to 2009-12-31 and 2015-01-02 to 2016-12-30.

- The core is the re-based core on the same Tiingo history. A line whose history has not started
  holds nothing (crypto before 2017, semiconductors before mid-2001, gold before late 2004).
- The sleeve is a proxy: RSP's daily return (the equal-weight S&P 500, from its first bar in 2003;
  SPY's before), times the selected cell's beta on that proxy over the study window. Only level
  changes trade it, and each trade pays N fee legs.

The HALT line is 75% of the lifetime peak, a −25% drawdown. The overlay is chosen as follows:

1. An option **qualifies** when its re-based whole book stays shallower than the HALT line in all
   five checks: in sample at base costs, in sample at doubled costs, and in each stress window.
2. Among qualifying options, pick the highest CAGR (in sample, base costs). CAGRs within 0.25 pp
   tie; the higher Sharpe wins, then the fewer legs.
3. If no option qualifies, report the one with the shallowest worst drawdown and fail check B1.

This is the pre-stated consequence of a pass in sample and a failure in stress: that option does
not qualify, and if every option fails somewhere, the answer is option 4 of section 12.

**Reported for every whole book** (current reference, the three re-based options, the index-sleeve
book): CAGR, volatility, Sharpe, maximum drawdown and its headroom to the HALT line; fee and cost
drag; WARN (−20%) and HALT (−25%) episodes from the soft-kill drill; 12-month windows with a −25%
drawdown; days above R8's realised-volatility line; calendar years.

## 12. Adoption gate (frozen)

| Check | Must hold |
|---|---|
| **G1** pool | The selected cell's sleeve, **before the fixed fee**, has net CAGR ≥ the equal-weight pool's **and** net Sharpe ≥ the pool's, at base and at doubled variable costs. (The pool pays no fixed fee either; fees are judged in B2.) |
| **G2** random | The selected cell's adjusted percentile (section 8) is at or above the **95th**. |
| **G3** regimes | The selected cell's Sharpe before the fixed fee is above the pool's in **each** sub-period (before 2023-05-01, and from it). |
| **B1** book | The overlay rule (section 11) qualified an option: in sample at base and doubled costs and in every stress window, the re-based book stays shallower than −25%. |
| **B2** index sleeve | With the chosen overlay, the re-based book with the stock sleeve has a net Sharpe at least as high as the same book with the index sleeve, and a maximum drawdown no more than 2 pp deeper. All fees included. |

**Reported, not gating:** the stand-alone sleeve (net of the fee) against QQQ and SPY buy and hold
(G4 in the first draft), and G1 net of the fee.

**All pass: ADOPT.** The selected cell and overlay become the reference sleeve through a separate,
tagged policy change, for the headline universe only (section 1). That change is pre-committed to:

- **No per-name trend levels.** The sleeve's level comes from the chosen SPX overlay only. The
  live engine does not give stock lines their own trend or volatility levels, since the study
  never tested them. R8 and R9 stay live safety rules on the whole book.
- **Stops.** R4's stock floor becomes the tested stop rule (section 13). If the stop sensitivity's
  net Sharpe is more than 0.10 below the headline cell's, the floor is the 0.35 cap instead
  (catastrophe only).
- **Staging.** A quarterly rotation may be spread over up to five sessions (R14 and R21), which the
  execution-lag sensitivity tests. Stock legs come only from the slots when the US market is open.

**Live against this study, item by item.** These are the engineering design's items R1-R14,
numbered L1-L14 here so they are not confused with the risk rules R1-R21. Each is what live runs
after an ADOPT (pre-committed) or a named divergence between live and this study.

| # | Item | In this study | Live after an ADOPT |
|---|---|---|---|
| L1 | Sleeve deadband | 0.25 × unit and 2% of NAV (R11's values), with the sealed fee on every drift trade, so the fee cost of drift trading is measured, not assumed | The same thresholds. No fee-derived threshold: it would disclose the funding |
| L2 | Trades between rebalances | A drift past the deadband, a level change, and the never-borrow trim, decided by `reference/sleeve.py::pending_trades` | R11 decides reference-origin legs with the same function. The target is the selection × the overlay level; the only live state is the last executed reference level per line, which is the simulator's held level |
| L3 | Catastrophe stops | Not in the headline; the `stock_stops` sensitivity models them, with the re-buy after the cool-off | **Named divergence**: live stop-outs (floor 0.15, cap 0.35), then the rule's re-buy after R4d's cool-off, which pays a fee |
| L4 | Fee level | One fee per real leg at the sealed funding (section 7) | The fee at both the virtual and the real level until the first live stock trade shows which applies; the doubled-cost sensitivity covers the difference |
| L5 | Volatility scaling and caps | The reference's volatility scaling and cap act on the core only | The same: the reference's volatility scale is computed over core lines only, and sleeve units are never volatility-capped. **Named divergence**: the engine's whole-book R8 stays (live is stricter); the quarterly report counts the cycles where it bound. The single-stock cap (0.10) cannot bind on the rule path: a unit of at most 0.0625 plus a 2% drift band stays below it (a test pins this) |
| L6 | Execution | The close of the session after D; a five-session lag as a sensitivity | **Named divergence**: the next US-session slots; R16, R17 and a gap guard can delay a name. Reported quarterly as tracking error |
| L7 | Code | The four rule modules, imported by the study script | The live rank and the reference book import the same modules; after an ADOPT, a test pins their blob hashes to the tag |
| L8 | The GC cell | A reported control, never selected; G2's null takes the best of the four selectable cells | Never live |
| L9 | The AI list | A hindsight sensitivity only (`ai_list`) | **Named divergence (user decision)**: ranked mechanically with the index members, same rule and filters; the public record labels it untested; the quarterly report shows how many selected names come from the AI list |
| L10 | Constants | This variants file | Live policy values are validated in code against the adopted values and the tagged variants file's sha256, never by reading git at run time |
| L11 | Tie-break | `score.ordered`: the variant's score, the global score, the symbol | The same function |
| L12 | "Held" for the hold buffer | The rule's previous selection | The rule's previous selection (the committed selected names); council anchors and stop-outs do not change it |
| L13 | Core level steps after the re-base | A level change always trades, on core lines too, whatever its size | The same, through `pending_trades`: a reference-origin level change skips R11's 2% NAV floor |
| L14 | Fee on core legs | The sealed fee on every core ETF leg in every whole book | Charged in the cost budget (R14) and the planner. The net-of-cost gate (R15) on reference legs leaves it out, as today: at today's variable costs that gate cannot bind on them |

The rest of the live work (onboarding, the fee in the engine, sessions, two-phase removal and so on)
is separate.

**Any fail: NOT ADOPTED.** Nothing in `policy/` changes. The user receives:

- the gate table with its numbers;
- all six cells with their own and adjusted random-null percentiles;
- the baselines and the sub-periods;
- the whole books, the stress windows and the HALT line;
- the sensitivities and diagnostics.

The options put to the user are:

1. Adopt the rule sleeve anyway, as a recorded override of this gate.
2. Keep a stock sleeve picked by the council from the ranked shortlist, without any claim of
   mechanical evidence.
3. Add no single stocks, so the equity share stays in the index lines.
4. If only B1 failed, try a smaller sleeve share or another overlay, pre-registered anew.
5. If G1 to G3 pass and B2 fails on fees, try more funding, fewer legs or a slower refresh,
   pre-registered anew.

The user decides.

## 13. Sensitivities and diagnostics (reported, never used for selection or the gate)

The script runs exactly the lists in the variants file and fails on an unknown or missing name.

**Sensitivities** (on the selected cell):

- `costs_x2`: variable costs and the fixed fee doubled.
- `no_hold_buffer`: the buffer is N.
- `lab_resolver`: the verbatim lab data layer.
- `ai_list`: the AI list added to the universe (hindsight list).
- `overlay_standalone`: the chosen overlay on the stand-alone sleeve.
- `zero_fixed_fee` and `nav_x5`: the fixed fee removed, and five times the sealed funding, each
  with its own 1,000-draw null. Together with G1 they separate a lack of skill from fee drag.
- `stock_stops`: a stop on every opened position at distance max(0.15, 3 × daily sigma × √5),
  capped at 0.35, with sigma from the 63 sessions up to the entry. It triggers on the daily low
  and fills at the lower of the open and the stop. The name may re-enter after 3 sessions (R4d).
- `execution_lag`: each rebalance executed five sessions after D instead of one.
- `index_sleeve_spy_only`: the B2 counterfactual with SPY only.

**Diagnostics:**

- `gc_overlap`: the share of SC's picks that GC also picks, per date and N.
- `concentration`: each name's contribution to the selected cell's return, the top contributor's
  share, and the Sharpe with the top contributor's slot held as cash, against the same statistic in
  the random null.
- `fee_arithmetic`: legs per year × the fee per leg ÷ the sleeve's capital, against the measured
  fee drag (section 14).
- `fee_feasibility`: the share of selected name-quarters whose share price exceeds one name's real
  amount (these fail if fractional copies do not work).
- `survivorship`: equal weight of all mapped S&P 500 members against RSP, and of all mapped
  Nasdaq-100 members against QQQE, both without costs. The gap sizes the panel's survivor tilt.
- `r8_realised_vol`: the share of days above R8's line, for the current and the re-based book.

## 14. Limits and power stated before the result

**Power of the gate** (measured before the tag, `power`, synthetic data only). Synthetic worlds with
the eligible set's sector mix (240 members, the fixture's filters applied), stock returns = beta (0.8 to 1.2)
× a synthetic market + idiosyncratic noise (20% to 35% a year), fundamentals independent of returns,
the real window length (42 rebalances), the sealed funding, and 100 null draws per cell. A planted
edge adds α a year to the top 10% of the eligible set by sector score, from each D to the next.
Pass rates over 24 worlds per α:

| α a year | G2 passes | G1, G2 and G3 pass | B1 passes | B2 passes | Whole gate passes | Median adjusted percentile |
|---:|---:|---:|---:|---:|---:|---:|
| 0% | 8% | 4% | 0% | 4% | 0% | 65% |
| 2% | 25% | 25% | 0% | 13% | 0% | 83% |
| 4% | 50% | 42% | 0% | 25% | 0% | 95% |
| 6% | 75% | 71% | 0% | 38% | 0% | 100% |

(The G1-G3 column counts worlds where G1, G2 and G3 all pass. With 24 worlds, each rate has a
standard error of up to 10 points; G2's 8% at α = 0 is consistent with its nominal 5%.)

- **Minimum detectable effect.** G2 reaches 75% power at a planted edge of 6% a year on the top
  decile, and 80% only near 6% to 7%. A FAIL of G2 therefore argues against an edge of that size;
  it says little about an edge of 2% to 4%, which G2 misses half the time or more. A PASS at α = 0
  happened in 2 of 24 worlds.
- **B1 passed in no synthetic world**, so the whole gate never passed. The cause is the synthetic
  market, not the sleeve: its random-walk core spends years in drawdown. In two worlds inspected,
  the current reference itself fell 23% and 29%, the re-based books 31% to 40% in sample and 43%
  to 74% at doubled costs, where years of costs compound inside one drawdown. The whole-gate column is
  therefore not a power estimate for real markets. What it does show: B1 binds whenever the core's
  own drawdown is near the line, and the real current reference reached −23.0% in the reference
  variants study.
- **Selection favours N = 8.** In 20 to 21 of 24 worlds at every α, the selected cell was an
  N = 8 cell: noisier cells win a best-Sharpe contest more often. The adjusted percentile of G2
  corrects for this; G1 and G3 do not.
- **Winner's curse.** The table includes it: each synthetic world applies the selection rule
  before the gate, as the real run does.
- **G3's second sub-period** covers about 3.3 years, so its Sharpe has a standard error near 0.55.
  G3 is a coarse consistency check, not a test.
- **B1 and B2 figures** depend on how the synthetic market and ETFs are drawn; the G2 and G1-G3
  figures depend only on the stocks' idiosyncratic risk and the planted edge.

**Fee arithmetic.** A leg costs one fee ÷ the sleeve's capital, that is 1 / (N × K) of the sleeve,
where K is the real amount per name in fees. With L legs a year, the fee drag is L / (N × K) of the
sleeve a year:

| N | Legs a year | K = 50 | K = 100 | K = 250 | K = 500 |
|---:|---:|---:|---:|---:|---:|
| 8 | 30 | 7.5% | 3.8% | 1.5% | 0.8% |
| 8 | 46 | 11.5% | 5.8% | 2.3% | 1.2% |
| 10 | 30 | 6.0% | 3.0% | 1.2% | 0.6% |
| 10 | 46 | 9.2% | 4.6% | 1.8% | 0.9% |

The synthetic worlds trade about 46 legs a year (median of the selected cells): the quarterly
refresh plus deadband trades when single names drift. The sealed funding places the book
in this table. Near the low end of K, the sleeve pays several percent a year in fees that an index
sleeve pays only a fraction of. That is why G1 and G3 are judged before the fee, why the fee-free and
five-times-funding runs are reported with their nulls, and why B2 carries the fee. A pass of G1 to
G3 with a failure of B2 reads as "skill, eaten by fees at this size".

**Other limits:**

- **Membership.** The history is Wikipedia-derived, as of the package's data (latest change
  2026-08-05). It may contain errors, and later changes are missing.
- **Price panel.** It was built from yearly top-liquidity sets of 10-K filers. Between 90% and 98%
  of members map to a price series (section 15).
  - Most unmapped members are second share classes (one class per CIK by design), foreign filers
    (excluded anyway), REITs and additions made after the panel ends.
  - Early in the window, about 30 are companies acquired, merged or renamed whose earlier history
    the vendor does not serve (for example AET, BCR, CA, CSRA, DPS, HAR, LLTC, TSS, TYC; CBS before
    it became PSKY, Cabot before CTRA, Delphi before APTV, Praxair before LIN). The identity table
    starts those securities at the change, so they cannot be recovered from it.
  - The rule and every stock baseline draw from the same mapped pool, so those comparisons are
    internally fair. The pool still tilts toward survivors in the early years; the survivorship
    diagnostic sizes the tilt against RSP and QQQE. B2 compares two books that both face real
    ETFs in their core, and the stock side carries the tilt.
- **Delistings.** A delisted name is valued at its last close, held as cash. Losses after a
  delisting, such as in a bankruptcy, are missed.
- **Sectors.** SIC codes are current, not point in time.
- **Taxonomy and derivation.** The taxonomy (us-gaap or ifrs-full) is detected per issuer from the
  2026 companyfacts snapshot, like the SIC code, so eligibility at D can depend on filings made after
  D. In change (c), which formula derives a quarter (the quarter ends that lie inside a cumulative
  period) is read from all filings, so it can also depend on later filings. The values themselves are
  always filed on or before the compared quarter's own filing, so no later value enters a feature.
- **Filing times.** Fundamentals carry the filing date without a time, so a filing dated D is
  ignored until D + 1, which is conservative. Values are first-reported, and the year-ago side as
  printed with the compared quarter (change (c)). Acquisitions inflate revenue growth.
- **The pool is narrow.** Requiring all four features removes 38% to 55% of the fresh
  non-financial members (39% at the last date). Most of them have no gross-profit line: most utilities, energy producers
  and telecoms, and many service firms. Some have no operating-income line. The early years lose
  more, because older filings use tags outside the lab's list. The eligible set holds 166-200 names
  up to mid-2019 and 217-246 names after.
- **In-sample.** The lab's evidence for the rule is from 2023 to 2026. Its snapshots start in 2017,
  and before May 2023 the rule sat at the base rate. That evidence came from a different universe
  (AI names) and a peer-relative label. The from-2023-05-01 sub-period therefore overlaps the
  rule's discovery period.
- **Stress proxy.** A beta-scaled equal-weight index is not the rule's sleeve: it has the market
  risk of the sleeve, not its concentration. The stress windows test the overlay and the book's
  structure, not the rule.
- **Not modelled in the headline** (sensitivities cover the first three): catastrophe stops and
  earnings gaps; the staging of a rotation over several cycles (R14 and R21); a whole-book
  volatility cap (R8, R9) acting on the sleeve; market hours; council deviations; mirror rounding.

## 15. Coverage measured before this pre-registration (counts only)

Eligibility funnel at selected dates, from `coverage.csv`:

| D | Members | Mapped | Common | Not financial | Fresh | All four features | Revenue floor | Eligible (plausible) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2016-05-20 | 529 | 478 | 449 | 380 | 375 | 170 | 170 | 166 |
| 2018-03-20 | 525 | 489 | 457 | 383 | 335 | 204 | 203 | 199 |
| 2019-05-20 | 522 | 496 | 465 | 392 | 387 | 202 | 201 | 200 |
| 2023-05-22 | 522 | 509 | 481 | 412 | 410 | 250 | 249 | 242 |
| 2026-08-20 | 518 | 500 | 474 | 402 | 399 | 245 | 245 | 238 |

- Across the 42 rebalances, 398 distinct securities are eligible at least once. 83 eligible
  name-dates read their fundamentals over a CIK chain.
- Unmapped members, by reason: 51 on 2016-05-20 (41 `no_identity`, 10 `identity_later`), 26 on
  2019-05-20 (21, 5), 13 on 2023-05-22 (11, 2) and 18 on 2026-08-20 (16, 2).
- The sector mix on 2026-08-20: BusEq 77, Shops 31, Manuf 30, Other 29, Hlth 27, NoDur 20,
  Chems 13, Utils 5, Durbl 3, Enrgy 3.
- In the AI sensitivity, the AI list adds 20 eligible names at the first rebalance and 41 at the
  last.

## 16. How to run

The pre-registration is done in this order. Nothing before `run` computes a return on real data.

    uv run --with pyarrow python scripts/stock_sleeve_study.py prepare     # the bundle; refused after the tag
    uv run python scripts/stock_sleeve_study.py coverage                   # counts only
    uv run python scripts/stock_sleeve_study.py power                      # synthetic only
    uv run python scripts/stock_sleeve_study.py freeze                     # hashes into the variants file
    git pull --ff-only origin main                                         # before staging; never rebase after the tag
    git add docs/stock-sleeve-spec.md policy/variants/stock-sleeve-variants-v1.yaml \
        scripts/stock_sleeve_study.py src/council/stocks src/council/reference/sleeve.py \
        tests/research tests/reference/test_sleeve.py tests/fixtures/sec tests/fixtures/make_sec_fixture.py
    git commit -m "Pre-register the stock-sleeve study"
    git tag -a stock-sleeve-spec -m "Stock-sleeve study: spec, rule modules and inputs frozen before the run"
    git push origin stock-sleeve-spec                                      # the tag first: the run checks it on origin
    git push origin main                                                   # if rejected: git pull --no-rebase origin main, push again
    git worktree add ../council-book-stock-sleeve-spec stock-sleeve-spec
    cd ../council-book-stock-sleeve-spec
    uv run python scripts/stock_sleeve_study.py run --synthetic            # smoke test, the worktree's environment
    COUNCIL_MODE=dry_run uv run python scripts/stock_sleeve_study.py run

The publisher pushes cycle commits to `origin/main` from its own clone, so the push of `main` can be
rejected. Only the tag push is required before the run. A rejected `main` push is resolved with a
merge (`git pull --no-rebase origin main`), never a rebase, which would leave the tagged commit off
`main`'s history.

`seal` (the private cost parameters) was run once and is refused after the tag. The real run always
executes from the worktree at the tag, so edits that land on `main` afterwards cannot reach it:
`uv run` inside the worktree builds the worktree's own environment from the tagged `uv.lock`, and
the script refuses to run when the `council` package it imports belongs to another checkout, when
HEAD is not the tagged commit, or when any tracked file differs from the tag. The private study
folder is outside both checkouts and shared, so the worktree reads the frozen bundle and the sealed
parameters. A run killed from outside leaves `results/LOCK` behind; the operator removes it only
after checking that no run folder holds `gate.json`. `run --synthetic` exercises the whole pipeline on a
seeded fixture that uses no real data. On synthetic data of the real shape (about 530 members, 42
rebalances, a 1,173-security panel, 1,000 random draws, every sensitivity, diagnostic and stress
window), a full run took about 15 minutes on one core, most of it in the three random nulls; the
real run adds the market-data fetch.

## 17. Review notes

A quantitative red team reviewed the first draft before the tag. Every blocker and major point was
fixed; where this spec departs from the suggested fix, the reason is given.

- **Mixed revenue concepts and standards (blocker).** Fixed as change (c) (section 4), with the
  plausibility guards, the verbatim lab layer kept as a sensitivity, and regression tests for HAS
  on 2016-05-20 and MSFT on 2018-08-20 (and PM). One departure: when the compared quarter's own
  filing does not carry the year-ago value, the fallback is the latest filing of the same concept
  up to that quarter's filing date, not the first report before D. A derived Q4 needs the prior
  year's Q1-Q3; under a full retrospective adoption the first reports are on the old standard
  while the new 10-K's year is on the new one, so the first report would build a mixed Q4. The
  measured effect is larger than the red team's count (358 pairs above 5 points, not 41) because
  change (c) also removes spin-off distortions: the comparative column is recast for discontinued
  operations.
- **The tag froze code, not data (major).** Fixed: the bundle's, sources' and code's sha256 in the
  variants file (`freeze`), `prepare` and `seal` refused after the tag, the tag checked on
  `origin`, one completed run at most, each attempt in its own folder. The market series fetched
  at run time (Tiingo, Binance) are not frozen: Tiingo re-adjusts past closes after every
  dividend, so a frozen file would not match the vendor, and the reference study used the same
  path. Their hashes are recorded in the result.
- **GC contradicted the user's decision (major).** Fixed: GC is a reported control; the null and
  the selection use the four selectable cells; the SC/GC overlap is reported per date.
- **G2 over-corrected, power unknown (major).** Fixed with common random numbers. Departure: the
  null takes the maximum of per-cell percentiles, not of raw Sharpes, because random N = 10 sleeves
  have higher Sharpes than N = 8 ones and would otherwise dominate the maximum. The power table is
  in section 14.
- **Fees make G1 and G4 fail by construction (major).** Fixed with the zero-fee and five-times-
  funding runs and their nulls, the fee drag in the gate table, and the fee arithmetic in section 14.
  Beyond the suggested fix, G1 and G3 compare the sleeve with the pool before the fixed fee (the
  pool pays none), so they test selection skill, and G4 is replaced by B2, as suggested. The fee is
  judged where it matters, against the real alternative.
- **No comparison with the status quo or the real alternative (major).** Fixed as B2. The index
  sleeve uses the NDX and SPX lines' ETFs in their base-weight ratio, because that is where the
  equity share would stay (option 3); the SPY-only version is reported.
- **Thin kill-line analysis (major).** Fixed: headroom and WARN/HALT counts against the current
  reference; B1 at doubled costs; three proxy stress windows. The pre-stated consequence is part of
  the overlay rule: an option that crosses −25% in any window does not qualify. On the whole-book
  volatility cap: the study keeps the reference's volatility scaling on the core only, which is
  what the live book will do (the sleeve's level comes from the overlay only, section 12); R8 and
  R9 remain safety rules, and the share of days above R8's line is reported.
- **Live universe differs from the tested one (major).** Fixed in sections 1 and 12: an ADOPT
  covers the headline universe only.
- **Live mechanics not modelled or pre-committed (major).** Fixed: the overlay-only rule, the stop
  floor and the staging are pre-committed in section 12; the stop and execution-lag sensitivities
  are added.
- **Survivorship size unknown (minor).** The diagnostic against RSP and QQQE and the per-date
  unmapped names with reasons are added. Filling the missing ticker changes from the identity
  table was tried and recovers none: on 11 sampled dates, no unmapped member (CBS/PSKY, CTRA, APTV,
  LIN and the others) has an earlier symbol or a same-CIK security valid at D in that table.
- **Holding-company reorganisations (minor).** Fixed: fundamentals over the CIK chain.
- **Uneven slippage (minor).** Fixed: slippage on every leg (section 7).
- **Fee feasibility (minor).** The assumption is stated in section 7 and the feasibility share is
  reported.
- **Frozen-dependency trap (minor).** The policy loader (and the synthetic-data module) are frozen
  dependencies; the run should follow the tag at once, else it runs from a worktree at the tag.
- **Spec and YAML disagreed; baselines skipped silently (minor).** Fixed: one list of sensitivities
  and diagnostics, run exactly and checked; the AI-list count is stated as 108 listed, 104 used by
  the lab; every required market series must be present or the run fails.
- **No concentration diagnostic (minor).** Added (section 13).

**Second review (before the tag).** An adversarial review of the move of the rule into `src/`
re-ran the synthetic comparisons (byte-identical outputs before and after) and found the gaps
below. All were fixed before the tag.

- **The design's live items were not in the spec (blocker, process).** Fixed: L1-L14 in section 12.
- **A real run did not have to use the tagged tree (medium).** Only the listed files were diffed, so
  a later commit to an imported module that no list named (the package init files, the reference
  report module, the data layer) passed every check. Fixed: HEAD must be the tagged commit and every
  tracked file must equal its tagged blob; `pyproject.toml` and `uv.lock` are frozen dependencies.
- **The summary disclosed the sealed funding (medium).** It printed the fee per leg as a share of
  the sleeve. Fixed: the line is gone, and section 7 says the outputs are operator-only and why.
- **Index flags hid edits (low).** `git diff` trusts `assume-unchanged`. Fixed: files are hashed
  from their bytes.
- **Holes in the once-only rule (low).** The verdict was printed before any file marked the run;
  concurrent runs, an overridden state folder and a failed fetch were not handled. Fixed: `gate.json`
  before the verdict, an exclusive lock, the default state folder only, the fetch before the folder.
- **The push order could split the tag from `main` (low).** Fixed in section 16.
- **Taxonomy and one derivation choice use later filings (low).** Stated in section 14.
- **The FF12 map sat in the study script (low).** Moved to `src/council/stocks/sectors.py`, a rule
  module; the universe filters stay in the script, and the header says how the live rank matches
  them.
- **The deadband at D was unstated (low).** Stated in section 6, with a test.
- Also fixed: the recorded market hashes now include the crypto series.
