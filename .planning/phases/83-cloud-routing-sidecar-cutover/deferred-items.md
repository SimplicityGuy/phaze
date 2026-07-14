> **RESOLVED 2026-07-14** (Phase 92 post-audit debt paydown — operator-directed). See `.planning/2026.7.5-MILESTONE-AUDIT.md` `debt_paydown_2026_07_14` + STATE.md Decisions.

# Phase 83 — Deferred / Out-of-Scope Items

Discoveries logged during execution that are NOT the discovering plan's responsibility.
Scope boundary: only auto-fix issues directly caused by the current task's changes.

Both entries below are instances of the same pre-existing defect class — non-hermetic,
order-dependent test setup against the shared `phaze_test` database — surfaced independently
in two different buckets. A single hygiene task should address both.

## 83-01 — `analyze` bucket order-dependent non-hermetic pollution (pre-existing)

- **Order-dependent non-hermetic pollution in the `analyze` bucket.** Running the whole
  `tests/analyze` bucket under pytest-randomly's random ordering produces a variable number of
  setup ERRORs + occasional FAILUREs concentrated in `test_recovery.py`,
  `test_release_awaiting_cloud.py`, and `test_submit_cloud_job.py` (177 errors under one seed, 55
  under another). Each of these files passes cleanly IN ISOLATION, and the whole bucket passes
  **523 passed / 0 failed / 0 errors** with deterministic ordering (`-p no:randomly`). This is the
  pre-existing "colima full-suite flake" / "CI bucket test-isolation" class already recorded in
  project memory — not introduced by 83-01 (which only adds an additive `hold_awaiting_cloud`
  helper + four new hermetic tests). Out of scope for this plan; the non-hermetic seeding in those
  three DB-heavy files should be hardened by whoever owns them.

## 83-03 — `agents` bucket non-hermetic test-isolation flake (pre-existing)

- **Found during:** Plan 83-03, Task 2 (`just test-bucket agents` in isolation).
- **Symptom:** Running the full `tests/agents` bucket produces `IntegrityError: duplicate
  key value violates unique constraint "pk_agents"` errors during test setup. The count and
  which tests error varies by run/ordering (observed 1, 12, and 14 errors across runs). Every
  affected test — including this plan's 4 new reaper tests — **passes in isolation**.
- **Root cause (pre-existing):** `tests/conftest.py` `async_engine` is function-scoped
  (`create_all`/`drop_all` per test) but all tests share the single `phaze_test` database, and
  `seed_test_agent` commits an Agent with a fixed id (`test-agent-01`). Under local colima VM
  pressure the per-test async teardown ordering flakes, leaving committed agent rows behind for
  the next test's `seed_test_agent` commit to collide with. This is the documented
  "local full-suite colima flake" / "CI bucket test-isolation" behavior.
- **Proof it is NOT caused by 83-03:**
  - The plan's source change (`agent_analysis.py` reaper) touches only `cloud_job` — **zero**
    references to the `agents` table (the constraint that violates is `pk_agents`).
  - Deselecting the 4 new reaper tests still reproduces the errors (12 errors observed).
  - The 4 new reaper tests pass in isolation (`-k "reaps or leaves_succeeded or leaves_running"`
    → 4 passed) and passed within the default full-bucket run (444 passed, 1 unrelated error).
- **Disposition:** DEFERRED — pre-existing test-infra hermeticity defect, out of scope for the
  D-14 reaper plan. A future hygiene task should make the agents bucket hermetic (e.g. truncate
  shared tables per test, or a session-scoped engine with per-test transactional rollback).

## 83-06 — backfill-held COMPUTE files carry a `process_file` recovery-ledger row the new drain conjunct now excludes

- **Found during:** Plan 83-06, Task 2 (drain cutover), while reconciling the existing
  `tests/shared/routers/test_pipeline.py` backfill tests against `~inflight_clause(ANALYZE)`.
- **Interaction:** `trigger_backfill_cloud` (`src/phaze/routers/pipeline.py:861-876`) HOLDS a long
  ANALYSIS_FAILED file in `AWAITING_CLOUD` (via `hold_awaiting_cloud` -> an `awaiting` cloud_job row)
  **and**, for the **compute** target only, also seeds a `process_file:<id>` scheduling-ledger row so
  `recover_orphaned_work` can replay it (the k8s branch deliberately SKIPS this seed, `:842-845`). The
  Phase-83 drain cutover (D-05) now excludes any file with a `process_file` ledger row via
  `~inflight_clause(ANALYZE)`. Pre-83 the drain read `state == AWAITING_CLOUD` and therefore DID pick
  up such a backfill-held compute file; post-83 it does not.
- **Effect:** a backfill-held **compute** file is skipped by `stage_cloud_window` (its recovery-seed
  ledger row reads as "analyze in flight") and instead falls to LOCAL analysis via
  `recover_orphaned_work` — i.e. mis-routed off the compute backend, NOT stranded forever. The
  `AWAITING_CLOUD` hold + the retained awaiting cloud_job row are otherwise intact. (The k8s backfill
  path is unaffected — it seeds no ledger row.)
- **Why out of scope for 83-06:** `routers/pipeline.py` (the backfill endpoint) is NOT in this plan's
  `files_modified`, and D-05 (`~inflight_clause(ANALYZE)` verbatim) is a LOCKED decision. Aligning the
  backfill compute path (e.g. drop the `process_file` ledger seed for compute, mirroring the k8s
  branch, and rely on the `cloud_job` awaiting row as the in-flight registry) is an architectural
  change spanning unowned code + the recovery contract (`recover_orphaned_work`) — a Rule-4 decision
  for the phase owner / milestone audit, not an auto-fix.
- **Not covered by any test:** no test exercises backfill -> `stage_cloud_window` dispatch, so the
  suite stays green; this is a latent behavioral interaction, flagged here for the verifier.
- **Disposition:** DEFERRED — assign to the backfill/recovery owner (or the Phase-90 cutover) to
  decide whether the compute backfill should stop seeding the `process_file` ledger row.
