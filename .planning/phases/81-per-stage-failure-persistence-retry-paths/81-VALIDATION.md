---
phase: 81
slug: per-stage-failure-persistence-retry-paths
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-07-08
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

> One row per real task across 81-01 … 81-06. `File Exists = ❌` for every row pre-execution
> (Wave 0 not yet built); execute-phase flips these to ✅ as artifacts land.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 81-01-T1 | 81-01 | 1 | FAIL-01, FAIL-04 | T-81-01-01 | DB-free agent boundary: import guard keeps `sqlalchemy`/`phaze.models` out of `enums/stage.py`; `domain_completed()` + `eligible()` refactor is semantics-preserving (D-04) | unit | `uv run just test-bucket shared` | ❌ | ⬜ pending |
| 81-01-T2 | 81-01 | 1 | FAIL-01, FAIL-04 | T-81-01-01 | `domain_completed_clause()` SQL twin mirrors the pure predicate (DERIV-04 anti-drift); no `saq_jobs` reference from derivation | smoke/import | `uv run python -c "from phaze.services.stage_status import domain_completed_clause; from phaze.enums.stage import Stage; print(bool(domain_completed_clause(Stage.FINGERPRINT) is not None))"` | ❌ | ⬜ pending |
| 81-01-T3 | 81-01 | 1 | FAIL-01, FAIL-04 | T-81-01-01 | SQL⇔Python equivalence: `domain_completed` cells assert `sql_status == py_status == expected` for every stage×status cell | integration | `uv run just test-bucket integration` | ❌ | ⬜ pending |
| 81-02-T1 | 81-02 | 1 | FAIL-01 | T-81-02-01 | D-09 cleanup UPDATE runs BEFORE `create_check_constraint`, so live-corpus mixed rows don't abort the migration; cleanup keeps `analysis_completed_at` (done wins, D-04) | migration | `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` | ❌ | ⬜ pending |
| 81-02-T2 | 81-02 | 1 | FAIL-01 | T-81-02-03 | empty autogenerate diff (ORM/migration cannot silently diverge); mixed row cleaned before the CHECK; rendered name `ck_analysis_analysis_completed_xor_failed` | migration test | `uv run just test-bucket integration` | ❌ | ⬜ pending |
| 81-02-T3 | 81-02 | 1 | FAIL-01 | — | forward-looking-only doc renumber (`033→034`); dated historical records left intact; no destructive-`033` reference survives | doc guard | `uv run just docs-drift` | ❌ | ⬜ pending |
| 81-03-T1 | 81-03 | 1 | FAIL-02 | T-81-03-02 | `MetadataFailurePayload` keeps `extra='forbid'` (unknown field → 422) and bounds `error` (`max_length=2000`) | unit/import | `uv run python -c "from phaze.schemas.agent_metadata import MetadataFailurePayload; MetadataFailurePayload(reason='error')"` | ❌ | ⬜ pending |
| 81-03-T2 | 81-03 | 1 | FAIL-02 | T-81-03-01 | `agent` from `Depends(get_authenticated_agent)`; ledger key from PATH `file_id` only, never the body; bodyless POST binds `None` → 200 and still clears the ledger | typecheck | `uv run mypy src/phaze/routers/agent_metadata.py src/phaze/services/agent_client.py src/phaze/tasks/metadata_extraction.py` | ❌ | ⬜ pending |
| 81-03-T3 | 81-03 | 1 | FAIL-02 | T-81-03-03 | bodyless AND with-body report both 200; `extra='forbid'` 422s an unknown field; success clears the ledger (incl. empty-body branch) | unit | `uv run just test-bucket metadata` | ❌ | ⬜ pending |
| 81-04-T1 | 81-04 | 1 | FAIL-04 | T-81-04-01 | `report_fingerprint_failed` persists NO synthetic `engine='_task'` row; per-engine joins unpoisoned; file stays `eligible(fingerprint)` | regression | `uv run just test-bucket fingerprint` | ❌ | ⬜ pending |
| 81-04-T2 | 81-04 | 1 | FAIL-04 | T-81-04-01 | docstrings document the fingerprint failure asymmetry (no persisted row; auto-retry intentional per `FAILURE_IS_TERMINAL[fingerprint]=False`) | regression/docstring | `uv run just test-bucket fingerprint` | ❌ | ⬜ pending |
| 81-05-T1 | 81-05 | 2 | FAIL-01 | T-81-05-02 | `pg_insert(...).on_conflict_do_update` stamps `failed_at` + clears `analysis_completed_at` (invariant preserved); `agent` from auth dep, ledger key from PATH `file_id` | typecheck | `uv run mypy src/phaze/routers/agent_analysis.py` | ❌ | ⬜ pending |
| 81-05-T2 | 81-05 | 2 | FAIL-01 | T-81-05-02 | `put_analysis` unconditionally clears `failed_at`/`error_message` on success (D-06 invariant restored) | typecheck | `uv run mypy src/phaze/routers/agent_analysis.py` | ❌ | ⬜ pending |
| 81-05-T3 | 81-05 | 2 | FAIL-01 | T-81-05-02 | failure-with-no-prior-row upserts a row; D-06 CHECK holds (no row ever has both markers); success-after-failure clears | unit + migration | `uv run just test-bucket analyze` | ❌ | ⬜ pending |
| 81-06-T1 | 81-06 | 2 | FAIL-03 | T-81-06-01 | `get_metadata_failed_files` selects `metadata.failed_at IS NOT NULL` files; retry path resolves the per-agent queue once (no default-queue fallthrough) | typecheck | `uv run mypy src/phaze/services/pipeline.py` | ❌ | ⬜ pending |
| 81-06-T2 | 81-06 | 2 | FAIL-03 | T-81-06-03 | `NoActiveAgentError` → no enqueue, no state mutation, never the default queue; reuse `_enqueue_extraction_jobs` (complete `ExtractMetadataPayload`, `extra='forbid'`) | typecheck | `uv run mypy src/phaze/routers/pipeline.py` | ❌ | ⬜ pending |
| 81-06-T3 | 81-06 | 2 | FAIL-03 | T-81-06-04 | re-enqueue leaves the failure row in place (D-11); `NoActiveAgentError` guard asserts no enqueue/mutation; deterministic-key dedup prevents duplicate in-flight jobs | integration | `uv run just test-bucket integration` | ❌ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*File Exists: ❌ artifact not yet built (pre-execution) · ✅ present*

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

