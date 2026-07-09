---
phase: 81
slug: per-stage-failure-persistence-retry-paths
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-08
audited: 2026-07-09
---

# Phase 81 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `81-RESEARCH.md` § Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (always via `uv run`) |
| **Config file** | `pyproject.toml`; per-bucket split via `tests/buckets.json` |
| **Quick run command** | `uv run just test-bucket <bucket>` (bucket of the touched file) |
| **Full suite command** | `uv run pytest` then `pre-commit run --all-files` |
| **Estimated runtime** | ~30-60s per bucket; full suite several minutes |

**Bucket routing for this phase:**

| Touched area | Bucket |
|--------------|--------|
| `enums/stage.py` (DB-free tables, `domain_completed`) | `shared` |
| `routers/agent_metadata.py`, `schemas/agent_metadata.py` | `metadata` |
| `routers/agent_analysis.py` | `analyze` |
| `routers/agent_fingerprint.py` | `fingerprint` |
| migration `033`, Python↔SQL equivalence, shadow gate, `pipeline.py` retry endpoint | `integration` |

Migration tests need `MIGRATIONS_TEST_DATABASE_URL` (`phaze_migrations_test`), provisioned by `just`.

---

## Sampling Rate

- **After every task commit:** `uv run just test-bucket <bucket>` for the touched bucket — **in isolation**, not only as part of the full suite. (Known non-hermetic hazards: `get_settings` lru_cache leak, `saq_jobs` stub poison.)
- **After every plan wave:** `uv run just test-bucket integration` + `uv run just test-bucket shared` (equivalence, migration, shadow gate).
- **Before `/gsd:verify-work`:** full suite green + `just docs-drift` green + Phase 79 shadow-compare gate green.
- **Max feedback latency:** ~60 seconds (single bucket). NOTE: a `tests/buckets.json` bucket run is ~30-60s, exceeding the <30s target — structural to the repo's bucket architecture, accepted (no per-file test selection available under `just test-bucket`).

---

## Per-Task Verification Map

