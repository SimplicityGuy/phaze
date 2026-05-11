---
phase: 24-schema-foundation-agent-registry
verified: 2026-05-11T21:08:11Z
status: passed
score: 4/4 DATA requirements verified
overrides_applied: 0
gaps: []
human_verification:
  - test: "Run just scan against a dev Postgres DB with migrations 011..013 applied"
    expected: "SELECT agent_id, count(*) FROM files GROUP BY agent_id returns one row: legacy-application-server"
    why_human: "Requires running Postgres + files on disk; not testable without live services"
  - test: "Run 21 migration integration tests against phaze_migrations_test DB"
    expected: "21/21 pass — includes D-16 dupe-detection guard, partial UQ, NOT NULL enforcement"
    why_human: "Tests exist and collect (21/21) but require a live postgres:18-alpine instance; operator smoke gate already PASSED against throwaway container"
---

# Phase 24: Schema Foundation & Agent Registry — Verification Report

**Phase Goal:** Establish the v4.0 distributed-agents schema foundation: `agents` table, `agent_id` FK columns on `files` and `scan_batches`, two-step additive+tightening migration pair, alembic-driven integration test substrate, and ingestion-service wiring to the legacy placeholder agent.

**Verified:** 2026-05-11T21:08:11Z
**Branch:** gsd/phase-24-schema-foundation-agent-registry
**Status:** PASS
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths (DATA Requirements)

| # | Truth | Status | Evidence |
|---|-------|--------|---------|
| DATA-01 | `agents` table exists with pk_agents, ck_agents_id_charset slug CHECK, JSONB scan_roots | VERIFIED | `src/phaze/models/agent.py` — Agent model; `alembic/versions/012_add_agents_table_and_backfill.py` — 8-step upgrade; slug regex byte-identical in both (`^[a-z0-9]+(-[a-z0-9]+)*$`); 13 migration integration tests collected |
| DATA-02 | `files.agent_id` and `scan_batches.agent_id` are NOT NULL FKs with ON DELETE RESTRICT; composite `uq_files_agent_id_original_path` replaces single-column UQ; ingestion stamps every row | VERIFIED | Model: `nullable=False`, FK `agents.id` RESTRICT, default `legacy-application-server` confirmed in both models; composite UQ verified (`uq_files_agent_id_original_path`, cols `[agent_id, original_path]`, unique=True); old `uq_files_original_path` absent; `ingestion.py` uses `index_elements=["agent_id", "original_path"]` |
| DATA-03 | LIVE sentinel scan_batch `scan_path='<watcher>'` seeded for legacy agent; partial unique index `uq_scan_batches_agent_id_live` enforces one-live-per-agent | VERIFIED | Migration 012 step 7 inserts sentinel, step 8 creates partial UQ after INSERT; `ScanBatch` model declares `uq_scan_batches_agent_id_live` with `postgresql_where=text("status = 'live'")` — byte-identical predicate; `ScanStatus.LIVE = "live"` confirmed importable |
| DATA-04 | Born-revoked legacy agent (`legacy-application-server`) seeded with SCAN_PATH-resolved `scan_roots`; D-16 downgrade guard in migration 013; alembic-driven test substrate exists | VERIFIED | Migration 012 inserts legacy agent with `revoked_at=NOW()`; `SCAN_PATH` env var resolution + `phaze-024:` audit log confirmed in code; D-16 `RuntimeError("Cannot downgrade 013->012: ...")` guard in migration 013 downgrade before any DDL; operator smoke gate PASSED (transcript in 24-04-SUMMARY.md) |

**Score:** 4/4 truths verified

---

## Plan-by-Plan Verification Summary

### Plan 24-01: Alembic-Driven Test Substrate

**Claimed deliverables — all VERIFIED in codebase:**

