# Phase 92 — Deferred Items

Out-of-scope discoveries logged during execution (per SCOPE BOUNDARY). These are NOT fixed by the plan
that found them; they are backlogged to the CURRENT milestone (2026.7.5) with the **92-05 full-suite
D-08 gate** as the close-out gate.

## DI-92-04-01 — `tests/agents/cli/test_agents_add.py` leaks committed agent rows (pre-existing, non-hermetic)

- **Found during:** 92-04 execution (running the `tests/agents` source bucket after the Option-B move).
- **Symptom:** In the combined `tests/agents` bucket, 5 `tests/agents/services/test_agent_bootstrap.py`
  cells fail (`ensure_dev_agent` returns `None` / "expected exactly one agent post-seed"). The bootstrap
  cells pass in isolation (6/6).
- **Root cause:** `test_agents_add.py` builds its OWN `create_async_engine(TEST_DATABASE_URL)` and commits
  agent rows (the CLI `agents add` path) that are never rolled back or cleaned up, so they survive into the
  later `test_agent_bootstrap.py` cells and make `ensure_dev_agent` believe the table is already seeded.
- **Pre-existing proof:** Reproduced identically at the base commit `be3d7687` (before any 92-04 continuation
  work): `tests/agents` shows 8 failures there — the 3 concurrency cells (RED, the reason 92-04 exists) PLUS
  the SAME 5 bootstrap failures. After the 92-04 move the count drops to 5 (the concurrency RED is gone); the
  5 bootstrap failures are untouched by 92-04's verify-site scope.
- **Why deferred:** This is a CLI-test engine-lifecycle leak, NOT a verify-session (`async_sessionmaker(async_engine)`)
  site and NOT one of the four Option-B donor files. It is a distinct hermeticity bug outside plan 92-04's scope.
- **Close-out gate:** 92-05 (full-suite D-08). Fix by giving `test_agents_add.py` a self-cleaning engine
  fixture (TRUNCATE/rollback its committed agents in teardown) or routing its writes through the hermetic
  `session`/`committed_db` fixtures.

## DI-92-04-02 — `tests/integration` bucket carries pre-existing red under a combined run

- **Found during:** 92-04 execution (running the full `tests/integration` bucket to validate the 8 moved cells).
- **Symptom (baseline, WITHOUT the 92-04 files):** `3 failed, 163 passed, 74 errors`.
  - `test_drain_double_dispatch.py` (3 cells) — seed via the hermetic `session` (uncommitted create_savepoint)
    but read via a fresh `async_engine` connection; broke under 92-03's create_savepoint conversion.
  - `test_lifespan_orphan_task.py::test_lifespan_launches_and_cleanly_cancels_orphan_task` — pre-existing.
  - `test_stage_status_equivalence.py` (74 errors) — full-suite ordering contamination; the file PASSES in
    isolation (59/59).
- **After 92-04:** `3 failed, 171 passed, 74 errors` — i.e. the SAME pre-existing red PLUS the 8 moved
  concurrency cells all green, and ZERO new failures. (An interim regression where the `committed_db`
  TRUNCATE wiped the session-scoped `test-fileserver` FK parent was fixed in commit
  `46d554c4` by re-seeding it at teardown.)
- **Why deferred:** All remaining red is pre-existing and independent of 92-04's verify-site/Option-B scope.
- **Close-out gate:** 92-05 (full-suite D-08) owns the green-the-integration-bucket work
  (`test_drain_double_dispatch` migration to committed seeds, the `test_stage_status_equivalence`
  ordering contamination, and `test_lifespan_orphan_task`).
