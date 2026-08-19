# burnrate

Self-hosted web dashboard for Claude Max plan usage. It polls the same
undocumented Anthropic endpoint Claude Code's `/usage` command uses, keeps the
history in SQLite, and serves a single dark vanilla-JS page showing where you
stand against each limit, the pace at which you are burning, a local
token-attribution section parsed from Claude Code's own session transcripts, and
live countdowns. Runs on `0.0.0.0:8377` so it is reachable from the LAN or over
Tailscale, usually as a macOS LaunchAgent.

## Commands

All verified on this machine (Python 3.12.12, ruff 0.16.2, fastapi 0.141.1):

- setup: `uv sync --frozen` (CI uses `uv sync --frozen`; the README says plain
  `uv sync`). A `uv run` boots the existing `.venv` automatically.
- test: `uv run pytest` — 491 passed, and clock-independent. The captured
  fixture's `resets_at` values are absolute, so `make_client` seeds the store
  through the `live_response_at(fetched_at)` fixture in `tests/conftest.py`,
  which shifts every reset by `fetched_at - CAPTURED_AT`. Put new
  projection/staleness tests on that fixture; `live_response` stays verbatim
  for parse-fidelity tests that assert the captured dates literally.
- lint: `uv run ruff check .` (selects E, F, I, UP, B; line-length 100)
- format: `uv run ruff format --check .` — `ruff format .` writes in place
  (this is what CLAUDE.md lists as the lint/format line; CI checks).
- run: `uv run burnrate` — or `uv run uvicorn burnrate.app:create_app --factory
  --host 0.0.0.0 --port 8377`. Entry point: `src/burnrate/__main__.py:main`.
  Verified booting, serving `/`, and completing a live poll against the API.
- deploy: `./deploy/install.sh` (renders a LaunchAgent plist via
  `python -m burnrate.plist` and bootstraps it), `./deploy/uninstall.sh [--purge]`.
  Shell syntax checked with `bash -n deploy/install.sh deploy/uninstall.sh`.
  These install into the user's real LaunchAgents — do not run them casually.

## Stack & layout

- Python `>=3.12`, FastAPI + uvicorn + httpx, all managed by **uv** (`uv.lock`
  committed). Runtime/formatting: ruff. No mypy; type hints are conventional but
  not enforced by CI.
- `src/burnrate/` — the package. Modules are deliberately small and each owns one
  concern:
  - `app.py` — FastAPI app factory (`create_app`, **no module-level `app`**);
    four endpoints plus the static mount at `/`.
  - `client.py` — httpx client for the unofficial usage endpoint; typed errors.
  - `poller.py` — the background loop (fetch → parse → store, backoff, Retry-After,
    prune, attribution rollup).
  - `usage.py` — tolerant parsing of the usage response into `Bucket`/`UsageSnapshot`.
  - `projection.py` — the pace projection (`project`, `pace_for`) and its statuses.
  - `store.py` — SQLite persistence: samples, `raw_snapshots`, the attribution
    rollup tables (`hourly_usage`, `sessions_rollup`, `jsonl_watermarks`), migrations.
  - `attribution.py` — read-only JSONL parser over Claude Code transcripts.
  - `credentials.py` — read Claude Code's stored OAuth token (keychain, then file).
  - `redact.py` — credential scrubbing, one choke point.
  - `config.py` — env-driven config; also a `python -m burnrate.config` entry used
    by the installer.
  - `plist.py` — plist rendering; also `python -m burnrate.plist`.
  - `static/` — `index.html`, `style.css`, `app.js`. Vanilla JS, ES modules,
    no build step, no dependencies.
- `tests/` — pytest, one file per module plus `test_store_and_api.py`;
  fixtures under `tests/fixtures/` (including a real captured
  `live_response.json` and JSONL transcripts). `deploy/install.sh` / `uninstall.sh`
  are exercised by sourcing them in tests (`test_install.py`, `test_uninstall.py`).
- `deploy/` — the LaunchAgent plist template and install/uninstall scripts.
- `.github/workflows/ci.yml` — Ubuntu CI: `uv sync --frozen` → `ruff check .` →
  `ruff format --check .` → `pytest -q`.

## Conventions

- Conventional commits `type(scope): summary` (see `git log`). PRs use the
  template in `.github/PULL_REQUEST_TEMPLATE.md` with a mandatory, copy-pasteable
  `## Verification` block.
