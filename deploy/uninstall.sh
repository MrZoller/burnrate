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
  # Captured through a sentinel, because `$(...)` strips ALL trailing newlines and
  # `plutil ... raw` appends one of its own. For an ordinary path those cancel out; for
  # a path that itself ends in a newline both went, so --purge deleted the shortened
  # name -- possibly an unrelated database -- and left the real one in place while
  # reporting success. The X survives the strip, and removing it leaves the bytes
  # exactly as plutil wrote them; then exactly one newline comes off, which is
  # plutil's terminator and not part of the value.
  DB=$(plutil -extract EnvironmentVariables.BURNRATE_DB raw -o - "$PLIST_DST" 2>/dev/null; printf X) || true
  DB=${DB%X}
  DB=${DB%$'\n'}
fi
DB="${DB:-${BURNRATE_DB:-$HOME/.local/share/burnrate/burnrate.db}}"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

# A relative path here cannot be resolved safely. The agent resolved it against
# the plist's WorkingDirectory; we would resolve it against wherever this script
# was invoked from, which is a different file -- so purging would leave the real
# database in place and delete something else that happens to share the name.
# Installs since the absolute-path fix cannot produce this, but a plist written
# before it can still be sitting on disk. Refuse rather than guess.
if [ "$PURGE" -eq 1 ]; then
  case "$DB" in
    /*) ;;
    *)
      printf 'error: the recorded database path is relative (%s), so --purge\n' "$DB" >&2
      printf '       cannot tell which file it means. Delete it by hand, or pass\n' >&2
      printf '       BURNRATE_DB=/absolute/path to say explicitly.\n' >&2
      exit 1
      ;;
  esac
fi

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
