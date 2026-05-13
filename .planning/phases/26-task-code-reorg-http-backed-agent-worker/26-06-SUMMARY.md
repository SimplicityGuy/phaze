---
phase: 26-task-code-reorg-http-backed-agent-worker
plan: 06
subsystem: api
tags: [python, fastapi, sqlalchemy, upsert, http-api, idempotent, agent-internal]

# Dependency graph
requires:
  - phase: 26-task-code-reorg-http-backed-agent-worker
    provides: "AnalysisWritePayload + AnalysisWriteResponse schemas (Plan 03)"
  - phase: 25-internal-agent-http-api-bearer-auth
    provides: "get_authenticated_agent dependency + Phase 25 CR-01 exclude_unset convention (agent_metadata.py)"
provides:
  - "src/phaze/routers/agent_analysis.py — PUT /api/internal/agent/analysis/{file_id} idempotent upsert (D-26)"
  - "_summarize_dict_to_string helper (W6) — deterministic top-3-by-score string compaction for mood/style dicts"
  - "Wire-to-storage funnel: D-26 wire fields without dedicated columns (danceability, energy) land in AnalysisResult.features JSONB without an Alembic migration"
affects:
  - "Plan 11 (Wave 3 process_file task rewrite — calls this endpoint after essentia analysis)"
  - "Plan 12 (Wave 4 main.py wiring — includes agent_analysis.router in create_app())"

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Three-tier dict-to-column funnel: pg_insert payload | _summarize_dict_to_string for String(50) columns | overflow funnel to features JSONB"
    - "Deterministic dict summarization: (-score, key) two-key sort for replay-safe storage of top-3 classifier outputs"
    - "Self-deleting tripwire pattern (Phase 26 W2 -> W3 handoff): `# type: ignore[import-not-found]` + `warn_unused_ignores=true` automatically alerts when parallel dependencies land"

key-files:
  created:
    - "src/phaze/routers/agent_analysis.py"
    - "tests/test_routers/test_agent_analysis.py"
    - "tests/test_routers/test_summarize_dict_to_string.py"
  modified:
    - "src/phaze/services/agent_client.py (removed four self-deleting `# type: ignore[import-not-found]` tripwires; see deviation 1 below)"

key-decisions:
  - "Overflow funnel for non-column wire fields: danceability and energy land in AnalysisResult.features JSONB, not dropped on the floor. Preserves D-26's wire contract end-to-end without requiring an Alembic migration this phase. Plan 11's process_file rewrite produces the same wire shape; a future migration can promote these to dedicated columns when query patterns demand it."
  - "mood/style serialization: top-3-by-score `k=v,k=v,k=v` summary bounded at 50 chars, with deterministic (-score, key) tiebreak. Trades information loss (drops keys 4..N) for a no-migration path. Replay-safe: identical inputs always produce identical strings."
  - "Storage discretion area resolved by funneling rather than expanding the schema, because the plan explicitly scoped the migration out (`no Alembic migration this phase`)."

patterns-established:
  - "Boundary-layer overflow funnel: handlers MAY route wire-format fields without backing columns into a JSONB sidecar column on the same row, preserving the wire contract while leaving the schema unchanged. The funnel respects field-level LWW (CR-01) because it merges into rather than replaces the JSONB column."
  - "Deterministic two-key sort for replay-safe string summarization: `sorted(items, key=lambda kv: (-kv[1], kv[0]))[:N]` is the canonical pattern for compacting classifier-score dicts into bounded strings."

requirements-completed:
  - TASK-03

# Metrics
duration: 13min
completed: 2026-05-12
---

# Phase 26 Plan 06: HTTP-Backed Audio Analysis Endpoint Summary

**PUT /api/internal/agent/analysis/{file_id} — idempotent essentia-analysis upsert with field-level LWW, top-3 dict summarization, and JSONB overflow funnel for un-columned wire fields**

## Performance

- **Duration:** 13 min
- **Started:** 2026-05-12T21:36:16Z
- **Completed:** 2026-05-12T21:49:45Z
- **Tasks:** 3
- **Files created:** 3
- **Files modified:** 1 (tripwire cleanup)

