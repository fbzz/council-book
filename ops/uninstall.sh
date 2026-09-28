#!/bin/sh
# Unload council launchd jobs (keeps releases, state and logs). m5-readiness M5-F.
#   ops/uninstall.sh               the live jobs
#   ops/uninstall.sh --rehearsal   the soak jobs
#   ops/uninstall.sh --all         both
# Behind the installed release's `council ops assert-operator`, like install.sh.
set -eu
usage() { echo "usage: ops/uninstall.sh [--rehearsal|--all]" >&2; exit 64; }
die() { echo "uninstall.sh: $*" >&2; exit 1; }
LIVE_LABELS="com.fbzz.council.cycle com.fbzz.council.watch"
SOAK_LABELS="com.fbzz.council.rehearsal.cycle com.fbzz.council.rehearsal.watch"
[ $# -le 1 ] || usage
case "${1:-}" in
  "") LABELS="$LIVE_LABELS"; WHAT="council jobs" ;;
  --rehearsal) LABELS="$SOAK_LABELS"; WHAT="soak jobs" ;;
  --all) LABELS="$LIVE_LABELS $SOAK_LABELS"; WHAT="council and soak jobs" ;;
  *) usage ;;
esac
COUNCIL="$HOME/Library/Application Support/council-book/releases/current/.venv/bin/council"
[ -x "$COUNCIL" ] || die "no installed release to check the operator context; unload by hand: launchctl bootout gui/\$(id -u)/<label>"
"$COUNCIL" ops assert-operator || die "not the operator's own terminal (council ops assert-operator); nothing unloaded"
DOMAIN="gui/$(id -u)"
for label in $LABELS; do
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "$WHAT unloaded"
