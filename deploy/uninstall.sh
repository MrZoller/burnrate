#!/usr/bin/env bash
# Remove the burnrate LaunchAgent. Collected samples are left alone unless
# --purge is passed.
set -euo pipefail

LABEL="com.mrzoller.burnrate"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/burnrate.log"

# The database path install.sh actually used is recorded in the plist. Read it
# from there first: an install that set BURNRATE_DB baked that value in, and a
# later plain `./deploy/uninstall.sh --purge` would otherwise delete the default
# path, report success, and leave the real database and its WAL files on disk.
DB=""
if [ -f "$PLIST_DST" ]; then
  DB=$(plutil -extract EnvironmentVariables.BURNRATE_DB raw -o - "$PLIST_DST" 2>/dev/null || true)
fi
DB="${DB:-${BURNRATE_DB:-$HOME/.local/share/burnrate/burnrate.db}}"

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