| Artifact | Exists | Substantive | Notes |
|----------|--------|-------------|-------|
| `tests/test_migrations/__init__.py` | Yes | Yes (0-byte marker, correct) | pytest package marker |
| `tests/test_migrations/conftest.py` | Yes | Yes (143 lines) | Exports `migrated_engine`, `upgrade_to`, `downgrade_to`, `_build_alembic_config`, `MIGRATIONS_TEST_DATABASE_URL`, `ALEMBIC_INI_PATH` via `__all__` |

Key implementation details verified:

- `_patched_settings_database_url` contextmanager present and correctly wraps `settings.database_url` mutation in `finally` restore.
- `upgrade_to` / `downgrade_to` both wrap `alembic.command.upgrade/downgrade` with the patch.
- `migrated_engine` fixture uses `asyncio.to_thread(upgrade_to, ...)` and `asyncio.to_thread(downgrade_to, ...)` (asyncio nesting defect fixed in Plan 03; teardown uses `_reset_schema` DROP/CREATE approach, not `downgrade_to('base')` — fixture teardown defect fixed in commit `075c70e`).
- `MIGRATIONS_TEST_DATABASE_URL = "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_migrations_test"` — correct dedicated DB.
- mypy: clean. ruff: clean. pre-commit: passes.

### Plan 24-02: Model-Side Schema for Agent Registry

**Claimed deliverables — all VERIFIED:**

| Artifact | Status | Evidence |
|----------|--------|---------|
| `src/phaze/models/agent.py` | VERIFIED | `Agent(TimestampMixin, Base)`, `__tablename__ = "agents"`, 6 declared columns, `CheckConstraint("id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="id_charset")` |
| `src/phaze/models/scan_batch.py` | VERIFIED | `ScanStatus.LIVE = "live"`, `agent_id` NOT NULL FK, `uq_scan_batches_agent_id_live` partial UQ, `ix_scan_batches_agent_id` plain index |
| `src/phaze/models/file.py` | VERIFIED | `agent_id` NOT NULL FK, old `uq_files_original_path` absent, `uq_files_agent_id_original_path` composite UQ on `(agent_id, original_path)` |
| `src/phaze/models/__init__.py` | VERIFIED | `from phaze.models.agent import Agent` + `"Agent"` in `__all__` — barrel export for Alembic autogenerate |
| `tests/test_models/test_agent.py` | VERIFIED | 9 tests (class-based); all pass (`9 passed in 0.01s`) |

**Canonical literal alignment verified:**
- Slug regex: `^[a-z0-9]+(-[a-z0-9]+)*$` — byte-identical match in `agent.py` and `012` migration.
- LIVE predicate: `status = 'live'` — byte-identical match in `scan_batch.py` and `012` migration.

### Plan 24-03: Migration 012 — Agents Table + Legacy Backfill

**Claimed deliverables — all VERIFIED:**

| Artifact | Status | Evidence |
|----------|--------|---------|
| `alembic/versions/012_add_agents_table_and_backfill.py` | VERIFIED | 122 lines; revision chain `011 -> 012`; 8-step upgrade; parameterized SQL (no f-strings); `phaze-024:` audit log; `SCAN_PATH` env var with `/data/music` fallback; `uuid.uuid4()` sentinel; partial UQ created AFTER sentinel INSERT |
| `tests/test_migrations/test_012_upgrade.py` | VERIFIED | 260 lines; 13 async test functions confirmed; covers column inventory, CHECK constraint, nullable, LIVE sentinel, partial UQ, backfill, SCAN_PATH resolution |
| `tests/test_migrations/conftest.py` (modified) | VERIFIED | `asyncio.to_thread` wrap for both upgrade/downgrade calls in `migrated_engine` fixture |

Downgrade correctness: drops partial UQ, deletes LIVE rows, drops FKs, drops agent_id columns, drops agents table — reverse order verified in code.

### Plan 24-04: Migration 013 — NOT NULL + Composite UQ Swap

