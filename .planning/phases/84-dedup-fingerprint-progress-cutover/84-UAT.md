---
status: complete
phase: 84-dedup-fingerprint-progress-cutover
source: [84-01-SUMMARY.md, 84-02-SUMMARY.md, 84-03-SUMMARY.md, 84-04-SUMMARY.md, 84-05-SUMMARY.md, 84-06-SUMMARY.md]
started: 2026-07-10
updated: 2026-07-10
method: executed by Claude against a fresh migrated database + the real ASGI app (not a manual checklist)
environment: phaze_uat_test on the ephemeral test Postgres (:5433); app booted via uvicorn on :8099
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test
expected: A virgin database migrates from zero to head, `035` applies, the derived-status tables exist, and the app boots and serves a health check.
result: pass
evidence: |
  `alembic upgrade head` on a freshly created `phaze_uat_test` ran 029 → 030 → 031 → 032 → 033 → 034 → 035
  with no errors. `alembic_version = 035`. `dedup_resolution` and `fingerprint_results` present.
  `uvicorn phaze.main:app` booted; `GET /health` → `{"status":"ok"}`.

### 2. `GET /api/v1/fingerprint/progress` derives from output tables
expected: Three keys. `total` counts music/video files only (a `.txt` is excluded). `completed` counts a file with any engine success. `failed` counts a FILE with all engines failed — once, not once per failed row (D-11).
result: pass
evidence: |
  Corpus: keeper.mp3, dup.mp3, ok.mp3 (1 engine success), none.mp4 (no rows), doc.txt (excluded), bad.mp3 (2 failed rows).
  Response: `{"total":5,"completed":1,"failed":1}`.
  `bad.mp3` carries TWO failed engine rows and contributes 1 to `failed` — the row-vs-file fix (D-11), observed end-to-end.
  `doc.txt` is excluded from `total` (D-10). No `FileRecord.state` is read anywhere in the path.

### 3. Duplicate group is visible in the operator UI
expected: A group of two files sharing a sha256 appears in the dedupe surface, driven by the new marker-existence predicate rather than `FileRecord.state`.
result: pass
evidence: |
  `GET /duplicates/` → 302 → `/s/dedupe`, which lists `keeper.mp3` and `dup.mp3`.
  The reader path (`find_duplicate_groups` → `~dedup_resolved_clause()`) works against a real DB with zero markers.

