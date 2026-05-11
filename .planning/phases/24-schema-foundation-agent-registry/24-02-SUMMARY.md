---
phase: 24
plan: 02
subsystem: data-model
tags: [sqlalchemy, declarative-schema, agent-registry, foreign-key, check-constraint, partial-unique-index]
requires:
  - "24-01"
provides:
  - phaze.models.agent.Agent
  - phaze.models.scan_batch.ScanStatus.LIVE
  - "FileRecord.agent_id (FK to agents.id)"
  - "ScanBatch.agent_id (FK to agents.id)"
  - "uq_files_agent_id_original_path (composite unique)"
  - "uq_scan_batches_agent_id_live (partial unique predicate status = 'live')"
  - "ck_agents_id_charset (slug-regex CHECK constraint)"
affects:
  - alembic/env.py (autogenerate consumes phaze.models export barrel)
  - Plan 03 migration 012 (must reuse the canonical regex literal byte-for-byte)
  - Plan 04 migration 013 (must reuse the canonical partial-index predicate)
  - Plan 05 ingestion-service edits (will set agent_id and switch conflict target to composite)
tech-stack:
  added: []
  patterns:
    - "Declarative SQLAlchemy 2.0 model with Mapped[...] + mapped_column"
    - "TimestampMixin contributing created_at + updated_at"
    - "JSONB column with server_default text(\"'[]'::jsonb\")"
    - "CheckConstraint short-name (id_charset) prefixed via base.py naming-convention dict"
    - "Partial unique index via postgresql_where=text(\"status = 'live'\")"
key-files:
  created:
    - src/phaze/models/agent.py
    - tests/test_models/test_agent.py
  modified:
    - src/phaze/models/scan_batch.py
    - src/phaze/models/file.py
    - src/phaze/models/__init__.py
    - tests/test_phase02_gaps.py
    - tests/test_models/test_core_models.py
decisions:
  - "Agent.id is VARCHAR(64) primary key (not UUID) per CONTEXT D-01"
  - "Slug regex literal: ^[a-z0-9]+(-[a-z0-9]+)*$ — must match Plan 03 migration 012 byte-for-byte"
  - "Partial-index predicate literal: status = 'live' — lowercase value; must match Plan 03 migration 012 byte-for-byte"
  - "agent_id columns declared nullable=False (post-013 final state) per CONTEXT D-08; migration 012 starts the column nullable and 013 tightens"
  - "No reverse Agent.files / Agent.scan_batches relationship() back-refs (deferred per CONTEXT.md)"
  - "Composite UQ leads with agent_id so Postgres can use it for agent_id-only lookups (D-15); no separate ix_files_agent_id needed"
metrics:
  duration: ~5 min
  completed: 2026-05-11
  tasks_completed: 3
  files_created: 2
  files_modified: 5
  commits: 5
---

# Phase 24 Plan 02: Model-side schema for Agent registry Summary

SQLAlchemy declarative layer now matches the post-migration-013 schema: new `Agent` model with slug CHECK constraint, `ScanStatus.LIVE` enum value, `agent_id` FK columns on `FileRecord` and `ScanBatch`, composite unique index swap on `files`, and partial unique index for the per-agent LIVE sentinel on `scan_batches`.

## Canonical Literals (must match Plan 03 migration 012 byte-for-byte)

The plan front-loads two string literals that are intentionally duplicated in the model and the upcoming migration. Plan 03 MUST read these before writing migration 012.

| Literal | Exact Value | Where Used | Why Duplicated (not shared constant) |
|---------|-------------|------------|--------------------------------------|
| Agent ID regex | `^[a-z0-9]+(-[a-z0-9]+)*$` | `src/phaze/models/agent.py` `CheckConstraint("id ~ '<regex>'", name="id_charset")` + Plan 03's migration 012 `op.create_table` DDL | D-14: duplicate literal in both files; avoids implicit model-version coupling to the migration runtime |
| LIVE-sentinel predicate | `status = 'live'` | `src/phaze/models/scan_batch.py` `postgresql_where=text("status = 'live'")` + Plan 03's migration 012 `op.create_index(..., postgresql_where=sa.text("..."))` | RESEARCH Pitfall 3: whitespace / casing drift between model and migration breaks `alembic check` and silently disables the unique guarantee |

## What Was Built

### Task 1 — Agent model + 9 model-level tests (commits 94ddca6 + c95809f)

`src/phaze/models/agent.py` declares `class Agent(TimestampMixin, Base)` with 8 columns total (6 declared + `created_at` / `updated_at` from the mixin):

- `id: Mapped[str] = mapped_column(String(64), primary_key=True)`
- `name: Mapped[str] = mapped_column(String(128), nullable=False)`
- `token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)`
- `scan_roots: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))`
- `last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)`
- `revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)`

`__table_args__` declares the slug CheckConstraint with the model-side short name `"id_charset"`; the project naming-convention dict in `base.py` prefixes to `ck_agents_id_charset` at metadata build time.

`tests/test_models/test_agent.py` contributes 9 assertions:
table name, in-metadata presence, required-column set, primary-key shape, `token_hash` nullable, `scan_roots` is JSONB, `ck_agents_id_charset` declared, `name` non-null, `token_hash` is sized as VARCHAR(128).

### Task 2 — FileRecord + ScanBatch + ScanStatus updates (commits c733678 + cf5538a)