> One row per real task across 81-01 … 81-06, plus the `post` rows for fixes that landed after the
> original map was written. Audited 2026-07-09: every row's command was re-run in **bucket isolation**.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 81-01-T1 | 81-01 | 1 | FAIL-01, FAIL-04 | T-81-01-01 | DB-free agent boundary: import guard keeps `sqlalchemy`/`phaze.models` out of `enums/stage.py`; `domain_completed()` + `eligible()` refactor is semantics-preserving (D-04) | unit | `uv run just test-bucket shared` | ✅ | ✅ green |
| 81-01-T2 | 81-01 | 1 | FAIL-01, FAIL-04 | T-81-01-01 | `domain_completed_clause()` SQL twin mirrors the pure predicate (DERIV-04 anti-drift); no `saq_jobs` reference from derivation | smoke/import | `uv run python -c "from phaze.services.stage_status import domain_completed_clause; from phaze.enums.stage import Stage; print(bool(domain_completed_clause(Stage.FINGERPRINT) is not None))"` | ✅ | ✅ green |
| 81-01-T3 | 81-01 | 1 | FAIL-01, FAIL-04 | T-81-01-01 | SQL⇔Python equivalence: `domain_completed` cells assert `sql_complete == py_complete == expected` across the 12 **non-in-flight enrich** cells (`DOMAIN_COMPLETED_CASES`). The twins are ledger-agnostic by design, so `*_inflight` seeds are deliberately excluded — see *Known Coverage Boundaries* (WR-02) | integration | `uv run just test-bucket integration` | ✅ | ✅ green |
| 81-01-T4 | 81-01 | post | FAIL-01, FAIL-04 | WR-03 | `eligible()` / `domain_completed()` compare `Status` **by value, not identity**, so a raw-string status agrees with its enum twin; an invalid status string fails loud | unit | `uv run just test-bucket shared` | ✅ | ✅ green |
| 81-02-T1 | 81-02 | 1 | FAIL-01 | T-81-02-01 | D-09 cleanup UPDATE runs BEFORE `create_check_constraint`, so live-corpus mixed rows don't abort the migration; cleanup keeps `analysis_completed_at` (done wins, D-04) | migration | `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` | ✅ | ✅ green |
| 81-02-T2 | 81-02 | 1 | FAIL-01 | T-81-02-03 | empty autogenerate diff (ORM/migration cannot silently diverge); mixed row cleaned before the CHECK; rendered name `ck_analysis_analysis_completed_xor_failed` | migration test | `uv run just test-bucket integration` | ✅ | ✅ green |
| 81-02-T3 | 81-02 | 1 | FAIL-01 | — | forward-looking-only doc renumber (`033→034`); dated historical records left intact; no destructive-`033` reference survives | doc guard | `uv run just docs-drift` | ✅ | ✅ green |
| 81-03-T1 | 81-03 | 1 | FAIL-02 | T-81-03-02 | `MetadataFailurePayload` keeps `extra='forbid'` (unknown field → 422) and bounds `error` (`max_length=2000`) | unit/import | `uv run python -c "from phaze.schemas.agent_metadata import MetadataFailurePayload; MetadataFailurePayload(reason='error')"` | ✅ | ✅ green |
| 81-03-T2 | 81-03 | 1 | FAIL-02 | T-81-03-01 | `agent` from `Depends(get_authenticated_agent)`; ledger key from PATH `file_id` only, never the body; bodyless POST binds `None` → 200 and still clears the ledger | typecheck | `uv run mypy src/phaze/routers/agent_metadata.py src/phaze/services/agent_client.py src/phaze/tasks/metadata_extraction.py` | ✅ | ✅ green |
| 81-03-T3 | 81-03 | 1 | FAIL-02 | T-81-03-03 | bodyless AND with-body report both 200; `extra='forbid'` 422s an unknown field; success clears the ledger (incl. empty-body branch) | unit | `uv run just test-bucket metadata` | ✅ | ✅ green |
| 81-03-T4 | 81-03 | post | FAIL-02 | T-81-03-04 | **oversized limb**: a 2001-char `error` → 422 `string_too_long` at the wire and persists **no** metadata row; a 2000-char `error` → 200 with the marker persisted (boundary pinned exactly, so lowering the bound also fails). **PG-invalid limb**: `sanitize_pg_text` strips NUL before persist, so the ledger clear commits | unit/wire | `uv run just test-bucket metadata` | ✅ | ✅ green |
| 81-04-T1 | 81-04 | 1 | FAIL-04 | T-81-04-01 | `report_fingerprint_failed` persists NO synthetic `engine='_task'` row; per-engine joins unpoisoned; file stays `eligible(fingerprint)` | regression | `uv run just test-bucket fingerprint` | ✅ | ✅ green |
| 81-04-T2 | 81-04 | 1 | FAIL-04 | T-81-04-01 | docstrings document the fingerprint failure asymmetry (no persisted row; auto-retry intentional per `FAILURE_IS_TERMINAL[fingerprint]=False`) | regression/docstring | `uv run just test-bucket fingerprint` | ✅ | ✅ green |
| 81-05-T1 | 81-05 | 2 | FAIL-01 | T-81-05-02 | `pg_insert(...).on_conflict_do_update` stamps `failed_at` + clears `analysis_completed_at` (invariant preserved); `agent` from auth dep, ledger key from PATH `file_id` | typecheck | `uv run mypy src/phaze/routers/agent_analysis.py` | ✅ | ✅ green |
| 81-05-T2 | 81-05 | 2 | FAIL-01 | T-81-05-02 | `put_analysis` unconditionally clears `failed_at`/`error_message` on success (D-06 invariant restored) | typecheck | `uv run mypy src/phaze/routers/agent_analysis.py` | ✅ | ✅ green |
| 81-05-T3 | 81-05 | 2 | FAIL-01 | T-81-05-02 | failure-with-no-prior-row upserts a row; D-06 CHECK holds (no row ever has both markers); success-after-failure clears | unit + migration | `uv run just test-bucket analyze` | ✅ | ✅ green |
| 81-05-T4 | 81-05 | post | FAIL-01 | T-81-05-03 | **oversized limb**: a 2001-char `error` → 422 `string_too_long`, writes **no** `analysis` row and leaves `files.state` unflipped (no `ANALYSIS_FAILED`); 2000 chars accepted. **PG-invalid limb**: NUL sanitized before persist | unit/wire | `uv run just test-bucket analyze` | ✅ | ✅ green |
| 81-06-T1 | 81-06 | 2 | FAIL-03 | T-81-06-01 | `get_metadata_failed_files` selects `metadata.failed_at IS NOT NULL` files; retry path resolves the per-agent queue once (no default-queue fallthrough) | typecheck | `uv run mypy src/phaze/services/pipeline.py` | ✅ | ✅ green |
| 81-06-T2 | 81-06 | 2 | FAIL-03 | T-81-06-03 | `NoActiveAgentError` → no enqueue, no state mutation, never the default queue; reuse `_enqueue_extraction_jobs` (complete `ExtractMetadataPayload`, `extra='forbid'`) | typecheck | `uv run mypy src/phaze/routers/pipeline.py` | ✅ | ✅ green |
| 81-06-T3 | 81-06 | 2 | FAIL-03 | T-81-06-04 | re-enqueue leaves the failure row in place (D-11); `NoActiveAgentError` guard asserts no enqueue/mutation; deterministic-key dedup prevents duplicate in-flight jobs (key applied centrally by the `before_enqueue` hook, asserted in `tests/analyze/core/test_deterministic_key.py`) | integration | `uv run just test-bucket integration` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*File Exists: ❌ artifact not yet built (pre-execution) · ✅ present*
*Wave `post` = landed after the original map was written (review fixes `1d6af9f7`, `feaebc48`, and the 2026-07-09 validation audit).*