- [ ] `src/phaze/schemas/agent_metadata.py::MetadataFailurePayload` — new Pydantic model mirroring `AnalysisFailurePayload` (`Literal` reason + bounded `error` + `extra='forbid'`)
- [ ] `tests/integration/test_migrations/test_migration_033_*.py` — new; template is the existing `test_migration_032_additive_schema.py` (which already asserts autogenerate-emptiness)
- [ ] `domain_completed` cells added to `_CASES` in the Phase 78 parametrized equivalence test
- [ ] Framework already present — no install needed

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Old agent image POSTs bodyless to `report_metadata_failed` and still receives 200 | FAIL-02 / D-10 | True version skew needs two deployed images; the automated test simulates it with a bodyless request | Deploy control plane at Phase 81, leave an agent at the prior image, force a metadata failure, confirm 200 and that `extract_file_metadata:<file_id>` clears from the ledger |
| Migration `033` cleanup against the **live** mixed-row corpus | FAIL-01 / D-09 | Live corpus row shapes cannot be fully reproduced in the test DB | On a restored production snapshot: count rows with both `analysis_completed_at` and `failed_at` set, run `033`, confirm count → 0 and no file's derived status changed |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] Every new test passes in **bucket isolation** (`just test-bucket <bucket>`), not only in the full suite
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
</content>