## Accomplishments

- New router `src/phaze/routers/agent_analysis.py` (133 lines) — third internal-agent endpoint after agent_metadata and agent_fingerprint. Mirrors agent_metadata.py byte-for-byte where the patterns apply: pg_insert + on_conflict_do_update with `exclude_unset=True` (CR-01 convention), `on_conflict_do_nothing` empty-body fallback, `payload["id"] = uuid.uuid4()` PK stamping for Python-only default columns.
- `_summarize_dict_to_string` helper (W6) with deterministic `(-score, key)` two-key sort, top-3 keys, 50-char cap.
- Overflow funnel for D-26 wire-only fields (danceability, energy): merged into `AnalysisResult.features` JSONB rather than dropped, preserving the wire contract without an Alembic migration.
- 8 contract tests for the router (happy path, replay idempotence, partial-PUT CR-01 LWW, empty-body no-op, first-PUT-with-empty-body row creation, 422 on extra fields, 401 missing auth, 403 unknown token) + 6 parametrized unit tests for the helper.
- All `uv run` verification commands clean: pytest (14/14), ruff check, ruff format, mypy strict (102 source files), pre-commit (all hooks).

## Task Commits

1. **Task 1: Contract tests (RED)** — `2fd9f79` (test)
2. **Task 2: Router implementation (GREEN)** — `cc5559d` (feat) — includes test contract update for the overflow funnel
3. **Task 3: Helper unit tests** — `72e9d31` (test)

## Files Created/Modified

### Created

- `src/phaze/routers/agent_analysis.py` — PUT /api/internal/agent/analysis/{file_id} handler + `_summarize_dict_to_string` helper.
- `tests/test_routers/test_agent_analysis.py` — 8 contract tests using the smoke-app pattern.
- `tests/test_routers/test_summarize_dict_to_string.py` — 6 parametrized unit tests (5 in `@pytest.mark.parametrize` + 1 length-cap case).

### Modified

- `src/phaze/services/agent_client.py` — removed four `# type: ignore[import-not-found]` tripwires that fired (as designed) when Plan 03 schemas merged into this branch. See deviation 1 below.

## Decisions Made

### D-1: Overflow funnel for un-columned wire fields

**Context:** D-26's `AnalysisWritePayload` declares `danceability: float | None` and `energy: float | None`, but `AnalysisResult` has no such columns (Phase 5 schema only has `bpm`, `musical_key`, `mood`, `style`, `fingerprint`, `features`).

**Options considered:**
1. **Drop the fields silently** — breaks D-26's wire contract and CR-01 field-level LWW (caller-set field is invisibly lost).
2. **Add columns via Alembic migration** — out of scope (plan explicitly says "no Alembic migration this phase"). Architectural change (Rule 4) blocked by scope.
3. **Funnel into existing `features` JSONB column** — preserves D-26 wire contract, no migration needed, CR-01 LWW honored via dict-merge semantics, future migration can promote columns when query patterns demand it.

**Selected:** Option 3 (funnel). The helper code is 5 lines:
```python
overflow = {k: dumped.pop(k) for k in list(dumped) if k not in _ANALYSIS_COLUMN_FIELDS}
if overflow:
    existing_features = dumped.get("features")
    merged_features: dict[str, object] = dict(existing_features) if isinstance(existing_features, dict) else {}
    merged_features.update(overflow)
    dumped["features"] = merged_features
```

### D-2: top-3 string summarization with deterministic tiebreak

**Context:** D-26's wire format is `mood: dict[str, float]` but the column is `String(50)`. A naive `json.dumps()` of a 10-mood classifier dict overflows the column (~70+ chars).

**Resolution:** Top-3 keys by score, formatted `"k=v,k=v,k=v"` with `(-score, key)` two-key sort for deterministic replay. Trades information (drops keys 4..N) for a bounded, replay-safe storage representation that fits the existing column. The wire contract is unchanged; only the on-disk representation is lossy.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking] Removed self-deleting tripwires in services/agent_client.py**

