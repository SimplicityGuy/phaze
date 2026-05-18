---
phase: 25-internal-agent-http-api-bearer-auth
plan: 01
subsystem: database
tags: [alembic, postgres, jsonb, sqlalchemy, pytest, fixtures, agent, bearer-auth]

# Dependency graph
requires:
  - phase: 24-schema-foundation-agent-registry
    provides: agents table, Agent model, LEGACY_AGENT_ID seed, migration 013 at head
provides:
  - Agent.last_status JSONB nullable column at the ORM layer
  - Alembic migration 014 adding agents.last_status + partial token-hash index ix_agents_token_hash_active WHERE revoked_at IS NULL
  - tests/conftest.py seed_test_agent + authenticated_client fixtures usable by Plans 02-06
  - DB_FIXTURES set extended so any test consuming the new fixtures is auto-marked integration
affects:
  - 25-02 (auth dep SELECT must use `.is_(None)` so Postgres uses the partial index)
  - 25-03 (heartbeat endpoint writes Agent.last_status JSONB)
  - 25-04 (file-upsert tests use authenticated_client)
  - 25-05 (execution-log tests use authenticated_client)
  - 25-06 (metadata / fingerprint tests use authenticated_client)

# Tech tracking
tech-stack:
  added: []  # No new dependencies — uses stdlib secrets, hashlib + existing SQLAlchemy + Alembic
  patterns:
    - "Partial index with sa.text() predicate matching SQLAlchemy `.is_(None)` byte-for-byte"
    - "Bearer-token test fixture pattern: secrets.token_urlsafe(32) + sha256(full_wire).hex"

key-files:
  created:
    - alembic/versions/014_add_last_status_to_agents.py
  modified:
    - src/phaze/models/agent.py
    - tests/conftest.py
    - tests/test_migrations/test_012_upgrade.py

key-decisions:
  - "Partial-index predicate literal `revoked_at IS NULL` MUST match SQLAlchemy's `Agent.revoked_at.is_(None)` byte-for-byte — Plan 02 auth dep must use `.is_(None)` not `== None`"
  - "Token wire format: `phaze_agent_<43 urlsafe-base64 chars>` (55 chars total). The hash stored in agents.token_hash is sha256 of the FULL wire string (prefix + secret), never the secret alone (D-02)"
  - "Last-status is JSONB nullable with no server_default — legacy agent never heartbeats so a backfill is unnecessary (per phase-24 D-06 + phase-25 D-07)"
  - "Test agent slug `test-agent-01` is kebab-case (lowercase letters + digits + single hyphens) — valid under ck_agents_id_charset CheckConstraint `^[a-z0-9]+(-[a-z0-9]+)*$`"

patterns-established:
  - "Partial token-hash index pattern: `unique=False, postgresql_where=sa.text(\"revoked_at IS NULL\")` — pattern mirrors migration 012:104-110 (uq_scan_batches_agent_id_live) but with non-unique semantics for an auth lookup index"
  - "seed_test_agent fixture commits Agent row before returning so a NEW session opened via Depends(get_session) inside the handler sees it"
  - "authenticated_client fixture chains seed_test_agent + the existing client fixture's session-override pattern, with Authorization: Bearer <raw_token> pre-set on the underlying AsyncClient"

requirements-completed:
  - AUTH-01
  - AUTH-04
  - DIST-04
  - DIST-05

# Metrics
duration: 6min
completed: 2026-05-11
---

# Phase 25 Plan 01: Bedrock Schema & Test Fixtures Summary

**Schema and test-infrastructure foundation for Phase 25 — last_status JSONB column, partial token-hash index, and bearer-token fixtures that every downstream plan consumes.**

## Performance

- **Duration:** 6 min
- **Started:** 2026-05-11T23:46:00Z (approx — first task commit)
- **Completed:** 2026-05-11T23:52:00Z (last task commit)
- **Tasks:** 3 of 3 completed
- **Files modified:** 4 (1 created, 3 modified)

## Accomplishments

