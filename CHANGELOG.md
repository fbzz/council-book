# Changelog

## transparency v2, stage 2: public-domain news for the news role, the broker feed switch, input capture wired — policy change (2026-09-26)
- **Policy change**: `policy/council.yaml` gains `news:` (`max_items: 40`, `lookback_h: 48`,
  `quotas: {broker_feed: 25, sec: 8, fed_board: 5, bls: 2, bea: 2, treasury: 2, eia: 2}`,
  `broker_feed: true`). The policy hash changes. What the agents read changes (the user's decisions of
  2026-09-26):
  - **Wider (public-domain news):** every cycle, in rehearsal and live, the news role reads
    public-domain items (Federal Reserve Board, BLS, BEA, TreasuryDirect, EIA, and SEC 8-K / 6-K
    metadata for the held and shortlisted stock lines, which exist only once
    `invariants.STOCK_SLEEVE_LIVE` flips). Before, it read the broker feed only, i.e. nothing until the
    token: from 0 items to up to 21. Items are cleaned and leak-scanned at fetch time, a failing or slow
    source is the flag `news_source_error:<source>:<type>` (30 s budget), and news flags stay out of the
    fact pack (a skew drop depends on an item dated after the slot).
  - **Narrower (quotas):** each source keeps at most its quota of the newest items available before the
    slot, then at most 40 in all; unused quota is not reassigned. The broker feed alone now gives at most
    25 items (was 40). Quotas apply after the time filter, so nothing at or after the slot takes a place
    (lookahead test).
  - **The broker feed switch (eToro Licensed Content):** the news role reads the feed whenever an Agent
    Portfolio is connected, as before (the feed serves the operator's personal use with their own
    account). New: `invariants.BROKER_FEED_ENABLED = True` is the code ceiling and `news.broker_feed:
    false` turns the feed off; off means zero feed requests, the flag `news_broker_feed:off`, and
    earnings from the SEC estimate alone. Feed text is never published (ids, counts, times, instruments
    and the agents' own paraphrase only) and its private copies are purged within 7 days
    (`invariants.LICENSED_RETENTION_DAYS`). A 401/403 from the feed is `news_source_error:broker_feed:auth`
    and the news role still runs on the public items. `N:` ids are now an HMAC of the post id under the
    private install key (`state_dir/keys/install.key`), no longer a plain sha256 (no `N:` id had been
    published).
  - **Stricter (T-D17):** a `news_material` card whose cited news items are all SEC filing items is kept
    but never qualifying (note `filing_metadata_only`): the model sees the form and item codes, never the
    filing's content, so 8-K metadata alone can never unlock a cut.
  - **No call without news:** with no admissible news item the news role is recorded `skipped`
    (`no_news_items`) and makes no model call. No prompt changed.
- Private capture wired (T1, T1v): every cycle records each model call's exact input before it is sent
  (`state_dir/calls/`, broker-licensed texts apart in `state_dir/licensed/calls/`, 0600 files in 0700
  folders), and the council keeps the inputs of calls that ran before a timeout. The ledger's
  `extras.news_fetch` records, privately, each source's counts and each public item's link and times
  (broker items are only counted). The first cycle of each UTC day purges licensed copies that would pass
  7 days before the next daily run (`purge_error:*` on failure). CLI: `council inputs <cycle> [--html]`,
  `council inputs verify|prune` and `council purge-licensed` are registered; each refuses outside the
  operator's interactive terminal (always under `CLAUDECODE=1`).
  The reading list in `council inputs` prints each public-domain item's link (from `extras.news_fetch`;
  a broker item never has one). The capture is written as soon as the council returns, so a later
  failure in the cycle does not lose what the agents saw.
- Publication safety (T0 handoffs): the cycle passes the install key, so the published material-change
  fingerprint is an HMAC (a key that cannot be loaded gives a one-cycle random key and
  `install_key_error:<type>`). The final leak scan before every publish now checks the pack's broker feed
  texts and the private canaries (`COUNCIL_LEAK_CANARIES`, and the NAV and mirror figures of at least
  10,000; smaller figures would match years, slot times and digests, so they stay with the redaction
  layer). Still open (M5-N): a planner skip `below_broker_minimum` is published as written.
- Docs: `docs/data-rights.md` rows for each public-domain source (fields, attribution, archived licence
  hash), the broker feed row, the evidence table and the Licences clause; `docs/architecture.md` News and
  Transparency sections; `docs/news-sources.md` status.

## transparency v2, stage 1: publication safety, private input capture, public-domain news parsers (2026-09-26)
- No policy file changes, no prompt change and no change to what any model reads: a golden test pins
  every desk, news list, debate transcript, instruction tail and every call's system and user message
  byte for byte against the code before this change (ten fixture packs, every call of a stub
  council run on each).
- Public record (T0): risk-engine hold reasons go through one closed table
  (`publish/trace_rules.py`). Every R11 variant (deadband, reference rule, size floor) publishes as
  the bare `LINE: R11`, so a size-floor hold, which bounds the NAV, reads like a deadband hold; an
  R15 hold keeps its SR_be only when the line's cost came from the policy floors and its history from
  Tiingo / Binance, otherwise `LINE: R15`; `R15_fee` and `R14_fee` never carry a number; an unknown
  note publishes as its bare code (or `held`). The R15 check value follows the same rule. News rows
  and events are labelled by their own source (SEC-derived earnings are `sec`, no longer
  `broker_feed`); `P:` public-domain news ids are accepted as evidence (`public_news` refs with their
  publisher), and a `P:` id whose item is not public-domain is dropped and counted
  (`news_licence_mismatch:<n>`). The private flag `size_floor_binding:*` is never published. The
  material-change fingerprint can be keyed by a private install key (`state_dir/keys/install.key`,
  0600); the cycle does not pass it yet. Known open channel (M5-N, before token day): a planner skip
  (`below_broker_minimum`) is still published as written; a strict xfail test tracks it.
