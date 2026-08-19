# Questions

Open blockers for the human. Agents append per the `factory-protocol` skill;
answers go inline under each question after `**A:**` (or answer in chat via
`/blocked`). Entries are never deleted — reconciliation marks an applied or
forwarded answer `consumed` in the same bookkeeping commit.

---

## Q1 (task T1, open) — Which landed trusted workflow template should enable Claude reviews?
Context: Issue #28 makes the fixed `MrZoller/opencode-factory` template an external prerequisite, but names no landed revision this repository can fetch or verify. T1 must not reconstruct the security-sensitive workflow from prose.
Options considered: provide the exact landed template URL/commit and proceed / leave T1 blocked until that reference exists.
**A:**

## Q2 (task T2, open) — What are the acceptance semantics when BURNRATE_PROJECTS_DIR changes?
Context: Issue #25 establishes that old-root rollups remain visible, but deliberately leaves the desired behavior undecided. The choice changes persistence, migration, and history-retention behavior.
Options considered: clear old rollups on root change / namespace rollups by root and query only the active root / document and retain the current mixed-history behavior. Recommendation: namespace by root if preserving history matters; otherwise clear on change as the smaller honest behavior.
**A:**

## Q3 (task T3, open) — What level of cross-file response deduplication should attribution guarantee?
Context: Issue #24 proves additive cross-file double counting but leaves the durability/cost tradeoff open. In-pass-only deduplication does not cover the continuously running incremental case.
Options considered: persistent response-identity index with retention / explicitly limited in-pass-only deduplication / document and accept duplicate copies as part of the proxy model. Recommendation: persistent deduplication, because it is the only option that fixes the stated steady-state defect.
**A:**

## Q4 (task T7, open) — What should the amber pace tier mean?
Context: Issue #18 shows that the current linear model makes amber unreachable, so implementation needs a product threshold or a smaller status vocabulary. No threshold is justified by the issue body.
Options considered: define a margin above projected cap for red and use amber below it / define a different buffer model / remove amber and keep green-red-neutral. Recommendation: remove amber unless there is a user-backed warning threshold.
**A:**
