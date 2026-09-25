# Operator runbook

Nothing in this runbook is automated: each step is run by the human operator.

## Onboarding (once the Agent Portfolio exists)
1. In the broker's web UI, create the **Agent Portfolio** and two API keys for it: one **read-only**
   and one **write**. Never create keys on the main account for this project.
2. In the operator terminal (a plain Terminal window, not an agent session):
   ```sh
   export COUNCIL_ROLE=operator
   council keys init-write-keychain      # creates the separate auto-locking write keychain
   council keys store-read               # prompts (no echo) for the read token and the app key
   council keys store-write              # prompts (no echo) for the write token
   council doctor --live-read            # scopes, eligibility, costs, candles, feeds entitlement
   ```
3. Record the private fixtures (`council doctor --record-fixtures`) and review the proposed vehicle
   for every line (real UCITS/ETC vs 1× CFD).
4. Tag the spec (`council-spec-v1`) and run the **smoke test**: minimum-size tickets, each approved
   separately — a 1× long on a core vehicle, a real crypto buy, a short CFD with a stop-loss, a stop
   modification and a partial close. Confirm in the main account that every mirrored position has the
   same stop-loss.
5. Install the tagged release and load the jobs: `ops/install.sh council-spec-v1 --load`.

## Every proposal
```sh
council inbox
council show <decision>
council approve <decision>            # re-checks everything, asks for a typed nonce, unlocks the write keychain
council reject <decision> --reason "…"
```
Never click "Always Allow" on a keychain prompt.

## Kill switch
- **WARN** (−20% from the lifetime peak): no new risk is proposed.
- **HALT** (−25%): a flatten proposal is issued every cycle with urgent notifications until you
  approve it (`council flatten`) or reject it. Resuming later requires `council resume --reason "…"`;
  the peak stays the lifetime peak.

## Weekly (10 minutes)
- Check the ops page: missed cycles, parse failures, fallbacks.
- Compare the main account's mirror with the published book (percentages only) and record the result.
- Check token expiry (`council doctor`) and free disk space.

## Incidents
- **Leak in the public repo:** stop the jobs (`ops/uninstall.sh`), remove the content, rotate any
  exposed credential, publish an incident note.
- **Execution unknown / blocked:** `council resume-exec <decision>` only looks orders up and
  reconciles; it never sends new orders. Outstanding opens need a fresh proposal.
- **Token compromised:** delete it in the broker UI, create a new one, store it again.
