#!/bin/sh
# The human dress rehearsal of token day (m5-readiness §7.3, gate O5). Run it once, in a plain
# Terminal, from the installed release:
#
#   "$HOME/Library/Application Support/council-book/releases/current/ops/rehearse-onboarding.sh"
#
# What it does:
#   1. `council ops assert-operator` must pass (an agent, CI or a pipe is refused). Leftover rehearsal
#      variables in your shell are refused. A throwaway sandbox is created under $TMPDIR with the
#      REHEARSAL marker, a local bare remote and a publisher clone; it never resolves to the real
#      state dir.
#   2. The onboarding FakeEtoro starts on 127.0.0.1 inside the sandbox, then a CLEAN subshell opens
#      (`env -i … zsh -f`, prompt `[REHEARSAL] `) carrying the sandbox variables. Your own shell never
#      exports them.
#   3. Inside it you walk the token-day commands with three FAKE tokens: create the throwaway
#      keychain (you type a throwaway password), paste the fake tokens, approve S1 and the first
#      proposal with the nonce, run `smoke verify` and `ops attest`.
#   4. On exit (also on Ctrl-C): the fake broker stops, the throwaway keychains are deleted, the
#      keychain search list must be byte-identical to its state before the run, and `council ops
#      record-dress` writes readiness/dress.json in the real state dir: the only file written outside
#      the sandbox. The sandbox is then removed (--keep keeps it).
#
# The real keychain, the real state dir, the real remote, launchd and the network are never touched
# (inside the marked sandbox `council cycle` / `watch` run on the stub model, synthetic bars and the
# fake broker, and publish only to the sandbox remote: rehearsal.onboarding.dress_cli_context);
# under the marker every eToro keychain item is read only from the throwaway file, and the broker URL
# is accepted only as http://127.0.0.1:<the sandbox's fake-broker.port>.
#
# Test hooks (tests/rehearsal/test_dress_script.py runs this script with fakes; never set them by hand):
#   REHEARSE_COUNCIL   the council command     (default: uv run --project <release> council)
#   REHEARSE_SECURITY  the security binary     (default: /usr/bin/security)
#   REHEARSE_SHELL     the [REHEARSAL] shell   (default: /bin/zsh)
#   REHEARSE_TTY       the operator terminal   (default: /dev/tty)
set -eu

usage() { echo "usage: ops/rehearse-onboarding.sh [--keep]" >&2; exit 64; }
die() { echo "rehearse-onboarding.sh: $*" >&2; exit 1; }

KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP=1 ;;
    *) usage ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
REAL_STATE="$HOME/Library/Application Support/council-book"
SECURITY="${REHEARSE_SECURITY:-/usr/bin/security}"
SUBSHELL="${REHEARSE_SHELL:-/bin/zsh}"
UV="$(command -v uv || true)"
if [ -n "${REHEARSE_COUNCIL:-}" ]; then
  COUNCIL="$REHEARSE_COUNCIL"
else
  [ -n "$UV" ] || die "uv not found on PATH"
  COUNCIL="$UV run --quiet --project $ROOT council"
fi

# ---- 1. refusals before anything is created
for name in COUNCIL_STATE_DIR COUNCIL_KEYCHAIN_FILE COUNCIL_ETORO_BASE_URL; do
  eval "value=\${$name:-}"
  # shellcheck disable=SC2154  # value is set by the eval above
  [ -z "$value" ] || die "$name is set in this shell: unset it (a leftover rehearsal variable could redirect the real tokens)"