- Type hints on public functions; module docstrings explain *why*; dense inline
  comments record the reasoning behind every non-obvious guard. Preserve that
  voice — many tests are named and commented as regressions for a specific bug.
- The OAuth token is server-side only. It must never reach the client, be logged,
  be written to the database, or appear in an API response. There is a test
  asserting exactly that. Scrubbing lives in `redact.py`; add paths through it,
  never around it.
- Never implement a token refresh flow. Claude Code owns refresh; we re-read the
  credential fresh on every poll and treat 401 as "stale", not "renew".
- `GET /api/oauth/usage` is an unofficial endpoint that will break someday. Parse
  it tolerantly — buckets are auto-discovered, any may be null or renamed. On
  failure the UI fails loudly (stale banner); it never shows a confident-looking
  wrong number. The parser reads `limits[]` first and top-level keys as fallback.
- Test idiom: plain pytest with `asyncio_mode = "auto"`; helpers construct
  `Bucket`/`Turn` objects directly; the `live_response` fixture is shared. Tests
  assert behavior under hostile inputs (huge ints, NaN, malformed JSON, past
  resets) — parse/project/poll functions must **never raise** (they run inside
  `/api/now` with no handler, or in the poll loop).
- `Config.from_env()` is the single source of truth for settings, including for
  the installer — do not duplicate validation rules in shell. New config fields
  are appended at the end of `print_effective`'s output order (pinned by a test).
- SQLite: per-operation connections (thread-safety between poller and handlers),
  WAL mode, `PRAGMA synchronous=NORMAL`.

## Factory

This repo is on the software-factory line. Load the `factory-protocol` skill
before factory work and keep durable task state in `.factory/` (spec, plan,
worklog, questions).

## Gotchas

- **The captured fixture is absolute — seed it through `live_response_at`.**
  `tests/fixtures/live_response.json` is a real response whose `resets_at`
  values are fixed dates (`2026-08-08` / `2026-08-15`), and `project()` refuses
  to project once `now >= resets_at`. Seeding a client with it verbatim
  therefore rots into failures by calendar rather than by regression, which is
  what happened before 623cf88 — that commit added the
  `live_response_at(fetched_at)` conftest fixture and switched `make_client` to
  it. Anything needing a *live* window goes through that fixture; anything
  asserting the capture's literal values (`test_usage.py:324`) keeps reading
  `live_response`.
- Do not change production code while onboarding. Both new files (`AGENTS.md`,
  `docs/codebase-map.md`) are additive.
- There is **no module-level `app`**: building one at import creates a database
  and a poller as a side effect (this polluted `$HOME` on every test run). Always
  serve through `create_app`.
- Poller behavior is load-bearing: backoff caps at `max(900s, configured
  interval)` — never more aggressive than normal polling, which a flat 900s cap
  got backwards for any interval above it — and a 429's `Retry-After` wins
  whenever it is larger, including past that ceiling (the header itself is
  clamped to 1h). Prunes every 6h, rolls up attribution every 10 minutes. Anything that could throw is wrapped — the poll loop must outlive any
  single failure (see the repeated `noqa: BLE001` comments).
- The API binds `0.0.0.0` by default and is served from any LAN/Tailscale
  client; there is no auth on the dashboard itself. The exposure is the point
  (README), but there is no authN on `/api/*` — do not add secrets there.
- `.claude/settings.json` hooks a PostToolUse formatter: `ruff format` on edited
  `.py`, `npx --no-install prettier --write` on `.js/.css`. There is **no
  `package.json`**, so prettier is not pinned and the JS/CSS hook silently
  no-ops (`2>/dev/null`) unless a prettier happens to be installed globally.
  `app.js` is hand-formatted to prettier style (2-space, trailing commas).
- Env overrides: `BURNRATE_DB`, `BURNRATE_HOST`, `BURNRATE_PORT`,
  `BURNRATE_POLL_INTERVAL`, `BURNRATE_PROJECTS_DIR`. An unusable port/interval
  falls back to the default rather than failing. `BURNRATE_DB` and
  `BURNRATE_PROJECTS_DIR` are expanded and made absolute.
- First credential read on a machine may pop a macOS keychain authorization
  prompt; burnrate falls back to `~/.claude/.credentials.json` and shows which
  source is live in the footer.
- CI runs on Ubuntu but the deploy scripts and the keychain path are macOS-only;
  the app itself is portable (attribution + polling paths are platform-neutral).