**Claimed deliverables — all VERIFIED:**

| Artifact | Status | Evidence |
|----------|--------|---------|
| `alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py` | VERIFIED | 60 lines (exactly at floor); revision chain `012 -> 013`; 4-step upgrade: alter NOT NULL on both, drop old UQ, create composite UQ |
| D-16 downgrade guard | VERIFIED | `SELECT original_path ... GROUP BY ... HAVING COUNT(*) > 1 LIMIT 5` runs BEFORE any DDL; raises `RuntimeError("Cannot downgrade 013->012: ...")` with ASCII arrow; abort is pre-mutation |
| `tests/test_migrations/test_013_upgrade.py` | VERIFIED | 106 lines; 5 async test functions; covers NOT NULL, same-path-different-agent, duplicate rejection, old UQ drop |
| `tests/test_migrations/test_downgrade.py` | VERIFIED | 147 lines; 3 async test functions; covers clean 013->012 roundtrip, D-16 error path, clean 012->011 |

**Operator smoke gate:** PASSED 2026-05-11 against throwaway `postgres:18-alpine` container. Full transcript recorded in 24-04-SUMMARY.md. All 12 acceptance criteria PASS (upgrade chain 001..013, SCAN_PATH override, D-16 guard, clean roundtrips).

### Plan 24-05: Ingestion Service Composite-Conflict-Target + LEGACY_AGENT_ID Stamping

**Claimed deliverables — all VERIFIED:**

| Artifact | Status | Evidence |
|----------|--------|---------|
| `LEGACY_AGENT_ID` constant | VERIFIED | Defined in `src/phaze/models/agent.py:14` as `"legacy-application-server"` (moved from `ingestion.py` in fix commit `1ef2e79`); `ingestion.py` imports it from there |
| `discover_and_hash_files` agent_id stamping | VERIFIED | Line 76: `"agent_id": LEGACY_AGENT_ID` in every record dict |
| `bulk_upsert_files` composite conflict target | VERIFIED | `index_elements=["agent_id", "original_path"]` — leading column matches composite UQ D-15 spec |
| `run_scan` ScanBatch attribution | VERIFIED | Line 137: `agent_id=LEGACY_AGENT_ID` in ScanBatch constructor |
| `test_bulk_upsert_same_path_different_agent` | VERIFIED | Test exists at line 321 of `test_ingestion.py`; exercises composite UQ invariant |

---

## Discovered Defects and Remediation

Four defects were discovered and fixed during phase execution. All are now committed to the branch.

### In-Scope Auto-Fixes (completed during plan execution)

| # | Defect | Fix | Commit | Classification |
|---|--------|-----|--------|----------------|
| 1 | Plan 24-01 fixture would crash: `alembic.command.upgrade` triggers `asyncio.run` inside pytest-asyncio's event loop → `RuntimeError` | Wrapped both upgrade/downgrade calls in `asyncio.to_thread` in `conftest.py` | `8f43f93` | In-scope auto-fix (fixture defect discovered first time fixture ran) |
| 2 | Plan 24-01 `migrated_engine` teardown called `downgrade_to('base')`, which trips D-16 guard when tests insert cross-agent duplicates, stranding DB mid-chain | Replaced with `DROP SCHEMA public CASCADE` + `CREATE SCHEMA public` — bypasses migration chain | `075c70e` | In-scope auto-fix (discovered when 013 tests exercised the D-16 path) |

### Out-of-Scope But Unblocking Fixes (latent defects from prior phases)

