---
phase: 84-dedup-fingerprint-progress-cutover
plan: 03
subsystem: database
tags: [sqlalchemy, dedup, marker, pg_insert, cas, shadow-compare, derive-dont-store]

# Dependency graph
requires:
  - phase: 84-01-migration-035
    provides: migration 035 bidirectional reconcile (marker ≡ state at cutover) + no-op downgrade
  - phase: 84-02-shared-predicate
    provides: services/stage_status.dedup_resolved_clause() — file-level correlated exists(marker)
provides:
  - "services/dedup.py cut over to the durable dedup_resolution marker: pg_insert writer in resolve_group, DELETE...RETURNING CAS undo, nine reader flips to ~dedup_resolved_clause()"
  - "tests/integration/test_dedup_divergence.py — mutation-tested inconsistent-corpus guard across the five dedup readers"
  - "tests/integration/test_dedup_resolve_undo_shadow.py — mutation-tested resolve→undo→re-resolve shadow-compare gate (D-16.1)"
affects: [phase-86-proposal-sidecar, phase-90-filestate-drop]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Marker as the single CAS domain: DELETE...RETURNING file_id scopes the FileRecord dual-write restore to only the ids that held a marker (83 D-09 analogue)"
    - "pg_insert bypasses a Python-side PK default — stamp id=uuid4() explicitly per row (agent_analysis.py precedent)"
    - "Inconsistent-corpus divergence testing: marker≢state so 'reads marker' vs 'reads state' are distinguishable; mutation-tested both directions"

key-files:
  created:
    - tests/integration/test_dedup_divergence.py
    - tests/integration/test_dedup_resolve_undo_shadow.py
  modified:
    - src/phaze/services/dedup.py
    - tests/discovery/services/test_dedup.py

key-decisions:
  - "D-02/D-03/D-07: one bulk pg_insert(DedupResolution) per non-canonical file with explicit uuid id + canonical_file_id=canonical_id, on_conflict_do_nothing(file_id); caller-owned txn, flush never commit"
  - "D-05/D-06: undo is one DELETE(DedupResolution)...RETURNING file_id CAS; previous_state restored ONLY for returned ids; stale replay finds no marker → 0 rows → no-op"
  - "D-00a: the dual-writer f.state = FileState.DUPLICATE_RESOLVED SURVIVES in resolve_group (dies Phase 90); only reliance on state is removed"
  - "T-84-03-02: undo coerces the browser-supplied previous_state to a real FileState member (FileState(value)) and skips unknown values before writing FileRecord.state"
  - "Two discovery unit tests updated to seed markers (the cutover makes the marker, not state, the exclusion/undo authority)"

patterns-established:
  - "A green guard proves nothing: both new tests were mutation-tested (reader-revert → count 1≠2 RED; writer-delete → hard_fail_total=1 RED) then restored to GREEN"

requirements-completed: [READ-04, SIDECAR-02]

# Metrics
duration: 40min
completed: 2026-07-09
---

# Phase 84 Plan 03: Dedup Marker Cutover Summary

**Cut `services/dedup.py` over to the durable `dedup_resolution` marker — adding the go-forward `pg_insert` writer that has not existed since migration 032's one-shot backfill, converting `undo_resolve` into a `DELETE ... RETURNING file_id` CAS, and flipping all nine `FileRecord.state != DUPLICATE_RESOLVED` read sites to `~dedup_resolved_clause()` — proven by two mutation-tested integration guards (inconsistent-corpus divergence across five readers; resolve→undo→re-resolve shadow-compare with a stale-replay no-op).**

## Performance
- **Duration:** ~40 min
- **Tasks:** 3
- **Files created:** 2 · **Files modified:** 2

