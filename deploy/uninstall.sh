#!/usr/bin/env bash
# Remove the burnrate LaunchAgent. Collected samples are left alone unless
# --purge is passed.
set -euo pipefail

LABEL="com.mrzoller.burnrate"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DB="${BURNRATE_DB:-$HOME/.local/share/burnrate/burnrate.db}"
LOG="$HOME/Library/Logs/burnrate.log"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  printf 'Stopped %s\n' "$LABEL"
else
  printf 'Not running.\n'
fi

if [ -f "$PLIST_DST" ]; then
  rm -f "$PLIST_DST"
  printf 'Removed %s\n' "$PLIST_DST"
fi

if [ "$PURGE" -eq 1 ]; then
  rm -f "$DB" "$DB-wal" "$DB-shm" "$LOG"
  printf 'Purged the sample database and log.\n'
else
  printf 'Kept sample history at %s (pass --purge to delete it).\n' "$DB"
fi
