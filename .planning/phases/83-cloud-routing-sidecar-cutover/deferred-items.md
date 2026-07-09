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