- Private (T1, T1v): every model call's exact input is recorded from structured sections before it
  is sent (`state_dir/calls/`, 0600 files in 0700 folders, never inside the repository), with every
  raw reply, the correction turn and salted commitments (32 random bytes per call and section).
  Broker-licensed texts are held apart in `state_dir/licensed/calls/`; `council purge-licensed`
  (operator only; at most 7 days) filters model output against them, then deletes them. Viewer:
  `council inputs <cycle> [--html]` (operator only). None of this is wired into the cycle or the CLI
  yet.
- News parsers (T3a): Federal Reserve Board, BLS, BEA, TreasuryDirect auctions, EIA and SEC 8-K / 6-K
  metadata (`data/gov_news.py`, `stocks/sec_news.py`) produce `P:` items, cleaned and leak-scanned at
  fetch time, with per-source timeouts, a 30 s budget and SEC requests through the shared limiter;
  licence pages archived under `tests/fixtures/news/licences/`, register in `docs/news-sources.md`.
  Not wired into any cycle.

## stock-sleeve follow-ups: earnings windows in the pack, unchecked lines, corporate actions at runtime, `keys store-read` (2026-09-26)
- No policy file changes and no stock line anywhere: `invariants.STOCK_SLEEVE_LIVE` stays False, so
  the stock-only items below have no live effect until the go-live commit. The core-only book is
  unchanged (each item says why).
- Fact pack (resolves the WP-H known limit): an event also stays in the pack while its R16 no-add
  window contains the slot, besides the [slot - 24 h, slot + 7 d] range. A confirmed earnings report
  stays until the later of 30 h after it and its reaction bar (a report after Friday's close, until
  Monday evening's bar); an estimated date from 24 h before the 5th US trading day before it to the
  end of its window, even while the date itself lies beyond the 7-day horizon. An event whose
  schedule became known only after the slot is still dropped. Stricter for stock lines only (R16 now
  sees the whole window WP-H specified); core-only: macro windows end 2 h after the event, inside the
  24 h look-back, so the pack's events are unchanged.
- Stricter (stock-only): every context `build_context` makes (live, dry-run and stub) whose policy
  has a stock line that was never eligibility-checked (a `--no-eligibility` rank stamps it null)
  carries the satellite-scoped blocker `stock_eligibility_unchecked`: R20 holds the stock sleeve,
  the core runs. The blocker string names no line; `council stocks status`, `onboard` and `doctor` name them.
- Stricter (stock-only): corporate actions at runtime (design §3.6). The cycle start checks the broker
  snapshot: a position no line owns that no open leg of ours created raises
  `satellite:corporate_action_pending`, a position on a retired vehicle
  `satellite:retired_line_held:<line>`, a check that fails `satellite:corporate_action_check_failed`.
  Each holds only the stock sleeve and sends the operator an URGENT alert naming the instrument and
  `council stocks adopt` (private; if the notifier's leak scan refuses it, a fixed text goes instead).
  Only when the policy has a stock sleeve.
- Looser (stock-only, design §17.2 #10): the executor's post-execution reconcile and the watch's
  reconcile of held orders no longer block the whole book on a pending corporate action. The position
  is taken out of the unknown positions and the missing stops and recorded as a reason; the next
  cycle start holds the stock sleeve. A credited line's position without a stop is the warning
  `credited_no_sl:<line>`. Every other unknown position or missing stop still blocks, and a
  core-only book keeps today's rule (an unknown position blocks).
- Changed (stock-only): the watch records a vanished stock position as a stop hit (R4d cool-off)
  only when its last observed bid was at or below its stop, or above it by at most 2 x the line's
  4-hour sigma (the daily sigma x sqrt(4 / 6.5), stored privately by each cycle); otherwise an URGENT
  `vanished_not_stop:<line>` and no cool-off. Missing data counts as a stop hit. The watch now keeps
  each position's stop rate and last bid in its private state (a state written before reads as
  before). Core lines: unchanged, a vanished position is a stop hit.
- Fixed: `council keys store-read` crashed with a TypeError (the CLI called the keychain helper
  without its keychain argument); it stores the app key and the READ token in the login keychain
  again. A CLI test now covers `store-read` and `store-write` (no token on a command line; the WRITE
  item only in the write keychain).

## `council stocks` commands, the stock eligibility gate and corporate actions (WP-D) (2026-09-26)
- No policy file changes and no stock line anywhere: `invariants.STOCK_SLEEVE_LIVE` stays False, so
  nothing below changes a live cycle. The commands read the COMMITTED policy (a snapshot of the
  commit under the private state directory), never write into `policy/`, never commit, tag or push,
  and reach the broker only through the READ client (eligibility, rates, the portfolio read). None
  of them imports the broker writer.
- `council stocks rank`: on a rule anchor date (the first US session on or after 03-20, 05-20,
  08-20 and 11-20; another date only with `--allow-off-anchor`, a named divergence), the adopted
  SQ-8 rule over the S&P 500 and Nasdaq-100 members (the MediaWiki revision as of the rank date)
  plus the AI-adjacent list (L9), with SEC identities and fundamentals and Alpaca price facts. "Held"
  for the hold buffer is the committed sleeve's `selected` names, matched by CIK. One eligibility
  request checks the selected, shortlisted and 15 reserve names; a failing name is replaced by the
  next eligible name of its sector in the rule's order (a live-versus-study divergence, counted in
  public, reasoned in private). The proposal is validated with the committed policy (plus an
  optional `--policy-overlay` of go-live drafts such as the re-based universe) through every
  `Universe` validator, the invariants and the adoption checks, in a temporary copy; an invalid
  proposal exits non-zero and leaves only a `.rejected` copy. Everything is written under the
  private state directory (`stocks/proposals/<quarter>/`): the sleeve file, `stock-rank.yaml` and
  the AI list when the committed policy has none yet, the public ranking document, a CHANGELOG
  snippet, the private ranked list and a summary; the command prints the copy, commit and tag
  commands for the human. History is prefetched for new names; a name without enough history is
  flagged, never replaced. `--no-eligibility` leaves the lines unchecked, and `onboard`, `status`
  and `doctor` report them as lines live runs must refuse.
- Two-phase retirement: a held company the rank drops becomes `retiring` (target 0) and keeps its
  line id; it moves to the append-only retired registry (keyed by CIK) only when it is flat in a
  fresh READ snapshot and no in-flight decision touches it. A renamed company is re-keyed by CIK
  with its old id kept as an alias; a registry company that comes back drops its registry row; a
  new company may take a registry ticker but never the id of a line still held.
- `council stocks onboard` (after the commit and the tag): records committed renames as explicit
  aliases, resolves every stock vehicle into the instrument map and re-runs the broker gate
  (closing only for retiring lines). `council stocks adopt <instrument id>`: a spin-off or
  stock-for-stock credit becomes a `retiring` line marked `credited: corporate_action`; a ticker
  rename keeps the line id for the quarter with the new broker symbol; a cash takeover retires the
  line, held at 0 and reported `untradable`. Each is a proposed sleeve-file edit, validated like the
  rank's. `council stocks prune` and `council stocks status` (tag state, roles, unchecked lines,
  retiring flatness, corporate actions, the Alpaca budget, council anchors, the real-account fee
  drag); `council doctor --live-read` runs the gate on a few stock names.
