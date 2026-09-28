#!/bin/sh
# Install a TAGGED release from origin as the runtime (m5-readiness M5-F). The launchd jobs run the
# release clone, never the dev tree, so an edit in a working copy cannot change live limits.
#
#   ops/install.sh <tag>                                   clone, pin, render the live plists (NOT loaded)
#   ops/install.sh <tag> --load [--track core|stocks]      also load them, only if doctor --ready is green
#   ops/install.sh <tag> --rehearsal [--load] [--publish local]
#                                                          the 48 h soak jobs instead (local remote only)
#
# Source of truth is origin: the tag is resolved with `git ls-remote` (peeled commit), the release is
# cloned from that URL (never from a dev checkout, with every stocks-* tag), its HEAD must equal the
# resolved commit, and the operator types the commit's first 8 characters on the terminal (/dev/tty:
# a pipe cannot answer, an agent without a TTY is refused). The release's `council ops
# assert-operator` must pass before anything is recorded, rendered or loaded. There is no override
# for a red readiness gate: fix it (rev 1's --force-load is gone).
set -eu
ORIGIN="https://github.com/fbzz/council-book.git"
LIVE_LABELS="com.fbzz.council.cycle com.fbzz.council.watch"
SOAK_LABELS="com.fbzz.council.rehearsal.cycle com.fbzz.council.rehearsal.watch"

usage() {
  echo "usage: ops/install.sh <tag> [--load [--track core|stocks] | --rehearsal [--load] [--publish local]]" >&2
  exit 64
}
die() { echo "install.sh: $*" >&2; exit 1; }

[ $# -ge 1 ] || usage
TAG="$1"; shift
case "$TAG" in ""|-*) usage ;; *[!A-Za-z0-9._-]*) die "bad tag name" ;; esac
LOAD=0; REHEARSAL=0; TRACK=core; TRACK_SET=0; PUBLISH_SET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --load) LOAD=1 ;;
    --rehearsal) REHEARSAL=1 ;;
    --track) [ $# -ge 2 ] || usage; TRACK="$2"; TRACK_SET=1; shift ;;
    --publish) [ $# -ge 2 ] || usage
               [ "$2" = local ] || die "the soak publishes to state_dir/rehearsal/remote.git only (--publish local)"
               PUBLISH_SET=1; shift ;;
    --force-load) die "--force-load was removed: a red readiness gate is fixed, not overridden" ;;
    *) usage ;;
  esac
  shift
done
case "$TRACK" in core|stocks) ;; *) die "--track must be core or stocks" ;; esac
if [ "$REHEARSAL" = 1 ] && [ "$TRACK_SET" = 1 ]; then die "--track applies to the live --load only"; fi
if [ "$REHEARSAL" = 0 ] && [ "$PUBLISH_SET" = 1 ]; then die "--publish applies to --rehearsal only"; fi
[ -z "${COUNCIL_STATE_DIR:-}" ] || die "unset COUNCIL_STATE_DIR: the launchd jobs use the default state dir"

STATE="$HOME/Library/Application Support/council-book"
REL="$STATE/releases/$TAG"
LOGS="$HOME/Library/Logs/council-book"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
TTY="${COUNCIL_INSTALL_TTY:-/dev/tty}"      # tests pass a pty slave; only a character device is accepted

loaded() {  # is any label in $1 loaded?
  for label in $1; do
    if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then return 0; fi
  done
  return 1
}

# ---- refusals that need nothing but launchctl and the terminal
if [ "$LOAD" = 1 ] && [ "$REHEARSAL" = 0 ] && loaded "$SOAK_LABELS"; then
  die "the rehearsal soak jobs are loaded; run ops/uninstall.sh --rehearsal first"
fi
if [ "$LOAD" = 1 ] && [ "$REHEARSAL" = 1 ] && loaded "$LIVE_LABELS"; then
  die "the live jobs are loaded; the soak runs before go-live only (ops/uninstall.sh first)"
fi
# the loaded live jobs run releases/current: repointing it without the live readiness gate would
# change the live code on their next run (a render-only or --rehearsal install must not do that)
if { [ "$LOAD" = 0 ] || [ "$REHEARSAL" = 1 ]; } && loaded "$LIVE_LABELS"; then
  die "the live jobs are loaded and run releases/current; use --load (the readiness gate) or ops/uninstall.sh first"
fi
# a rendered plist of the other kind would be started by launchd at the next login
if [ "$LOAD" = 1 ]; then
  if [ "$REHEARSAL" = 1 ]; then OTHER="$LIVE_LABELS"; else OTHER="$SOAK_LABELS"; fi
  for label in $OTHER; do
    [ ! -e "$AGENTS/$label.plist" ] || die "$label.plist is still in ~/Library/LaunchAgents; run ops/uninstall.sh first"
  done
