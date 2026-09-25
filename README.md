# council-book

**A council of LLM agents proposes trades for a small real-money leveraged portfolio every four hours.
Code enforces the risk limits, a human approves every order, and every decision is sealed publicly
before it can be executed.**

> Status: **AWAITING ACCOUNT** — the system is being built in public. Nothing trades yet.

## What this is
- An experiment: can a council of language-model agents, bounded by deterministic code, run an
  aggressive multi-asset book (US stocks and ETFs, BTC/ETH, index, commodity and FX CFDs, with
  leverage and shorts) without fooling itself?
- A public record: every cycle's evidence, every agent's card, the debate, the portfolio manager's
  decision, the risk engine's clips, the costs, and the human's approve/reject — all in `journal/`.

## What this is not
- Not investment advice, not a signal service, not a fund. See [DISCLAIMER.md](DISCLAIMER.md).
- Not evidence that LLMs can trade. The honest prior, from the research that preceded this repo, is
  that the council adds about zero over a mechanical reference book. This repo measures it forward.

## How a cycle works
1. **Data steward (code)** builds a percentage-only fact pack from completed bars. Every fact carries
   the time it became available; nothing later than the cycle start is admissible.
2. **Reference book (code)**: a mechanical trend × volatility-target book. It is the default
   position, the fallback, and the benchmark the council is scored against.
3. **Analysts (LLM)**: news, SEC filings, macro and sector analysts write *evidence cards* — claims
   that must cite evidence IDs from the pack. Event, volatility and cost officers are code.
4. **Debate (LLM)**: a bull opens, a bear rebuts the bull's specific claims, the bull answers.
5. **Portfolio manager (LLM, 3 independent replicates)** tilts the reference book inside bands that
   code enforces. The medoid replicate is used — a real decision, never an average.
6. **Auditor and risk officer (code)**: uncited or self-contradicting changes are reverted; hard
   limits (gross ≤ 2.0×, kill switch at −25%, stop budget, costs, carry, deadband, minimum holds)
   are enforced in code, not in prompts.
7. **Seal**: a hash commitment of the full cycle is pushed here before any order can be approved;
   the cycle itself is revealed once the decision is final.
8. **Human gate**: a person approves every order in a separate terminal. Nothing trades on its own.

## Rules the code enforces
See [`policy/risk.yaml`](policy/risk.yaml) — every number the risk engine uses, versioned and hashed
into each cycle. Changing it is a tagged policy change recorded in [CHANGELOG.md](CHANGELOG.md).

## What counts as evidence
Only forward, sealed cycles. Language models have read the history we could backtest on, so no
council backtest is ever shown. The mechanical reference book's backtest is labelled as in-sample.

## What this can never prove
A small edge. The standard error of a Sharpe ratio is about 1/√years: telling an information ratio of
0.5 from zero at two standard errors takes about 16 years of data. The record can show rule-keeping,
costs, drawdown behaviour and how often the council disagrees with the reference.

## Layout
`policy/` frozen numbers · `prompts/` versioned prompts (hashed) · `src/council/` code ·
`journal/` the public record (written only by the publisher) · `site/` the static page builder ·
`docs/` architecture, risk policy, data rights, runbook.

## License
Code: [Apache-2.0](LICENSE). Journal, docs and site text: [CC BY 4.0](LICENSE-CONTENT)
(excluding third-party material).
