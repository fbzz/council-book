# Operator runbook

Nothing in this runbook is automated: the human operator runs every step, in a plain Terminal.app
window, never in an agent session, an editor terminal or under launchd. Every operator command
refuses anywhere else (`council ops assert-operator` shows why), and most refuse unless they run from
the installed release (release pinning). Amounts in this public runbook are placeholders (`<N>`).

```sh
REL="$HOME/Library/Application Support/council-book/releases/current"
alias council-op='"$HOME/Library/Application Support/council-book/releases/current/.venv/bin/council"'
export COUNCIL_ROLE=operator
```

`council-op doctor --ready [--track core|stocks] [--post-token] [--json] [--network]` lists every
readiness gate with its code and the next command to run; it exits 0 when ready, 1 when not.

## 1. Before the token

1. **eToro licence question.** Ask eToro in writing (support ticket) whether the agents may read the
   broker's news feed (Licensed Content) for your personal use. When the answer is yes:
   `council-op ops attest etoro-licence --ref <ticket>` (the ticket is checked, never stored). Until
   then `doctor --ready` shows the feed gate red; turning the feed off is a policy change
   (`policy/council.yaml` `news.broker_feed: false`).
2. **Disk and power:** at least 10 GiB free; the Mac on AC with `sudo pmset -c sleep 0`.
3. **Non-broker secrets**, one Keychain item each, typed at a no-echo prompt (never on the command
   line): `council-op keys store <name>` for `tiingo` (a dedicated account), `gov-user-agent`
   (`council-book (contact: <e-mail>)`), `ntfy-topic`, `healthcheck-url` (a 15-minute check with a
   45-minute grace), optionally `fred`, `sec-user-agent` and `alpaca` (Track S), and `soak-probe`
   (paste the output of `openssl rand -hex 16`).
4. **Publisher deploy key** with write access to the repository (GitHub → Settings → Deploy keys),
   used by the publisher clone in the state directory.
5. **Tag and install from `origin`, without loading.** Tag the commit you reviewed, push the tag, and
   install from a fresh clone of that tag, so neither a local tag nor a dev-tree script is trusted:
   ```sh
   git tag -a council-spec-v1 -m "council-spec-v1: first live release (core)" <reviewed-commit>
   git push origin council-spec-v1
   T=$(mktemp -d) && git clone -q --depth 1 --branch council-spec-v1 https://github.com/fbzz/council-book.git "$T/cb"
   "$T/cb/ops/install.sh" council-spec-v1    # shows the commit; type its first 8 characters; loads nothing
   ```
   Then add the `council-op` alias above to `~/.zshrc`.
6. **Agent deny rules** are project-level and checked in (`.claude/settings.json`, source
   `ops/claude/deny-rules.json`). Open Claude Code in the repository and check that `/permissions`
   lists them. They narrow accidents; they are not a security boundary.
7. **Write keychain and checks:**
   ```sh
   council-op keys init-write-keychain      # pick a password you will type at every approval
   council-op notify test
   council-op ops attest ntfy-received
   council-op ops attest tiingo-dedicated
   council-op ops attest power-ok           # only if you accept the power gate as amber
   ```
8. **Dress rehearsal** (about 20 minutes, against a fake broker in a throwaway sandbox with its own
   keychain; it opens a `[REHEARSAL]` shell and walks you through token day):
   ```sh
   cd "$REL" && ops/rehearse-onboarding.sh     # --keep keeps the sandbox
   ```
   Its cycle step runs on the stub model, synthetic bars and the fake broker (no network), but on
   the wall clock: run it within two hours after a slot (02:40, 06:40, 10:40, 14:40, 18:40 or
   22:40 UTC), or the slot is recorded `missed` and nothing is proposed.
   On exit it records the result itself (`ops record-dress`).
9. **launchd soak** (48 hours: the stub model hourly, one real-model rehearsal cycle a day, a local
   remote only):
   ```sh
   "$REL/ops/install.sh" council-spec-v1 --rehearsal --load
   # after 48 h:
   "$REL/ops/uninstall.sh" --rehearsal
   ```
