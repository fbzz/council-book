# Changelog

## site v3 (2026-09-25)
- The Portfolio page opens with a broker-style holdings list, percent only: asset (neutral monogram
  tile, ticker, name, session), 1-day move, position (long / short, leverage, real / CFD), P/L % since
  open, weight with a tick at the reference, and the reference weight. Held lines sort by weight;
  flat lines fold under "Not held". Asset-class filter chips work without script (radio inputs and
  CSS). Before go-live it shows the latest run's target book. A sealed proposal is announced without
  its content until the decision.
- A run page is a transcript of every agent in execution order: status, call log (prompt, tokens,
  time, error code), what it saw, the full argument with each claim's evidence resolved to its value,
  and what happened to it (conceded, contested, set aside by the manager, used). Failed calls say
  what went wrong and what the system did instead. A facts table closes the page.
- New Agents pages: every agent's job, model, call record, usable-reply rate and history.
- Dark by default. Interface styles adapted from OpenSourceUI (MIT) and icons from Lucide (ISC), in
  plain CSS and inline SVG; see `THIRD_PARTY_NOTICES.md`. The last script (the stale badge) is gone:
  the CSP is now `script-src 'none'`. Not a policy change.
- Review fixes. Phone: holdings are two-line rows (tile, ticker and name; weight with its bar, then
  the day's move and P/L), the filter chips scroll in one row, the kill switch moves under the list;
  run-page facts, call log, orders and fills restack as cards; no page scrolls sideways at 390 px
  (the agent-header status overflowed by 17 px). A target book has no P/L column and a
  "Target, last day" tile marked hypothetical; a live book gains an open-P/L tile. Weights have one
  fixed decimal and the bar scale is stated. Every failed model call is listed in an amber alert
  with what the run did instead; a timed-out attempt is explained by its call, not as unreadable.
  Advocates are judged by outcome ("did what it asked"), not only by the side the manager named;
  a bare claim number several advocates used is shown once as unclear, never as a firm fate.
  Every order says why (council change, bought up to the reference, back to the reference).
  Identical manager attempts fold under the used one; the code officers share one compact card.
  Agent pages end each run with the outcome chain and list failed calls with links; element ids
  are unique on every page. The macro analyst has its own colour (lime); non-agent sections are
  neutral.
- Public record: a line id may be one character or carry "_" (BRK_B, V); the facts table honours
  a fact's `publishable=False` for every source; a floored cost quote that used a broker what-if is
  labelled `costs:whatif` (withheld), not policy; bare price levels are scrubbed from model text.

## public record — additive fields (2026-09-25)
- Cycles publish the whole debate argument (up to about 1,500 characters; it was cut at 600), the
  macro analyst's output (`macro`), an evidence table of the pack the agents saw (`facts`, values
  only where `docs/data-rights.md` allows), each line's 1-day move (`reference[line].day_change_pct`)
  and a fixed error code per model call (`calls[].error_kind`, never the error text).
- The book describes each line: name, asset class, session, the settlement and leverage of its
  largest open position, the P/L % since open and the 1-day move.
- Every new field is optional and left out while empty, so a document sealed before this change
  re-serialises to its exact sealed bytes and still verifies. Not a policy change.
- A council cancelled by the cycle's time budget keeps the calls that ran.

## model — policy change (2026-09-25)
- Ollama Cloud retired `deepseek-v4-flash` (version 0731) on 2026-09-25; every call returned HTTP 410
  and the outage guard correctly fell back to the reference. The council now runs on its official
  successor `deepseek-v4.1-flash:cloud` (same family, `think:false`, JSON mode). Replicate agreement
  and parse rates are re-measured from scratch; nothing measured on v4 is carried over.

## reference v2 — policy change (2026-09-25)
- The reference-spec-v1 backtest showed 21-25 trend flips per line per year (764% turnover, 2.1%/yr
  cost drag, 59% mean gross). Seven variants and a selection rule were pre-registered (tag
  `reference-variants-spec`) and run: **V3** was selected — a 2% hysteresis band around each moving
  average and a "mixed" level of 0.75. In-sample: CAGR 14.1%, vol 12.1%, max drawdown −23.0%, 253%
  turnover. Variants with more volatility breached the −25% kill line. See `docs/reference-variants.md`.
- One trend implementation (`reference.signals.trend_states`) now serves both the live cycle and the
  backtest.

## council-spec-v1 (pending tag) — policy change
- **Policy v2** (`policy/`): exposure lines with a signal history and ordered vehicle candidates
  (UCITS/ETC real first, then 1× CFD); a composition-based reference book (≈1× gross, never levered,
  never short); soft kill switch (WARN −20%, HALT −25% from the lifetime peak); catastrophe stop-loss
  on every open; the PM may deviate on at most three lines per cycle; the net-of-cost gate amortises
  moves toward the reference over a 90-day hold (120 days for crypto) and council deviations over
  20 days (60 for crypto); crypto is reference-only in v1.
- **Prompts v1** (`prompts/`, hashed in `prompts/manifest.json`): news, macro, bull (opening and
  rebuttal), bear, portfolio manager, single-agent control; a shared desk brief. No performance or
  backtest claims (enforced by `tests/unit/test_prompt_lint.py`).
- Hard-coded invariants (`src/council/invariants.py`): gross ≤ 2.0×, halt at −25% from the lifetime
  peak, a stop-loss on every open, human approval for every order, never the main account.