- The stock gate fails closed: exactly one eligibility row for the exact symbol; open, close and
  partial close allowed, each an explicit true (an absent close permission, which the core parser
  reads as allowed, refuses the name); a real, long, unlevered, non-potential config that allows
  setting and editing a stop-loss, with stop-loss bounds that admit every distance from the stock
  floor to the stop cap; `requiresW8Ben` false or absent (any other value refuses the name and the
  user is asked); orders by units allowed and a unit (share) instrument; fractional shares as the
  broker names them (an absent or unknown quantity type counts as whole shares), or a whole-share
  price of at most half of one unit's notional; a quote. Reasons are codes, never numbers.
- Stricter (all instruments): the instrument resolver maps a symbol only through exactly one
  eligibility row; two or more rows leave it unresolved and reported as ambiguous. Eligibility rows
  now keep `requiresW8Ben`, `allowedOrderQuantityType` and `tradeUnitType`, plus a fail-closed
  re-read of the stop-loss fields for the stock gate; the core lines' parsing is unchanged.
- The instrument map accepts explicit rename aliases (an optional key; older files read as
  before): the new symbol shares the instrument, the old one stays readable for ledger rows, and an
  inferred rename still raises.
- Corporate-action checks for the runtime (not wired into the cycle, the watch or reconcile yet):
  an unknown position no leg of ours opened raises one satellite-scoped blocker,
  `corporate_action_pending`, and an URGENT alert naming the command; a position on a retired
  vehicle raises `retired_line_held:<line>`; a vanished stock position is a stop hit when its last
  bid was at or below its stop (a gap through it), or above it by at most 2 × the 4-hour sigma, or
  the history says the stop closed it, else `vanished_not_stop` with no cool-off; a credited
  position without a stop is a reconcile warning, `credited_no_sl:<line>`; and at reconcile a
  pending corporate action (an unknown position no leg of ours opened) becomes the satellite-scoped
  `corporate_action_pending` instead of a whole-book `blocked` decision.
- The ranking document is percent-only and SEC-derived: per sector the top five names with the four
  features, the two score percentiles and their roles, the funnel, the exclusions by reason, and the
  sources with the MediaWiki CC BY-SA attribution. It never shows a revenue level, a price, an
  amount, a CIK, a broker symbol, an instrument id or a broker reason, and it passes
  `assert_public_safe` and the leak scan with the private values as canaries.
