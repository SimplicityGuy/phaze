---
phase: 24
slug: schema-foundation-agent-registry
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-11
---

# Phase 24 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio (auto mode) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_models/test_agent.py tests/test_migrations/ -x -q` |
| **Full suite command** | `uv run pytest -x -q` |
| **Estimated runtime** | quick ~5–15s · full ~30–60s |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_models/test_agent.py tests/test_migrations/ -x -q`
- **After every plan wave:** Run `uv run pytest -x -q`
- **Before `/gsd-verify-work`:** Full suite must be green AND `just db-upgrade` → `just db-downgrade` → `just db-upgrade` roundtrip succeeds on a throwaway DB
- **Max feedback latency:** 15s (quick) / 60s (full)

---

## Per-Task Verification Map

Bound to requirements DATA-01..DATA-04 plus compatibility tests. Task IDs use `24-{plan}-{task}` form and will be finalized by the planner; this map specifies the verifications that MUST be wired up.

| # | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01 | TBD | 0 | infra | — | N/A | infra | `uv run pytest tests/test_migrations/ -x -q` (collects) | ❌ W0 | ⬜ |
| 02 | TBD | 1 | DATA-01 | T-V5-01 | CHECK rejects hostile slugs (`UPPER`, `--double`, `-leading`, `trailing-`, `under_score`) | integration | `uv run pytest tests/test_models/test_agent.py::test_id_charset_check -x` | ❌ W0 | ⬜ |
| 03 | TBD | 1 | DATA-01 | — | N/A | unit | `uv run pytest tests/test_models/test_agent.py::test_agents_table_columns -x` | ❌ W0 | ⬜ |
| 04 | TBD | 1 | DATA-01 | — | N/A | unit | `uv run pytest tests/test_models/test_agent.py::test_scan_roots_is_jsonb -x` | ❌ W0 | ⬜ |
| 05 | TBD | 1 | DATA-01 | T-V6-01 | `token_hash` nullable; no plaintext token defaults | unit | `uv run pytest tests/test_models/test_agent.py::test_token_hash_nullable -x` | ❌ W0 | ⬜ |
| 06 | TBD | 1 | DATA-03 | — | N/A | unit | `uv run pytest tests/test_phase02_gaps.py::test_scan_status_enum_values -x` (extend existing) | ✅ extend | ⬜ |
| 07 | TBD | 1 | DATA-01 | — | N/A | unit | `uv run pytest tests/test_models/test_core_models.py::test_all_tables_defined -x` (extend existing) | ✅ extend | ⬜ |
| 08 | TBD | 2 | DATA-04 | T-V5-02 | Parameterised SQL only — backfill uses `sa.text(...)` with bind params | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_backfill_files -x` | ❌ W0 | ⬜ |
| 09 | TBD | 2 | DATA-04 | — | N/A | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_backfill_scan_batches -x` | ❌ W0 | ⬜ |
| 10 | TBD | 2 | DATA-04 | — | N/A | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_legacy_agent_scan_roots_from_env -x` | ❌ W0 | ⬜ |
| 11 | TBD | 2 | DATA-04 | — | N/A | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_legacy_agent_scan_roots_fallback -x` | ❌ W0 | ⬜ |
| 12 | TBD | 2 | DATA-04 | T-V4-01 | Legacy agent born revoked — unauthenticatable by design | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_legacy_agent_born_revoked -x` | ❌ W0 | ⬜ |
| 13 | TBD | 2 | DATA-03 | — | N/A | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_legacy_sentinel_exists -x` | ❌ W0 | ⬜ |
| 14 | TBD | 2 | DATA-04 | — | N/A | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_sentinel_scan_path_literal -x` | ❌ W0 | ⬜ |
| 15 | TBD | 2 | DATA-03 | — | N/A | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_partial_uq_rejects_dup_live -x` | ❌ W0 | ⬜ |
| 16 | TBD | 2 | DATA-03 | — | N/A | integration | `uv run pytest tests/test_migrations/test_012_upgrade.py::test_partial_uq_allows_multiple_non_live -x` | ❌ W0 | ⬜ |
| 17 | TBD | 3 | DATA-02 | — | N/A | integration | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_files_agent_id_not_null -x` | ❌ W0 | ⬜ |
| 18 | TBD | 3 | DATA-03 | — | N/A | integration | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_scan_batches_agent_id_not_null -x` | ❌ W0 | ⬜ |
| 19 | TBD | 3 | DATA-02 | — | N/A | integration | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_same_path_different_agent -x` | ❌ W0 | ⬜ |
| 20 | TBD | 3 | DATA-02 | — | N/A | integration | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_composite_unique_rejects_dup -x` | ❌ W0 | ⬜ |
| 21 | TBD | 3 | DATA-02 | — | N/A | integration | `uv run pytest tests/test_migrations/test_013_upgrade.py::test_old_unique_dropped -x` | ❌ W0 | ⬜ |
| 22 | TBD | 3 | DATA-04 | — | N/A | integration | `uv run pytest tests/test_migrations/test_downgrade.py::test_downgrade_013_clean -x` | ❌ W0 | ⬜ |
| 23 | TBD | 3 | DATA-04 | T-V5-03 | Dupe-detection refuses silent data loss on downgrade | integration | `uv run pytest tests/test_migrations/test_downgrade.py::test_downgrade_013_fails_on_dupes -x` | ❌ W0 | ⬜ |
| 24 | TBD | 3 | DATA-04 | — | N/A | integration | `uv run pytest tests/test_migrations/test_downgrade.py::test_downgrade_012_clean -x` | ❌ W0 | ⬜ |
| 25 | TBD | 3 | compat | — | N/A | integration | `uv run pytest tests/test_services/test_ingestion.py::test_bulk_upsert_with_agent_id -x` | ✅ extend | ⬜ |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_migrations/__init__.py` — new package marker
- [ ] `tests/test_migrations/conftest.py` — alembic-driven test DB fixture; isolated schema per test; `alembic.command.upgrade(cfg, rev)` / `downgrade(cfg, rev)` helpers
- [ ] `tests/test_migrations/test_012_upgrade.py` — DATA-01, DATA-03 (partial), DATA-04 (backfill + legacy agent shape + env-var resolution + sentinel literal)
- [ ] `tests/test_migrations/test_013_upgrade.py` — DATA-02, DATA-03 (NOT NULL), constraint swap
- [ ] `tests/test_migrations/test_downgrade.py` — D-16 (dupe-detection error) and clean roundtrip
- [ ] `tests/test_models/test_agent.py` — `Agent` model field assertions, CHECK constraint behavior under live SQL
- [ ] Extend `tests/test_models/test_core_models.py::test_all_tables_defined` — add `"agents"` to expected set
- [ ] Extend `tests/test_phase02_gaps.py::test_scan_status_values` — add `LIVE` to expected enum members
- [ ] Extend `tests/test_services/test_ingestion.py` — exercise new composite conflict target

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `just db-upgrade` → `just db-downgrade` → `just db-upgrade` roundtrip succeeds on a throwaway dev DB | DATA-04 | Smoke confirms operator-facing `just` commands wrap the real Alembic env correctly; pytest exercises the lib API path, not the CLI path | 1) `docker compose up -d postgres` 2) `just db-upgrade` 3) `just db-downgrade base` 4) `just db-upgrade` — all three must exit 0 |
| Migration log shows the resolved `SCAN_PATH` for the legacy agent's `scan_roots` | D-05 audit trail | Logger output verification is operator-visible behavior; assertable in tests but the actual operator-facing run is the truth | `SCAN_PATH=/some/path uv run alembic upgrade 012` — confirm log line `Resolved scan_roots = ['/some/path']` appears |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s (full) / 15s (quick)
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
