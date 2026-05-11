---
phase: 24
plan: 05
subsystem: ingestion-service
tags: [sqlalchemy, postgres-on-conflict, composite-unique-index, agent-attribution, tdd]

# Dependency graph
requires:
  - phase: 24-02
    provides: "FileRecord.agent_id (NOT NULL), ScanBatch.agent_id (NOT NULL), composite UQ uq_files_agent_id_original_path"
  - phase: 24-03
    provides: "legacy-application-server agent row seeded in migration 012 (born revoked, scan_roots resolved from SCAN_PATH)"
provides:
  - "phaze.services.ingestion.LEGACY_AGENT_ID module constant"
  - "discover_and_hash_files now stamps agent_id on every record dict (10-key shape)"
  - "bulk_upsert_files conflict target aligned with post-013 composite UQ"
  - "run_scan now constructs ScanBatch with agent_id=LEGACY_AGENT_ID"
  - "tests/test_services/test_ingestion.py::test_bulk_upsert_same_path_different_agent (new)"
affects:
  - Phase 25 HTTP API integration (MUST remove LEGACY_AGENT_ID and replace with per-request agent attribution from bearer-token-derived agent_id)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Module-level placeholder constant with cross-reference comment to the resolving phase (LEGACY_AGENT_ID = ..., comment names Phase 25)"
    - "Postgres INSERT ... ON CONFLICT (composite_index_columns) DO UPDATE — leading-column order in index_elements matches the composite UQ leading column (D-15)"

key-files:
  modified:
    - src/phaze/services/ingestion.py
    - tests/test_services/test_ingestion.py

key-decisions:
  - "Constant placed between the TYPE_CHECKING block and the module logger so it sits with module-level metadata, not buried inside a function body"
  - "Conflict-target column order is ['agent_id', 'original_path'] (leading column = agent_id) to match the composite UQ uq_files_agent_id_original_path declared in Plan 02's model (D-15)"
  - "test_discover_files_record_keys expected_keys set updated to include 'agent_id' — Rule 1 fix; the assertion enumerated the pre-Plan-05 9-key shape and the new contract requires 10 keys"
  - "No Agent model imported into ingestion.py — the service only needs the string slug, not the model class; importing the model would create an unnecessary coupling and would NOT be exercised by any callsite"

patterns-established:
  - "Phase placeholder constant pattern: name the resolving phase in the comment so future maintainers can trace removal (LEGACY_AGENT_ID's comment cites Phase 25 explicitly)"

requirements-completed: [DATA-02]

# Metrics
duration: ~20 min
completed: 2026-05-11
tasks_completed: 1
files_created: 0
files_modified: 2
commits: 2
---

# Phase 24 Plan 05: Ingestion service composite-conflict-target + LEGACY_AGENT_ID stamping Summary

**Ingestion service now stamps the Phase 24 placeholder `legacy-application-server` agent_id on every newly-discovered FileRecord and ScanBatch, and its bulk-upsert uses the composite conflict target `(agent_id, original_path)` matching the post-013 unique index. Plan 04's migration 013 (which drops `uq_files_original_path` and creates `uq_files_agent_id_original_path`) no longer breaks `bulk_upsert_files` at runtime — RESEARCH Pitfall 1 is closed.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-05-11T20:08Z
- **Completed:** 2026-05-11T20:28Z
- **Tasks:** 1 (`tdd="true"` — RED + GREEN commits)
- **Files created:** 0
- **Files modified:** 2 (`src/phaze/services/ingestion.py`, `tests/test_services/test_ingestion.py`)
- **Commits:** 2 (RED, GREEN)

## Accomplishments

- `LEGACY_AGENT_ID = "legacy-application-server"` declared once at module level with a comment citing Phase 25 as the resolving phase.
- Three production edit sites delivered exactly as specified:
  1. Module constant after the TYPE_CHECKING block, before the logger
  2. `agent_id` added to every record dict in `discover_and_hash_files` (10-key shape)
  3. `bulk_upsert_files` conflict target swapped from `["original_path"]` → `["agent_id", "original_path"]`
  4. `run_scan`'s ScanBatch constructor now passes `agent_id=LEGACY_AGENT_ID`
- New integration test `test_bulk_upsert_same_path_different_agent` exercises the composite UQ invariant: two records with the same `original_path` under different `agent_id` values both persist as separate rows (no upsert collision).
- Both existing integration tests (`test_bulk_upsert_stores_paths`, `test_bulk_upsert_handles_duplicates`) updated for the new record shape: each now seeds the legacy `Agent` row, passes `agent_id=LEGACY_AGENT_ID` to the `ScanBatch` constructor, and includes `"agent_id": LEGACY_AGENT_ID` on every record dict.
- One Rule 1 deviation auto-fixed: `test_discover_files_record_keys` updated to include `agent_id` in its `expected_keys` set (see Deviations below).
- All 18 unit tests in `tests/test_services/test_ingestion.py` pass. 505 tests in the broader suite pass with no regressions from these edits (migration-test failures are unrelated, pre-existing Plan 24-03-documented operator-pre-condition gaps).

