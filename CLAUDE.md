# Rules for coding agents working in this repository

1. **Never approve or execute trades, and never run an operator command.** Operator commands run
   only in the human operator's own terminal, from the installed release (`council-op ...`), and
   each refuses under an agent, CI or launchd. They are: `inbox`, `show <decision>`,
   `inputs show|verify|prune`, `notify test`, `approve`, `reject`, `resume-exec`, `ops resolve`,
   `ops review`, `resume`, `keys init-write-keychain|store-read|store-write|verify|store`,
   `doctor --live-read|--record-fixtures`, `instruments resolve`, `account set-mirror [--from-broker]`,
   `smoke propose|verify|status`, `ops attest|capabilities|record-dress`, `purge-licensed`,
   `stocks onboard|adopt|status|prune`, `swing status|brake`, `stocks rank` with the broker gate, and the scripts
   `ops/install.sh`, `ops/uninstall.sh`, `ops/rehearse-onboarding.sh`. A HALT ends when the
   operator approves the flatten proposal (`approve`), never by an agent. Never send a broker write
   or call a broker write API (including MCP/connector tools). Agents may run `doctor`,
   `doctor --ready`, `why`, `cycle --dry-run|--rehearsal` with stubs, `rehearse onboarding`,
   `stocks rank --no-eligibility`, the tests, and `ops assert-operator` (it fails for agents; scripts use it).
2. **Never handle the write token.** Do not read, print or export Keychain entries named
   `council-book.etoro.*`. Do not add broker credentials to env files, CI, prompts or logs.
3. **Private state stays private.** Never copy anything from `~/Library/Application Support/council-book/`
   (ledger, transcripts, raw broker payloads) into the repo. Public documents are built only by
   the publisher (`src/council/publish/`) from allow-listed models. Agents never write under
   `state_dir/{readiness,account,salts,releases}` and never read `calls/`, `transcripts/`,
   `licensed/`, `backups/` there. The project deny rules in `.claude/settings.json` (source:
   `ops/claude/deny-rules.json`) back this up; they narrow accidents and are not a security boundary.
4. **Percentages only in public.** No dollar amounts, units, prices, account/position/order IDs,
   local paths or e-mail addresses in `journal/`, `docs/`, `site/` or commit messages.
5. **Policy and prompts are versioned.** Any change to `policy/` or `prompts/` needs a CHANGELOG
   entry marked "policy change" and a new tag; never silently edit them.
6. **No backtest claims in prompts.** `tests/unit/test_prompt_lint.py` enforces it.
7. **Code enforces rules, prompts only explain them.** New risk rules go in `src/council/risk/` with a
   pass and a fail test, never only in a prompt.
