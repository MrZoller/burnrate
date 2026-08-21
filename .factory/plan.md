# Plan: GitHub issue backlog

## Approach

Each open GitHub issue is an independently shippable task, with its issue body
serving as the source of scope and acceptance. Existing package boundaries stay
intact: attribution work remains in the parser/store/poller path, dashboard work
remains in the vanilla-JS static page, and deployment/reviewer configuration
remains isolated from application runtime code. Tasks with unresolved product
semantics or an unavailable external prerequisite remain blocked rather than
having acceptance invented. No dependencies are inferred from follow-up or
related-issue references; only explicit prerequisites would constrain selection.

## Tasks

- [!] T1 (standard) — Enable Claude reviews once the fixed workflow template lands (Fixes #28)
  - acceptance: copy the landed fixed workflow template wholesale into `.github/workflows/`; review same-repo PR content from a trusted `pull_request_target` definition while stripping workspace instructions and executable Claude configuration; restore trusted base instruction files at their original depths; publish an always-run `claude-code-review` status against the PR head SHA; verify the subscription token cannot be disclosed and the shepherd can detect the head-keyed verdict; blocked pending Q1
- [x] T2 (standard) — attribution: changing BURNRATE_PROJECTS_DIR leaves the old root's rollups mixed in for 30 days (follow-up to #16) (Fixes #25)
  - acceptance: namespace attribution rollups by normalized projects root; `Config`, persistence, incremental aggregation, and `/api/attribution` query only the active root while retaining prior-root history in its own namespace; migration and root-switch tests prove totals never mix and no prior-root data is cleared
  - pr: 35
- [~] T3 (standard) — attribution: cross-file duplicate assistant rows (session resume/fork/compaction) double-count token totals (follow-up to #16) (Fixes #24)
  - acceptance: identify duplicate assistant responses across transcript files, persist their identities with bounded retention, and make incremental aggregation skip already-counted responses across resumed/forked/compacted files; tests cover in-pass and later-pass duplicates without suppressing distinct responses
- [x] T4 (standard) — attribution: a non-UTF-8 filename freezes aggregation via the watermark/session-fallback binds (follow-up to #16) (Fixes #23)
  - acceptance: `aggregate_jsonl` commits a surrogate-bearing path and advances its watermark without `UnicodeEncodeError`; the session fallback is SQLite-bindable; a second unchanged pass reads no new rows or double-counts; tests cover both path-derived bind sites in `store.py`/`attribution.py`
  - pr: 29
- [x] T5 (standard) — poller: run attribution aggregation concurrently with the fetch so the first scan does not delay the usage meter (follow-up to #16) (Fixes #22)
  - acceptance: a slow first attribution scan does not delay the remote usage fetch in `poller.py`; aggregation still runs when fetching fails; tests preserve `now`/`fetched_at`, Retry-After, 429, and backoff behavior
  - pr: 31
- [x] T6 (standard) — attribution: surface aggregation freshness / health so a stalled aggregator does not serve stale rollups as current (follow-up to #16) (Fixes #21)
  - acceptance: the last successful aggregation time is exposed by `/api/attribution` and is not advanced by a failed aggregation; the static attribution panel labels when counts were generated and visibly reports a persistently failed/stale aggregator instead of presenting frozen rollups as current; poller/API tests cover success and failure, with the UI behavior manually verified
  - pr: 32
- [ ] T7 (standard) — dashboard: "Ahead of pace" (amber) pace tier is unreachable under linear projection (follow-up to #15) (Fixes #18)
  - acceptance: remove the amber/ahead tier from projection, API, and dashboard vocabulary while preserving green, red, and neutral behavior; tests prove no projection result or rendered status uses the removed tier
- [x] T8 (trivial) — dashboard: details-table status word is not staleness-aware (follow-up to #5) (Fixes #13)
  - acceptance: `renderTable` receives snapshot staleness on success and outage paths; stale rows retain numeric details but show no live Healthy/Watch/Critical judgment or live color carrier; regression coverage verifies the static-page wiring
  - pr: 33
- [x] T9 (trivial) — dashboard: refreshHistory() has the same newest-issued starvation as refresh() (#4) (Fixes #11)
  - acceptance: `refreshHistory()` applies any response newer than the last rendered history response even when a later request is already in flight, discards genuinely older successes and failures, and preserves range-change/error behavior; overlapping-request behavior is regression-verified in `src/burnrate/static/app.js`
  - pr: 34

## Risks

- T1 handles a long-lived subscription token in same-repository PR automation; any deviation from the trusted template or missing head-SHA status is a stop condition.
- T2 and T3 can silently discard or inflate historical attribution if their product semantics and migration behavior are not decided before implementation.
- T5 changes carefully ordered poll/backoff logic; regressions in fetch timing, 429 handling, or poll-loop survivability block shipment.
- The static page has no JavaScript test harness; tasks must keep frontend verification proportionate and must not introduce a build system solely for these fixes.

## Open questions

- Q1: which landed, trusted external workflow-template revision should T1 copy?
- Q2: should an attribution projects-root switch clear prior rollups, namespace them by root, or preserve/document the current mixed history?
- Q3: should response deduplication be persistent across incremental passes, limited and documented as in-pass-only, or explicitly accepted as a proxy limitation?
- Q4: what should amber pace mean, or should the unreachable tier be removed?

## Ad-hoc

- [ ] T10 (trivial) — parked review minors (batch)
  - acceptance: ship the accumulated non-blocking review minors as one focused, verified cleanup when the shepherd activates this rolling batch
  - #30: replace the `backslashreplace` SQLite path identity with an injective filesystem-byte encoding; Codex finding from PR #29, verifier-classified minor
  - PR #35: make projects-root identity encoding injective for a surrogate-bearing path versus a literal `\udcXX` path; Codex finding, verifier-classified minor
