---
phase: 81
slug: per-stage-failure-persistence-retry-paths
status: draft
nyquist_compliant: false
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
- **Max feedback latency:** ~60 seconds (single bucket).

---

## Per-Task Verification Map

> Populated by `/gsd:plan-phase` once PLAN.md task IDs exist. Rows below are the
> requirement-level contract each task must ladder up to.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | TBD | FAIL-01 | — | Failure marker cannot be forged by agent body; `agent` from auth dep only | unit + migration | `uv run just test-bucket analyze` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | FAIL-02 | — | `extra='forbid'` rejects unknown body fields (422); bodyless POST still 200 | unit | `uv run just test-bucket metadata` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | FAIL-03 | — | `NoActiveAgentError` → no enqueue, no state mutation, no default-queue fallthrough | integration | `uv run just test-bucket integration` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | FAIL-04 | — | `report_fingerprint_failed` persists no row; per-engine joins unpoisoned | regression | `uv run just test-bucket fingerprint` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

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
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
