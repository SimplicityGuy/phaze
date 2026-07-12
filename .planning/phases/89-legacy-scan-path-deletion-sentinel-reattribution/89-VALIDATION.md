---
phase: 89
slug: legacy-scan-path-deletion-sentinel-reattribution
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-11
audited: 2026-07-11
---

# Phase 89 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/integration/test_migrations/ -q` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~migration bucket <60s; full suite several min |

> **Migration-test footgun (project memory):** export both `MIGRATIONS_TEST_DATABASE_URL` (port 5433, `just test-db`) and the base DB URL, or migration tests fail in isolation looking like a colima flake.

---

## Sampling Rate

- **After every task commit:** Run the quick run command (plus the affected bucket).
- **After every plan wave:** Run `uv run pytest` for touched buckets.
- **Before `/gsd:verify-work`:** Full suite green + `pre-commit run --all-files` (ruff, mypy strict, bandit) + `uv run pytest --cov` ≥ 90%.
- **Max feedback latency:** ~60 seconds for the migration bucket.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 89-01-01 | 01 | 1 | LEGACY-03 | — | Fixtures repoint to a real fileserver while default still present (green) | integration | `uv run pytest tests/integration/test_stage_status_equivalence.py tests/integration/test_files_page.py -x -q` | ✅ | ✅ green (64 passed) |
| 89-01-02 | 01 | 1 | LEGACY-01, LEGACY-03 | T-89-01 | App boots without scan router; no NOT-NULL/FK flush failure after default drop | integration | `uv run pytest tests/shared/routers/test_pipeline.py tests/agents/services/test_agent_upsert.py -x -q` + full `uv run pytest -q` | ✅ | ✅ green (110 passed) |
| 89-02-01 | 02 | 2 | LEGACY-02, LEGACY-03 | T-89-02 | Migration 038 revision/down_revision correct; downgrade raises NotImplementedError | unit (import) | `uv run python -c "…revision=='038' and down_revision=='037'…pytest.raises(NotImplementedError, m.downgrade)"` | ✅ | ✅ green (import assert OK) |
| 89-02-02 | 02 | 2 | LEGACY-02, LEGACY-03 | T-89-02 | 8 scenarios: reattribute, delete-live-batch, abort-0-fileserver, abort->1-no-override, -x override, COUNT=0 assert, sentinel deleted | migration/integration | `uv run pytest tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py -x -q` | ✅ | ✅ green (12 passed) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/integration/test_migrations/test_migration_038_*.py` — migration 038 up/down + reattribution + abort paths (LEGACY-02, LEGACY-03) — delivered (12 passed)
- [x] `tests/conftest.py` — repoint seed from `legacy-application-server` to a real fileserver (`id='test-fileserver'`, `kind='fileserver'`, non-revoked) — delivered (D-08)
- [x] Confirm the ~10 integration tests' `_LEGACY_AGENT_ID` constant repoint (D-08) — delivered

*Framework already installed — no framework bootstrap needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live-corpus reattribution + sentinel delete against a restore of prod | LEGACY-02/03 | Prod corpus (~11,428 files, real nox agent) not reproducible in unit tests | Rehearse `alembic upgrade head` against a restore; confirm 0 legacy-owned rows remain and sentinel deleted (operational, at ship time) |

*All in-repo behaviors have automated verification via the migration test bucket.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (conftest/constant repoints land in 89-01-01)
- [x] No watch-mode flags
- [x] Feedback latency < 60s (targeted `uv run pytest` invocations)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-11 (plan-checker: 0 blockers)

---

## Validation Audit 2026-07-11

Post-execution audit (State A). All four automated commands re-run green against the live migration test DB (`:5433`); every referenced test/impl file confirmed present.

| Metric | Count |
|--------|-------|
| Requirements audited | 3 (LEGACY-01, LEGACY-02, LEGACY-03) |
| Tasks COVERED (green) | 4 / 4 |
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

**Re-run evidence:** 89-01-01 → 64 passed · 89-01-02 → 110 passed · 89-02-01 → import assert OK · 89-02-02 → 12 passed. One appropriately-scoped Manual-Only item remains (live-corpus reattribution against a prod restore, verified operationally at ship time). **Verdict: Nyquist-compliant.**
