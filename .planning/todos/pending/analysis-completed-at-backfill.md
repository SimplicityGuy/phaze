---
title: analyzed ⇒ analysis_completed_at — 1001 production rows will fail the shadow gate
created: 2026-07-10
severity: major
found_by: Phase 84 UAT (84-UAT.md, test 8)
owner: milestone / Phase 79 follow-up
blocks: "a green `just shadow-compare` after the first release carrying 032-035"
resolves_phase: null
---

# `analyzed` hard invariant will be red on the first deploy

Measured read-only against production (2026-07-10):

- The analyze stage is backed by table **`analysis`** (`AnalysisResult`), not `analysis_results`.
- `done_clause(Stage.ANALYZE)` requires `analysis.analysis_completed_at IS NOT NULL`
  (DERIV-03, `services/stage_status.py:123`).
- Production has **1050** files at `state='analyzed'`. All have an `analysis` row.
  Only **49** have `analysis_completed_at` set. **1001 have it NULL.**
  (1165 of 1214 total `analysis` rows are NULL.)
- `032` upserts `analysis.failed_at` for `analysis_failed` files only (`_BACKFILL_ANALYZE_FAILED`).
  **Nothing in `032`–`035` populates `analysis_completed_at`.**
- The `analyzed` invariant is HARD (`soft=False`) in `services/shadow_compare.py`'s registry.

So the first `just shadow-compare` after deploying `032`–`035` reports ~1001 divergences and exits 1.

**Not a Phase 84 defect.** Phase 84's own invariant (`duplicate_resolved`) is clean and has zero
exposure. This is a milestone-level data gap that Phase 79's deferred live-gate run (79 D-02) would
have surfaced — the same root cause as D-01.

## Options

1. **Backfill** `analysis.analysis_completed_at` from `updated_at` for `state='analyzed'` rows — a
   `036` data-only migration mirroring `032`'s analyze-failed upsert. Cheapest; makes the gate green.
2. **Reclassify** `analyzed` to the soft allowlist with a documented rationale (it would join
   `fingerprinted` and `local_analyzing`).
3. **Accept** a non-zero `hard_fail_total` until Phase 90 and gate only on named invariants.

Until settled, scope the post-deploy check to the invariant Phase 84 owns:
`duplicate_resolved: 0 divergent`.

## Reproduce (read-only)

```sql
BEGIN TRANSACTION READ ONLY;
SELECT count(*) FROM files f WHERE f.state='analyzed'
  AND NOT EXISTS (SELECT 1 FROM analysis a
                  WHERE a.file_id=f.id AND a.analysis_completed_at IS NOT NULL);
COMMIT;
```