done
# shellcheck disable=SC2086  # COUNCIL is a command line on purpose
COUNCIL_ROLE=operator $COUNCIL ops assert-operator || die "not an operator terminal: run this from your own Terminal"
TTY="${REHEARSE_TTY:-/dev/tty}"   # tests pass a pty slave; only a terminal character device is accepted
case "$TTY" in /dev/tty*|/dev/pts/*) ;; *) die "no terminal: run this in your own Terminal" ;; esac
[ -c "$TTY" ] || die "no terminal: run this in your own Terminal"

BASE="${TMPDIR:-/tmp}"
BOX="$(mktemp -d "${BASE%/}/council-dress.XXXXXX")"
BOX="$(cd "$BOX" && pwd -P)"
STATE="$BOX/state"
REAL_RESOLVED="$(cd "$REAL_STATE" 2>/dev/null && pwd -P || echo "$REAL_STATE")"
case "$STATE/" in
  "$REAL_RESOLVED"/*) rm -rf "$BOX"; die "the sandbox would resolve inside the real state dir" ;;
esac
case "$REAL_RESOLVED/" in
  "$STATE"/*) rm -rf "$BOX"; die "the sandbox would contain the real state dir" ;;
esac
mkdir -p "$STATE" "$BOX/bin"
chmod 700 "$BOX" "$STATE"
echo "rehearsal sandbox: never the real state dir" >"$STATE/REHEARSAL"
THROWAWAY="$BOX/rehearsal-throwaway.keychain-db"
WRITE_KC="$STATE/council-write.keychain-db"

SEARCH_BEFORE="$BOX/search-list.before"
"$SECURITY" list-keychains -d user >"$SEARCH_BEFORE"

restore_search_list() {  # `security create-keychain` adds the new file to the search list: undo it
  sed -e 's/^[[:space:]]*//' "$SEARCH_BEFORE" | xargs "$SECURITY" list-keychains -d user -s
}

SERVER_PID=""
STEPS_OK=0
cleanup() {
  status=$?
  trap - EXIT INT TERM HUP
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  for kc in "$THROWAWAY" "$WRITE_KC"; do
    if [ -e "$kc" ]; then "$SECURITY" delete-keychain "$kc" >/dev/null 2>&1 || rm -f "$kc"; fi
  done
  rm -f "$STATE/rehearsal-tokens.txt"
  if ! "$SECURITY" list-keychains -d user | cmp -s - "$SEARCH_BEFORE"; then restore_search_list; fi
  if "$SECURITY" list-keychains -d user | cmp -s - "$SEARCH_BEFORE"; then
    echo "keychain search list unchanged"
  else
    echo "WARNING: the keychain search list changed during the rehearsal; compare with:" >&2
    cat "$SEARCH_BEFORE" >&2
    status=1
  fi
  if [ "$STEPS_OK" = 1 ]; then
    # Your own terminal again: COUNCIL_STATE_DIR is unset, so the record lands in the real state dir.
    # shellcheck disable=SC2086
    if ! COUNCIL_ROLE=operator $COUNCIL ops record-dress --sandbox "$STATE"; then status=1; fi
  fi
  if [ "$KEEP" = 1 ]; then echo "sandbox kept at $BOX"; else rm -rf "$BOX"; fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# ---- local bare remote + publisher clone (the only place anything is published)
git init -q --bare -b main "$BOX/remote.git"
git clone -q "$BOX/remote.git" "$STATE/publisher-clone" 2>/dev/null
mkdir -p "$STATE/publisher-clone/journal"
: >"$STATE/publisher-clone/journal/.keep"
git -C "$STATE/publisher-clone" config user.name council-publisher
git -C "$STATE/publisher-clone" config user.email 18754232+fbzz@users.noreply.github.com
git -C "$STATE/publisher-clone" add journal/.keep
git -C "$STATE/publisher-clone" commit -q -m "rehearsal remote"
git -C "$STATE/publisher-clone" push -q -u origin main
case "$(git -C "$STATE/publisher-clone" remote get-url --push origin)" in
  "$BOX/remote.git") ;;
  *) die "the rehearsal publisher clone must push to the sandbox remote only" ;;
esac

# ---- 2. the fake broker (loopback only, inside the sandbox)
# shellcheck disable=SC2086
COUNCIL_STATE_DIR="$STATE" COUNCIL_ROLE=dev $COUNCIL rehearse fake-broker --scenario onboarding \
  >"$BOX/fake-broker.log" 2>&1 &
