# burnrate — codebase map

A self-hosted dashboard for Claude Max plan usage. Two largely independent
subsystems live behind one FastAPI app:

1. **The meter** — polls Anthropic's *unofficial* `GET /api/oauth/usage`
   endpoint every 60s, stores bucket samples in SQLite, serves them over
   `/api/now` and `/api/history`, and projects when you'll hit a cap.
2. **Local attribution** — parses Claude Code's own session transcripts
   (`~/.claude/projects/**/*.jsonl`) into windowed token-usage panels, served
   over `/api/attribution`. Read-only, incremental, and explicitly a *proxy*,
   not the meter.

Everything is written to fail loudly rather than display a confident wrong
number, because the upstream data source is undocumented and will break someday.
That posture is the single most important design invariant in the repo — most of
the "buried bodies" below are places the code deliberately chose one side of that
trade-off.

---

## Entry points & lifecycle

- `src/burnrate/__main__.py:main()` — the `burnrate` console script. Configures
  logging, builds `Config.from_env()`, and hands `create_app(config)` to uvicorn.
  This is what `deploy/` runs under launchd (`python -m burnrate`, cwd = repo,
  env baked into the plist).
- `src/burnrate/app.py:create_app(config)` — the **only** way to build the app.
  There is deliberately **no module-level `app`**: importing the module must be
  side-effect free. `create_app` constructs `Store(config.db_path)` (which
  creates/opens the SQLite file) and `Poller(...)`, then registers a `lifespan`
  that starts the poller task and stops it on shutdown.
- Request handlers are sync `def`, not `async def` (comment at `app.py:39`): they
  do blocking SQLite reads, and Starlette runs sync handlers in a threadpool. An
  async handler would stall the event loop on a large history query.

The two JSON endpoints were deliberately `def` for the same reason the *poller*
runs its blocking reads on worker threads — see `poller.py:378` for the
`asyncio.to_thread(read_credential)` note.

---

## Core domain objects

| Object | Location | Notes |
|---|---|---|
| `Bucket` | `usage.py:80` | One rate-limit bucket, normalized across both upstream shapes (`limits[]` and top-level keys). Frozen. `.sort_key` orders session → weekly → other, known before unknown. |
| `UsageSnapshot` | `usage.py:98` | Everything parsed from one response: `buckets`, `warnings`, `notices`, `fetched_at`. `.weekly_primary` is the all-models weekly bucket the projection runs on. |
| `Projection` | `projection.py:71` | The pace forecast: status, rate, elapsed hours, window start, `hits_cap_at`, `hours_to_cap`. |
| `Pace` | `projection.py:275` | Per-bucket pace verdict + the time-elapsed bar's numbers (`elapsed_fraction`, `window_opened_at`). |
| `Credential` | `credentials.py:37` | `access_token` with `repr=False` (a frozen dataclass would otherwise print the secret in tracebacks), plus source and advisory expiry. |
| `Sample` | `store.py:129` | One stored bucket reading (ts, bucket, utilization, resets_at, label, known). |
| `Turn` | `attribution.py:65` | One assistant turn's token usage from a transcript line, normalized for aggregation. |
| `PollerStatus` | `poller.py:65` | Everything the UI needs to distinguish "live" from "broken": last success/error, failure count, next attempt, credential source, warnings/notices. |

---

## Data flow

### Meter path (fetch → parse → store → serve)

```
uvicorn (__main__) ──> Poller._run (background task)
   Poller.poll_once (every interval; survives any single failure):
     1. prune old rows (every 6h)
     2. aggregate local attribution (every 10 min, worker thread, before fetch)
     3. read_credential from scratch (worker thread; keychain then file)
     4. fetch_usage (GET api.anthropic.com/api/oauth/usage, Bearer + anthropic-beta)
         └─ on 401, re-read credential once; if unchanged -> record failure
         └─ payload scrubbed HERE with the exact token (only place that holds one)
     5. parse_usage -> UsageSnapshot (never raises; warns/notices on drift)
     6. store.append_snapshot(snapshot, raw_body) -> samples + deduped raw_snapshots
     7. poller.snapshot + status updated
   └─ any failure -> _record_failure (backoff doubles up to 900s; honors 429 Retry-After ≤1h)
```

`/api/now` (`app.py:66`) reads the **live** `poller.snapshot` when present, else
falls back to `store.latest_per_bucket()` (so a restart shows real numbers
immediately) — both carry their true age through `staleness_seconds`. It
computes `stale` from age vs `Config.stale_after_seconds` **plus**
`consecutive_failures > 0`, and anchors the projection at `reading_at` (the
sample's timestamp), never at wall-clock `now` — that separation (`now` vs
`reading_at` vs `stale`) is the heart of `projection.py` and is heavily
regression-tested.

`/api/history` (`app.py:102`) downsamples the window to ≤720 points per bucket
(keeps the **last** sample per slot so the reset sawtooth survives), anchored to
the query's own cutoff, not epoch boundaries.

### Attribution path (transcript tree → SQLite rollup → serve)