### Backing Test Files

| Test file | Bucket | Covers |
|-----------|--------|--------|
| `tests/shared/test_stage_resolver.py` (pre-existing) | shared | 81-01-T1 DB-free import guard |
| `tests/shared/test_stage_eligibility_dag.py` | shared | 81-01-T1 semantics-preserving `eligible()` refactor (ELIG-01..04) |
| `tests/shared/test_domain_completed_contract.py` | shared | 81-01-T2 twin symmetry (CR-02), 81-01-T4 value-not-identity (WR-03) |
| `tests/shared/test_pg_text.py` | shared | 81-03-T4 / 81-05-T4 PG-invalid limb |
| `tests/integration/test_stage_status_equivalence.py` | integration | 81-01-T3 `DOMAIN_COMPLETED_CASES` drift-lock |
| `tests/integration/test_migrations/test_migration_033_additive_check.py` | integration | 81-02-T1, 81-02-T2 |
| `tests/integration/routers/test_pipeline_metadata_retry.py` | integration | 81-06-T1, 81-06-T2, 81-06-T3 |
| `tests/integration/routers/test_pipeline_analysis_retry_clears_marker.py` | integration | CR-01 dual-clear regression |
| `tests/analyze/core/test_deterministic_key.py` (pre-existing) | analyze | 81-06-T3 dedup key |
| `tests/metadata/routers/test_agent_metadata.py` | metadata | 81-03-T3, 81-03-T4 |
| `tests/analyze/routers/test_agent_analysis_failure.py` | analyze | 81-05-T3, 81-05-T4 |
| `tests/fingerprint/routers/test_agent_fingerprint_failure.py` | fingerprint | 81-04-T1, 81-04-T2 |

---

## Known Coverage Boundaries

Places where an automated test deliberately stops short. Each is a conscious scope line, not an oversight —
recorded so a future reader does not mistake a passing suite for a stronger guarantee than it makes.