SERVER_PID=$!
tries=0
while [ ! -s "$STATE/fake-broker.port" ] || [ ! -s "$STATE/rehearsal-tokens.txt" ]; do
  tries=$((tries + 1))
  if [ "$tries" -gt 150 ] || ! kill -0 "$SERVER_PID" 2>/dev/null; then
    die "the fake broker did not start (see $BOX/fake-broker.log)"
  fi
  sleep 0.2
done
PORT="$(cat "$STATE/fake-broker.port")"
case "$PORT" in ""|*[!0-9]*) die "bad fake-broker.port" ;; esac
URL="http://127.0.0.1:$PORT"

# ---- the throwaway keychain for the READ items (you type a throwaway password twice)
echo "Creating the throwaway keychain: type a THROWAWAY password (never your login password)."
"$SECURITY" create-keychain "$THROWAWAY" || die "throwaway keychain not created"
restore_search_list
"$SECURITY" list-keychains -d user | cmp -s - "$SEARCH_BEFORE" || die "the keychain search list could not be restored"

# `council-op` inside the [REHEARSAL] shell: the same release, the sandbox variables only
cat >"$BOX/bin/council-op" <<EOF
#!/bin/sh
exec $COUNCIL "\$@"
EOF
chmod 755 "$BOX/bin/council-op"

# ---- 3. the walkthrough
cat <<EOF

================================ [REHEARSAL] ================================
Sandbox:      $BOX   (removed on exit)
Fake broker:  $URL   (FAKE tokens; nothing leaves this machine)
Fake tokens:  (paste these when asked; they are worthless)
$(sed 's/^/  /' "$STATE/rehearsal-tokens.txt")

Walk token day (runbook §11.2) in the shell that opens now:

  council-op keys init-write-keychain              # the sandbox write keychain
  council-op keys store-read                       # paste app-key, then the read token (no echo)
  council-op keys store-write                      # paste the write token (no echo)
  council-op keys verify
  council-op ops attest terms-version
  council-op doctor --live-read
  council-op account set-mirror --funding-usd 1000 --from-broker   # any CANARY amount, never your real funding
  council-op instruments resolve
  council-op doctor --record-fixtures
  council-op smoke propose S1 --preview
  council-op smoke propose S1                      # prints <id>
  council-op watch                                 # publishes the weightless ops row to the local remote
  council-op show <id>
  council-op approve <id>                          # the typed nonce, then the write-keychain password
  council-op smoke verify <id>
  council-op ops attest mirror-copied --decision <id>
  env COUNCIL_ROLE=runner council-op cycle         # the first cycle: stub model, synthetic bars, fake broker;
                                                   # within 2 h after a slot (02:40, 06:40, ... 22:40 UTC)
  council-op inbox ; council-op show <id> ; council-op approve <id>
  council-op watch

Type 'exit' when done. On exit the keychains are deleted, the search list is checked and
readiness/dress.json (gate O5) is recorded in your real state dir.
=============================================================================
EOF

env -i \
  HOME="$HOME" \
  USER="${USER:-}" \
  LOGNAME="${LOGNAME:-${USER:-}}" \
  TERM="${TERM:-xterm-256color}" \
  TMPDIR="$BASE" \
  PATH="$BOX/bin:${UV:+$(dirname "$UV"):}/usr/bin:/bin:/usr/sbin:/sbin" \
  COUNCIL_ROLE=operator \
  COUNCIL_MODE=stub \
  COUNCIL_STATE_DIR="$STATE" \
  COUNCIL_KEYCHAIN_FILE="$THROWAWAY" \
  COUNCIL_ETORO_BASE_URL="$URL" \
  PS1='[REHEARSAL] ' \
  PROMPT='[REHEARSAL] ' \
  "$SUBSHELL" -f -i || true

STEPS_OK=1
