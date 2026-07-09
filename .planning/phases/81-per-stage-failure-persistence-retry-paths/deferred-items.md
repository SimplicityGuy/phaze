# Phase 81 — Deferred / Out-of-Scope Items

Discoveries during execution that are OUT OF SCOPE for the touching plan (SCOPE BOUNDARY rule).
Not fixed here — logged for later triage.

## 81-01

- **`tests/shared/core/test_migration_019_dedupe.py::test_upgrade_019_dedupes_pending_and_creates_partial_unique_index`**
  fails when the full `shared` bucket runs but **passes in isolation** (verified `1 passed` standalone
  against the 5433 test DB). Migration-019 dedupe test — entirely unrelated to 81-01's
  `enums/stage.py` / `services/stage_status.py` changes. This is the known local full-suite /
  bucket-isolation flake (colima + shared migrations DB ordering). Out of scope for 81-01; left for the
  test-isolation hardening line. Not a regression from this plan.
