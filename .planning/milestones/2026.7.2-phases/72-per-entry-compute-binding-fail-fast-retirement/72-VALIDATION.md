---
phase: 72
slug: per-entry-compute-binding-fail-fast-retirement
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-05
---

# Phase 72 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio) |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `uv run pytest tests/analyze/services/test_backends.py tests/shared/config/test_backend_registry.py -q` |
| **Full suite command** | `uv run pytest tests/ -q` |
| **Estimated runtime** | ~7s (targeted) / ~11min (full suite) |

**Integration DB required** for DB-touching cells (ephemeral Postgres on 5433, Redis on 6380):
```
TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test"
MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test"
PHAZE_REDIS_URL="redis://localhost:6380/0"
```
Config-only registry tests (`test_backend_registry.py`) use a `backends_toml_env` fixture and need no DB.

---

## Sampling Rate

- **After every task commit:** Run the quick run command
- **After every plan wave:** Run the affected test directories (`tests/analyze/services/`, `tests/shared/config/`)
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** ~7s (targeted)

---

## Per-Task Verification Map

| Decision | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|----------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| D-06/D-07 golden ≤1-compute byte-identical + zero-compute regression | 01 | 1 | MCOMP-01 | T-72-01-01 | golden baseline blocks any non-preserving refactor | unit | `uv run pytest tests/analyze/services/test_compute_binding_golden.py -q` | ✅ | ✅ green |
| D-03 retire compute-only `>1` raises (`resolved_non_local_kind`, `active_compute_scratch_dir`) | 02 | 2 | MCOMP-01 | T-72-02-02 | N compute-only no longer raises; ≤1 byte-identical | unit | `uv run pytest tests/analyze/services/test_backends.py tests/shared/config/test_bucket_registry.py -k "compute or scratch" -q` | ✅ | ✅ green |
| D-01 `select_agent_by_id` id-only + liveness + kind scope + raise-on-absent/offline/revoked | 03 | 3 | MCOMP-01 | T-72-03-01/02 | binds by Agent.id only, no name fallback; degrade-to-hold | unit | `uv run pytest tests/analyze/services/test_backends.py -k select_agent_by_id -q` | ✅ | ✅ green |
| D-01 parameterized-query injection-safety | 03 | 3 | MCOMP-01 | T-72-03-01 | SQL metacharacters in agent_ref treated as a literal, never injected | unit (Nyquist gap-fill) | `uv run pytest tests/analyze/services/test_backends.py -k sql_metacharacters -q` | ✅ | ✅ green |
| D-02 `is_available` resolves bound agent_ref per-call; `_agent_ref()` fails loud | 03 | 3 | MCOMP-01 | T-72-03-01 | each backend probes its OWN agent; unbound → ValueError | unit | `uv run pytest tests/analyze/services/test_backends.py -k "is_available" -q` | ✅ | ✅ green |
| D-04 boot-time duplicate-agent_ref guard (id-tagged ValueError) | 04 | 3 | MCOMP-01 | T-72-04-01/03 | two backends sharing one agent_ref fails fast naming value + ids | unit | `uv run pytest tests/shared/config/test_backend_registry.py -k "duplicate or distinct" -q` | ✅ | ✅ green |
| D-05 degrade-to-hold; guard opens no DB session | 04 | 3 | MCOMP-01 | T-72-04-02 | unregistered agent_ref boots cleanly, holds at runtime | unit | `uv run pytest tests/shared/config/test_backend_registry.py -k unregistered -q` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. The phase was executed test-first (golden characterization in Wave 1 before any production change); no new framework or fixtures were needed.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| D-07 `agent_push.py` untouched | MCOMP-01 | "file unmodified" is a structural/git claim, not pytest-verifiable. The strongest behavioral proxy IS automated: `test_single_compute_registry_resolution_is_byte_identical` pins the exact `/pushed` scratch-path format string `agent_push.py` depends on. | `git diff a09a4f00..HEAD -- src/phaze/routers/agent_push.py` → expect empty |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (none — existing infra sufficed)
- [x] No watch-mode flags
- [x] Feedback latency < 10s (targeted)
- [x] `nyquist_compliant: true` set in frontmatter

## Validation Audit 2026-07-05

| Metric | Count |
|--------|-------|
| Gaps found | 1 |
| Resolved | 1 |
| Escalated | 0 |

Gap: D-01 parameterized-query/injection-safety clause had no direct coverage. Filled by `tests/analyze/services/test_backends.py::test_select_agent_by_id_treats_sql_metacharacters_as_a_literal_value` (green first attempt, 0 debug iterations). 6/7 decisions were already fully COVERED. Targeted re-run: 76/76 passed. Run by: gsd-nyquist-auditor (sonnet).

**Approval:** approved 2026-07-05