## Accomplishments
- **`resolve_group` writer (D-01/D-02/D-03/D-07):** after the surviving dual-write loop, one bulk `pg_insert(DedupResolution).values(rows).on_conflict_do_nothing(index_elements=["file_id"])` for every non-canonical file, executed only when `files` is non-empty, inside the caller-owned transaction (flush, never commit). Each row stamps an explicit `id=uuid_mod.uuid4()` (pg_insert bypasses the model's Python-side `default=uuid.uuid4` → NULL-PK otherwise) and `canonical_file_id=canonical_id` (the operator's actual pick, strictly better than 032's `ORDER BY c.id LIMIT 1` guess); `resolved_at` rides its server default.
- **`undo_resolve` CAS (D-05/D-06):** one `delete(DedupResolution).where(file_id.in_(ids)).returning(file_id).execution_options(synchronize_session=False)`; `previous_state` restored **only** for the returned ids. A stale-tab replay against a file with no marker returns 0 rows and no-ops. The browser `[{id, previous_state}]` payload shape is unchanged (no template/router churn).
- **Threat mitigation (T-84-03-02):** `undo_resolve` coerces the attacker-controllable `previous_state` to a real `FileState` member via `FileState(entry["previous_state"])` and skips unknown values before writing `FileRecord.state`; combined with the D-06 CAS scoping (T-84-03-01), a crafted payload of arbitrary ids restores nothing.
- **Nine reader flips (READ-04):** `find_duplicate_groups` (×2), `find_duplicate_groups_with_metadata` (×2), `count_duplicate_groups`, `get_duplicate_stats` (×3), and `resolve_group`'s selection now filter on `~dedup_resolved_clause()`. Zero `FileState.DUPLICATE_RESOLVED` reads remain; the surviving dual-writer assignment is the only occurrence left. The `LIMIT/OFFSET`-without-`ORDER BY` at :81/:131/:207 (Pitfall 7, deferred) was left untouched.
- **Divergence guard (D-14):** inconsistent corpus (File A: marker + `state='analyzed'` → EXCLUDED; File B: `state='duplicate_resolved'` + no marker → INCLUDED) across all five dedup readers.
- **Shadow-compare gate (D-16.1):** `run_shadow_compare(session).hard_fail_total == 0` asserted after resolve, undo, and re-resolve, plus a stale-replay CAS no-op case.

## Task Commits
1. **Task 1 — writer + CAS undo + nine reader flips** — `5f5ab426` (feat)
2. **Task 2 — inconsistent-corpus divergence guard (five readers)** — `7ed693d3` (test)
3. **Task 3 — resolve→undo→re-resolve shadow-compare test (D-16.1)** — `a67ed16a` (test)

## Mutation-Check Evidence (both guards proven RED then restored GREEN)
- **Divergence guard (Task 2):** reverted `count_duplicate_groups`' predicate at `dedup.py:191` from `~dedup_resolved_clause()` back to `FileRecord.state != FileState.DUPLICATE_RESOLVED`. Observed RED:
  ```
  >       assert stats["groups"] == 2
  E       assert 1 == 2
  FAILED tests/integration/test_dedup_divergence.py::test_count_duplicate_groups_marker_is_authority
  FAILED tests/integration/test_dedup_divergence.py::test_get_duplicate_stats_marker_is_authority
  ```
  Restored → 5 passed (GREEN). Under the state-read mutation the H2 group collapses (File D excluded by state), so `count_duplicate_groups` returns 1 and `get_duplicate_stats` groups=1 — both assertions invert exactly as designed.
- **Shadow-compare guard (Task 3):** neutralized the `pg_insert(DedupResolution)` writer in `resolve_group` (dual-write state stays, no marker written). Observed RED: `run_shadow_compare` after a resolve reported **`hard_fail_total = 1`** (the `duplicate_resolved ⇒ marker exists` hard invariant, `shadow_compare.py:135`), and the test failed at `assert dup.id in await _marker_file_ids(...)` / `assert await _marker_file_ids(...) == {f_c.id}`. Restored → 2 passed (GREEN).

## Decisions Made
- **`previous_state` validation (T-84-03-02):** chose to coerce via `FileState(value)` inside `undo_resolve` and `continue` on `ValueError`, rather than trusting the raw browser string. `FileState` is a `StrEnum`, so unit tests passing `FileState.DISCOVERED` (a str) and the browser passing `"discovered"` both round-trip; only genuinely unknown values are skipped. Payload shape unchanged (constraint 4).
- **Undo restore statement shape (Claude's Discretion):** N per-file `update(FileRecord)` statements gated on membership in the returned set — clear and small; a full bulk page is rare and the `update` import stays live (Pitfall 9 resolved: `update` is still used, so it was NOT dropped; `delete` and `pg_insert` were added).
- **Stale-replay test design:** re-resolve uses a **different canonical**, so the stale payload's file becomes the keeper (no marker) and the replayed `DELETE` matches 0 rows — the faithful realization of D-06's "finds no marker, returns zero rows" (a same-canonical re-resolve would re-mark the same file_id and the DELETE would legitimately match it).
- **Group files seeded at `DISCOVERED`** in the shadow test — the one `FileState` with no shadow invariant — so the corpus can only ever trip the `duplicate_resolved` invariant the writer owns, keeping the mutation signal clean.

## Deviations from Plan
### Auto-fixed Issues
**1. [Rule 1 — Bug] Two discovery unit tests broken by the reader/undo cutover**
- **Found during:** Task 1
- **Issue:** `tests/discovery/services/test_dedup.py::test_find_duplicate_groups_excludes_resolved` and `::test_undo_resolve` seeded `state=DUPLICATE_RESOLVED` **without** a marker and asserted the OLD state-based contract. Post-cutover the readers key on the marker, so the first test would surface a spurious group and the second's undo would restore nothing (CAS returns 0 rows).
- **Fix:** updated both to seed a `DedupResolution` marker (alongside the surviving dual-write state), matching the new marker-is-authority contract.
- **Files modified:** `tests/discovery/services/test_dedup.py`
- **Commit:** `5f5ab426`
- **Note:** The `tests/review/routers/test_duplicates.py` resolve/undo/bulk tests needed **no** change — they drive the real endpoints, so `resolve_group` creates the marker first and the corpus stays consistent (verified: 10 passed).

## Threat Flags
None — no new network endpoints, auth paths, or schema surface introduced. `undo_resolve`'s attacker-controllable payload is covered by the plan's existing threat register (T-84-03-01/02, both mitigated as documented above).

## Issues Encountered
- `just test-bucket` does not export `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`, which default to port 5432 in `tests/conftest.py`, while the ephemeral test DB (`phaze-test-db`) is on **5433** (known footgun). Exported both plus `PHAZE_QUEUE_URL` against 5433 for every run.

## Verification
- `uv run ruff check .` and `uv run mypy .` both exit 0 (206 source files).
- `grep -c "FileRecord.state != FileState.DUPLICATE_RESOLVED" src/phaze/services/dedup.py` → **0**; `grep -c "dedup_resolved_clause()"` → **9**; surviving dual-writer `f.state = FileState.DUPLICATE_RESOLVED` → **1**; `on_conflict_do_nothing` → **1**; `delete(DedupResolution)` → **1**.
- `tests/discovery/services/test_dedup.py` → 17 passed; `tests/review/routers/test_duplicates.py` → 10 passed.
- `tests/integration/test_dedup_divergence.py` → 5 passed; `tests/integration/test_dedup_resolve_undo_shadow.py` → 2 passed; run together with `test_shadow_compare.py` → **43 passed** (no cross-test contamination).
- Both mutation checks observed RED (`assert 1 == 2`; `hard_fail_total = 1`) then restored GREEN.

## Next Phase Readiness
- Plan 84-04 (`get_fingerprint_progress`) can consume `dedup_resolved_clause()` **function-locally** (agent-worker boundary, D-00e); this plan touched neither `services/fingerprint.py` nor `services/stage_status.py`.
- D-16.2 (the live-corpus `just shadow-compare` run after migration 035, before merge) remains operator-gated — the CI test proves the writer/undo paths, but only the live run proves 035 covered the real post-032 resolved-without-marker rows.

## Self-Check: PASSED
- FOUND: src/phaze/services/dedup.py
- FOUND: tests/integration/test_dedup_divergence.py
- FOUND: tests/integration/test_dedup_resolve_undo_shadow.py
- FOUND: tests/discovery/services/test_dedup.py
- FOUND commit: 5f5ab426 (Task 1)
- FOUND commit: 7ed693d3 (Task 2)
- FOUND commit: a67ed16a (Task 3)

---
*Phase: 84-dedup-fingerprint-progress-cutover*
*Completed: 2026-07-09*