| Boundary | Where | Why | Consequence |
|----------|-------|-----|-------------|
| `domain_completed` equivalence excludes in-flight cells | `DOMAIN_COMPLETED_CASES` omits the `*_inflight` seed fns | The twins are ledger-agnostic: `domain_completed_clause` is `or_(done, failed)` with no `inflight` disjunct, while the Python twin reads a resolved status that ranks `IN_FLIGHT` above `FAILED`. In-flight precedence is layered separately at `resolve_status` / `eligible` | **This is exactly WR-02.** The twins genuinely diverge on `in_flight ∧ failed` rows — a cell FAIL-03's retry now makes reachable. The drift-lock will not catch it. Open, deferred to Phase 80's recovery cutover (`deferred-items.md`) |
| Metadata failure conflict-branch leaves payload columns intact | `report_metadata_failed` upsert | Out of Phase 81's scope | **This is WR-01.** A file with real tags that later fails extraction derives FAILED while still holding usable metadata. No test asserts the payload is nulled, because it is not |

---

## Requirement → Signal Map

| Req | Observable signal | Sampling point | Anti-drift check |
|-----|-------------------|----------------|------------------|
| FAIL-01 | After `POST /analysis/{id}/failed`: `analysis.failed_at` NOT NULL, `error_message == "{reason}: {error}"`, `analysis_completed_at` NULL, `state=ANALYSIS_FAILED` still written, ledger cleared; D-06 CHECK holds | analyze-bucket router test + DB assertion; migration `033` up/down/up in integration bucket | existing equivalence cell `(ANALYZE, seed_analysis_failed, "failed")` stays green; new autogenerate-empty test; shadow gate green (no derived-status change) |
| FAIL-02 | After `POST /metadata/{id}/failed` — **bodyless AND with body** — a `metadata` row exists with `failed_at` NOT NULL and payload columns NULL; `resolve_status(METADATA)` derives FAILED not DONE; ledger cleared; 200 both ways | metadata-bucket router test covering both body paths | new equivalence cell `(METADATA, seed_metadata_failed_only, "failed")`; `extra='forbid'` rejects unknown field with 422 |
| FAIL-03 | `POST /pipeline/metadata-failed/retry` re-enqueues every `metadata.failed_at IS NOT NULL` file, **leaves the failure row in place**, returns an HTMX fragment; `NoActiveAgentError` → no enqueue and no mutation | integration-bucket endpoint test mirroring `retry_analysis_failed`'s tests; `get_metadata_failed_files` unit test | assert the failure row survives a retry that has not yet succeeded (D-11); assert no consumer-less default-queue fallthrough (Phase 30 regression) |
| FAIL-04 | `report_fingerprint_failed` persists **no** `fingerprint_results` row and only clears the ledger; a per-engine `status='failed'` row keeps the file `eligible(fingerprint)` and leaves `_trackid_engine_badge` unpoisoned | fingerprint-bucket regression tests + docstring assertions | assert `fingerprint_results` row count is unchanged after `report_fingerprint_failed`; assert the aliased per-engine joins (`services/pipeline.py:939-940`) never see `engine='_task'` |

---

## Wave 0 Requirements

