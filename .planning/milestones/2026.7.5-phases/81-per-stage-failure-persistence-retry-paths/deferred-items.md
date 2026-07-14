> **RESOLVED 2026-07-14** (Phase 92 post-audit debt paydown — operator-directed). See `.planning/2026.7.5-MILESTONE-AUDIT.md` `debt_paydown_2026_07_14` + STATE.md Decisions.

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

## Code review (81-REVIEW.md) — warnings left OPEN

Both criticals (CR-01, CR-02) were fixed before phase close. These two warnings were not, and both are
real. They are recorded here because each is a live hole that Phase 80's reader cutover will walk into.

> **Bookkeeping correction (2026-07-09, security audit).** WR-03 was present in `81-REVIEW.md` but was
> omitted from this file when the review outcome was first recorded — an orchestrator error, caught by
> `gsd-security-auditor`. It has since been **FIXED** in `feaebc48`: `eligible()` / `domain_completed()`
> compared `Status` (a StrEnum) with `is`, so a raw-string status map — exactly what a SQL round-trip
> yields, since `stage_status_case` emits `Status.X.value` — made `eligible({ANALYZE: "failed"}, ANALYZE)`
> return `True`, reporting a terminally-failed analyze as eligible (the 44.5K over-enqueue class). Now
> coerced through `Status(...)` and compared by value; an unrecognised status raises. 21 new cells.
>
> The security audit also found and fixed a threat-register gap: the PG-invalid (NUL) limb of
> `T-81-03-04` / `T-81-05-03` was never mitigated — a NUL in an agent's error text aborted the
> transaction that also clears the scheduling ledger, stranding the file in an unbounded recovery loop.
> Fixed in `1d6af9f7`. See `81-SECURITY.md`.

- **WR-02 — the `domain_completed` drift-lock has a hole exactly where this phase started putting rows.**
  The Python twin's ladder ranks `IN_FLIGHT` above `FAILED` and returns `False`; the SQL
  `domain_completed_clause` has no `inflight` disjunct and returns `True`. So the twins disagree on any
  `in_flight ∧ failed` row. `tests/integration/test_stage_status_equivalence.py:421-427` acknowledges this
  and excludes the `*_inflight` seeds — defensible while that cell was unreachable, because every failure
  writer used to clear the ledger row in the same transaction. FAIL-03 changed that: `retry_metadata_failed`
  deliberately leaves `metadata.failed_at` set (D-11) and then enqueues, so **every bulk-retried file now
  occupies the excluded cell**. Phase 80 must either add an `inflight` term to the SQL twin (and un-exclude
  the seeds) or apply the ledger check at the call site, as 81-01's SUMMARY already warns. Not fixed here:
  it changes the SQL twin's shape and 81-06's retry semantics depend on current behavior.

- **WR-01 — `report_metadata_failed`'s upsert can mark a fully-populated metadata row as failed.**
  The `ON CONFLICT DO UPDATE` sets only `failed_at` / `error_message`, so a file that already has real tags
  ends up with complete metadata *and* a failure marker. Three docstrings
  (`routers/agent_metadata.py:126`, `services/pipeline.py:1348`, and the test helper) claim the payload
  columns are NULL on a failure row. Reachable because `POST /api/v1/extract-metadata` re-enqueues all
  music/video files regardless of state. Result: a file with usable metadata derives FAILED and loses
  `propose` eligibility. No test starts from a row that already carries payload.