## Task Commits

| # | Stage | Commit | Message |
|---|-------|--------|---------|
| 1 | RED | `cae0c86` | `test(24-05): add failing tests for LEGACY_AGENT_ID stamping and composite UQ` |
| 1 | GREEN | `b63d74e` | `feat(24-05): stamp LEGACY_AGENT_ID and swap to composite conflict target` |

No REFACTOR commit was needed — the GREEN-state code matches the plan's target shape exactly and is already as concise as it can be.

## Files Created/Modified

### `src/phaze/services/ingestion.py` (modified, +5 / -1)

Four production edits:

1. **Module constant** (between TYPE_CHECKING block and logger):
   ```python
   LEGACY_AGENT_ID = "legacy-application-server"  # Phase 24 placeholder; Phase 25 wires real attribution per agent.
   ```
2. **`discover_and_hash_files` record dict** (line ~74) — `"agent_id": LEGACY_AGENT_ID` added as second key after `"id"`.
3. **`bulk_upsert_files` conflict target** (line ~106):
   ```python
   index_elements=["agent_id", "original_path"],  # composite UQ swapped in migration 013
   ```
4. **`run_scan` ScanBatch constructor** (line ~135) — `agent_id=LEGACY_AGENT_ID` passed alongside `id`, `scan_path`, `status`, etc.

### `tests/test_services/test_ingestion.py` (modified, +83 / -5)

- Imports: added `from phaze.models.agent import Agent` and `LEGACY_AGENT_ID` to the existing `from phaze.services.ingestion import ...` line (sorted alphabetically per ruff isort).
- `test_discover_files_record_keys` — added `"agent_id"` to `expected_keys` set (Rule 1 fix; ruff reflowed across multiple lines).
- `test_bulk_upsert_stores_paths` — seeds legacy `Agent` row before `ScanBatch`, passes `agent_id=LEGACY_AGENT_ID` to ScanBatch, includes `"agent_id": LEGACY_AGENT_ID` in every record dict.
- `test_bulk_upsert_handles_duplicates` — same fixture additions on both `original_record` and `updated_record`.
- New `test_bulk_upsert_same_path_different_agent` — seeds two agents (`legacy-application-server` + `agent-b`), inserts two ScanBatch rows (one per agent), and bulk-upserts two records with identical `original_path = "/music/shared.mp3"` but different `agent_id`. Asserts both rows persist.

## Decisions Made

