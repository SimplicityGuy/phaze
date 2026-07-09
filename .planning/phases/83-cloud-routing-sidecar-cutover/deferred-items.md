# Phase 83 — Deferred / Out-of-Scope Items

Discoveries logged during execution that are NOT this plan's responsibility. Do not fix here.

## 83-01

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
