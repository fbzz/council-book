# Swing book: pre-registration (DRAFT — not in force, not tagged)

Status: **draft** (work package SW-7). It becomes binding when it is committed and tagged
`swing-prereg-v1` in the go-live commit (the one that sets `invariants.SWING_BOOK_LIVE = True`).
After the tag, any change is a recorded deviation: a new file version, a new tag
(`swing-prereg-v2`, ...) and a CHANGELOG entry saying what changed and why, never an edit in place.

Design: swing-book design rev 2, §8. Policy: `policy/swing.yaml`. Code: `swing/paper.py`,
`swing/metrics.py`, `benchmark/sq8.py`.

## 1. What is being tested

Whether language-model agents that read the news (a Scout proposes, a blind Skeptic on another model
family checks whether the news is already in the price, a bull, a bear and a manager decide, code
enforces the S-rules and a human approves every trade) produce swing trades — US stocks, long or
short (short = 1x stock CFD), held 3 to 15 sessions — with a positive expected R **after a declared
cost of 1.25% of the position per leg**.

It cannot be backtested honestly: the models have read the news of their training period and what
happened next. It is judged only on trades opened after go-live.

## 2. Operating decisions fixed before the first trade (user, 2026-09-28)

- Live directly, without the 6–8 week paper run. Every idea is still paper-tracked (§4), so the
  funnel exists from day one.
- At most 6 new swing trades per 7 days; at most 6 open positions, about 8% of NAV each; idle budget
  stays cash.
- Net reward / risk at entry >= 1.2 after the round-trip cost.
- One swing slot per US session (winter 18:40 UTC); an entry's approval is valid 60 minutes.
- Earnings exits are proposals for the human, never automatic.
- The Skeptic runs on `deepseek-v4.1-flash:cloud`, the Scout's model (user decision 2026-09-29, after
  `glm-5.3-flash:cloud` returned parseable JSON on 1 of 3 real calls). It stays blind by input: it never
  sees the Scout's thesis, only the claim, the catalyst and the fact card.
- Skeptic relaxed (user decision 2026-10-01, after 0 passes in 27 ideas over 6 paper runs):
  prompt `council-skeptic/v3` passes a hard new catalyst that supports the side unless the
  stock-specific reaction is clearly against it (worse than about −0.75 sigma vs its sector) or
  chased; only `priced_in: fully` forces a reject in code (`mostly` + pass no longer becomes wait;
  stale or restated + pass still does); a supported `wait` (priced_in no or partly) is still heard
  by the bull / bear debate and the PM (group `skeptic_wait_debated`), and the PM's 2-of-3 vote and
  every S-rule still apply. The S11 hard chase drop is 4 sigma (was 3); the prior-wait sigma stays 2.
- Day-2 confirmation, early economics, 5 reviews (user decision 2026-10-01, after 7 paper runs, 32
  ideas, 0 entries): a Skeptic `wait` stays pending and code re-proposes it as the live setup
  `day2_confirmation` at the next slot after >= 1 completed session, within the re-proposal limit
  (<= 2 within 3 sessions); the gate requires a completed-close move since the news in the trade's
  direction, and the Skeptic (`council-skeptic/v4`) treats a hard catalyst confirmed by about 0.5 up
  to 2 sigma, not chased, as a pass. Levels whose net reward/risk at the declared 1.25% a leg is
  below 1.2 are dropped at the code gate before any Skeptic call (`S6:net_rr_below_min`); the Scout
  (`council-scout/v3`) is told the implied minimum target. The Skeptic reviews up to 5 ideas a slot
  (12 calls, 450 s). Position size and capital are unchanged (the user's decision is pending).
- The SQ-8 mechanical stock rule is a public PAPER benchmark only; it never trades.

## 3. Primary metric

`r_declared` per closed trade = (exit − entry) × side / planned stop distance, net of the declared
cost (1.25% per leg, plus the cost-route carry for shorts, as % of the position). Published.
The same net of the actual costs (`r_net`) and the slippage against the slot-time reference price
are kept private.

## 4. One paper convention for every group

Every idea — executed, manager-passed, Skeptic-rejected, Skeptic-wait, Skeptic-wait-debated, code-dropped (eligible names
only), paper-only setups, missed entries — is tracked the same way: entry at the slot-time reference
price (the fact card's slot price: the delayed 15-minute Alpaca SIP bar at or just before the
slot on a paper run; the last completed close only as a flagged fallback, `paper_reference_last_close`), exits on completed daily bars at the
idea's stop, target or time stop; a bar touching both books the stop; a gap through the stop books at
that session's open; the declared cost on both legs. Real fills are measured separately as slippage.

## 5. Secondary metrics (§8.2)

Hit rate; expectancy with a 90% percentile bootstrap interval (10,000 resamples, fixed seed); payoff;
swing contribution in bps of NAV; mean difference vs the matched index (side × β60d × sector ETF over
the same window, same declared cost); the book vs SQ-8 paper and vs a 48% SPX hold; the funnel's paper
R per group; exit mix and average days held; behaviour counters (fee legs, S12, S15/S17, refusals by
rule, Skeptic pass rate, canaries).

## 6. Review and pre-declared rules

- **Review** at 30 closed trades or 90 calendar days after go-live, whichever comes first; then every
  further 30 trades.
- **Pause rule**: at the first review with >= 20 closed trades, new swing entries pause if the mean
  `r_declared` <= 0 **or** the swing contribution trails its matched index. Either is enough. The
  user then chooses: stop the swing book; continue unchanged for 30 trades; or a pre-registered
  revision (a new tag). With fewer than 20 closed trades at day 90 the review reports and the rule
  applies at 20.
- **Skeptic test**: once >= 40 ideas have a Skeptic pass or reject and a finished paper outcome: if
  rejected ideas beat passed ones by >= 0.3 R (mean paper `r_declared`), the Skeptic's veto becomes
  advisory and its prompt is revised under a recorded revision; if passed beat rejected by >= 0.3 R,
  that is published as evidence of value. `wait` ideas are reported as their own group.
- **Setup promotion**: a paper-only setup goes live only by a recorded revision after >= 30 paper ideas
  with mean paper `r_declared` > 0 and a bootstrap lower bound > −0.1 R.
- **Stop at once** (independent of the review): the S15 brake twice in 60 days; any core-scoped
  `execution_unknown` caused by a swing leg; a CFD-short outcome the smoke ticket did not predict
  (mirror, fee or carry-sign mismatch, a forced close); two missed canaries in a row.

## 7. Honest power statement

At 30 trades, a mean of +0.3 R with a per-trade s.d. of about 1.2 R has a standard error of about
0.22 R: the review can detect a disaster, not prove an edge. Telling skill from luck at plausible
edges (0.1–0.2 R) needs several hundred trades. The site says so on every swing section.

## 8. What is published

Percent-only (docs/data-rights.md, swing rows): ideas with their stage and drop code, Skeptic
verdicts, PM votes, trades with stop / target distances, R and % net of the declared cost, the
metrics with n and interval, the three benchmark curves and the idea funnel. Never prices, rates,
units, amounts, instrument or position ids, live-layer values, short interest or dollar volume.

## 9. Open before tagging

- Q-S10: the Alpaca free-plan terms (until resolved, Alpaca-derived fields and the reference price
  stay withheld).
- The go-live date and the first slot (filled in at tagging).