- **Found during:** Task 1 commit (pre-commit mypy hook failed).
- **Issue:** Pre-existing `# type: ignore[import-not-found]` comments on four schema imports (agent_analysis, agent_identity, agent_proposals, agent_tracklists) were placed in Phase 26 Plan 02 by design as "self-deleting tripwires": once Plan 03 schemas existed, `warn_unused_ignores=true` would correctly error and force the comment removal. Plan 03 has merged into this branch (commit `b303468`), so the tripwires fire on every mypy run, blocking pre-commit on every commit in this worktree.
- **Fix:** Removed the four `# type: ignore[import-not-found]` directives + the now-obsolete explanatory comment above the first one.
- **Files modified:** `src/phaze/services/agent_client.py`
- **Verification:** `uv run mypy .` exits 0 with no errors (102 source files).
- **Committed in:** `2fd9f79` (rolled into Task 1 because it blocked Task 1's pre-commit gate).

**2. [Rule 1 — Bug fix in plan defect] Updated test assertions to match the overflow funnel storage**

- **Found during:** Task 2 GREEN attempt (first test run after router shipped).
- **Issue:** Plan 06's test specification (lines 230-250) asserted `row.danceability == 0.8` and `row.energy == 0.9` against the `AnalysisResult` ORM model. These columns don't exist on the model — `pg_insert(...).values([{..., "danceability": ..., ...}])` raised `KeyError: 'danceability'` from SQLAlchemy's `ColumnCollection.__getitem__`. The plan author appears to have written the tests assuming an unspecified schema shape.
- **Fix:** Adopted the overflow funnel (D-1 above) so the wire contract is preserved without a migration, and updated the affected test assertions to verify against `row.features["danceability"]` / `row.features["energy"]` (where the funnel lands them). The contract for `bpm`, `musical_key`, `mood`, `style` is unchanged.
- **Files modified:** `tests/test_routers/test_agent_analysis.py`
- **Verification:** All 8 contract tests pass; CR-01 LWW invariant verified for both columns AND JSONB sub-fields (partial PUT preserves `features.danceability`).
- **Committed in:** `cc5559d` (rolled into Task 2 GREEN commit).

---

**Total deviations:** 2 auto-fixed (1 blocking pre-commit gate, 1 plan-spec defect fixup).
**Impact on plan:** Both deviations are mechanical and preserve the plan's intent. The wire contract from D-26 is unchanged; the storage representation is documented in this SUMMARY and in the router docstring. Future migration to dedicated `danceability`/`energy` columns is deferred but straightforward.

## Issues Encountered

- **Pre-existing test-isolation issue in `tests/test_routers/`:** running the entire `tests/test_routers/` directory together produces 119 pre-existing errors (e.g., `test_tracklists.py::test_match_discogs_enqueues_task`) due to a `legacy-application-server` agent fixture collision across files. This is **NOT caused by Plan 06's work** — verified by stashing Plan 06 changes and observing the same failure count. Out of scope for this plan; deferred. Plan 06's own tests pass cleanly in isolation and alongside `test_agent_metadata.py` (19/19).

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- **Plan 11 (process_file rewrite)** can now PUT essentia-analysis results to `/api/internal/agent/analysis/{file_id}` instead of using the ORM-bound `analysis_result.bpm = ...; await session.commit()` pattern.
- **Plan 12 (main.py wiring)** needs to add `app.include_router(agent_analysis.router)` to `create_app()` — the router is not yet wired into the FastAPI app (per scope, Plan 12 is responsible).
- No blockers for downstream waves.

## Self-Check: PASSED

- All 3 created files exist on disk.
- All 3 task commits (`2fd9f79`, `cc5559d`, `72e9d31`) are reachable in `git log`.
- Full verification suite clean: `uv run pytest tests/test_routers/test_agent_analysis.py tests/test_routers/test_summarize_dict_to_string.py` (14/14 passed), `uv run mypy .` (no issues, 102 source files), `uv run ruff check .` (all checks passed), `uv run ruff format --check .` (171 files formatted), `pre-commit run --all-files` (all hooks passed).

---
*Phase: 26-task-code-reorg-http-backed-agent-worker*
*Completed: 2026-05-12*
