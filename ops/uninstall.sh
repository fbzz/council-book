#!/bin/sh
# Unload the council launchd jobs (keeps releases, state and logs).
set -eu
for name in cycle watch; do
  launchctl bootout "gui/$(id -u)/com.fbzz.council.$name" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.fbzz.council.$name.plist"
done
echo "council jobs unloaded"