10. **Check:** `council-op doctor --ready --track core --network` shows only token gates open.

## 2. Token day (core track)

Start on a weekday morning, London time; allow about three hours.

1. **In the eToro web UI** (you, never an agent): create the **Agent Portfolio**, fund its copy, and
   set the copy's **Copy Stop Loss** looser than the kill switch (40% recommended). Create two user
   tokens for it: `council-read` (read scope only) and `council-write` (write scope). Note the
   Builders' Economy terms version. Never create keys on the main account.
2. **Keys and checks:**
   ```sh
   council-op keys store-read                 # app key + READ token, no echo
   council-op keys store-write                # WRITE token into the write keychain, no echo
   council-op keys verify                     # GET only; unlocks the write keychain once
   council-op ops attest terms-version
   council-op doctor --live-read              # token gates, codes only
   council-op account set-mirror --funding-usd <N> --from-broker
   council-op instruments resolve             # check the vehicle, currency and unit of every line
   council-op doctor --record-fixtures        # private; purged after 7 days
   council-op doctor --ready --track core --post-token
   ```
   If `keys verify` reports that the API exposes no scopes, check both tokens in the UI, then run
   `council-op ops attest token-scopes`.
3. **If a gate needs a policy change** (a floor, a dead candidate, a unit): the agents write the
   change with a CHANGELOG policy-change entry; you review it, tag it (for example
   `council-spec-v1.1`) and install from `origin` as in §1.5, then repeat the last line of step 2.
4. **Smoke tickets**, minimum size, one at a time: S1 to S4 in LSE hours (a real ETF/ETC long, its
   stop-loss moved, a partial close, the close), then S5 (BTC), then S6 (a CFD short, outside the FX
   break). For each step:
   ```sh
   council-op smoke propose S1 --preview      # the exact request: currency, units, stop-loss
   council-op smoke propose S1                # prints <id>
   council-op show <id>
   council-op approve <id>                    # typed nonce, then the write-keychain password
   council-op smoke verify <id>               # automatic checks + the manual checklist
   council-op ops attest mirror-copied --decision <id>   # after looking at the main account
   ```
   After S1: `council-op ops attest fee-charged-on=virtual,mirror` (the levels you saw charged) and
   `council-op ops attest copy-stop-loss`. A step refused with `smoke_min_above_cap` is skipped and
   its capability stays off. Smoke records stay private; only a weightless ops row is published.
5. **First live cycle by hand**, at a slot (:40 of 02, 06, 10, 14, 18 or 22 UTC, at most 120 minutes
   late), once `council-op smoke status` shows no open or pending ticket:
   ```sh
   env COUNCIL_ROLE=runner COUNCIL_MODE=live "$REL/.venv/bin/council" cycle
   ```
   Then handle the proposal as in §3.
6. **Load the jobs** (refused unless `doctor --ready --track core --post-token` is green):
   ```sh
   "$REL/ops/install.sh" council-spec-v1 --load
   launchctl list | grep com.fbzz.council
   ```
7. **The next 48 hours:** approve or reject proposals from the notifications; run
   `council-op doctor --ready --post-token` once a day.

## 3. Every proposal

```sh
council-op inbox
council-op show <id>                  # legs and, per line, why it moves (--why: every line's trail)
council-op approve <id>               # re-checks everything, typed nonce, unlocks the write keychain
council-op reject <id> --reason "…"   # the reason is published
council-op inputs show <cycle> --html # optional: exactly what each agent read (private, local)
```

After the reveal anyone, agents included, can run `council why <cycle> [<line>]` on the public
record. Never click "Always Allow" on a keychain prompt. An execution has succeeded only when it
reaches `completed`; `blocked` and `execution_unknown` halt later live cycles until you act (§5).

## 4. Kill switch

- **WARN** (−20% from the lifetime peak): no new risk is proposed.
- **HALT** (−25%): a flatten proposal is issued every cycle, with urgent notifications, until you
  approve it with `council-op approve <id>` or reject it. After recovery run
  `council-op resume --reason "…"`; the lifetime peak stays, so the next check halts again while
  equity is still below the halt line.

## 5. Incidents

