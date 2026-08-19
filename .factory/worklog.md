# Worklog

Append-only. One entry per task cycle or session, one bullet stamped
`- YYYY-MM-DD HH:MM UTC - ` (date and 24-hour clock time, UTC), then task id,
what happened, decisions and why, verification commands run, follow-ups.
Newest at the bottom.

---

- 2026-08-19 03:21 UTC - Imported 9 open GitHub issues with no label filter as T1-T9, added the rolling parked-minors batch T10, and recorded four issue-defined blockers as Q1-Q4. Set the external issue tracker as the approved spec and moved the new plan to its sole human approval gate. Verification: `gh issue list --state open --limit 1000 --json number,title,body,labels` returned 9 issues; no production tests run (bookkeeping-only import).
- 2026-08-19 03:25 UTC - Approved the populated plan with `spec_approved: true`; set `plan_approved: true` and phase `build` because runnable tasks remain. Verification: confirmed non-empty Approach, Tasks, Risks, and Open questions sections in `.factory/plan.md`; no production tests run (bookkeeping-only approval).
- 2026-08-19 03:37 UTC - T4 shipped as PR #29. Repaired both path-derived SQLite identities so a surrogate-bearing transcript path can commit its fallback session and watermark atomically; regression coverage proves an unchanged second pass begins at the committed offset and does not double-count. Verification: `uv run pytest` (492 passed), `uv run ruff check .` (passed), and `uv run ruff format --check .` (passed). Panel: 1 round; findings 0 blocking / 0 minor / 0 invalid; 0 fixed; shipped standard (planned standard).
