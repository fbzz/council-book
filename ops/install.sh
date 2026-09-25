#!/bin/sh
# Install a TAGGED release as the live runtime. The launchd jobs run the release checkout, never the
# dev tree, so an edit in the working copy cannot silently change live limits.
#   ops/install.sh <tag>            render plists into ~/Library/LaunchAgents (NOT loaded)
#   ops/install.sh <tag> --load     also load them (only after the Agent Portfolio token exists)
set -eu
TAG="${1:?usage: ops/install.sh <tag> [--load]}"
LOAD="${2:-}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$HOME/Library/Application Support/council-book"
REL="$STATE/releases/$TAG"
LOGS="$HOME/Library/Logs/council-book"
mkdir -p "$STATE/releases" "$LOGS"
git -C "$REPO" rev-parse -q --verify "refs/tags/$TAG" >/dev/null || { echo "unknown tag $TAG" >&2; exit 1; }
if [ ! -d "$REL" ]; then
  git clone --quiet --branch "$TAG" --depth 1 "$REPO" "$REL"
fi
(cd "$REL" && uv sync --frozen --no-dev --quiet)
ln -sfn "$REL" "$STATE/releases/current"
for name in cycle watch; do
  src="$REPO/ops/launchd/com.fbzz.council.$name.plist.tmpl"
  dst="$HOME/Library/LaunchAgents/com.fbzz.council.$name.plist"
  sed -e "s|{{RELEASE}}|$STATE/releases/current|g" -e "s|{{HOME}}|$HOME|g" -e "s|{{LOGS}}|$LOGS|g" "$src" > "$dst"
  plutil -lint "$dst" >/dev/null
  echo "wrote $dst"
done
if [ "$LOAD" = "--load" ]; then
  for name in cycle watch; do
    launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.fbzz.council.$name.plist"
  done
  echo "loaded (release $TAG)"
else
  echo "not loaded; run again with --load once the Agent Portfolio token exists"
fi
