# Changelog

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