- [x] `src/phaze/schemas/agent_metadata.py::MetadataFailurePayload` — new Pydantic model mirroring `AnalysisFailurePayload` (`Literal` reason + bounded `error` + `extra='forbid'`) — shipped `d6d76e4f`
- [x] `tests/integration/test_migrations/test_migration_033_*.py` — landed as `test_migration_033_additive_check.py` (`8f7b464d`); asserts autogenerate-emptiness, cleanup-before-CHECK ordering, and down/up round-trip
- [x] `domain_completed` cells added to the Phase 78 parametrized equivalence test — landed as the separate `DOMAIN_COMPLETED_CASES` list + `test_domain_completed_sql_equals_python` (`dbaf8bcc`), rather than extending `CASES` (which asserts derived *status*, not terminality)
- [x] Framework already present — no install needed

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Old agent image POSTs bodyless to `report_metadata_failed` and still receives 200 | FAIL-02 / D-10 | True version skew needs two deployed images; the automated test simulates it with a bodyless request | Deploy control plane at Phase 81, leave an agent at the prior image, force a metadata failure, confirm 200 and that `extract_file_metadata:<file_id>` clears from the ledger |
| Migration `033` cleanup against the **live** mixed-row corpus | FAIL-01 / D-09 | Live corpus row shapes cannot be fully reproduced in the test DB | On a restored production snapshot: count rows with both `analysis_completed_at` and `failed_at` set, run `033`, confirm count → 0 and no file's derived status changed |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 60s — *with the documented exception:* the `shared` (~181s) and `analyze` (~114s) buckets exceed it. Structural to the repo's bucket architecture (no per-file selection under `just test-bucket`), accepted at plan time.
- [x] Every new test passes in **bucket isolation** (`just test-bucket <bucket>`), not only in the full suite
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated 2026-07-09

---

## Validation Audit 2026-07-09

| Metric | Count |
|--------|-------|
| Task rows audited | 16 |
| Gaps found | 1 (across 2 payloads) |
| Resolved | 2 |
| Escalated | 0 |
| Rows added (post-hoc fixes) | 3 |

**Gap found.** `81-SECURITY.md` closes `T-81-03-04` and `T-81-05-03` on two limbs: PG-invalid characters
(NUL / lone surrogates) **and** oversized free text bounded by `max_length=2000`. The PG-invalid limb was
tested (`test_pg_text.py` plus the NUL round-trip tests from `1d6af9f7`). The **oversized limb had no test
in either payload** — a refactor deleting `max_length` would have left the entire suite green while
silently re-opening both threats. This was the sole MISSING classification; the other 15 rows were COVERED
and re-verified green.

**Resolved** by `gsd-nyquist-auditor` (tests only, no implementation touched):

| Test | File | Bucket |
|------|------|--------|
| `test_metadata_failed_oversized_error_rejected_and_no_row_persisted` | `tests/metadata/routers/test_agent_metadata.py` | metadata |
| `test_metadata_failed_error_at_max_length_boundary_is_accepted` | `tests/metadata/routers/test_agent_metadata.py` | metadata |
| `test_report_failed_oversized_error_rejected_and_no_row_persisted` | `tests/analyze/routers/test_agent_analysis_failure.py` | analyze |
| `test_report_failed_error_at_max_length_boundary_is_accepted` | `tests/analyze/routers/test_agent_analysis_failure.py` | analyze |

Each reject/accept pair pins the boundary at exactly 2000/2001, so the guard fails if the bound is removed
**or lowered** — not merely if huge strings start being accepted. The reject cases additionally assert no
row is persisted and (for analyze) that `files.state` never flips to `ANALYSIS_FAILED`.

**Audit evidence — every bucket re-run in isolation, both DB URLs exported (port 5433):**

| Bucket | Before | After |
|--------|--------|-------|
| `shared` | 997 passed | 997 passed |
| `metadata` | 75 passed | **77 passed** |
| `fingerprint` | 83 passed | 83 passed |
| `analyze` | 517 passed | **519 passed** |
| `integration` | 155 passed | 155 passed |
| `docs-drift` | 10 passed | 10 passed |

**Also corrected in this audit:** row 81-01-T3 claimed SQL⇔Python equivalence "for every stage×status
cell." The implemented `DOMAIN_COMPLETED_CASES` deliberately excludes the `*_inflight` seeds — which is
precisely where WR-02 says the twins diverge. The test is right; the claim was overstated. Both WR-01 and
WR-02 are now recorded under *Known Coverage Boundaries* so the passing suite is not mistaken for a
stronger guarantee than it makes.
</content>