```
aggregate_jsonl (store.py, on a worker thread, every ~10 min):
  for each ~/.claude/projects/**/*.jsonl (sorted):
    read only bytes past the per-file watermark (read_new_lines, bounded 8MB chunks)
    parse_lines -> Turn objects (tolerant; malformed/skipped lines tallied)
    fold each turn into in-memory hourly_usage + sessions_rollup
  flush all three (hourly, sessions, watermarks) in ONE transaction
  └─ watermark advances in the same transaction as the sums -> exactly-once,
     crash-safe, no double counting
```

`/api/attribution` (`app.py:117`) reads only the pre-aggregated tables:
`attribution_totals` (windowed per-project/model/sidechain/large-context sums)
and `attribution_sessions` (longest sessions *active* in the window, each
carrying its **lifetime** token total — deliberately not a windowed share).
Project labels are derived at request time and disambiguated only where two cwd
basenames collide (`_project_display_names`, `app.py:333`).

---

## External dependencies & integration points

| Integration | Direction | Where | Notes |
|---|---|---|---|
| Anthropic usage endpoint | outbound (poll) | `client.py:USAGE_URL` | **Unofficial, no stability guarantee.** `anthropic-beta: oauth-2025-04-20`. This is the whole reason the app's posture is fail-loudly. |
| Claude Code OAuth credential | read-only | `credentials.py` | keychain (`security find-generic-password`, service `Claude Code-credentials`) then `~/.claude/.credentials.json`. Re-read on every poll. **Never refreshed here.** |
| Claude Code session transcripts | read-only | `attribution.py`, `store.py` | `~/.claude/projects/**/*.jsonl` (override `BURNRATE_PROJECTS_DIR`). Only assistant turns with usage are read; message bodies never touched. |
| SQLite (stdlib `sqlite3`) | local | `store.py` | WAL + `synchronous=NORMAL`, per-operation connections. |
| macOS launchd / LaunchAgent | deploy | `deploy/`, `plist.py` | install/uninstall scripts render the plist via `python -m burnrate.plist` (not `sed`). macOS-only; CI is Ubuntu. |
| macOS keychain (via `security` binary) | outbound | `credentials.py` | Can pop an interactive authorization prompt on first read. |

No other network or storage dependencies. Frontend is dependency-free vanilla
JS (no build step, no `package.json`).

---

## The unofficial endpoint: how the parser stays honest

`usage.py` reads buckets from **two** places and unions them:

- `limits[]` (primary, self-describing: `kind`, `percent`, `resets_at`,
  `scope.model.display_name`) — this is where the scoped weekly bucket (e.g.
  "Weekly (Fable)") lives today, and it is the only place it appears.
