# Phase 81 — Deferred / Out-of-Scope Items

Discoveries during execution that are OUT OF SCOPE for the touching plan (SCOPE BOUNDARY rule).
Not fixed here — logged for later triage.

## 81-01

- ~~**`tests/shared/core/test_migration_019_dedupe.py::test_upgrade_019_dedupes_pending_and_creates_partial_unique_index`**
  fails when the full `shared` bucket runs but passes in isolation — known colima/bucket-isolation flake.~~
  **RETRACTED by the orchestrator at the wave-1 post-merge gate.** The original diagnosis was wrong on
  both counts: the test fails *in isolation too*, and it is not a flake. Root cause is environmental —
  `MIGRATIONS_TEST_DATABASE_URL` (`tests/integration/test_migrations/conftest.py:35-37`) defaults to port
  **5432**, but `just test-db` provisions the ephemeral Postgres on **5433**. `just test-bucket` does not
  export it; CI sets it externally. Exporting
  `MIGRATIONS_TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test`
  makes the test pass standalone (`2 passed`) and the whole `shared` bucket green (`939 passed`).
  Not a regression from 81-01 — that part of the original note stands.

  Residual (genuinely deferred): the 5432 default is a footgun for local runs. Either point the default at
  5433 to match `just test-db`, or have the `test-bucket` recipe export it. Left for the
  test-isolation hardening line.