- **Agent.last_status JSONB column** added at the ORM layer, mypy-strict clean, mirrors `FileMetadata.raw_tags` pattern
- **Migration 014** writes the column + partial index `ix_agents_token_hash_active` (predicate `WHERE revoked_at IS NULL` matches `.is_(None)` byte-for-byte). Applied to live DB (alembic current = 014 head); roundtrip downgrade/upgrade verified clean
- **Test fixtures** `seed_test_agent` and `authenticated_client` exposed in `tests/conftest.py`; `DB_FIXTURES` set extended so consumer tests are auto-marked `integration`
- **All 790 existing tests pass** — one regression caught in `test_012_upgrade.py::test_agents_table_columns` (hardcoded expected column set didn't include the new `last_status`); fixed inline per Rule 1

## Task Commits

Each task was committed atomically:

1. **Task 1: Add last_status JSONB column to Agent model** - `7f7fbfc` (feat)
2. **Task 2: [BLOCKING] Migration 014 + alembic upgrade head** - `1e8b6c0` (feat)
3. **Task 3: seed_test_agent + authenticated_client fixtures (+ regression fix)** - `281f359` (test)

## Files Created/Modified

- `alembic/versions/014_add_last_status_to_agents.py` (CREATED) — Migration adds `agents.last_status` JSONB nullable + non-unique partial index `ix_agents_token_hash_active` over `(token_hash) WHERE revoked_at IS NULL`. Downgrade reverses cleanly with no data-loss guard (legacy agent never heartbeats).
- `src/phaze/models/agent.py` (MODIFIED) — One-line addition: `last_status: Mapped[dict | None] = mapped_column(JSONB, nullable=True)` between `revoked_at` and `__table_args__`. JSONB import already present from Phase 24.
- `tests/conftest.py` (MODIFIED) — Three changes: added `import hashlib` + `import secrets`; extended `DB_FIXTURES` set with `"authenticated_client"` and `"seed_test_agent"`; appended two new `@pytest_asyncio.fixture` functions. Token generation uses `secrets.token_urlsafe(32)` (never `random`); hash is `hashlib.sha256(raw_token.encode("utf-8")).hexdigest()` of the FULL wire string.
- `tests/test_migrations/test_012_upgrade.py` (MODIFIED, Rule 1 fix) — `test_agents_table_columns` expected set now includes `last_status` since migration 014 ran via `migrated_engine`'s upgrade-to-head.

## Key Decisions

- **Partial-index predicate is byte-exact:** `postgresql_where=sa.text("revoked_at IS NULL")` — Plan 02's auth dep MUST use `Agent.revoked_at.is_(None)` (NOT `== None`) so SQLAlchemy renders identical SQL and Postgres uses the partial index for the auth lookup.
- **Token wire format & hashing contract:**
  - Wire format: `phaze_agent_<43 urlsafe-base64 chars>` (total 55 chars)
  - Hash stored: `hashlib.sha256(raw_token.encode("utf-8")).hexdigest()` of the FULL wire string (NOT the secret-only portion). 64 hex chars.
  - `agents.token_hash` is `String(128)` from Phase 24 → plenty of headroom.
- **No server_default on `last_status`:** column is nullable and legacy agent never writes to it.
- **Test agent slug `test-agent-01`:** kebab-case, valid under CheckConstraint `^[a-z0-9]+(-[a-z0-9]+)*$`. Slug also used as `name` (no separate human-readable name needed for tests).

## Acceptance Criteria — All Met

- [x] `grep -c "last_status: Mapped\[dict | None\] = mapped_column(JSONB, nullable=True)" src/phaze/models/agent.py` returns 1
- [x] `uv run mypy src/phaze/models/agent.py` reports `Success: no issues found`
- [x] `uv run alembic current` output contains `014 (head)`
- [x] Roundtrip downgrade/upgrade succeeds (013 ↔ 014)
- [x] Partial index `ix_agents_token_hash_active` exists in Postgres with predicate `(revoked_at IS NULL)` (verified via `\d+ agents`)
- [x] `grep -c "async def seed_test_agent" tests/conftest.py` returns 1
- [x] `grep -c "async def authenticated_client" tests/conftest.py` returns 1
- [x] `grep -F 'secrets.token_urlsafe(32)' tests/conftest.py` succeeds (no `random` import)
- [x] `grep -F 'hashlib.sha256(raw_token.encode("utf-8")).hexdigest()' tests/conftest.py` succeeds
- [x] `uv run pytest tests/ -x -q` passes (790 tests, no regression after Rule 1 fix)
- [x] Pre-commit clean on all four modified files

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated `test_agents_table_columns` expected column set**

- **Found during:** Task 3 (running `uv run pytest tests/` after fixture additions)
- **Issue:** Phase 24's regression test `tests/test_migrations/test_012_upgrade.py::test_agents_table_columns` hardcodes the EXPECTED set of agents-table columns and asserts equality after `migrated_engine`'s upgrade-to-head. Migration 014 (Task 2 of this plan) added `last_status`, so the test now sees an "extra" column.
- **Fix:** Added `"last_status"` to the `expected` set in `test_agents_table_columns`. Reformatted to one-column-per-line for diff hygiene. Added inline comment explaining the change.
- **Files modified:** `tests/test_migrations/test_012_upgrade.py`
- **Commit:** Bundled with Task 3 (`281f359`)
- **Rationale:** Bug directly caused by my Task 2 migration; scope per Rule 1 deviation rules.

## Threat Mitigations Verified

- **T-25-01-T (migration corruption):** Pattern mirrors migration 012:104-110 byte-for-byte for JSONB column + partial index. Roundtrip test confirms clean reversibility. Only additive DDL — no data mutation, FK changes, or row-level ops.
- **T-25-01-I (predicate drift):** `postgresql_where=sa.text("revoked_at IS NULL")` literal. Verified in Postgres `\d+` output: `"ix_agents_token_hash_active" btree (token_hash) WHERE revoked_at IS NULL`. Plan 02 must use `.is_(None)` to render the same SQL.
- **T-25-01-S (predictable test tokens):** `secrets.token_urlsafe(32)` — no `random` anywhere in the fixtures.
- **T-25-01-E (test fixture in production):** Accepted — fixture lives in `tests/conftest.py`, not reachable from production code.

## Notes for Downstream Plans

**Critical for Plan 02 (auth dep):**
- The auth dep SELECT must use `Agent.revoked_at.is_(None)` (NOT `== None`) so SQLAlchemy renders `WHERE revoked_at IS NULL` byte-for-byte matching the partial index predicate.
- Hash incoming bearer with `hashlib.sha256(token.encode("utf-8")).hexdigest()` of the FULL wire string (do NOT strip the `phaze_agent_` prefix).
- Use `select(Agent).where(Agent.token_hash == hashed, Agent.revoked_at.is_(None))` — the partial index `ix_agents_token_hash_active` covers this lookup with O(log n) probing.

**Critical for Plan 03 (heartbeat endpoint):**
- `Agent.last_status` is `Mapped[dict | None]` (JSONB nullable). Assign directly via `agent.last_status = {"agent_version": ..., "worker_pid": ..., "queue_depth": ...}`. SQLAlchemy serialises dict → JSONB automatically.
- Combine with `agent.last_seen_at = datetime.now(tz=timezone.utc)` (use timezone-aware datetime; the column is `DateTime(timezone=True)`).

**Critical for all router test files (Plans 02-06):**
- Import: `from tests.conftest import seed_test_agent, authenticated_client` (already auto-discovered by pytest if tests live under `tests/`).
- Use `authenticated_client` fixture for handlers gated by `Depends(get_authenticated_agent)`. The Authorization: Bearer <token> header is pre-set.
- Use `seed_test_agent` directly when the test needs the raw token (e.g., to assert 401/403 with a forged token).
- Any test using these fixtures is auto-marked `integration` via `DB_FIXTURES` + `pytest_collection_modifyitems`.

## Self-Check: PASSED

**Files verified to exist:**
- `src/phaze/models/agent.py` (modified)
- `alembic/versions/014_add_last_status_to_agents.py` (created)
- `tests/conftest.py` (modified)
- `tests/test_migrations/test_012_upgrade.py` (modified, Rule 1 fix)

**Commits verified in git log:**
- `7f7fbfc` — Task 1: feat add last_status JSONB column to Agent model
- `1e8b6c0` — Task 2: feat add migration 014 for last_status column and token-hash index
- `281f359` — Task 3: test add seed_test_agent and authenticated_client fixtures (+ Rule 1 fix)
