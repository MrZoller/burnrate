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

Override defaults with `BURNRATE_DB`, `BURNRATE_HOST`, `BURNRATE_PORT`,
`BURNRATE_POLL_INTERVAL`, and `BURNRATE_PROJECTS_DIR` (the Claude Code transcript
tree the attribution section reads, default `~/.claude/projects`) — set them before
running `install.sh` and they get
baked into the plist, after the same validation the app itself applies — an
unusable port or interval falls back to the default rather than being installed
verbatim. `BURNRATE_DB` is expanded and made absolute (relative to where you run
`install.sh`), so the agent, a foreground run, and `uninstall.sh --purge` all
mean the same file.

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

### No promo / adjusted-cap field is present

Claude Code's `/usage` screen sometimes shows a promotional note under the weekly
bar ("+50% weekly limits promo through …"). We checked whether the same response
we already poll and archive carries a field for it — a promo, overage, bonus, or
adjusted-cap value alongside the buckets. Across every archived `raw_snapshots`
body, **it does not.** The keys that exist and were inspected are
`omelette_promotional` (always `null`), `extra_usage` and `spend` (the paid
overage-credit pool, not a cap boost), `limits[].group` / `limits[].is_active`,
and the per-bucket `*_dollars` fields — none of which encode an adjusted weekly
cap, and no weekly `percent` ever exceeds 100.

So there is nothing to surface and nothing to feed the projection. The pace math
runs against the plain 100% cap, and the hook for an adjusted cap stays **dormant
until such a field actually appears** in the response — at which point this note
is the place to start. We do not scrape the promo text from anywhere else.

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
| **On pace to hit the cap \<time\>** | At the current pace the line crosses 100% before the reset. This is the one to act on. |
| **On pace to clear the reset** | The projection lands past `resets_at` — the week resets before you run out. |
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

One radial gauge per bucket, colored by **pace** rather than raw level: how far
you have burned measured against how far into the window you are. Burning no
faster than the clock reads green (**On pace**); a pace projected to reach the cap
before the window resets reads red (**On pace to cap**). A window too new to judge,
or an unrecognized bucket, stays neutral (**Too early to tell** / **Unknown**) —
never a color-coded verdict. Each gauge pairs the color with the written pace
word, so the color is never the only signal. (An amber **Ahead of pace** tier also
exists in the code but is an unreachable boundary case under the current linear
projection — a real threshold for it is a planned follow-up.)

Under each gauge is a thin time-elapsed bar — window start at the left, reset at
the right, a marker at the reading — so percent-of-time-elapsed reads directly
against percent-burned. Below that, the window's span (open → reset) and a live
countdown to the reset.

### The charts

One area chart per bucket over the selected window (24h / 3d / 7d), drawn from
stored samples. They fill in as polling continues; a fresh install starts empty.
Hovering gives a crosshair and exact values, and the **Table view** at the bottom
carries the same numbers for anything that does not read well as a chart.

### What's burning tokens (local attribution)

A separate section, fed not by the usage endpoint but by parsing Claude Code's own
session transcripts under `~/.claude/projects/`. It answers "what is consuming
tokens on this machine" — by project, by model, main vs subagent, and the share of
tokens spent at large context (turns near the top of the 200k window) — all bounded
by a 24h or 7d toggle. It also lists the **longest sessions active in the window**;
because a session spans hours and crosses the window edge, those rows carry each
session's duration and its **lifetime** token total (labelled as such), not a
windowed percentage.

Read this as a **proxy, not the meter.** It counts local tokens, which is not the
same quantity the rate-limit bars above report, and the section says so on its face:

> This machine only — local token counts, not the usage meter. Not other devices or
> claude.ai.

The parser is read-only and tolerant: unknown fields are ignored, malformed lines
are skipped and counted, and a missing or half-written file never crashes a panel.
Because the transcript tree runs to hundreds of megabytes, it is **not** re-read on
every request — a background pass rolls new turns into SQLite (`hourly_usage`,
`sessions_rollup`) every ~10 minutes, reading only the bytes each file has grown by
since last time (a per-file byte-offset watermark), and the endpoint serves those
pre-rolled aggregates. Nothing from a message body is read or stored — only token
counts, model, working directory, session id, timestamp, and the sidechain flag.

---

## API

| Endpoint | Returns |
|---|---|
| `GET /api/now` | Latest reading per bucket, `staleness_seconds`, the projection, and poller status |
| `GET /api/history?hours=168` | Samples grouped into one series per bucket (1 ≤ hours ≤ 2160) |
| `GET /api/attribution?window=7d` | Local token attribution over the window (`24h` or `7d`): by project, model, main-vs-subagent, the windowed large-context share, and the longest sessions active in the window (lifetime totals) |
| `GET /api/healthz` | 200 when the last poll succeeded, 503 otherwise |

Polling is every 60s, backing off exponentially to a 15-minute ceiling while
failures persist, and resetting on the first success. Samples are kept 90 days,
raw response bodies 14, and local-attribution rollups 30.

---

## License

MIT — see [LICENSE](LICENSE).
