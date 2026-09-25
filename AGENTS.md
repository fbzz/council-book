# Rules for coding agents working in this repository

1. **Never approve or execute trades.** Do not run `council approve`, `council flatten`,
   `council resume-exec`, or any broker write, and never call a broker write API (including
   MCP/connector tools). Approval happens only in the human operator's terminal.
2. **Never handle the write token.** Do not read, print or export Keychain entries named
   `council-book.etoro.*`. Do not add broker credentials to env files, CI, prompts or logs.
3. **Private state stays private.** Never copy anything from `~/Library/Application Support/council-book/`
   (ledger, transcripts, raw broker payloads) into the repo. Public documents are built only by
   `council publish` from allow-listed models.
4. **Percentages only in public.** No dollar amounts, units, prices, account/position/order IDs,
   local paths or e-mail addresses in `journal/`, `docs/`, `site/` or commit messages.
5. **Policy and prompts are versioned.** Any change to `policy/` or `prompts/` needs a CHANGELOG
   entry marked "policy change" and a new tag; never silently edit them.
6. **No backtest claims in prompts.** `tests/unit/test_prompt_lint.py` enforces it.
7. **Code enforces rules, prompts only explain them.** New risk rules go in `src/council/risk/` with a
   pass and a fail test, never only in a prompt.
