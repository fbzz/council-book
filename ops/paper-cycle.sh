#!/bin/bash
# The paper swing cycle for launchd (ops/launchd/com.fbzz.council-paper.cycle.plist.tmpl; runbook §7a). Operator-installed
# only: no agent loads it. Runs `COUNCIL_MODE=dry_run council cycle --paper` (no broker token, own state
# dir <state>/paper, publishes nothing) and appends its output to <state>/paper/logs/paper-cycle.log.
#
# launchd starts it at 14:52 and 18:52 UTC on weekdays. The 14:52 run is a swing slot only while New
# York observes daylight saving time (US summer); in winter (from 1 Nov 2026) only 18:52 UTC is a swing
# slot, so a winter 14:52 start logs a skip and exits. The cycle itself decides whether the slot is due.
#
# Test hooks (never set by the plist): PAPER_CYCLE_ECHO=1 prints the command instead of running it;
# PAPER_CYCLE_UTC_HOUR, PAPER_CYCLE_UTC_DOW (1=Mon..7=Sun) and PAPER_CYCLE_NY_ZONE (EDT/EST) replace the clock.
set -u
umask 077

REPO="${COUNCIL_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE="${COUNCIL_STATE_DIR:-$HOME/Library/Application Support/council-book}"
LOGDIR="$STATE/paper/logs"
LOG="$LOGDIR/paper-cycle.log"
mkdir -p "$LOGDIR"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
hour="${PAPER_CYCLE_UTC_HOUR:-$(date -u +%H)}"
dow="${PAPER_CYCLE_UTC_DOW:-$(date -u +%u)}"
zone="${PAPER_CYCLE_NY_ZONE:-$(TZ=America/New_York date +%Z)}"

if [ "$dow" -gt 5 ]; then
  echo "$(stamp) paper-cycle: weekend, skipped" >>"$LOG"
  exit 0
fi
if [ "$hour" = "14" ] && [ "$zone" != "EDT" ]; then
  echo "$(stamp) paper-cycle: 14:52 UTC is not a swing slot in US winter time (18:52 only), skipped" >>"$LOG"
  exit 0
fi

if [ -x "$REPO/.venv/bin/council" ]; then
  CMD=("$REPO/.venv/bin/council" cycle --paper)
else
  CMD=(uv run --project "$REPO" council cycle --paper)
fi

if [ "${PAPER_CYCLE_ECHO:-}" = "1" ]; then
  echo "COUNCIL_MODE=dry_run ${CMD[*]}"
  exit 0
fi

echo "$(stamp) paper-cycle: start" >>"$LOG"
cd "$REPO" || exit 1
COUNCIL_MODE=dry_run "${CMD[@]}" >>"$LOG" 2>&1
rc=$?
echo "$(stamp) paper-cycle: exit $rc" >>"$LOG"
exit $rc
