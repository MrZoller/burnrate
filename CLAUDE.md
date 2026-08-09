# burnrate

Self-hosted web dashboard for Claude Max plan usage.

## Commands
- Test: `uv run pytest`
- Run: `uv run burnrate` (or `uv run uvicorn burnrate.app:create_app --factory
  --host 0.0.0.0 --port 8377`)
- Lint/format: `uv run ruff check . && uv run ruff format .`

## Stack
- Python 3.12, FastAPI + uvicorn, uv-managed; SQLite sample store; static
  vanilla-JS frontend served from `/` with no build step.

## Conventions
- Conventional commits: `type(scope): summary`
- The OAuth token is server-side only. It must never reach the client, be
  logged, be written to the database, or appear in an API response.
- Never implement a token refresh flow. Claude Code owns refresh; we re-read
  the credential fresh on every poll and treat 401 as "stale", not "renew".
- `GET /api/oauth/usage` is an unofficial endpoint that will break someday.
  Parse it tolerantly — buckets are auto-discovered, any may be null or
  renamed. On failure the UI fails loudly (stale banner); it never shows a
  confident-looking wrong number.
- Type hints on public functions. Tests cover credential parsing, tolerant
  schema parsing, and projection math.