| Situation | Command |
|---|---|
| `execution_unknown` | `council-op resume-exec <id>` (broker lookups only; never sends) |
| An order waiting for a closed market | check the broker, then `council-op ops resolve <id> --filled` or `--cancelled` |
| `blocked` with no active or waiting leg | check the broker, then `council-op ops review <id> --reason "…"` |
| HALT | §4 |
| Token rejected or compromised | delete and recreate it in the eToro UI, then `keys store-read` or `keys store-write`, then `keys verify` |
| Keychain locked after a reboot | log in; the next cycle recovers |
| eToro asks for Licensed Content to be deleted | `council-op purge-licensed --all` within 24 hours (`--dry-run` counts first); reply with its receipt |
| Leak in the public repository | `"$REL/ops/uninstall.sh"`, remove the content, rotate any exposed credential, publish an incident note |

Outstanding opens after an incident need a fresh proposal; nothing re-sends an old one.

## 6. Weekly (10 minutes)

- `council-op doctor --ready --post-token`: token expiry, disk, backups, pings, the licensed-content
  sweeper.
- The ops page: missed cycles, parse failures, fallbacks.
- The main account's mirror against the published book (percentages only).
- `council-op stocks status` once the stock sleeve is live.

## 7. Paper swing run (no broker)

The swing pipeline on real data and real models, with no broker token and nothing published
(`council cycle --paper`: own state dir `<state>/paper`, where `<state>` is
`~/Library/Application Support/council-book` unless `COUNCIL_STATE_DIR` says otherwise).

1. **Keychain items** (no-echo prompt): `council-op keys store alpaca-key-id`, `alpaca-secret`
   (or `alpaca` for both), `sec-user-agent` and `tiingo`. A missing one fails closed: the cycle
   shows `swing_source_unavailable:<source>` and the swing council does not run.
   Data keys (never broker tokens) may instead sit in the git-ignored `.env` at the repository root
   (or `COUNCIL_ENV_FILE`), mode 0600: `COUNCIL_ALPACA_KEY_ID`, `COUNCIL_ALPACA_SECRET`,
   `COUNCIL_TIINGO_TOKEN`, `COUNCIL_SEC_USER_AGENT`, `COUNCIL_FRED_TOKEN`. The `.env` wins over the
   Keychain; a group- or world-readable file is ignored; stub mode never reads it.
2. **Funded NAV, once** (private, costs only; without it every entry drops `cost_unavailable`):
   ```sh
   mkdir -p "<state>/paper/account" && umask 077
   echo '{"funded_real_nav_usd": <N>}' > "<state>/paper/account/swing.json"
   ```
