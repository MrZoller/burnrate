#!/usr/bin/env bash
# Install burnrate as a user LaunchAgent.
#
# Runs in the user's GUI session on purpose: reading the credential from the
# login keychain needs that session, and a LaunchDaemon would not have it.
set -euo pipefail

LABEL="com.mrzoller.burnrate"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO/deploy/$LABEL.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$REPO/.venv/bin/python"
LOG="$HOME/Library/Logs/burnrate.log"

HOST="${BURNRATE_HOST:-0.0.0.0}"
PORT="${BURNRATE_PORT:-8377}"
DB="${BURNRATE_DB:-$HOME/.local/share/burnrate/burnrate.db}"
# Absolute, always. A relative path is resolved by the agent against the plist's
# WorkingDirectory ($REPO) but by `uninstall.sh --purge` against whatever
# directory the uninstaller was run from -- so purge would miss the real database
# and could delete an unrelated file of the same name somewhere else. Anchoring
# it here to the invoking shell's cwd, which is what a user setting a relative
# BURNRATE_DB means, makes every consumer agree on one path.
case "$DB" in
  /*) ;;
  *) DB="$PWD/$DB" ;;
esac
# launchd starts the agent with none of this shell's environment, so anything not
# baked into the plist is simply absent at runtime. The README documents all four
# of these as install-time overrides; the interval was the one not being captured,
# so setting it did nothing and the agent quietly polled at the 60s default.
INTERVAL="${BURNRATE_POLL_INTERVAL:-60}"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "launchd is macOS-only; run 'uv run burnrate' directly elsewhere"
[ -f "$PLIST_SRC" ] || die "missing template: $PLIST_SRC"

if [ ! -x "$PYTHON" ]; then
  printf 'Creating virtualenv...\n'
  command -v uv >/dev/null || die "uv not found — install it, or create .venv yourself"
  (cd "$REPO" && uv sync --frozen 2>/dev/null || uv sync)
fi
[ -x "$PYTHON" ] || die "no interpreter at $PYTHON — run 'uv sync' in $REPO"

"$PYTHON" -c 'import burnrate' 2>/dev/null || die "burnrate is not importable — run 'uv sync' in $REPO"

mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")" "$(dirname "$DB")"

# Unload an existing copy first so this is safe to re-run after an edit.
if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
  printf 'Stopping the running agent...\n'
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
fi

# Rendered by the package, not by sed. In a sed replacement `&` means "the text
# that matched", so a path like /tmp/a&b.db silently became /tmp/a__DB__b.db --
# well-formed XML, so the lint below passed and the agent used the wrong
# database. `|` broke the expression and `<` produced invalid XML; those at least
# failed loudly. The values also need XML escaping, and layering both escapes by
# hand in shell is exactly where this goes wrong. Values are passed as argv, so
# the shell's quoting is authoritative and nothing is re-parsed.
"$PYTHON" -m burnrate.plist "$PLIST_SRC" "$PLIST_DST" \
  LABEL "$LABEL" \
  PYTHON "$PYTHON" \
  REPO "$REPO" \
  DB "$DB" \
  HOST "$HOST" \
  PORT "$PORT" \
  INTERVAL "$INTERVAL" \
  LOG "$LOG" || die "could not render the plist"

plutil -lint "$PLIST_DST" >/dev/null || die "generated plist is invalid: $PLIST_DST"

launchctl bootstrap "gui/$UID" "$PLIST_DST"
launchctl enable "gui/$UID/$LABEL"

printf '\nInstalled %s\n' "$LABEL"
printf '  plist : %s\n' "$PLIST_DST"
printf '  db    : %s\n' "$DB"
printf '  poll  : every %ss\n' "$INTERVAL"
printf '  log   : %s\n' "$LOG"
printf '  url   : http://%s:%s/\n' "$(hostname -s)" "$PORT"

printf '\nWaiting for the first poll'
for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/healthz" >/dev/null 2>&1; then
    printf '\nUp and healthy.\n'
    exit 0
  fi
  printf '.'
  sleep 1
done

printf '\n'
printf 'Serving, but the first poll has not succeeded yet.\n'
printf 'This is expected on the very first run if macOS is waiting on a keychain\n'
printf 'prompt. Check the dashboard banner and: tail -f %s\n' "$LOG"
