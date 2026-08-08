# burnrate

A self-hosted dashboard for Claude Max plan usage. It polls the same endpoint
Claude Code's `/usage` command uses, keeps the history in SQLite, and serves a
single dark page showing where you are against each limit and where your current
pace lands.

Runs as a macOS LaunchAgent on `0.0.0.0:8377`, so it is reachable from the LAN or
over Tailscale but is not exposed publicly.

---

## Setup

```bash
git clone https://github.com/MrZoller/burnrate.git
cd burnrate
uv sync
```

Run it in the foreground to check it works:

```bash
uv run burnrate          # http://127.0.0.1:8377/
```

Install it as a background service:

```bash
./deploy/install.sh
```

That renders a LaunchAgent plist into `~/Library/LaunchAgents/`, bootstraps it
into your GUI session, and waits for the first successful poll. To remove it:

```bash
./deploy/uninstall.sh            # keeps collected history
./deploy/uninstall.sh --purge    # deletes the database and log too
```

| Path | What |
|---|---|
| `~/.local/share/burnrate/burnrate.db` | sample history |
| `~/Library/Logs/burnrate.log` | service log |
| `~/Library/LaunchAgents/com.mrzoller.burnrate.plist` | the agent |

Override defaults with `BURNRATE_DB`, `BURNRATE_HOST`, `BURNRATE_PORT`, and
`BURNRATE_POLL_INTERVAL` — set them before running `install.sh` and they get
baked into the plist. A relative `BURNRATE_DB` is resolved against the directory
you run `install.sh` from and stored absolute, so `uninstall.sh --purge` later
deletes the same file the agent was writing.

### Tests

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

---

## How the token is handled

burnrate never authenticates on its own. It reads the credential Claude Code
already stores, and that is the entire auth story:

1. macOS Keychain — generic password, service `Claude Code-credentials`
2. `~/.claude/.credentials.json` — `.claudeAiOauth.accessToken`

The credential is re-read **from scratch on every poll**, so when Claude Code
refreshes the token the next poll picks it up with no coordination. There is
deliberately **no refresh flow here**. On a 401 burnrate re-reads once (covering
the case where the token rotated mid-request) and, if the value has not changed,
marks the data stale and says so. It will never try to mint or renew a token.

The token stays server-side. It is never logged, never written to the database,
and never included in any API response — there is a test asserting exactly that.

> **First run may need a keychain click.** Reading a secret from the login
> keychain requires an interactive grant. On the machine this was built on,
> `security find-generic-password -w` returns rc 36 (authorization required)
> from a non-GUI context, and burnrate silently falls back to the credentials
> file. If you want the keychain path used, approve the prompt when macOS shows
> it. The file fallback works either way; the footer shows which source is live.

---

## The data source is unofficial

`GET https://api.anthropic.com/api/oauth/usage`, with `Authorization: Bearer …`
and `anthropic-beta: oauth-2025-04-20`.

This endpoint is undocumented and carries no stability guarantee. **It will break
someday.** The design assumption throughout is that a wrong number is worse than
a visibly missing one, so every failure path is loud:

- A failed fetch keeps the last good reading on screen but raises a banner naming
  the error and the age of the data.
- Anything older than 180s is marked stale, banner and all.
- A 200 whose body yields no readable buckets counts as a **failure**, not as a
  successful poll with nothing in it.
- Buckets are discovered, not hardcoded. An unrecognized one renders under its
  raw key with a dashed border rather than being dropped.
- Every raw response body is archived (deduplicated) in the `raw_snapshots`
  table, so when the shape does change you can see exactly what changed.

### What the response actually looks like

Worth knowing, because it is not what you would guess. Buckets appear in two
places and the interesting one is only in the second:

| Source | Contents |
|---|---|
| Top-level keys | `five_hour`, `seven_day`, plus a dozen mostly-`null` keys including `seven_day_opus`, `seven_day_sonnet`, and assorted codenames |
| `limits[]` | self-describing entries with `kind`, `percent`, `resets_at`, and — for `weekly_scoped` — the model name under `scope.model.display_name` |

On the account this was developed against, `seven_day_opus` is `null` while
`limits[]` carries a `weekly_scoped` entry for **Fable**. A parser reading only
the top-level keys silently drops a bucket that is actively being burned. So
`limits[]` is the primary source, top-level keys fill in whatever it does not
cover, and the scoped bucket's label is taken from the response at runtime — it
reads "Weekly (Fable)" today and follows the account if that scope changes,
with no code change.

---

## Reading the projection

The hero line at the top answers one question: **at the rate you have been
burning this week, do you run out before the week resets?**

The math is deliberately the simplest thing that can be stated in one line:

```
rate         = utilization ÷ hours since the weekly period opened
hits the cap = now + (100 − utilization) ÷ rate
```

The window opens at `resets_at − 7 days`. That is an **average since the reset**,
not a recent-velocity estimate — one heavy afternoon keeps pulling the line up
for the rest of the week, and an idle day drags it down slowly.

It reports one of:

| Reading | Meaning |
|---|---|
| **Cap at \<time\>** | The line crosses 100% before the reset. This is the one to act on. |
| **Clears the reset** | The projection lands past `resets_at` — the week resets before you run out. |
| **Too early to project** | Less than 30 minutes since the reset. |
| **No usage yet** | Zero utilization this period. |
| **Weekly cap reached** | Already at 100%. |
| **Projection unavailable** | No weekly bucket, or no reset time in the response. |

The 30-minute floor matters more than it looks. Minutes after a reset the
denominator is tiny, so ten minutes of work projects to "cap in three hours."
burnrate refuses to project rather than print an alarming number built on noise.

**Treat this as a trend line, not a forecast.** It assumes you keep working at
the same average pace, which nobody does. It is useful for "am I on track to run
out midweek?" and not for anything more precise.

### The gauges

One radial gauge per bucket, colored by threshold — green under 70%, amber
70–89%, red at 90%+ — each with a written state label beside it so the color is
never the only signal. Below each, a live countdown to that bucket's reset.

### The charts

One area chart per bucket over the selected window (24h / 3d / 7d), drawn from
stored samples. They fill in as polling continues; a fresh install starts empty.
Hovering gives a crosshair and exact values, and the **Table view** at the bottom
carries the same numbers for anything that does not read well as a chart.

---

## API

| Endpoint | Returns |
|---|---|
| `GET /api/now` | Latest reading per bucket, `staleness_seconds`, the projection, and poller status |
| `GET /api/history?hours=168` | Samples grouped into one series per bucket (1 ≤ hours ≤ 2160) |
| `GET /api/healthz` | 200 when the last poll succeeded, 503 otherwise |

Polling is every 60s, backing off exponentially to a 15-minute ceiling while
failures persist, and resetting on the first success. Samples are kept 90 days,
raw response bodies 14.

---

## License

MIT — see [LICENSE](LICENSE).