- top-level `{utilization, resets_at}` objects (fallback, fills anything
  `limits[]` missed, so a `limits` disappearance doesn't blank the dashboard).

Every unreadable-but-present value is a **warning** (drift → banner); every
`null` is silence (that's how the endpoint disables a limit). Unrecognized
buckets render under their raw key with a dashed border plus a notice, never a
warning. The raw body of every response (deduped by content) is archived in
`raw_snapshots` so a future schema change is diagnosable after the fact.

The README documents a completed investigation ("No promo / adjusted-cap field
is present", README.md:129-144): a promo/adjusted-cap field was hunted for across
every archived body and none exists, so the projection runs against the plain
100% cap with a documented dormant hook. The tests pin this
(`test_promo_and_overage_sections_are_ignored_by_the_parser`,
`test_a_hypothetical_promo_field_would_survive_archiving`).

---

## Security invariants

These are enforced by tests and must survive any change:

1. **The OAuth token never leaves the server.** Never in a response, never in
   the DB, never in logs. Test: `test_no_endpoint_leaks_the_token`
   (`test_store_and_api.py:447`) and the `test_a_credential_in_the_response_never_survives_parsing`
   family (`test_usage.py:132`).
2. **Scrubbing is centralized in `redact.py`** (`scrub`, `scrub_json`). Add new
   diagnostic paths *through* it, never around it. The exact token is scrubbed at
   the one point that holds it (`poller.py:_fetch_with_one_auth_retry`); the
   `sk-ant-` regex in `redact.py` is the backstop for credentials the code never
   held, and is deliberately broader than a parse.
3. **No refresh flow. Ever.** Claude Code owns the credential; a 401 means
   "stale", not "renew". Re-read on every poll picks up rotations automatically.
4. **The token is not in `Credential.__repr__`** (`credentials.py:47`,
   `field(repr=False)`).
5. **No authN on the dashboard itself** — the `0.0.0.0` bind + no auth is a
   deliberate, documented design (README). Don't add secrets to `/api/*`.

---

## Bodies are buried here (fragile / surprising / dead)

- **The captured fixture is absolute — seed it through `live_response_at`.**
  `tests/fixtures/live_response.json` hardcodes `resets_at`
  (`2026-08-08T23:30` five_hour, `2026-08-15T16:00` seven_day), and `project()`
  refuses to project once `now >= resets_at` (`projection.py:191`). Seeding a
  client with the capture verbatim therefore fails by calendar rather than by
  regression. 623cf88 fixed that: `live_response_at(fetched_at)` shifts every
  reset by `fetched_at - CAPTURED_AT` (anchored to the `NOW` the suite already
  declared), and `make_client` seeds from it. The old caution still holds in
  one direction — several tests pin behavior on the *specific* captured values
  (weekly util 14.0, `resets_at` starting `2026-08-15T16:00`,
  `test_usage.py:324`), so those keep reading `live_response` verbatim.
- **The amber "Ahead of pace" tier is provably unreachable.** `projection.py`
  keeps `AHEAD_OF_PACE` in `_classify_pace`, but under linear projection
  burn% > elapsed% ⟺ the pace crosses 100% before the reset, so no input ever
  produces it. Documented in `test_projection.py:441` as a faithful-but-dead
  rendering of the issue's tree; a *real* amber threshold is a planned follow-up.
  Colors are keyed on the status token, so wiring a real threshold later is a
  projection change + `app.js` `PACE` map.
- **`/api/now` has no exception handler.** `project()` and the bucket assembly
  run inside a plain `def now()` handler; an uncaught exception there is a 500
  for the whole dashboard until a later poll replaces the reading. The code's
  answer is defensive arithmetic everywhere (`_as_percent`, `_parse_timestamp`,
  `project`'s `try` around `window_start`), and the invariant "parse/project/
  poll must never raise" is tested directly (`test_hostile_payloads_never_raise`,
  `test_an_out_of_range_reset_costs_the_projection_not_the_page`). New code on
  this path must follow suit.
- **The poll loop must outlive any single failure.** `_schedule_next`,
  `poll_once`'s parser/store calls, `_maybe_prune`, `_maybe_aggregate`,
  `_archive_unreadable`, and `stop` all wrap exceptions (`noqa: BLE001`), because
  an escaping exception killed the background task in the past (overflow in
  backoff, overflow in the parser, a RecursionError from deeply-nested JSON,
  a far-future timestamp, an unrepresentable lone surrogate). Preserve this
  contract.
- **Aggregation is exactly-once via one transaction, and any flush failure is a
  permanent freeze.** The watermark advances in the same SQLite transaction as
  the sums, so a poison record that raises at flush rolls back *everything*
  including the offset → the next pass re-reads the same line and re-fails
  forever. That failure class is the reason for the per-field `MAX_TOKENS_PER_FIELD`
  cap, the `_utf8_safe` repair, and the `_parse_timestamp` guards in
  `attribution.py`. Don't "simplify" them.
- **`read_new_lines` assumes append-only JSONLs.** If a file is smaller than the
  recorded offset, it restarts from 0 (`attribution.py:228`); detecting a
  same-size replacement or truncate-then-append (which would double-count) is an
  **intentional omission**, documented inline. Fine as long as Claude Code keeps
  transcripts append-only.
- **The prettier hook silently no-ops.** `.claude/settings.json` formats `.py`
  with `ruff format` but `.js/.css` with `npx --no-install prettier` — and there
  is **no `package.json`**, so prettier is not pinned and won't be found unless
  installed globally; the hook swallows the failure (`2>/dev/null`). `app.js` is
  hand-formatted to prettier style (2-space, trailing commas, semicolons); keep
  it that way by hand.
- **Deploy scripts are macOS-only and dangerous to run casually** — they install
  into the real `~/Library/LaunchAgents/`. Tests exercise them by `source`-ing
  (guard at `install.sh:215`) with the install path no-oped, never by running
  them. Do not run `deploy/*.sh` during development.
- **Keychain prompt on first credential read.** `security` may require an
  interactive grant (rc 36) from a non-GUI context; burnrate falls back to
  `~/.claude/.credentials.json` and reports the source in the footer.

---

## Dead code / not-currently-reachable paths

- `AHEAD_OF_PACE` — see above.
- The `else` branch in `app.py:138-141` ("static directory missing") is
  `pragma: no cover` — only reachable when the package is installed wrongly.
- The `__main__` blocks in `config.py:188` and `plist.py:116` are
  `pragma: no cover` (exercised via subprocess in tests, and used by
  `deploy/install.sh`).
- The two fallback arms in `_parse_retry_after` (`poller.py:520` HTTP-date
  handling) exist but the delta-seconds form is the common case; both are tested.

## Test layout at a glance

`tests/` mirrors modules one file each: `test_usage`, `test_projection`,
`test_poller`, `test_store_and_api` (store + both JSON endpoints), `test_client`,
`test_credentials`, `test_redact`, `test_config`, `test_plist`, `test_attribution`,
plus `test_install` / `test_uninstall` which source the deploy scripts.
`conftest.py` provides the shared `live_response` fixture (the verbatim capture)
and `live_response_at(fetched_at)`, which rebases its resets onto a given clock. `asyncio_mode = "auto"`
means async tests need no decorator. Tests construct `Bucket`/`Turn` objects
directly and favor hostile inputs over happy paths — new tests should follow suit.