fi
case "$TTY" in /dev/tty*|/dev/pts/*) ;; *) die "no terminal: run this in your own terminal" ;; esac
[ -c "$TTY" ] || die "no terminal: run this in your own terminal"

# ---- resolve the tag on origin (the peeled commit for an annotated tag)
REFS="$(git ls-remote "$ORIGIN" "refs/tags/$TAG" "refs/tags/$TAG^{}")" || die "cannot read tags from origin"
PEELED="$(printf '%s\n' "$REFS" | awk -v r="refs/tags/$TAG^{}" '$2 == r { print $1 }')"
DIRECT="$(printf '%s\n' "$REFS" | awk -v r="refs/tags/$TAG" '$2 == r { print $1 }')"
COMMIT="${PEELED:-$DIRECT}"
[ -n "$COMMIT" ] || die "tag $TAG is not on origin"
case "$COMMIT" in *[!0-9a-f]*) die "origin returned a malformed commit for $TAG" ;; esac
[ "${#COMMIT}" -eq 40 ] || die "origin returned a malformed commit for $TAG"

# ---- the release clone, from origin, with the stocks-* tags
mkdir -p "$STATE/releases" "$LOGS"
if [ ! -e "$REL" ]; then
  git clone --quiet --branch "$TAG" "$ORIGIN" "$REL" || die "cannot clone $TAG from origin"
fi
[ "$(git -C "$REL" config --get remote.origin.url)" = "$ORIGIN" ] \
  || die "$REL was not cloned from origin; remove it and run again"
git -C "$REL" fetch --quiet origin "refs/tags/stocks-*:refs/tags/stocks-*" \
  || die "cannot fetch the stocks-* tags into the release clone"
HEAD_COMMIT="$(git -C "$REL" rev-parse HEAD)" || die "cannot read the release clone's HEAD"
[ "$HEAD_COMMIT" = "$COMMIT" ] \
  || die "the release clone's HEAD $HEAD_COMMIT is not $TAG on origin ($COMMIT); remove $REL and run again"
[ -z "$(git -C "$REL" status --porcelain --untracked-files=normal)" ] || die "the release clone has local changes"

# ---- the operator confirms the commit on the terminal
PREFIX="$(printf '%s' "$COMMIT" | cut -c1-8)"
echo "release $TAG is commit $COMMIT"
{ printf 'type the first 8 characters of that commit to continue: ' >"$TTY"; } 2>/dev/null \
  || die "no terminal: run this in your own terminal"
ANSWER=""
{ IFS= read -r ANSWER <"$TTY"; } 2>/dev/null || die "no terminal: run this in your own terminal"
[ "$ANSWER" = "$PREFIX" ] || die "the typed prefix does not match; nothing installed"

(cd "$REL" && uv sync --frozen --no-dev --quiet) || die "uv sync failed in the release clone"
COUNCIL="$REL/.venv/bin/council"
"$COUNCIL" ops assert-operator || die "not the operator's own terminal (council ops assert-operator); nothing installed"

# ---- pin: releases/installed.json (0600) and releases/current
umask 077
PREV_CURRENT="$(readlink "$STATE/releases/current" 2>/dev/null || true)"
rm -f "$STATE/releases/.installed.json.prev"
if [ -f "$STATE/releases/installed.json" ]; then
  cp -p "$STATE/releases/installed.json" "$STATE/releases/.installed.json.prev"
fi
unpin() {  # a red live gate leaves the previous pin in place
  if [ -n "$PREV_CURRENT" ]; then ln -sfn "$PREV_CURRENT" "$STATE/releases/current"; else rm -f "$STATE/releases/current"; fi
  if [ -f "$STATE/releases/.installed.json.prev" ]; then
    mv -f "$STATE/releases/.installed.json.prev" "$STATE/releases/installed.json"
  else
    rm -f "$STATE/releases/installed.json"
  fi
}
TMP="$STATE/releases/.installed.json.$$"
printf '{"commit": "%s", "tag": "%s", "origin": "%s", "installed_at": "%s"}\n' \
  "$COMMIT" "$TAG" "$ORIGIN" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$TMP"
mv -f "$TMP" "$STATE/releases/installed.json"
ln -sfn "$REL" "$STATE/releases/current"
echo "pinned $TAG ($COMMIT) as releases/current"

# ---- render (templates come from the release, never from this checkout)
if [ "$REHEARSAL" = 1 ]; then LABELS="$SOAK_LABELS"; else LABELS="$LIVE_LABELS"; fi
mkdir -p "$AGENTS"
for label in $LABELS; do
  src="$REL/ops/launchd/$label.plist.tmpl"
  dst="$AGENTS/$label.plist"
  [ -f "$src" ] || die "the release has no $label template"
  sed -e "s|{{RELEASE}}|$STATE/releases/current|g" -e "s|{{HOME}}|$HOME|g" -e "s|{{LOGS}}|$LOGS|g" "$src" >"$dst"
  plutil -lint "$dst" >/dev/null || die "$dst does not lint"
  # a plist in LaunchAgents is started at the next login; keep it disabled until the gate passes
  launchctl disable "$DOMAIN/$label" 2>/dev/null || true
  echo "wrote $dst"
done

if [ "$LOAD" = 0 ]; then
  if [ "$REHEARSAL" = 1 ]; then
    echo "not loaded; run again with --rehearsal --load to start the 48 h soak"
  else
    echo "not loaded; run again with --load once doctor --ready --track $TRACK --post-token is green"
  fi
  exit 0
fi

# ---- gate, then load (bootout before bootstrap, so a reload is clean)
if [ "$REHEARSAL" = 0 ]; then
  if ! "$COUNCIL" doctor --ready --track "$TRACK" --post-token; then
    unpin
    die "doctor --ready --track $TRACK --post-token is not green; fix the red gates (there is no override)"
  fi
elif [ -f "$STATE/rehearsal/soak-log.jsonl" ]; then   # each soak is judged on its own window
  mv -f "$STATE/rehearsal/soak-log.jsonl" "$STATE/rehearsal/soak-log.$(date -u +%Y%m%dT%H%M%SZ).jsonl"
fi
for label in $LABELS; do
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl enable "$DOMAIN/$label" || die "launchctl enable $label failed"
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist" || die "launchctl bootstrap $label failed"
done
if [ "$REHEARSAL" = 1 ]; then
  echo "soak loaded (release $TAG); after 48 h: ops/uninstall.sh --rehearsal"
else
  echo "loaded (release $TAG, track $TRACK)"
fi
