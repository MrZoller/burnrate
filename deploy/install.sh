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

# Every effective setting comes from the package, not from a second copy of its
# rules written in shell. The installer used to keep whatever was in the
# environment, so a BURNRATE_PORT of "abc" or 99999 went into the plist and into the
# readiness URL while Config.from_env quietly rejected it and the agent listened on
# 8377 -- healthy service, installer probing a port nobody was on, wrong URL
# printed. The database path had the same split: absolutizing it here turned a
# quoted BURNRATE_DB='~/private/burnrate.db' into $PWD/~/private/burnrate.db while
# expanduser() gave $HOME/private/..., so the agent and a foreground run named
# different files. Asking the package makes them agree by construction instead of
# by my keeping two implementations in step -- and Python handles `~user`, which
# shell string-matching would not.
#
# launchd also starts the agent with none of this shell's environment, so whatever
# comes back here has to be baked into the plist or it is simply absent at runtime.
EFFECTIVE=$("$PYTHON" -m burnrate.config) \
  || die "could not read the effective configuration"
# IFS= on every read, or the shell strips leading and trailing whitespace from each
# line. A path legitimately ending in a space would then differ between the plist and
# a foreground run using the same environment -- which defeats the point of taking
# these values from the package in the first place.
{
  IFS= read -r DB
  IFS= read -r HOST
  IFS= read -r PORT
  IFS= read -r INTERVAL
} <<EOF
$EFFECTIVE
EOF
[ -n "$DB" ] && [ -n "$HOST" ] && [ -n "$PORT" ] && [ -n "$INTERVAL" ] \
  || die "incomplete configuration: $EFFECTIVE"

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

# Both URLs below come from the configured bind address. A wildcard bind answers
# anywhere, so it gets loopback to probe and the machine's short name to advertise.
# A specific interface -- a LAN or Tailscale address, an ordinary thing to set here
# -- answers only on itself: probing 127.0.0.1 failed for the full timeout and
# reported the service unhealthy when it was fine, and printing `hostname -s` for it
# advertised a URL that need not resolve to that interface at all.
case "$HOST" in
  0.0.0.0 | "::" | "" | "*")
    PROBE="127.0.0.1"
    ADVERTISE="$(hostname -s)"
    ;;
  *)
    PROBE="$HOST"
    ADVERTISE="$HOST"
    ;;
esac
# An IPv6 literal needs brackets in a URL; a hostname or IPv4 address must not
# have them. A colon is what separates the two cases.
url_for() { case "$1" in *:*) printf 'http://[%s]:%s' "$1" "$PORT" ;; *) printf 'http://%s:%s' "$1" "$PORT" ;; esac; }
PROBE_URL="$(url_for "$PROBE")"
PUBLIC_URL="$(url_for "$ADVERTISE")"

printf '\nInstalled %s\n' "$LABEL"
printf '  plist : %s\n' "$PLIST_DST"
printf '  db    : %s\n' "$DB"
printf '  poll  : every %ss\n' "$INTERVAL"
printf '  log   : %s\n' "$LOG"
printf '  url   : %s/\n' "$PUBLIC_URL"

# Classify the health endpoint as one of: healthy, unhealthy, foreign, down.
#
# Status alone cannot answer this. `curl -f` conflates the health check's own 503
# with a refused connection (exit 22 versus 7, both falsy), and dropping -f to
# separate them then accepts any completed response -- so a different HTTP service
# occupying this port passed for burnrate. Neither form is enough on its own: a
# catch-all 200 satisfies the -f test, and a 404 satisfies the other. So the body is
# checked for a field only our health endpoint emits, and the status is then read for
# what it means rather than merely for having arrived.
probe_health() {
  local response code body
  response=$(curl -sS --max-time 2 -w '\n%{http_code}' "$PROBE_URL/api/healthz" 2>/dev/null) \
    || { printf 'down'; return; }
  code=${response##*$'\n'}
  body=${response%$'\n'*}
  case "$body" in
    *'"poller_healthy"'*) ;;
    *) printf 'foreign'; return ;;
  esac
  case "$code" in
    200) printf 'healthy' ;;
    503) printf 'unhealthy' ;;
    *) printf 'foreign' ;;
  esac
}

report_not_ours() {
  printf 'error: something other than burnrate is serving on %s.\n' "$PROBE_URL" >&2
  printf '       The port is already in use, so the agent could not bind it.\n' >&2
  printf '       Free the port, or reinstall with BURNRATE_PORT set to another one.\n' >&2
}

report_dead() {
  printf 'error: nothing is listening on %s.\n' "$PROBE_URL" >&2
  printf '       The agent is loaded but not serving -- an address that cannot be\n' >&2
  printf '       bound, or a crash at startup.\n' >&2
  printf '\nlaunchd state:\n' >&2
  launchctl print "gui/$UID/$LABEL" 2>&1 | grep -E 'state|last exit|program' | head -5 >&2 || true
  printf '\nLast lines of %s:\n' "$LOG" >&2
  tail -5 "$LOG" 2>/dev/null >&2 || printf '  (no log yet)\n' >&2
}

printf '\nWaiting for the first poll'
for _ in $(seq 1 20); do
  case "$(probe_health)" in
    healthy)
      printf '\nUp and healthy.\n'
      exit 0
      ;;
    foreign)
      # Waiting will not help, and this is a diagnosis worth giving immediately.
      printf '\n'
      report_not_ours
      exit 1
      ;;
  esac
  printf '.'
  sleep 1
done

printf '\n'
case "$(probe_health)" in
  unhealthy)
    printf 'Serving, but the first poll has not succeeded yet.\n'
    printf 'This is expected on the very first run if macOS is waiting on a keychain\n'
    printf 'prompt. Check the dashboard banner and: tail -f %s\n' "$LOG"
    exit 0
    ;;
  healthy)
    printf 'Up and healthy.\n'
    exit 0
    ;;
  foreign)
    report_not_ours
    exit 1
    ;;
esac

report_dead
exit 1
