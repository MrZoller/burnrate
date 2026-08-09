#!/usr/bin/env bash
# Remove the burnrate LaunchAgent. Collected samples are left alone unless
# --purge is passed.
set -euo pipefail

LABEL="com.mrzoller.burnrate"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/burnrate.log"

# The database path install.sh actually used is recorded in the plist. A plain
# `./deploy/uninstall.sh --purge` uses that recorded value: an install that set
# BURNRATE_DB baked it in, and falling back to the default here would delete the
# default path, report success, and leave the real database and its WAL files on
# disk. An explicit BURNRATE_DB in the environment overrides the recorded value
# (precedence set below) -- the escape hatch for a plist that recorded an
# unusable path.
DB=""
EXTRACT_FAILED=0
if [ -f "$PLIST_DST" ]; then
  # `&& printf X`, not `; printf X`. The sentinel exists because `$(...)` strips ALL
  # trailing newlines while `plutil ... raw` appends one of its own -- for an ordinary
  # path those cancel out, but a path ending in a newline lost both, so --purge deleted
  # the shortened name and left the real database in place. Sequencing it with `;`
  # however made the substitution succeed unconditionally, which hid plutil's own
  # failure: a damaged plist or a missing key then produced a bogus path silently. It
  # is worse than falling back to the default, because plutil writes its error to
  # STDOUT -- so the captured "path" became the error text, which begins with the
  # plist's own absolute path and therefore passed the absolute-path guard below.
  # `--purge` then removed nothing and said it had. With `&&` the sentinel is only
  # written on success, so failure is a nonzero status here and an empty capture.
  if DB=$(plutil -extract EnvironmentVariables.BURNRATE_DB raw -o - "$PLIST_DST" 2>/dev/null \
            && printf X); then
    DB=${DB%X}
    DB=${DB%$'\n'}
  else
    DB=""
    EXTRACT_FAILED=1
  fi
fi
# Precedence: an explicit BURNRATE_DB wins over the recorded value, which wins over
# the default. A plain uninstall (BURNRATE_DB unset) still uses the recorded path;
# setting BURNRATE_DB is how the user overrides a bad or relative record -- and it
# must win here, or the guards below would keep recommending a command that the old
# `${DB:-...}` order silently ignored.
DB="${BURNRATE_DB:-${DB:-$HOME/.local/share/burnrate/burnrate.db}}"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

# A relative path here cannot be resolved safely. The agent resolved it against
# the plist's WorkingDirectory; we would resolve it against wherever this script
# was invoked from, which is a different file -- so purging would leave the real
# database in place and delete something else that happens to share the name.
# Installs since the absolute-path fix cannot produce this, but a plist written
# before it can still be sitting on disk. Refuse rather than guess.
# An explicit BURNRATE_DB has already overridden DB above, so an absolute one makes
# DB absolute and passes this guard -- the escape hatch the message below offers. A
# relative BURNRATE_DB still refuses, since it is no more resolvable than the record.
if [ "$PURGE" -eq 1 ] && [ "$EXTRACT_FAILED" -eq 1 ] && [ -z "${BURNRATE_DB:-}" ]; then
  printf 'error: %s exists but its BURNRATE_DB could not be read, so --purge\n' "$PLIST_DST" >&2
  printf '       does not know which database this install was using. Deleting the\n' >&2
  printf '       default path could remove an unrelated one and leave the real one\n' >&2
  printf '       behind. Pass BURNRATE_DB=/absolute/path to say explicitly.\n' >&2
  exit 1
fi

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
