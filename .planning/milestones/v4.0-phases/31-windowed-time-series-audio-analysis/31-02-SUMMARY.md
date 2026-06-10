---
phase: 31-windowed-time-series-audio-analysis
plan: 02
subsystem: database
tags: [sqlalchemy, alembic, postgres, audio-analysis, time-series, cascade, partial-index]

# Dependency graph
requires:
  - phase: 03-add-file-companions-table
    provides: child-table create + FK-constraint migration pattern (analog for 018)
  - phase: prior-models
    provides: AnalysisResult + TimestampMixin + naming-convention Base reused by AnalysisWindow
provides:
  - AnalysisWindow ORM model (1:many child of files, ON DELETE CASCADE)
  - Alembic migration 018 creating analysis_window + 5 query indexes (additive only)
  - Cross-archive queryability surface (partial index on bpm WHERE tier='fine')
affects: [agent_analysis schema/router (windows payload), analysis service rewrite, review-UI timeline]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "CASCADE FK only on the new child table; aggregate table left structurally unchanged (no ORM ondelete without matching DB constraint)"
    - "Partial indexes (postgresql_where) to make tier-scoped time-series columns cheaply queryable"

key-files:
  created:
    - alembic/versions/018_add_analysis_window_table.py
    - tests/test_models/test_analysis_window.py
    - tests/test_migrations/test_migration_018.py
  modified:
    - src/phaze/models/analysis.py
    - src/phaze/models/__init__.py
    - tests/test_models/test_core_models.py

key-decisions:
  - "CASCADE FK lives ONLY on AnalysisWindow.file_id; AnalysisResult.file_id keeps unique=True and no ondelete (migration 018 is additive-only, so an ORM CASCADE there would be a constraint Postgres never enforces)."
  - "Migration 018 issues no in-place schema change against the existing analysis table — the analysis table stays structurally unchanged (CONTEXT.md)."
  - "Bare-number revision strings ('018'/'017') per repo convention, overriding RESEARCH's long-name citation."
  - "file_id is indexed but NOT unique (1:many); the composite index (file_id, tier, window_index) covers per-file ordered reads via its leftmost prefix."

patterns-established:
  - "Per-window child row (AnalysisWindow): fine-tier columns (bpm/musical_key) + coarse-tier columns (mood/style/danceability/features), all nullable so either tier omits the other's fields."
  - "Partial-index queryability: bpm WHERE tier='fine' and danceability WHERE tier='coarse' for cross-archive scans."

requirements-completed: [ANL-01]

# Metrics
duration: ~20min
completed: 2026-06-10
---

# Phase 31 Plan 02: AnalysisWindow Model + Migration 018 Summary

**Added the queryable per-window time-series child table (`analysis_window`, 1:many to `files`, `ON DELETE CASCADE`) and an additive Alembic migration 018 with composite + partial + label indexes, leaving the existing 1:1 `analysis` aggregate structurally untouched.**

## Performance

- **Duration:** ~20 min
- **Tasks:** 2/2 completed
- **Files modified:** 6 (3 created, 3 modified)

## Accomplishments

- **Task 1 (TDD):** `AnalysisWindow(TimestampMixin, Base)` added to `models/analysis.py`. `file_id` is a UUID FK to `files.id` with `ondelete="CASCADE"`, `index=True`, `nullable=False`, NOT unique (1:many). Fine-tier columns (`bpm`, `musical_key`) and coarse-tier columns (`mood`, `style`, `danceability`, `features` JSONB) are all nullable. `AnalysisResult` was left structurally unchanged (still `unique=True`, no `ondelete`). Registered in `models/__init__.py` for Alembic discovery and updated `test_all_tables_defined` to expect 15 tables.
- **Task 2:** Migration `018_add_analysis_window_table.py` — `create_table("analysis_window", ...)` with a CASCADE FK and 5 indexes: composite `(file_id, tier, window_index)`, partial `bpm WHERE tier='fine'`, partial `danceability WHERE tier='coarse'`, and label indexes on `mood` and `style`. No data migration, no in-place schema change against `analysis`. Bare-number `revision="018"` / `down_revision="017"`; single alembic head.

## Verification

- `uv run pytest tests/test_models/test_analysis_window.py` — 13 passed.
- `uv run pytest tests/test_migrations/test_migration_018.py` — 2 passed (table+index creation, real-DB CASCADE delete leaves no orphans, table dropped on downgrade).
- `uv run pytest tests/test_models/ tests/test_migrations/` — 123 passed (no regressions; `test_all_tables_defined` updated).
- `uv run alembic heads` — reports single head `018`.
- `uv run mypy src/phaze/models/analysis.py` — clean.
- `uv run ruff check` on all new/changed files — clean.
- Grep gates: 1× `class AnalysisWindow(TimestampMixin, Base)`; exactly 1 `ondelete="CASCADE"` in the model file; `revision="018"`/`down_revision="017"`; 1 `bpm` partial index `tier = 'fine'`; 0 occurrences of "alter" in migration 018.
- Pre-commit hooks (ruff, ruff-format, bandit, mypy) passed on every commit.

## Threat Model Coverage

- **T-31-02-01 (orphaned children):** mitigated — `ON DELETE CASCADE` on `AnalysisWindow.file_id`; migration test asserts a file delete cascade-removes its windows.
- **T-31-02-02 (bad migration / branch):** mitigated — bare-number `down_revision="017"`; `alembic heads` confirms a single head.
- **T-31-02-03 (ORM/DB CASCADE mismatch):** mitigated — `AnalysisResult` unchanged; grep gate confirms exactly 1 CASCADE FK in the module.
- **T-31-02-SC (dependency installs):** mitigated — zero new packages added.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Updated `test_all_tables_defined` for the new table**
- **Found during:** Task 1
- **Issue:** `tests/test_models/test_core_models.py::test_all_tables_defined` asserts the exact set of tables in `Base.metadata`; adding `AnalysisWindow` introduced a 15th table (`analysis_window`), failing the pre-existing equality assertion.
- **Fix:** Added `"analysis_window"` to the expected set and updated the docstring/count (14 → 15).
- **Files modified:** tests/test_models/test_core_models.py
- **Commit:** c6b46ef

**2. [Rule 3 - Blocking] Reworded migration docstring to avoid the literal token "ALTER"**
- **Found during:** Task 2
- **Issue:** The acceptance-criteria gate `grep -ci "alter"` must be 0, but the explanatory docstring used the word "ALTER" twice (describing that there is NO ALTER), tripping the gate.
- **Fix:** Reworded to "in-place schema change" / "DB-level constraint change" while preserving meaning. The migration genuinely performs no `ALTER`.
- **Files modified:** alembic/versions/018_add_analysis_window_table.py
- **Commit:** 25e5fd2

## Known Stubs

None. The table is intentionally created empty (0 rows) — it is populated by later plans (analysis service rewrite + agent_analysis router windows payload), which this plan's `affects` list tracks.

## Self-Check: PASSED

- FOUND: src/phaze/models/analysis.py (AnalysisWindow)
- FOUND: alembic/versions/018_add_analysis_window_table.py
- FOUND: tests/test_models/test_analysis_window.py
- FOUND: tests/test_migrations/test_migration_018.py
- FOUND: commit 061c8e9 (test RED)
- FOUND: commit c6b46ef (model GREEN)
- FOUND: commit 25e5fd2 (migration 018)