| # | Defect | Fix | Commit | Classification |
|---|--------|-----|--------|----------------|
| 3 | Migration 010 (`add_discogs_links`): duplicate FK constraint declaration (inline `sa.ForeignKey(...)` + explicit `sa.ForeignKeyConstraint(...)` with same auto-generated name) → `DuplicateObjectError` on fresh DB | Removed redundant explicit `ForeignKeyConstraint` | `a4a375b` | Out-of-scope / pre-existing (never surfaced on already-migrated DBs; unblocking for phase 24 smoke gate) |
| 4 | Migration 011 (`add_tag_write_log`): same duplicate FK pattern | Removed redundant explicit `ForeignKeyConstraint` | `0a63c61` | Out-of-scope / pre-existing (same root cause as #3) |

**Post-fix 5 (commit `1ef2e79`):** 157 pre-existing tests across `test_services`, `test_routers`, `test_tasks` broke when migration 013's NOT NULL constraint applied at `Base.metadata.create_all` time (the model-level `nullable=False` declaration is authoritative for schema creation). Root cause: those tests constructed `FileRecord`/`ScanBatch` without setting `agent_id`. Resolution: moved `LEGACY_AGENT_ID` from `ingestion.py` to `phaze.models.agent` as canonical definition; added Python-level column `default="legacy-application-server"` to both models; seeded legacy `Agent` row in root `conftest.py` `async_engine` fixture. This is in-scope (phase 24 introduced the constraint; fixing pre-existing tests is a phase 24 responsibility).

---

## Quality Gates

| Gate | Result | Details |
|------|--------|---------|
| mypy (src/) | PASS | "Success: no issues found in 69 source files" |
| ruff check (all phase 24 files) | PASS | All checks passed |
| ruff format (all phase 24 files) | PASS | All files correctly formatted |
| bandit | PASS | Pre-commit hook passes on all phase 24 files |
| pre-commit (all hooks, key files) | PASS | All 17 hooks pass on phase 24 modified files |
| Unit test suite (`not integration`, ignore test_migrations) | PASS | 505 passed, 264 deselected, 0 failed |
| Model-level tests (`test_models/test_agent.py`) | PASS | 9/9 passed |
| Total test collection | 790 tests collected | Full suite confirmed at 790 |
| Migration test collection | 21/21 collected | test_012 (13) + test_013 (5) + test_downgrade (3) |
| Operator smoke gate (Task 24-04) | PASS | Full 001..013 roundtrip + SCAN_PATH override + D-16 guard — transcript in 24-04-SUMMARY.md |
| Migration revision chain | Intact | 011 → 012 → 013 (head); no orphan branches |
| Duplicate FK constraints in 010/011 | Removed | No `ForeignKeyConstraint` objects remain in either migration |

---

## Requirements Coverage

The phase claimed coverage of DATA-01 through DATA-04. These were derived from plan frontmatter (`requirements-completed:` fields) as no v4.0 REQUIREMENTS.md exists yet (v4.0 milestone is not formally opened in ROADMAP.md). All four are verified against codebase artifacts.

| Requirement | Covered By | Status |
|-------------|-----------|--------|
| DATA-01: agents table with slug CHECK, JSONB scan_roots | Plans 02, 03 | SATISFIED |
| DATA-02: agent_id NOT NULL FKs; composite UQ; ingestion stamping | Plans 02, 04, 05 | SATISFIED |
| DATA-03: LIVE sentinel; partial UQ uq_scan_batches_agent_id_live | Plans 02, 03 | SATISFIED |
| DATA-04: born-revoked legacy agent; SCAN_PATH resolution; D-16 guard; test substrate | Plans 01, 03, 04 | SATISFIED |

---

## Recommendations for v4.0 Phase 25

Phase 25 consumes the following codebase state:

**Schema (at migration head 013):**
- `agents` table: VARCHAR(64) PK with slug CHECK (`ck_agents_id_charset`), JSONB `scan_roots`, `token_hash` nullable, `revoked_at` nullable (but set for legacy agent).
- `files.agent_id`: NOT NULL, FK `agents.id` ON DELETE RESTRICT, covered by composite `uq_files_agent_id_original_path(agent_id, original_path)`.
- `scan_batches.agent_id`: NOT NULL, FK `agents.id` ON DELETE RESTRICT, covered by partial UQ `uq_scan_batches_agent_id_live` (one LIVE row per agent).

**Code state:**
- `LEGACY_AGENT_ID = "legacy-application-server"` lives in `phaze.models.agent`. Phase 25 must import from there, not redefine it.
- `phaze.services.ingestion` stamps every `FileRecord` and `ScanBatch` with `LEGACY_AGENT_ID`. Phase 25 must replace these three stamping sites (`discover_and_hash_files` record dict, `bulk_upsert_files` conflict target verification, `run_scan` ScanBatch constructor) with per-request agent attribution derived from a bearer token.
- The legacy agent is born revoked (`revoked_at` set). Phase 25 auth middleware must NOT treat this agent as authenticated; it exists solely for pre-v4.0 file attribution.
- Root conftest seeds legacy `Agent` row in `async_engine` fixture — Phase 25 tests that add new agents should insert them in their own fixtures, not depend on the root conftest behavior.

**Operator pre-conditions for Phase 25:**
1. `phaze_test` DB must exist on localhost:5432 (standing requirement from phase 2 onward).
2. `phaze_migrations_test` DB must exist on localhost:5432 (added in phase 24-01).
3. Migration head is 013 — apply via `just db-upgrade head` before running integration tests.

---

## Anti-Patterns Scan

No blockers or warnings found.

| File | Pattern | Severity | Assessment |
|------|---------|----------|-----------|
| `src/phaze/services/ingestion.py` | `LEGACY_AGENT_ID` constant (Phase 24 placeholder) | Info | Intentional placeholder with forward citation to Phase 25; not a stub — value is real, FK-valid, and flow-tested |
| `tests/test_migrations/conftest.py` | Hard-coded `localhost:5432` test URL | Info | Documented operator pre-condition; matches pattern in root conftest |

---

## Human Verification Required

### 1. End-to-End just scan Verification

**Test:** With a dev Postgres on localhost:5432 at migration head 013, run `just scan` against a music directory.
**Expected:** `SELECT agent_id, count(*) FROM files GROUP BY agent_id` returns one row: `agent_id = 'legacy-application-server'`, `count > 0`.
**Why human:** Requires running Postgres, real music files on disk, and the full SAQ worker stack.

### 2. Migration Integration Test Suite

**Test:** Provision `phaze_migrations_test` DB on localhost:5432, then run `uv run pytest tests/test_migrations/ -v`.
**Expected:** 21/21 tests pass — including the D-16 guard path (`test_downgrade_013_fails_on_dupes`) and the SCAN_PATH override test.
**Why human:** Operator smoke gate PASSED (transcript in 24-04-SUMMARY.md), but the full automated suite against a live DB has not been formally captured as a CI run. Tests are statically correct, collection confirms 21/21, and smoke gate confirms migration behavior — but live DB execution of all 21 is an operator step.

---

## Verdict

**PASS.**

All four DATA requirements are implemented, substantive, and wired. The `agents` table, `agent_id` FK columns, two-step migration pair (012 additive + 013 tightening), alembic-driven test substrate, and ingestion-service composite conflict target all exist exactly as specified and are verified against the live codebase at HEAD. The operator smoke gate ran end-to-end against a throwaway postgres:18-alpine container and all 12 acceptance criteria passed. Four defects were discovered and fixed during execution: two were in-scope fixture defects (asyncio nesting, teardown stranding), and two were pre-existing latent defects in migrations 010/011 that only manifested on a fresh DB. The full pre-commit suite is clean, mypy reports no issues across 69 source files, and 505 unit tests pass without a running database.

The two human verification items (live `just scan` end-to-end and running all 21 migration integration tests against a provisioned DB) are expected to pass based on the smoke gate transcript and static code review, but require operator execution to formally close.

---

_Verified: 2026-05-11T21:08:11Z_
_Verifier: Claude (gsd-verifier)_