- **Constant placement.** Plan suggested "near line 26 (before logger), after the imports block." Placed it between the existing `if TYPE_CHECKING:` block and `logger = logging.getLogger(__name__)`. This keeps module-level metadata (TYPE_CHECKING + constants + logger) grouped together. Two blank lines after the TYPE_CHECKING block (PEP 8) and one blank line between the constant and the logger.
- **Conflict-target comment.** Added inline comment `# composite UQ swapped in migration 013` next to `index_elements=["agent_id", "original_path"]` (not a `TODO`, per the plan's "DO NOT add an inline `# TODO`" directive). The comment documents the migration the conflict target tracks; the LEGACY_AGENT_ID constant's own comment covers the Phase 25 follow-up.
- **No reformat of the record dict for alphabetical key order.** Plan said "as the second key (after `\"id\"`), or alphabetically; preserve the existing key ordering otherwise." Chose second-after-id (matches the plan's first preference and matches the existing ordering convention where `id` is always first). The remaining keys keep their pre-existing insertion order.
- **One commit per TDD phase.** RED commit (`cae0c86`) lands tests-only changes; GREEN commit (`b63d74e`) lands the production edits plus the Rule 1 fix to `test_discover_files_record_keys` (a non-behavior assertion update that exists only because the production contract is changing). The Rule 1 fix was bundled into the GREEN commit because it would have failed at GREEN time without it; splitting it into a third commit would not have improved bisectability.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `test_discover_files_record_keys::expected_keys` enumerated the pre-Plan-05 9-key shape**

- **Found during:** Task 1 GREEN-phase test run (`uv run pytest tests/test_services/test_ingestion.py -x -v --no-cov`).
- **Issue:** After adding `"agent_id": LEGACY_AGENT_ID` to the record dict in `discover_and_hash_files`, the assertion `assert set(results[0].keys()) == expected_keys` in `test_discover_files_record_keys` failed with "Extra items in the left set: 'agent_id'". The test enumerates the exact expected key set, and the old 9-key set is now wrong.
- **Fix:** Updated `expected_keys` in `test_discover_files_record_keys` (lines 154-165) to include `"agent_id"`. This is the same test contract being updated in step with the production contract — the test is asserting against the production contract, so when production changes, the assertion must too. Bundled into the GREEN commit because it is directly tied to the GREEN production change.
- **Files modified:** `tests/test_services/test_ingestion.py`
- **Commit:** `b63d74e` (GREEN)
- **Rationale for Rule 1 (not Rule 4):** No architectural change. The test's expected_keys set is a literal enumeration of the production dict's keys; updating it to match the new production contract is the same kind of mechanical-correctness fix that Rule 1 is designed for.

### Quality-tool reflows

**1. [Format - ruff] `expected_keys` set reformatted across multiple lines**

- **Found during:** GREEN commit's pre-commit hook (`ruff format` rewrote 1 file).
- **Issue:** With `"agent_id"` added, the single-line `expected_keys` set became 158 characters, over the 150-character `line-length` ceiling.
- **Fix:** Accepted ruff's reformat (multi-line set literal, one key per line, trailing comma). Semantically identical; satisfies pre-commit's ruff-format hook.
- **Files modified:** `tests/test_services/test_ingestion.py`
- **Commit:** `b63d74e` (GREEN, after re-staging the post-hook contents)

### Other observations

- **`just scan` end-to-end verification deferred.** Plan output section asked for "confirmation that `just scan` against a dev `phaze` DB still works end-to-end." This requires a running Postgres on `localhost:5432` (the same operator pre-condition documented in Plan 24-03's SUMMARY). The Docker daemon is not running in this sandbox (`docker ps` reports `failed to connect to the docker API`); `psql` is not present (`command -v psql` is empty). The static analysis is complete and grep-verifiable: `bulk_upsert_files`' new conflict target matches the index name and column order from Plan 02's `FileRecord.__table_args__` byte-for-byte (`uq_files_agent_id_original_path` on `("agent_id", "original_path")`), and Plan 03's migration 012 seeds the legacy `Agent` row that the new `agent_id` FK now references. **Operator must run `just scan` once Postgres is available** to confirm end-to-end behavior.
- **No test fixtures beyond the documented Agent seeding required.** The plan asked for a note on whether test fixtures needed shape changes beyond Agent seeding. None did. The existing `session` fixture in `tests/conftest.py` builds the schema via `Base.metadata.create_all` against the models, which already include `agents`, both `agent_id` columns, and the composite UQ from Plan 02. The fixture works without modification.

## Issues Encountered

- **Postgres not available locally.** This sandbox has no running Postgres on `localhost:5432` and no Docker daemon. The 3 integration tests in `tests/test_services/test_ingestion.py` (the two updated existing ones + the new `test_bulk_upsert_same_path_different_agent`) collect successfully and reach the TCP connect step in `asyncpg.connect`, where they fail with `OSError: [Errno 61] Connect call failed ('127.0.0.1', 5432), [Errno 61] Connect call failed ('::1', 5432, 0, 0)`. This is the same documented operator-pre-condition gap Plan 24-03 surfaced; nothing about Plan 05 changes the pre-condition requirements. **Operator must provision `phaze_test` on `localhost:5432` to run these three tests to a passing assertion.** Once available, all three are expected to pass: the production code now matches the schema, and the test bodies seed the FK target Agent row before the FK-bearing ScanBatch/FileRecord rows.
- **Migration tests fail with the same operator pre-condition.** The full-suite run shows 4 failures + 9 errors in `tests/test_migrations/test_012_upgrade.py`. All are `OSError: Connect call failed ('127.0.0.1', 5432)` — same documented pre-existing operator pre-condition, identical to what Plan 24-03's SUMMARY recorded. These are not regressions from Plan 05; they pre-exist and are out of scope per the executor's scope-boundary rule.

## User Setup Required

**To complete end-to-end verification of `just scan`:** provision Postgres on `localhost:5432` with the `phaze` database (the dev application DB, not the migrations-test DB) and run:

```bash
just db-upgrade head    # ensures migrations 011, 012, 013 are applied
just scan               # scans the SCAN_PATH directory; should populate files with agent_id='legacy-application-server'
```

Then verify: `psql phaze -c "SELECT agent_id, count(*) FROM files GROUP BY agent_id"` — expected output: one row, `agent_id = 'legacy-application-server'`, `count > 0`.

To run the three new/updated integration tests, the same `phaze_test` DB pre-condition from earlier ingestion-test work applies: `CREATE DATABASE phaze_test OWNER phaze;` on `localhost:5432`.

## Verification

| Check | Result |
|-------|--------|
| `grep -c '^LEGACY_AGENT_ID = "legacy-application-server"' src/phaze/services/ingestion.py` | 1 |
| `grep -v '^#' src/phaze/services/ingestion.py \| grep -c '"legacy-application-server"'` | 1 |
| `grep -c '"agent_id": LEGACY_AGENT_ID' src/phaze/services/ingestion.py` | 1 |
| `grep -c 'agent_id=LEGACY_AGENT_ID' src/phaze/services/ingestion.py` | 1 |
| `grep -F 'index_elements=["agent_id", "original_path"]' src/phaze/services/ingestion.py` | matches |
| `grep -c 'index_elements=\["original_path"\]' src/phaze/services/ingestion.py` | 0 |
| `grep -v '^#' src/phaze/services/ingestion.py \| grep -c 'from phaze.models.agent'` | 0 |
| `grep -c 'test_bulk_upsert_same_path_different_agent' tests/test_services/test_ingestion.py` | 1 |
| `grep -c 'session.add(Agent(' tests/test_services/test_ingestion.py` | 4 (>=3) |
| `uv run mypy src/phaze/services/ingestion.py` | Success: no issues found in 1 source file |
| `uv run ruff check src/phaze/services/ingestion.py tests/test_services/test_ingestion.py` | All checks passed! |
| `uv run ruff format --check ...` | 2 files already formatted |
| `uv run pytest tests/test_services/test_ingestion.py -v --no-cov` (unit subset) | 18 passed, 3 deselected |
| Full unit suite: `uv run pytest -q --no-cov -m "not integration" --ignore=tests/test_migrations` | 505 passed, 264 deselected, 0 failed |
| Integration tests reach actual upsert path | Fail at `OSError: Connect call failed ('127.0.0.1', 5432)` (documented operator pre-condition) |
| Pre-commit hooks on both commits | All hooks pass (ruff, ruff-format, bandit, mypy) |

The grep-contract acceptance criteria from the plan are all green. The "all 3 integration tests pass" acceptance criterion requires operator-provisioned Postgres and is documented as unmet-in-this-sandbox; once Postgres is available the three integration tests are expected to pass against the schema Plan 02 and migrations 012/013 already establish.

## Next Phase Readiness

- **Plan 24-06 / Phase 24 close-out.** Plan 05 is the final plan in Phase 24. All four Phase 24 requirements (DATA-01..DATA-04) are now covered: DATA-01 (agents table — Plan 03), DATA-02 (ingestion stamping + composite conflict target — this plan), DATA-03 (LIVE sentinel + partial UQ — Plan 03), DATA-04 (born-revoked legacy agent + SCAN_PATH-resolved scan_roots — Plan 03).
- **Phase 25 entry conditions.** Phase 25 must:
  1. Remove the `LEGACY_AGENT_ID` constant from `src/phaze/services/ingestion.py`.
  2. Replace `LEGACY_AGENT_ID` references with per-request agent attribution from the bearer-token-derived `agent_id` (the HTTP API request context).
  3. Decide whether `discover_and_hash_files` accepts `agent_id` as an explicit parameter (passed down from the HTTP handler) or whether it derives it from a context-var the request middleware sets. Recommend the explicit-parameter approach for testability.
  4. The legacy agent's `revoked_at` is set; Phase 25's authentication path must explicitly enroll new agents (or unrevoke the legacy one via an explicit operator action) before they can authenticate.

## Known Stubs

None. The `LEGACY_AGENT_ID` constant is intentionally a placeholder — but it is a real, well-typed string that produces a real, FK-valid row in Postgres. It is not "empty data flowing to UI rendering"; it is a working attribution value that Phase 25 will replace with operator-supplied values. The Phase 25 commitment is documented inline in `src/phaze/services/ingestion.py` and in the `key-decisions` frontmatter above.

## Threat Flags

None. The plan's `<threat_model>` enumerated two trust-boundary threats:

- **T-24-05-T (wrong agent_id stamped):** Mitigated. Single module-level constant with one defined value; `grep -v '^#' src/phaze/services/ingestion.py | grep -c '"legacy-application-server"'` returns 1 (the constant declaration is the only occurrence of the literal outside comments).
- **T-24-05-T (conflict-target column order mismatch):** Mitigated. `grep -F 'index_elements=["agent_id", "original_path"]' src/phaze/services/ingestion.py` matches the exact byte string asserted by the threat model's mitigation; the alternative order `["original_path", "agent_id"]` would silently change Postgres index selection (RESEARCH Pitfall 1 variant).

No new surface introduced beyond the planned trust boundaries; no `threat_flag` entries needed.

## Self-Check: PASSED

- `src/phaze/services/ingestion.py` — FOUND
- `tests/test_services/test_ingestion.py` — FOUND
- Commit `cae0c86` (RED) — FOUND in git log
- Commit `b63d74e` (GREEN) — FOUND in git log

---
*Phase: 24-schema-foundation-agent-registry*
*Plan: 05*
*Completed: 2026-05-11*
