---
phase: 45
slug: scheduling-ledger-for-orphan-recovery
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-20
---

# Phase 45 — Validation Strategy

> Reconstructed from phase artifacts (State B) after execution. Every phase
> requirement (L-01…L-06) maps to an existing automated test that targets the
> behavior and ran green in the post-merge full suite (1988 passed, 97.49%
> coverage, 2026-06-19).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_task_split.py tests/test_models/test_scheduling_ledger.py tests/test_services/test_scheduling_ledger.py -q` (Postgres-free) |
| **Full suite command** | `just integration-test` (ephemeral Postgres :5433 + Redis :6380, auto teardown) |
| **Estimated runtime** | ~210 seconds (full suite); ~3 seconds (Postgres-free quick subset) |

**DB note:** Recovery, migration, backfill, and router tests require the
ephemeral DB. Pure-task boundary tests (`test_task_split.py`), the schema-rejection
tests, and the model/service unit tests run DB-free.

---

## Sampling Rate

- **After every task commit:** Run the Postgres-free quick subset (boundary + model + service).
- **After every plan wave:** Run `just integration-test` (full suite against ephemeral DB).
- **Before `/gsd:verify-work`:** Full suite must be green.
- **Max feedback latency:** ~210 seconds (full suite).

---

## Per-Task Verification Map

| Plan | Wave | Requirement | Behavior verified | Test Type | Automated Command | File Exists | Status |
|------|------|-------------|-------------------|-----------|-------------------|-------------|--------|
| 01 | 1 | L-01 | Durable ledger row written at the single `before_enqueue` chokepoint (`apply_deterministic_key`); model + routing classifier | unit + integration | `uv run pytest tests/test_models/test_scheduling_ledger.py tests/test_services/test_scheduling_ledger.py tests/test_deterministic_key.py -q` | ✅ | ✅ green |
| 01 | 1 | L-02 (controller half) | Controller-stage clear on success AND terminal failure via one `after_process` hook gated on `TERMINAL_STATUSES` (never on retry) | integration | `just integration-test` → `tests/test_services/test_scheduling_ledger.py` | ✅ | ✅ green |
| 02 | 2 | L-02 (agent half) | Agent-stage clears in control-side callbacks (analyze success+/failed, metadata, fingerprint, scan match + terminal-ack); `not job.retryable` guard | integration | `just integration-test` → `tests/test_routers/test_agent_{analysis,metadata,fingerprint,tracklists}.py`, `tests/test_tasks/test_scan.py` | ✅ | ✅ green |
| 05 | 1 (gap) | L-02 (CR-01) | `scan_live_set` no-match path guards `report_scan_terminal`: re-raise on retryable, swallow+log on terminal so the row is not leaked on a controller hiccup | unit | `uv run pytest tests/test_tasks/test_scan.py -q` | ✅ | ✅ green |
| 06 | 1 (gap) | L-02 (CR-02) | `extract_file_metadata` + `fingerprint_file` get control-side `POST /{file_id}/failed` terminal-failure ledger clears + agent-worker terminal acks | integration | `just integration-test` → `tests/test_routers/test_agent_{metadata,fingerprint}.py`, `tests/test_tasks/test_{metadata_extraction,fingerprint}.py` | ✅ | ✅ green |
| 03 | 2 | L-03 | Recovery re-queues exactly `ledger − live keys − domain-completed` via existing keyed producers; never-scheduled files left alone (incident regression) | integration | `just integration-test` → `tests/test_tasks/test_recovery.py::test_never_scheduled_files_are_left_alone`, `::test_no_op_on_durable_restart` | ✅ | ✅ green |
| 04 | 3 | L-04 | Idempotent one-time startup backfill from live `saq_jobs`; runs before recovery, never aborts boot, no-overwrite on re-run | integration | `just integration-test` → `tests/test_tasks/test_ledger_backfill.py`, `tests/test_tasks/test_recovery.py::test_startup_backfills_ledger_before_recovery` | ✅ | ✅ green |
| 01–06 | all | L-05 | Control-only boundary: the agent worker / watcher / bootstraps stay Postgres-free (no `phaze.database` / `phaze.models` / `sqlalchemy` import) | unit (import-graph) | `uv run pytest tests/test_task_split.py -q` | ✅ | ✅ green |
| 01 | 1 | L-06 | Reversible Alembic migration 022 (upgrade creates `scheduling_ledger`, downgrade to 021 drops it — round-trip) + ≥85% coverage | integration + coverage gate | `just integration-test` → `tests/test_migrations/test_022.py::test_upgrade_022_creates_then_downgrade_drops`; coverage 97.49% ≥ 85% | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. No Wave 0 stubs were
needed — pytest + pytest-asyncio + the ephemeral integration DB (`just test-db`)
were already in place from prior phases (quick task 260520-bcl).

---

## Manual-Only Verifications

All phase behaviors have automated verification.

The one production-runtime behavior not exercised by the suite — the idempotent
backfill reconciling a *live* deployed `saq_jobs` table on first boot after the
022 migration — is covered structurally by `test_ledger_backfill.py` (against a
seeded ephemeral `saq_jobs`) and will be confirmed operationally at the
release + homelab redeploy that lands migration 022.

---

## Validation Sign-Off

- [x] All requirements (L-01…L-06) have an automated test targeting the behavior
- [x] Sampling continuity: no requirement without an automated verify
- [x] Wave 0 covers all MISSING references (none — existing infra sufficient)
- [x] No watch-mode flags
- [x] Feedback latency < 210s (full suite)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-06-20

---

## Validation Audit 2026-06-20

| Metric | Count |
|--------|-------|
| Requirements audited | 6 (L-01…L-06) |
| COVERED | 6 |
| PARTIAL | 0 |
| MISSING | 0 |
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

Reconstructed from 6 SUMMARY.md files (State B). All requirements were already
covered by behavior-targeting tests that passed in the 2026-06-19 post-merge
full suite (1988 passed, 97.49% coverage). No auditor spawn or test generation
required.