3. **Run** within about 2 hours after a swing slot (14:40 UTC in US summer time, 18:40 UTC in
   winter; `--slot auto` runs the due slot up to 120 minutes late):
   ```sh
   COUNCIL_MODE=dry_run council-op cycle --paper
   COUNCIL_STATE_DIR="<state>/paper" council-op swing status
   COUNCIL_STATE_DIR="<state>/paper" council-op inputs show <cycle> --html
   ```
   Expected flags on a paper run: `swing_eligibility_unverified`, `paper_book` (the S-rules see the
   paper book and its drawdown; `swing_paper_assumed_book` only when the paper book failed) and
   sometimes `paper_reference_last_close` or `swing_screen_missing` (the after-close screen is
   built once per session after 20:30 New York).
   **Paper book** (`council.paperbook`): a paper run keeps a persisted paper broker in
   `<state>/paper/book.json` (+ `book_ledger.jsonl`, both 0600), started at the funded NAV above
   (missing -> the policy's assumed NAV, flag `paper_book_assumed_nav`). Each paper cycle marks it to
   market (core: the cycle's last closes; swing: stop / target / time stop on the completed daily
   bars, `swing.paper` conventions), hands it to the engine as the snapshot (flag `paper_book`; no
   more "current book taken as flat") and then executes the decision on paper at once (core legs at
   the declared policy cost, kept swing entries at the paper reference with 1.25% per leg; flag
   `paper_book_filled:<core legs>+<swing entries>`). The first build of an empty book carries
   `initial_build` (R13 / R14 cycle and 30-day cost / R15 exempt for that one cycle; carry, gross,
   net, margin, R7, vol and R21 still apply); every later cycle trades deltas within R14.
   `council paper status` prints it (percent only). `council.paperbook.paper_book_public(state)` is
   the percent-only dict the public paper record / site renders as "the portfolio":
   `paper_return_pct` (since start), `core_weights_pct` {line: %}, `swing_trades` [{ticker, side,
   setup, status, weight_pct, stop_pct, target_pct, entry_day, days_held, exit_reason,
   return_net_pct (net of the declared cost)}], `split_pct` {core, swing, cash}, `started_at`,
   `marked_at`, `last_cycle`, `funding`.

4. **FRED key (free)**: without one every cycle carries `calendar:release_dates_skipped_no_fred_key`
   (CPI / NFP / PCE release dates are not loaded). Request a free key at fred.stlouisfed.org
   (My Account -> API Keys) and put it in the mode-0600 `.env` as `COUNCIL_FRED_TOKEN=<key>` (or
   `council-op keys store fred`, Keychain item `council-book.fred`). The calendar reads it through
   the same loader; no code change.
5. **Outage alerts (ntfy)**: `council-op keys store ntfy-topic` (or `COUNCIL_NTFY_TOPIC` in the
   environment) enables URGENT phone alerts. When Ollama refuses model calls with HTTP 401 / 402 /
   403 (e.g. "payment past due"), the cycle carries the flag `llm_billing_error`, the council stops
   waiting for the outage retries, and one URGENT `llm_billing_error` alert goes out per 4 hours
   (paper and dry runs included; record `<state dir>/llm_alerts.json`). Fix the ollama.com account,
   then the next slot runs normally.
6. **RSS notes**: the per-ticker Yahoo feed is read for at most 10 names (open trades, carried
   ideas, top movers), one request per 1.5 s; after an HTTP 429 it is skipped for the rest of the
   New York day (`news_source_backoff:rss:yahoo_ticker`, flagged once; record
   `<state dir>/rss_backoff.json`). A market feed (e.g. PR Newswire) is retried once on a transient
   404 / 5xx; a persistent failure is still `news_source_error:rss:<feed>`.

## 7b. Publishing a paper run (the public decisions record)

A paper run trades nothing and has no approval, so its public record is revealed at once. `--publish`
writes it into the repo through the same public pipeline as a live cycle (allow-listed fields,
licensed `N:` items as id + source label only, agent text that overlaps licensed text withheld,
percent-only, sealed and revealed, leak-scanned before anything is written). It never commits or
pushes: you do.

```bash
cd <this checkout>                                  # or pass --publish-dir <repo>
COUNCIL_MODE=dry_run council cycle --paper --trace-all --publish
#   (add --at 2026-10-01T18:40Z to replay a missed swing slot)
git status journal/paper                            # cycles/YYYY/MM/<cycle>.json + .reveal.json,
                                                    # decisions.jsonl (#1, #2, ...), latest.json
git add journal/paper && git commit -m "paper decision #N" && git push origin main
```

- The cycle prints `paper_published:<N>` in its flags; `paper_publish_error:<type>` means nothing was
  written (the run itself is unaffected) — re-run with `--force` after fixing it. A re-run of the same
  slot keeps its number and replaces its row in place; numbers are never reused.
- The site shows it on the home page (paper portfolio + "Latest decision #N"), on `/decisions/`
  and on `/decisions/<N>/` (the whole flow). The private, licensed-text view stays
  `council paper report <cycle>` (local only, never commit it).
- Paper P&L: closed paper swing legs only (size x net return after the declared 1.25% per leg), in %
  of the paper NAV since the first published decision. The core shows weights only (no paper fills).

## 7a. Paper swing job (launchd, operator-installed)

`ops/paper-cycle.sh` runs `COUNCIL_MODE=dry_run council cycle --paper` from this checkout at
14:52 and 18:52 UTC on weekdays (`ops/launchd/com.fbzz.council-paper.cycle.plist.tmpl`). In US
winter time (from 1 Nov 2026) only 18:52 UTC is a swing slot: the script logs a skip for a winter
14:52 start. Output goes to `<state>/paper/logs/paper-cycle.log`. The label is outside
`com.fbzz.council.*`, so `ops/install.sh`, readiness and smoke never touch it. Prerequisites: steps
1-2 of section 7 and `uv sync` in the checkout (the job runs `<checkout>/.venv/bin/council`).
Agents never install or load it; the operator runs, in a terminal:

```sh
REPO="$HOME/Code/council-book"                       # this checkout
LOGS="$HOME/Library/Application Support/council-book/paper/logs"
PLIST="$HOME/Library/LaunchAgents/com.fbzz.council-paper.cycle.plist"
mkdir -p "$LOGS"
sed -e "s|{{REPO}}|$REPO|g" -e "s|{{HOME}}|$HOME|g" -e "s|{{LOGS}}|$LOGS|g" \
  "$REPO/ops/launchd/com.fbzz.council-paper.cycle.plist.tmpl" > "$PLIST"
plutil -lint "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl print "gui/$(id -u)/com.fbzz.council-paper.cycle" | head -20
tail -n 40 "$LOGS/paper-cycle.log"                    # after the next slot
```

Stop and remove: `launchctl bootout "gui/$(id -u)/com.fbzz.council-paper.cycle" && rm "$PLIST"`.
Run one slot by hand (same command the job runs): `COUNCIL_MODE=dry_run council cycle --paper`.

## 8. Swing book incidents (rehearsal sign-off)

| Situation | What to do |
|---|---|
| Swing brake (S15) or canary pause (URGENT) | New swing entries drop (`S15:brake_on` for the 30-day net loss brake, `S15:brake_engaged` for the Skeptic canary pause, `S15:brake_unknown` when the brake figure cannot be computed, e.g. an open swing trade without a mark); exits continue. The cycle engages the brake at a 30-day realised + open swing loss of `brake.pnl_nav` (net of all costs), and the canary pause after two missed weekly canaries in a row or two Skeptic pass-rate alarms within 30 days; either sends one URGENT and queues a public row. Read `council-op swing brake` (state, causes, last lifts; never the P&L figure) and `council-op swing status`. After review (for the canary: a recorded prompt revision), lift it: `council-op swing brake --lift --reason "…"` (S15) or `council-op swing brake --lift --canary --reason "…"`. The reason is published on `journal/ops/swing_brake.jsonl`: no amounts, number runs, ids, links or paths (refused otherwise). The flag `swing_brake_twice_60d` is a stop-at-once condition (design §8.3): stop the swing book and review. |
| A trade in `open_tp_missing` | Its take-profit is not at the broker; it holds a `swing:` blocker (new swing entries only). The next swing slot proposes a `set_tp` leg: check with `council-op show <id>`, then `council-op approve <id>`. The stop-loss is at the broker throughout. |
| 3 unapproved time-stop exits (URGENT) | Each rejected or expired time-stop exit proposal of an open trade counts one unapproved slot; at `earnings.urgent_after_unapproved_slots: 3` the cycle sends ONE URGENT for that trade (flag `swing_exit_unapproved`; never repeated for the same trade). Approve the newest exit proposal (`council-op inbox`, `council-op approve <id>`), or close by hand in the eToro UI and let the watch record it. |
| `swing:` blocker scope | A swing decision `blocked` / `execution_unknown` / `entry_unknown` halts new swing entries only, never the core. `council-op resume-exec <id>` (lookups only); for a `blocked` decision with no active or waiting leg, check the broker, then `council-op ops review <id> --reason "…"`. |
| `closed_unclassified` (URGENT) | The broker closed the position and no closed-trade record could be read: never a guessed stop hit. Check the close in the eToro UI and write it in the incident note; the trade stays out of the metrics (no close rate). |
| Forced close of a short (`closed_external`, URGENT) | Check the eToro UI for the reason (borrow recall, corporate action). A forced close is a stop-at-once condition (design §8.3): reject new swing entries until reviewed (`council-op reject <id> --reason "…"`) and record the capability result. |