### 4. Resolving a duplicate group persists the marker and the state
expected: `POST /duplicates/{hash}/resolve` writes one `dedup_resolution` row (`canonical_file_id` = the operator's pick) and dual-writes `files.state = 'duplicate_resolved'`. Reloading the page no longer shows the group.
result: issue
reported: "POST returns HTTP 200 and the HTMX partial reports the group resolved, but the database has 0 markers and 0 duplicate_resolved files. A fresh page load still lists the group."
severity: blocker

### 5. Undo restores previous state and deletes the marker
expected: `POST /duplicates/{hash}/undo` DELETEs the marker and restores `previous_state` for the returned ids.
result: issue
reported: "POST returns HTTP 200 but the marker and the duplicate_resolved state are both unchanged. Same root cause as test 4."
severity: blocker

### 6. Shadow-compare gate is green after the cutover (SC#3)
expected: `python -m phaze.cli.shadow_compare --database-url <uat>` reports `duplicate_resolved: 0 divergent` and exits 0 on a corpus containing a real marker + `duplicate_resolved` pair.
result: pass
evidence: |
  All 14 HARD invariants `0 divergent`; both soft-allowlist invariants `0 divergent`.
  `TOTALS: hard_fail_total=0, soft_divergence_total=0`; exit code 0.
  Ran against a corpus where the marker and the state were both present — the pairing the writer must maintain.

### 7. Migration `035` is idempotent
expected: Re-running `alembic upgrade head` neither duplicates markers nor errors.
result: pass
evidence: "Second `upgrade head` was a no-op; `dedup_resolution` still holds exactly 1 row (not 2)."

### 8. Post-deploy shadow-compare prediction holds on the real corpus
expected: Per `84-06-SUMMARY.md`, the first release carrying `032`–`035` should yield `hard_fail_total = 0`, because `032` backfills the analyze rows for the 1050 `analyzed` and 429 `analysis_failed` files.
result: issue
reported: "The prediction is wrong. The analyze stage is backed by table `analysis` (not `analysis_results`, which does not exist). `done_clause(ANALYZE)` requires `analysis.analysis_completed_at IS NOT NULL`, and 1001 of the 1050 production `analyzed` files have that column NULL. Nothing in 032-035 backfills it (032 backfills `analysis.failed_at` only). So `hard_fail_total` will be ~1001, not 0, and `just shadow-compare` will exit 1 on the first deploy."
severity: major

## Summary

total: 8
passed: 5
issues: 3
pending: 0
skipped: 0
blocked: 0

## Gaps

```yaml
- truth: "POST /duplicates/{hash}/resolve persists the dedup marker and the dual-written FileRecord.state"
  status: failed
  reason: "User-observable: HTTP 200 + success partial, but 0 rows written. `get_session` (database.py:48-51) yields the session and never commits; `routers/duplicates.py` never calls `session.commit()`; `resolve_group` only `flush()`es per the caller-owned-transaction discipline. The transaction is rolled back when the session closes. Proven: calling `resolve_group` directly and issuing an explicit `commit()` writes 1 marker + 1 duplicate_resolved file, so the SELECT and the INSERT are both correct — only the commit is missing."
  severity: blocker
  test: 4
  artifacts: ["src/phaze/routers/duplicates.py", "src/phaze/database.py"]
  missing: ["await session.commit() in resolve_group_endpoint"]

- truth: "POST /duplicates/{hash}/undo DELETEs the marker and restores previous_state"
  status: failed
  reason: "Identical root cause to the resolve gap. `undo_resolve_endpoint` (:168), `bulk_resolve` (:198) and `bulk_undo` (:232) also never commit. All four write endpoints in this router are affected."
  severity: blocker
  test: 5
  artifacts: ["src/phaze/routers/duplicates.py"]
  missing: ["await session.commit() in undo_resolve_endpoint, bulk_resolve, bulk_undo"]

- truth: "The first release carrying 032-035 yields hard_fail_total = 0 (recorded in 84-06-SUMMARY.md)"
  status: failed
  reason: "Not a Phase 84 code defect, but a false claim in a committed Phase 84 artifact. The analyze stage is backed by `analysis`, not `analysis_results`. 1001 of 1050 production `analyzed` files have `analysis.analysis_completed_at IS NULL`, and `done_clause(ANALYZE)` requires it non-NULL (DERIV-03). `032` backfills `analysis.failed_at` for `analysis_failed` files only. The `analyzed` invariant is HARD (soft=False), so shadow-compare will exit 1 with ~1001 divergences on the first deploy. This is a milestone-level data gap that Phase 79's deferred live run would have caught — the same root cause as D-01."
  severity: major
  test: 8
  artifacts: [".planning/phases/84-dedup-fingerprint-progress-cutover/84-06-SUMMARY.md", "alembic/versions/032_add_derived_status_schema.py", "src/phaze/services/stage_status.py"]
  missing: ["corrected post-deploy prediction in 84-06-SUMMARY.md", "a decision on backfilling analysis.analysis_completed_at (owner: milestone / Phase 79 follow-up)"]
```

## Notes

The pre-existing commit bug explains an observation from the Phase 84 live-corpus probe that was
previously attributed to operator behaviour: production has **6 duplicate groups and 0
`duplicate_resolved` files**. The likeliest reading is not "nobody ever resolved a duplicate" but
"resolutions were clicked, appeared to succeed, and silently rolled back."

The bug predates Phase 84 — before this phase `resolve_group` dual-wrote `FileRecord.state` and also
only `flush()`ed, so the state change never persisted either. Phase 84 did not introduce it, but the
phase's own SC#1 ("dedup resolve/undo read/write the durable marker") is not achieved at the HTTP
layer, so it belongs to this phase's gap closure.

Both the planning docs and the code review asserted that "`routers/duplicates.py` relies on
`get_session` to commit" (84-CONTEXT `<code_context>`, D-02, and the `resolve_group` docstring). That
claim is false. It is the third fabricated fact found in this phase's artifacts, after the
`_test`-suffix destructive-write guard and the `analysis_results` table name.

The UAT database `phaze_uat_test` (port 5433) is left in place for fix verification.