`src/phaze/models/scan_batch.py`:
- Added `ScanStatus.LIVE = "live"` (lowercase stored value matching existing enum casing)
- Added `agent_id: Mapped[str]` column with `ForeignKey("agents.id", ondelete="RESTRICT")` and `nullable=False`
- Added `__table_args__` with `ix_scan_batches_agent_id` plain index and `uq_scan_batches_agent_id_live` partial unique index keyed on `agent_id` with `postgresql_where=text("status = 'live'")`
- Imports updated: `from sqlalchemy import ForeignKey, Index, Integer, String, Text, text`

`src/phaze/models/file.py`:
- Added `agent_id: Mapped[str]` column with `ForeignKey("agents.id", ondelete="RESTRICT")` and `nullable=False` (post-013 final state; migration 012 starts the column nullable)
- Swapped single-column `uq_files_original_path` → composite `uq_files_agent_id_original_path` on `("agent_id", "original_path")`
- Per D-15, no separate `ix_files_agent_id` — the composite leading column covers agent_id-only filters

`tests/test_phase02_gaps.py` — renamed `test_scan_status_has_three_values` → `test_scan_status_has_four_values` with `assert len(members) == 4` and `assert ScanStatus.LIVE == "live"`.

`tests/test_models/test_core_models.py` — added `"agents"` to the `test_all_tables_defined` expected set and bumped the docstring count from 13 → 14.

### Task 3 — Export barrel update (commit b37e635)

`src/phaze/models/__init__.py` — added `from phaze.models.agent import Agent` (alphabetically first in the import block) and `"Agent"` (alphabetically first in `__all__`). This is the entry point that `alembic/env.py` uses via `from phaze.models import *` for autogenerate; missing it would silently exclude the new model.

## Verification (all passing)

- `uv run pytest tests/test_models/ tests/test_phase02_gaps.py --deselect tests/test_models/test_core_models.py::test_tables_created_in_database` — 92 passed
- `uv run mypy src/phaze/models/` — Success: no issues found in 14 source files
- `uv run python -c "from phaze.models import Agent; from phaze.models.scan_batch import ScanStatus; assert ScanStatus.LIVE == 'live'; assert Agent.__tablename__ == 'agents'"` — exits 0
- Grep contracts:
  - `grep -F "id ~ '^[a-z0-9]+(-[a-z0-9]+)*\$'" src/phaze/models/agent.py` — OK
  - `grep -F "status = 'live'" src/phaze/models/scan_batch.py` — OK
  - `grep -c "uq_files_original_path" src/phaze/models/file.py` — 0
  - `grep -c "uq_files_agent_id_original_path" src/phaze/models/file.py` — 1
- Pre-commit hooks pass on all 5 commits (ruff, ruff-format, bandit, mypy).

## TDD Gate Compliance

Both behavior-adding tasks followed the RED → GREEN cycle with separate commits.

| Task | RED commit | GREEN commit |
|------|------------|--------------|
| Task 1 (Agent model) | 94ddca6 `test(24-02): add failing test for Agent model` | c95809f `feat(24-02): implement Agent model with slug CHECK constraint` |
| Task 2 (scan_batch + file edits) | c733678 `test(24-02): expand ScanStatus to 4 values and add agents to expected tables` | cf5538a `feat(24-02): add agent_id FKs, LIVE enum value, composite UQ swap` |

Task 3 (`__init__.py` export barrel) was not behavior-adding — it is a wiring change that makes Task 1's class importable from the public package namespace. Committed as a single `feat` per the plan's `type="auto"` (not `tdd="true"`) classification.

## Deviations from Plan

### Auto-fixed Issues

None — the plan executed without any Rule 1/2/3 deviations.

### Quality-tool reflows

**1. [Format - ruff] `scan_roots` line collapsed to one line**
- **Found during:** Task 1 quality gate (`uv run ruff format`)
- **Issue:** The plan example showed the `scan_roots` `mapped_column(...)` declaration wrapped across two lines for readability; ruff format with `line-length = 150` collapsed it to a single line.
- **Fix:** None needed — accepted ruff's reformat (semantically identical, still under 150 chars).
- **Files modified:** `src/phaze/models/agent.py`
- **Commit:** c95809f

### Other observations

- `src/phaze/models/` is listed in the project `.gitignore` (line 218: `models/` — intended for downloaded ML model artifacts). All existing Python model files (`file.py`, `tag_write_log.py`, etc.) are tracked because they were force-added in earlier phases. The new `agent.py` was also force-added with `git add -f`. This is a working pattern; the `.gitignore` rule could be tightened in a future cleanup phase (e.g., to `^models/` or by adding `!src/phaze/models/`) but doing so is out of scope for Phase 24.

## Known Stubs

None. All declared columns and constraints are wired through to the consuming tests; nothing returns empty/placeholder data.

## Threat Flags

None. The Plan's `<threat_model>` already enumerates the relevant trust boundaries (operator slug → DB, watcher status → predicate). All identified mitigations (`id_charset` CheckConstraint, byte-exact regex/predicate literals, nullable `token_hash` with NULL default) are implemented; no new surface introduced.

## Self-Check: PASSED

All claimed artifacts verified:

- `src/phaze/models/agent.py` — FOUND
- `src/phaze/models/file.py` — FOUND
- `src/phaze/models/scan_batch.py` — FOUND
- `src/phaze/models/__init__.py` — FOUND
- `tests/test_models/test_agent.py` — FOUND
- `tests/test_models/test_core_models.py` — FOUND
- `tests/test_phase02_gaps.py` — FOUND

All claimed commit hashes verified in `git log --all`:

- 94ddca6 — FOUND (Task 1 RED)
- c95809f — FOUND (Task 1 GREEN)
- c733678 — FOUND (Task 2 RED)
- cf5538a — FOUND (Task 2 GREEN)
- b37e635 — FOUND (Task 3)
