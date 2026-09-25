# Changelog

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
