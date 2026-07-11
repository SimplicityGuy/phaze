---
phase: 89
slug: legacy-scan-path-deletion-sentinel-reattribution
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-11
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
| _to be filled by planner from PLAN.md tasks_ | | | LEGACY-01/02/03 | | | migration / unit / integration | `uv run pytest ...` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/integration/test_migrations/test_migration_038_*.py` — migration 038 up/down + reattribution + abort paths (LEGACY-02, LEGACY-03)
- [ ] `tests/conftest.py` — repoint seed from `legacy-application-server` to a real fileserver (`id='test-fileserver'`, `kind='fileserver'`, non-revoked)
- [ ] Confirm the ~10 integration tests' `_LEGACY_AGENT_ID` constant repoint (D-08)

*Framework already installed — no framework bootstrap needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live-corpus reattribution + sentinel delete against a restore of prod | LEGACY-02/03 | Prod corpus (~11,428 files, real nox agent) not reproducible in unit tests | Rehearse `alembic upgrade head` against a restore; confirm 0 legacy-owned rows remain and sentinel deleted (operational, at ship time) |

*All in-repo behaviors have automated verification via the migration test bucket.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
