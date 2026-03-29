---
phase: 3
slug: companion-files-deduplication
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-27
validated: 2026-03-28
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest tests/ --cov --cov-report=term-missing` |
| **Estimated runtime** | ~10 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q`
- **After every plan wave:** Run `uv run pytest tests/ --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 03-01-01 | 01 | 1 | ING-06 | unit | `uv run pytest tests/test_services/test_companion.py tests/test_phase03_gaps.py -x -q` | ✅ | ✅ green |
| 03-01-02 | 01 | 1 | ING-06 | integration | `uv run pytest tests/test_routers/test_companion.py -x -q` | ✅ | ✅ green |
| 03-02-01 | 02 | 1 | ING-04 | unit | `uv run pytest tests/test_services/test_dedup.py tests/test_phase03_gaps.py -x -q` | ✅ | ✅ green |
| 03-02-02 | 02 | 1 | ING-04 | integration | `uv run pytest tests/test_routers/test_companion.py -x -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_services/test_companion.py` — 5 tests for ING-06 (companion association)
- [x] `tests/test_services/test_dedup.py` — 5 tests for ING-04 (duplicate detection)
- [x] `tests/test_routers/test_companion.py` — 7 integration tests (associate + duplicates endpoints)
- [x] `tests/test_phase03_gaps.py` — 20 gap-filling tests (model fields, schemas, migration 003)
- [x] `tests/conftest.py` — shared fixtures (exists from Phase 2)

---

## Gap Coverage (Nyquist Audit — 2026-03-28)

| Gap | Tests Added | File | Status |
|-----|-------------|------|--------|
| FileCompanion tablename | `test_file_companion_tablename` | `test_phase03_gaps.py` | ✅ green |
| FileCompanion columns (id, companion_id, media_id, timestamps) | 4 column tests | `test_phase03_gaps.py` | ✅ green |
| FileCompanion FK references files table | `test_file_companion_companion_id_references_files`, `test_file_companion_media_id_references_files` | `test_phase03_gaps.py` | ✅ green |
| FileCompanion unique constraint on (companion_id, media_id) | `test_file_companion_has_unique_constraint_on_pair` | `test_phase03_gaps.py` | ✅ green |
| FileCompanion FK CASCADE on delete | `test_file_companion_fk_cascade_delete_companion`, `test_file_companion_fk_cascade_delete_media` | `test_phase03_gaps.py` | ✅ green |
| AssociateResponse schema fields | `test_associate_response_required_fields`, `test_associate_response_new_associations_is_int` | `test_phase03_gaps.py` | ✅ green |
| DuplicateFile schema fields | `test_duplicate_file_required_fields` | `test_phase03_gaps.py` | ✅ green |
| DuplicateGroup schema fields | `test_duplicate_group_required_fields` | `test_phase03_gaps.py` | ✅ green |
| DuplicateGroupsResponse schema fields | `test_duplicate_groups_response_required_fields`, `test_duplicate_groups_response_groups_is_list_of_duplicate_group` | `test_phase03_gaps.py` | ✅ green |
| Migration 003 down_revision chain | `test_migration_003_down_revision_is_002`, `test_migration_003_revision_is_003` | `test_phase03_gaps.py` | ✅ green |
| Migration 003 upgrade/downgrade callable | `test_migration_003_has_upgrade_function`, `test_migration_003_has_downgrade_function` | `test_phase03_gaps.py` | ✅ green |

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 15s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** 2026-03-28 — Nyquist audit complete, 20 gap tests added, 176/176 suite green
