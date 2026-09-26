# Sleeve policy fixture

Test data only (never loaded live). `tests/conftest.py::sleeve_policy_dir` copies every top-level
`policy/*.yaml` into a temporary directory and overlays the files here:

- `universe.yaml`: `policy/universe.yaml` with the core re-base (in-reference weights × 0.45/0.95),
  so the core plus a 0.50 sleeve fits `reference_gross_max`;
- `stock-rank.yaml`: the live-only settings the loader validates;
- `stock-sleeve.yaml`: a synthetic quarter (`TSTA`, `TSTB`, `TSTC_B`, `F` selected; `TSTD`, `TSTE`
  shortlisted; one retired company).

`policy` (the core-only pin) and `sleeve_policy` are the two fixtures tests use. Runtime contexts
(live, dry run, approval) merge a sleeve only once `invariants.STOCK_SLEEVE_LIVE` is True; a test
that needs a context with stock lines monkeypatches that switch.