- Looser than today (stock lines only, design §17.2 #3 and #4; no live effect before the go-live
  switch): R11 for reference-origin legs orders a stock line whose rule target is 0 flat whatever
  its drift, so shares credited by a corporate action, or what a partial stop leaves, are sold in
  full instead of being stranded below the 2% drift threshold (a retiring line could otherwise
  never be pruned). The frozen study rule is unchanged: in the study that state cannot arise, and a
  test pins that the live decision equals the frozen one on every state the study can reach. Once
  the watch and the executor adopt it (WP-F), a pending corporate action at reconcile holds only the
  stock sleeve (§17.2 #10).

## earnings and events (WP-H) — policy change (2026-09-26)
- **Policy change**: `policy/risk.yaml` `event_block` gains `earnings_before_h: 24`,
  `earnings_after_h: 30` and `earnings_estimate_window_days: 5`. The policy hash changes. No stock
  line is added anywhere and `invariants.STOCK_SLEEVE_LIVE` stays False, so nothing below has a live
  effect until the go-live commit. The core-only book is unchanged: no core line is a stock,
  earnings events exist only for stock lines, and the calendar asks for them only when the policy
  has stock lines.
- Stricter (stock-only): R16 counts stock lines among the macro-sensitive classes, so a scheduled
  FOMC/CPI/NFP/PCE window blocks adds on them as it does on the core.
- Stricter (stock-only, new): earnings windows, per symbol. An `earnings` event blocks adds only on
  the stock line it names (never market-wide, never a core line), from 24 h before the report until
  the later of 30 h after it and the moment the first completed daily bar after it is usable (a
  report after Friday's close blocks until Monday's bar). An ESTIMATED date blocks from 24 h before
  the start of the 5th US trading day before it until the same end for a report at the close of the
  5th trading day after it. Like every R16 window it blocks increases of any origin (council adds,
  swaps-in and the rule's own buys) and never forces a sale.
- Earnings events (`council.stocks.earnings`, wired through the calendar) for every stock line, at
  most 25 a cycle (selected, retiring, then shortlist): the latest SEC 8-K Item 2.02 release accepted
  by the slot is a confirmed report (`sec_8k`; a filer that never files Item 2.02 releases through
  its 10-Q/10-K, `sec_periodic`); the next report is estimated at the release a year earlier + 364
  days, or on a 91-day cadence without one (`sec_estimate`); once the broker news feed exists, its
  `earningsDate` for the line replaces the estimate (`etoro_feed`). A release accepted after the
  slot is invisible to that cycle. A feed date stamped at midnight (UTC or New York) is a date, not
  a report time: 08:00 New York for a report before the open, else 16:00. New quality flags:
  `earnings_unknown`, `earnings_overdue`,
  `earnings_failed`, `earnings_time_budget`, `earnings_skipped`,
  `earnings_unavailable:no_sec_user_agent`, `earnings_feed_failed`, `calendar:earnings_error`; a
  failure there never costs the macro calendar or the cycle.
- The broker news feed is requested once per slot and shared by the news role and the earnings
  override (unchanged in effect: the cycle reads it once).
- Looser than today: nothing.
- Known limit, pending a fact-pack change: the pack admits events whose time lies in
  [slot - 24 h, slot + 7 d], which cuts an estimated window short from 24 h after its date and a
  confirmed report's window 24 h after it.

## live data path (WP-G) (2026-09-26)
- No policy file changes and no stock line anywhere: `invariants.STOCK_SLEEVE_LIVE` stays False, so
  the stock-only items below have no live effect until the go-live commit. The core-only changes
  listed below apply from this change.
- Stock history source: Alpaca market data (free plan, personal use), for stock lines only, whatever
  a line's policy signal source says; Tiingo stays the source of the core lines and is never called
  for a stock. The keys are the Keychain items `council-book.alpaca-key-id` and
  `council-book.alpaca-secret`, read only when the policy has stock lines (the environment overrides
  work in stub mode only); without them every stock line is no_data (the satellite is held) and the
  core is unaffected. An unexpected failure of the stock history or the fundamentals source is a
  flag (`history_failed:<line>`, `fundamentals_error:<type>`), never a failed cycle. The public
  record may show derived percentages only (`docs/data-rights.md` gains the row); until the publish
  layer lists the source, facts from it are withheld (`unknown_source`).
- Core-only book, intentional changes:
  - Changed: daily history is cached per trading day (the day of the newest bar the source's
    availability rule allows) instead of per slot, so each symbol is fetched once a day instead of
    six times. The newest bar a cycle sees is the same (the availability rule never admitted a newer
    one); the requested window now starts 900 days before that trading day instead of 900 days
    before the slot. An answer that does not reach that day yet (a late publication) is used and
    asked for again at the next slot.
  - Changed: a Tiingo 429 is no longer retried (it was, up to three times, honouring Retry-After), and
    a Tiingo 5xx is retried once (it was three times). A 429 trips a breaker that stops every Tiingo
    call for an hour (recorded in the private state directory); the lines not fetched use their last
    copy while it is still fresh under R18's daily-bar limit, else they are frozen no_data. A request
    budget (requests per hour and per day, distinct symbols per month, reserved before each request)
    and a five-minute history time budget bound every cycle. An empty answer keeps the still-fresh
    copy too. New quality flags: `history_rate_limited`, `history_breaker`, `history_budget`,
    `history_time_budget`, `history_cached`.
  - Stricter: the material-change rule (MC) is per line. A discretionary change needs new material
    evidence on its own line (trend, data-freeze status, qualifying cards naming it, its events, its
    fundamentals) or market-wide (kill state, market-wide events, qualifying cards naming no line). A
    session opening or closing is no longer material evidence, and new evidence on one line no longer
    licenses a discretionary move on another. Evidence is consumed per line when a proposal issues,
    and only by a line that could act on it in that cycle (admitted: usable fresh data and an open
    session, and not held by a blocker); new daily bars land at the 02:40 slot, so a crypto proposal
    issued then no longer uses up the equity lines' new evidence before London opens (with the old
    single fingerprint, only the session opening re-armed it). The stored fingerprint becomes a
    per-line map; a ledger that holds only the old single fingerprint is read once through it.
  - Unchanged in effect: the covariance treats a variance or pair with fewer than 60 common rows as
    missing (variance from sigma_ann, correlation 1); every core line has years of history.
  - The plan reads eligibility and rates only for the vehicles of the current lines and the
    instruments of held positions, not for every instrument the append-only map ever resolved.
- Stock-only rules (inactive until go-live):
  - Looser (design §17.2 #9): R18 is per sleeve. A frozen share above the limit among the core's
    reference lines holds every line, as before; among the satellite's, it holds only the satellite.
  - Looser (design §17.2 #8): R9's book-ratio proxy leaves the satellite out; each stock line keeps its
    own instrument breaker.
  - A retiring stock line is reduce-only whatever its band, and a data freeze (its own or the
    satellite's) keeps it reduce-only instead of holding it; a closed session, a blocker or a core
    freeze still holds it.
  - Returns are aligned on the core lines' calendar: a short-history stock never shortens the core's
    covariance window.
  - Stock lines get fundamentals facts `F:<line>:<field>` (kind `fundamental`: revenue growth, its
    acceleration, gross and operating margin changes, filing age, sector, and the rank's scores when
    the rank left them), computed daily by the live rank's own code on SEC filings dated before the
    slot's date and available from the SEC acceptance time of the filing. The public record withholds
    their values (`not_publishable`).

## costs, trade size and the reference trading rule (WP-E) — policy change (2026-09-26)
- **Policy change**: `policy/costs.yaml` gains `per_side_bps.stock_real: 10` (real US shares),
  `fixed_commission_charged_on: [virtual, mirror]`, `min_trade: {copy_min_real_usd: 1.0,
  copy_min_multiple: 3}` and `assumed: {virtual_nav_usd: 10000, mirror_ratio: 0.1}`;
  `policy/risk.yaml` gains `net_of_cost_gate.reference_hold_days.stock: 91` and
  `proposal.max_legs_total: 16`. The policy hash changes. No stock line is added anywhere, and
  `invariants.STOCK_SLEEVE_LIVE` stays False: the stock-specific rules below have no live effect
  until the go-live commit; the core changes listed below apply from this change.
- The fixed fee per real non-crypto trade (`fixed_commission_usd.real`) is now priced, as a
  private per-cycle share of NAV (1e4 × fee × Σ 1/NAV over the charged levels). It is never
  published, never a fact and never in a prompt. `council account set-mirror` (operator only) stores the mirror ratio privately in the
  state directory; while it is missing the cycle uses the assumed ratio and publishes the flag
  `mirror_ratio_missing`; `council doctor` reports it.
- Every leg records its origin: reference (the move goes toward the mechanical rule's target) or
  discretionary; legs recorded before this change count as discretionary. Filled reference legs
  record the reference level they traded toward (the held level the rule compares with; a stop-loss
  hit or a kill-switch flatten resets it to 0; lines without one migrate once from the current
  book).
- Core-only book, intentional changes:
  - Stricter: R14's cycle budget and the 30-day budget charge the fixed fee on every real UCITS/ETC
    leg (NDX, SEMIS, SPX, GOLD), so a build from a flat book spreads over more cycles.
  - Stricter: a risk-increasing discretionary leg's R15 adds the fee paid twice as a share of the
    traded notional, so small council adds on real UCITS lines fail more often. Reference legs keep
    today's R15.
  - Unchanged on purpose: the fee never holds a risk-reducing leg. R15 prices a risk-reducing leg
    on its variable costs, as today; in R14 the fees of risk-reducing legs count against the
    budgets (so they squeeze risk-increasing legs, which are held first), but a risk-reducing leg is
    held only when the variable costs of the risk-reducing legs alone exceed the budget (for the
    30-day budget: the trailing variable costs, fees aside), which is today's rule.
  - Stricter: a hard floor of 3 × the copy minimum in real dollars on every open and partial close
    (`below_real_minimum` in the planner; R11 in the engine; for discretionary legs it binds only
    on a very small account, below R11's 2% floor).
  - Stricter: the kill switch's WARN and HALT use the worse of the virtual drawdown and the
    drawdown of the real-adjusted equity (virtual equity less the real account's cumulative extra
    fee drag) from its own lifetime peak, which the cycle stores privately. It can trip earlier,
    never later, and years of accumulated drag never become a permanent drawdown at a new high.
  - Looser: moves toward the reference follow the studied rule (`reference.sleeve.pending_trades`)
    instead of R11's deadband: a changed reference level always trades, with no 2% NAV floor and no
    minimum level step (so a re-based 0.25 SEMIS step, or a crypto 0.25 step, now executes); a drift
    trades at max(0.25 × unit, crypto 0.5 × unit, 2% NAV). Only the copy floor and the broker
    minimum remain.
  - Looser: risk-reducing reference legs are exempt from R14's per-cycle cap and from R21's count;
    `max_legs: 8` now counts risk-increasing and discretionary legs, and a new hard
    `max_legs_total: 16` bounds every plan (planner included).
  - Looser: R13's trailing turnover and R14's 30-day budget count only discretionary legs from the
    ledger (reference legs executed from now on no longer use them up; legacy legs still count).
  - Changed order (neither looser nor stricter): R14 and R21 hold lines risk-increasing first, then
    discretionary before reference, then the highest SR_be and the largest move. R21 used to hold
    the smallest moves first.
  - Public record: an R14 check with a fee-bearing leg in its total publishes the code `R14_fee`
    instead of a value (and R15 publishes `R15_fee` when a risk-increasing discretionary
    fee-bearing leg is priced); the R15 hold reason of such a leg has no number. Public leg and plan costs stay
    variable-only; the fees are private plan fields. R14's cycle value leaves out exempt legs.
- Stock-only rules (inactive until go-live): stock cost quotes are real, long and 1× only, with the
  `stock_real` floor; a stock line closing to zero skips R11's level step and size floor; sleeve
  lines get the rule's never-borrow trim; the planner skips a stock open whose price has gapped
  more than 2.5 daily σ from the pack's last close (`gap_guard`). The reference backtest charges
  `stock_real` plus the fixed fee, and `live_commission_nav` matches its per-leg fee to the live
  scalar.

## SEC data layer and the live rank engine (WP-C) (2026-09-26)
- No policy file changes and no stock line anywhere; nothing here runs in a cycle. (This entry was
  added afterwards: WP-C landed in the same commit as WP-B...H without one.)
- `council.stocks.sec`: an SEC EDGAR client (ticker to CIK, filer submissions, XBRL companyfacts; US
  public domain). The user agent (Keychain item `council-book.sec-user-agent`, override
  `COUNCIL_SEC_USER_AGENT`) is required: missing or malformed, the client raises before any request,
  and the value travels only in the request header, never in a log line, a repr or an error. Fair
  access: a token bucket paces every attempt, retries included, at no more than 7 requests a second
  with no burst (SEC's published limit is 10); a faster rate is refused. Retries honour
  Retry-After; a filer without XBRL facts is a normal empty answer. Documents are trimmed to what the
  frozen rule module reads (a test pins the rule's output as unchanged) and cached as gzip JSON in
  the private state directory with a TTL (tickers 24 h, submissions and companyfacts 20 h). The rank
  always forces a refresh of companyfacts, so a stale copy is never ranked.
- `council.stocks.universe`: the S&P 500 and Nasdaq-100 members from Wikipedia's constituents tables
  through the MediaWiki API (the current page, or the revision as of a date for a historical rank;
  CC BY-SA: the lists are used, never republished), plus the AI-adjacent list (spec L9); the optional
  `index_constitution` cross-check; one line id per ticker (`BRK.B`, `BF-B`, `BRK/B` become
  `BRK_B`); one company per CIK (the higher 63-session median dollar volume wins, as in the study);
  SEC SIC to Fama-French 12 through the frozen sector map with no overrides (the rank excludes
  Money); 20-F/40-F filers flagged as foreign. Price facts come from the history source; the module
  fetches none.
- `council.stocks.rank`: a pure function (no network, files or clock) that reproduces the study's
  universe filters step by step on filings available strictly before the rank date, then scores and
  selects with the frozen rule modules, which it imports and never copies (a test monkeypatches
  `score.select_rule`; another checks that no stock module holds a copy). Its constants come from the
  adoption record (SQ-8). Live-only additions, none of which changes the selection: an 8-name
  shortlist chosen by the same rule, the full order for eligibility replacements, a reason for each
  excluded name, and a membership source older than 120 days or dated after the rank date is
  refused. Parity tests pin the eligible set, the funnel and the selections to the tagged study script
  on its seeded fixture, and on the study's frozen input bundle when that is on the machine.
- `council.data.cache` gains the `json.gz` format and a forced refresh that never reads the entry;
  `council.data.credentials` gains the SEC user agent.

## stock-sleeve study result and recorded override (WP-I) — policy change (2026-09-26)
- **Policy change (a record only)**: new `policy/variants/stock-sleeve-adopted.yaml`. It sits in
  `variants/`, which `Policy.load` never reads and `policy_sha` does not hash, so no live cycle,
  number or hash changes. Stock lines stay off in every runtime (`invariants.STOCK_SLEEVE_LIVE` is
  False) until the go-live commit, which brings the re-based core and the reference sleeve section.
- The pre-registered stock-sleeve study (tag `stock-sleeve-spec`) ran once, from a checkout of the
  tag. Verdict **NOT ADOPTED**: G1 passed; G2 (adjusted random-null percentile 85%, needs 95%), G3,
  B1 and B2 failed. The selection rule picked SQ-8 (sector score, sector quotas, 8 names); no overlay
  option qualified. Results, percent-only: `docs/stock-sleeve-study.md`.
- **User decision (2026-09-25): adopted anyway as a recorded override.** Live will run SQ-8 with no
  overlay, the sleeve at 50% of NAV and the in-reference core re-based pro rata to 45% (× 0.45 / 0.95),
  with the AI-adjacent list ranked mechanically too: an untested, hindsight-flattered extension,
  named divergence L9 and labelled so in the public record. The record holds the decision, the gate
  verdicts, the cell, overlay, N and shares, the run id, the SHA-256 of the run's result and gate
  files, the spec tag and the status of L1-L14.
- `council.stocks.adopted` loads the record, checks every value against its own constants and the
  tagged variants file's SHA-256, and fails closed on any difference; it also provides the checks the
  policy loader will apply to `reference.yaml` `sleeve:` and `stock-rank.yaml` `rule:` at go-live.
- `tests/stocks/test_frozen_rule.py` pins the git blob hashes of the four rule modules, the stock
  package's init file, the study script, the spec and the variants file to the tag. Any edit fails CI;
  a bug in them needs a new pre-registration.
- `tests/stocks/test_study_doc.py` runs the leak scan on the study page and fails if the page ever
  publishes numbers that together would disclose the account's funding (trade counts, turnover, fee
  or cost drag, per-trade shares).
- Looser than today: nothing; no live rule changes. The risks the override accepts are the gate's
  failures, listed in the study page.

## site v4 — "council chamber" (2026-09-26)
- The Portfolio page opens with the book and the cast together: the title, and beside it (under it
  below 1100 px) the council's teaser: the fourteen numbered seats in speaking order and one sentence
  on how a run works, linking to the council. Then the map of the book: a two-level squarified
  treemap computed at build time (one box per asset class with its name and share, then one tile
  per line sized by its weight; a desktop and a phone layout, positions as generated classes). Every
  tile has the same neutral fill: asset class is shown by the box and its name, never by a hue, so
  hues stay with the agents and with long/short. What a tile shows (name, weight, last day's move,
  ticker) is decided by CSS container queries on its real size, so no label is clipped at any width;
  every line is also in the list. Shorts carry a coral edge, a hatch band along it, a solid "short"
  pill and a minus sign; hover is a ring, never a lighter fill. The manager's violet dot marks a line
  the latest run's council decision moved (never drift between runs). The figures (invested, cash,
  lines held, last day, open P/L, kill switch) follow the map, then the broker-style list (every
  row opens the line's page; on a phone the position and vehicle get their own line), with the
  legend folded into "How to read the list". Target, rehearsal and awaiting-account states stay
  labelled as before.
- The council: a plain paragraph, then one nameplate per seat in speaking order, grouped by phase:
  the seat number large in the seat's colour, the name, what it said or did in the latest run
  ("hold ref → cut SEMIS" when the bull's rebuttal asks for something else than its opening,
  "cut SEMIS · short GBPUSD · 2/3 agree", "9/9 checks pass") and its job; marked when its last call
  failed. On a phone the roster is a compact list (number, name · verdict). The latest run, recent
  runs (each agent chip in its seat colour) and performance come after, smaller.
- Line pages (`assets/<line>.html`) for every line that ever appeared: position against the
  reference; a step chart of the weight across runs (reference, council, executed book; thin lines,
  a dashed zero baseline, a dot for the executed book at each run, rehearsal runs shaded, a date
  under each run when there are few, direct labels, hover titles with the outcome, a table twin with
  an outcome column, no script; the last 60 runs); a line at 0% everywhere, or a single run, is said
  in words. The executed book is stated only where the record knows it (see docs/data-rights.md).
  The trend history, oldest first with identical runs merged, gives each run's allowed range (as
  sizes and as portfolio shares), its reason and the qualifying card, and follows the policy's
  trend levels (lines outside the reference say so). Everything the agents said about the line
  (claims and answers that name it or cite its facts, concessions, requests, manager decisions,
  decisive facts, cards, risk holds, auditor corrections), each claim with what happened to it
  (answered, conceded, set aside, used). Its orders say buy or sell and the position they work on,
  and its latest facts.
- Lines link to their page wherever structured data names them: stances, the manager's decisions and
  attempts, the run chain, risk holds, card scopes, evidence chips, tables.
- Agent pages: a run-by-run table (what it said or did, whose side the manager took, claims set
  aside, outcome) before the history; each run's history folds, the latest open. The human operator
  has a page of decisions.
- Every page's header comes before its contents list, so on a phone a page opens on its title; long
  "Other lines" / "Other agents" lists fold on narrow screens.
- New identity: an ink-navy base; every agent owns one seat colour used everywhere it appears
  (code officers and the checks share desaturated families; the bull's teal and the bear's coral
  are also long and short). Nothing that is not an agent wears a seat colour: card chips take their
  author's colour, LLM/CODE/HUMAN tags and authorities are neutral, and progress rings use only the
  executed and halted colours, neutral in between. Space Grotesk, IBM Plex Sans and IBM Plex Mono are
  self-hosted (`site/static/fonts`, SIL Open Font License; `OFL.txt` with the notices ships next to
  the fonts in every build; see `THIRD_PARTY_NOTICES.md`); the CSP gains `font-src 'self'` and still
  forbids every script. Type on a 1.25 scale with one label size (nothing smaller than 0.75rem) and
  spacing on a 4/8 px grid, as tokens. Dark only; no shadows or glass; subtle motion that
  `prefers-reduced-motion` turns off. No page scrolls sideways at 390 px. Not a policy change.
- Palette check, recorded: the approved seat palette does not pass the data-viz palette validator
  (dark mode, surface #0E1320). News (#E3B341) and the approved macro green (#9BD35A) separated by
  only ΔE 2.3 for deutan vision, so macro was darkened to #54A31B (user decision, 2026-09-26); the
  risk seat's chroma (0.066) and the code-officer and checks families
  are under the chroma floor by design; the bright seats sit above the dark lightness band. So a
  seat colour never carries meaning alone: it always comes with the agent's name, seat number or
  icon (the hemicycle's seats are numbered).
- Test fixture: the synthetic journal uses the policy's trend levels (mixed = ¾) and its book holds
  the mixed lines at three quarters; it was regenerated (its policy hash follows the working tree).

## policy plumbing and line identity (WP-B) (2026-09-25)
- Not a policy change: no file under `policy/` changes, and today's nine-line policy loads with the
  same lines, numbers and SHA-256.
- `Policy.load` can merge a quarterly `policy/stock-sleeve.yaml` (none exists yet; it arrives with
  the go-live commit and a `stocks-<quarter>` tag). Each row becomes a stock line of real shares,
  long only, never levered, in the satellite sleeve, appended after the core lines; the file cannot
  express a CFD, a short or leverage, and an unquoted YAML boolean (`ON`) fails. `stock-rank.yaml`
  becomes `Policy.stocks`; every `calendar-*.yaml` is merged (the 2027 FOMC dates now apply).
- Line identity is checked when the policy loads, never mid-cycle: line ids follow the public
  pattern (1–12 characters, `_` inside, `BRK_B`, `F`) and never start with `UNMAPPED`; line ids,
  vehicle symbols and aliases form one namespace (a symbol belongs to exactly one line); in-reference
  core weight plus the sleeve weight stays within `reference_gross_max`; the sleeve's name, shortlist
  and history-ticker counts and its company (CIK) rules are enforced. A hard-coded invariant repeats
  that stock lines are real, long and 1× (`leverage_caps.stock` must be 1).
- Stricter: live cycles and the watch load policy from a snapshot of the committed `HEAD:policy/`,
  never the working tree, so an uncommitted edit cannot reach them and the recorded policy SHA is
  always committed content. The approval path compares against the same kind of snapshot, taken
  from the installed release (`releases/current`) when there is one. Git calls ignore inherited
  `GIT_*` variables and refuse a directory that is not the top of its own checkout.
- If git cannot provide the snapshot (for example a broken developer-tools shim after an OS
  upgrade), a live cycle or watch run continues on the last snapshot it verified for the same
  checkout, re-checked byte for byte, with the whole-book blocker `policy_snapshot_unavailable`
  (every line held; the kill switch, stop checks and flatten proposals keep running) and an URGENT
  alert at most every four hours. With no verified snapshot it sends the alert and refuses to
  start. The approval path does not fall back.
- A committed `stock-sleeve.yaml` stays out of every runtime policy (live, dry run, rehearsal,
  approval) until the hard-coded switch `invariants.STOCK_SLEEVE_LIVE` is flipped in the go-live
  commit. Only then is the sleeve's tag checked: a committed sleeve that differs from the blob at
  its quarter's tag raises `sleeve_policy_untagged`, scoped to the satellite sleeve.

## market hours and execution safety (WP-F) — policy change (2026-09-25)
- **Policy change**: new `policy/calendar-2027.yaml` with the Federal Reserve's tentative 2027 FOMC
  decision times and the NYSE/LSE 2027 exchange calendar as a tested record. The policy hash
  changes. The FOMC 2027 blocks apply through the calendar merge that WP-B adds to `Policy.load`.
- Exchange calendars run through the end of 2027: NYSE holidays and 13:00 ET early closes
  (2026-11-27, 2026-12-24, 2027-11-26), UK bank holidays and the LSE 12:30 half days (24 and
  31 December), all through zoneinfo, so the weeks when only one side is on summer time are right.
  Past the calendars an open is refused (`calendar_missing:<year>`) and a close uses weekday
  hours; a test fails from 1 October of the last year unless the next year is added. FX, index
  and commodity CFDs have no session on 25 December and 1 January.
- Every leg carries its market session and its own deadline: the next slot − 5 min, or its
  session's close − 10 min if earlier (FX daily break and Friday close included). An 18:40 UTC
  proposal with US legs is approvable until 19:50 UTC in US summer time (20:50 in winter); on an
  early-close day the 14:40 proposal ends at 17:50 UTC and no US-session line is admitted at 18:40.
- A rebalance stays approvable while any leg is; flatten and compliance until the next slot − 5 min.
  At approval a leg past its deadline, or whose market is closed or closes within 5 minutes, is
  dropped and printed; a rebalance applies the drop rule (a dropped risk-reducing leg drops every
  risk-increasing leg), and a dropped re-open of a remainder or vehicle switch drops its close too,
  so a trim never becomes a full exit; nothing left → refused. The clock is read again after the
  typed nonce: an expired decision, or market-hours drops that changed while the operator typed,
  are refused with nothing sent. The drift and hard-gross checks count opens the broker still
  holds, as the engine does. A rebalance made under another policy (the committed `HEAD` policy
  the approval now always loads) is refused. Flatten and compliance skip that check (they only
  reduce).
- The executor re-checks each leg's session just before sending (closed → skipped). Broker status
  11 (held until the market opens) no longer times out into `execution_unknown`: while the session
  is open by the calendar (a halt) polling continues through the normal window; after that the
  order is cancelled once a cancel route is verified (the writer must declare
  `CANCEL_ROUTE_VERIFIED`; `resume` never cancels), otherwise the decision becomes
  `waiting_for_market`, which the watch resolves read-only (filled → completed; cancelled →
  completed_partial; a partial fill waits for its remainder until the leg's deadline; still held
  an hour after the next full session closes → blocked, then `council ops resolve`). The timeout
  applies on every watch run, also when the broker read fails or no broker is connected. A late
  fill is checked on its units and the broker's exposure (units × fill price), not the planned
  price: a gap at the open raises `sl_refit_needed` and completes, and drift from the approval's
  targets is recorded, not a block. A waiting stock order will hold only the satellite sleeve;
  any other holds the whole book, as a blocker did before, and the engine counts a held open as
  already held. Each cycle resolves held orders before its broker snapshot (no double count), and
  a flatten adds no second close for a position whose close the broker already holds.
- `council inbox` and `council show` print the approval deadline, the markets a plan needs and the
  first leg deadline; `inbox` and `council ops resolve` open only the ledger (they work when the
  working-tree policy does not load). The ledger (schema 3) stores each decision's policy SHA and
  blocker scope.
- Rules looser than before: past the calendar horizon a close uses weekday hours (opens refused);
  flatten and compliance skip the policy-SHA check; a satellite-scoped blocker would leave the core
  tradable (no stock line exists yet), and a stock-only hold that times out to `blocked` keeps that
  scope (a broken fill holds the whole book). Stricter: per-leg deadlines, the drop rule and the
  re-open rule, the re-check after the nonce, the pre-send session check, pending held orders
  counted as held by the risk engine and the approval.

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
