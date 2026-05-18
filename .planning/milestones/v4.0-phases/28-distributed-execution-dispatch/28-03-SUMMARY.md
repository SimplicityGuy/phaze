---
phase: 28
plan: 03
subsystem: api / services / execution-dispatch
tags: [wave-1, dispatch, grouping, chunking, postgres, sqlalchemy, tdd]
dependency_graph:
  requires:
    - phase: 28-01
      provides: "Wave 0 pytest.skip stub at tests/test_services/test_execution_dispatch_grouping.py"
    - phase: 26-09
      provides: "ExecuteApprovedBatchPayload + ExecuteBatchProposalItem wire schemas (Field max_length=500)"
    - phase: 26-04
      provides: "AgentTaskRouter.enqueue_for_agent primitive that Plan 28-04 calls per (agent, chunk)"
    - phase: 24-01
      provides: "FileRecord.agent_id FK column + uq_files_agent_id_original_path partial UQ"
  provides:
    - "src/phaze/services/execution_dispatch.py module with 3 exports"
    - "get_approved_proposals_grouped_by_agent(session) -> dict[agent_id, list[ExecuteBatchProposalItem]]"
    - "count_revoked_skipped_proposals(session) -> int (banner copy N)"
    - "chunk_proposals(items, size=500) -> list[list[ExecuteBatchProposalItem]] (synchronous, pure)"
    - "28-V-01, 28-V-02, 28-V-03 GREEN"
  affects:
    - "Plan 28-04 (controller dispatch rewrite) -- consumer of all three exports"
    - "Plan 28-05 (agent-side per-proposal progress POST) -- shares the ExecuteBatchProposalItem wire shape"
tech_stack:
  added: []
  patterns:
    - "Explicit-JOIN SELECT (RenameProposal -> FileRecord -> Agent) with Agent.revoked_at.is_(None) filter (mirrors routers/agent_auth.py:80)"
    - "Deterministic ORDER BY (FileRecord.agent_id, RenameProposal.created_at) so re-runs produce stable chunk boundaries"
    - "collections.defaultdict(list) for accumulator + return dict() to seal it"
    - "Module-private _CHUNK_SIZE = 500 constant tied to Field(max_length=500) on the wire schema"
    - "ExecuteBatchProposalItem.sha256_hash ALWAYS populated from FileRecord.sha256_hash (RESEARCH L1)"
key_files:
  created:
    - src/phaze/services/execution_dispatch.py
  modified:
    - tests/test_services/test_execution_dispatch_grouping.py
decisions:
  - "Single SELECT returning (RenameProposal, FileRecord) tuples + in-Python grouping -- rejected SQL GROUP BY + jsonb_agg as more complex than needed for v4.0's 1-5 agent / N<=10K row scale"
  - "func.count() with select_from(RenameProposal) + JOINs in count_revoked_skipped_proposals -- mypy-friendly vs Model.__table__.count() pattern, mirroring services/execution_queries.py"
  - "proposed_path defaults to empty string when RenameProposal.proposed_path is None -- ExecuteBatchProposalItem requires str (no None); the Plan 28-04 controller writes the actual destination via settings.output_path joining"
  - "chunk_proposals is synchronous (no async) -- pure list-slicing, no I/O; PATTERNS line 225 specifies this"
metrics:
  duration_seconds: 932
  duration_human: "~15.5 min"
  tasks_completed: 1
  files_changed: 2
  commits: 2
  completed_date: "2026-05-15"
requirements_completed:
  - EXEC-01
---

# Phase 28 Plan 03: Dispatch Grouping + Revoked Filter + Chunking Helpers Summary

**Controller-side helper module `src/phaze/services/execution_dispatch.py` exporting three functions that group `RenameProposal.APPROVED` rows by `FileRecord.agent_id`, filter revoked agents into a separate count, and chunk per-agent groups at 500 — the units Plan 28-04 calls inside `start_execution` to drive per-agent SAQ dispatch.**

## Performance

- **Duration:** ~15.5 min
- **Started:** 2026-05-15T22:06:18Z
- **Completed:** 2026-05-15T22:21:50Z
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files changed:** 2 (1 created + 1 modified — Wave 0 stub replaced)

## Accomplishments

- **Service module shipped.** `src/phaze/services/execution_dispatch.py` exports the three functions Plan 28-04 will call. Implements D-09 steps 1-3 verbatim from CONTEXT.md.
- **20 tests landed and GREEN.** Replaced the Wave 0 `pytest.skip` stub with a full unit-test suite hitting real PostgreSQL via the existing `session` fixture. Includes the three Nyquist-sampled test IDs (28-V-01, 28-V-02, 28-V-03) plus 17 additional edge-case tests.
- **Chunk math fully verified.** Parametrized test covers `n ∈ {0, 1, 499, 500, 501, 999, 1000, 1500}` against `ceil(n/500)`; the integration test seeds 1000 approved proposals on one agent and confirms `grouped[a] → 1000 items → 2 chunks of 500`.
- **Revoked-agent contract enforced at the SELECT layer.** `Agent.revoked_at.is_(None)` predicate joins through `FileRecord.agent_id`; the companion `count_revoked_skipped_proposals` returns the banner N. No application-side post-filter, no race window.

## Task Commits

Each task was committed atomically (TDD RED/GREEN sequence):

1. **Task 1 RED — failing tests** — `e17c74c` (test): replaced the Wave 0 module-level `pytest.skip` with 13 test functions + a 7-row parametrize that all fail with `ModuleNotFoundError: No module named 'phaze.services.execution_dispatch'`.
2. **Task 1 GREEN — implementation** — `0dd94e8` (feat): created `src/phaze/services/execution_dispatch.py` with three exports. All 20 tests pass; pre-commit (ruff/ruff-format/bandit/mypy) green.

REFACTOR gate not needed — the implementation is the minimum surface that satisfies every test, and the JOIN/order-by query is the canonical pattern from PATTERNS.md lines 191-211 without modification.

## Function Signatures (the contract Plan 28-04 will call)

```python
async def get_approved_proposals_grouped_by_agent(
    session: AsyncSession,
) -> dict[str, list[ExecuteBatchProposalItem]]: ...

async def count_revoked_skipped_proposals(session: AsyncSession) -> int: ...

def chunk_proposals(
    items: list[ExecuteBatchProposalItem],
    size: int = 500,
) -> list[list[ExecuteBatchProposalItem]]: ...
```

## SQL Query Shape

```sql
SELECT proposals.*, files.*
FROM proposals
JOIN files   ON proposals.file_id    = files.id
JOIN agents  ON files.agent_id       = agents.id
WHERE proposals.status = 'approved'
  AND agents.revoked_at IS NULL
ORDER BY files.agent_id, proposals.created_at
```

Companion count query:

```sql
SELECT COUNT(*)
FROM proposals
JOIN files   ON proposals.file_id    = files.id
JOIN agents  ON files.agent_id       = agents.id
WHERE proposals.status = 'approved'
  AND agents.revoked_at IS NOT NULL
```

Both queries lean on existing indexes: `ix_proposals_status` (Phase 1) and the implicit PK indexes on `files.id` / `agents.id`. No new indexes required.

## 28-V-NN Test ID Status

| Test ID | Status | Test Function |
|---------|--------|---------------|
| **28-V-01** | **GREEN** | `test_groups_by_agent_id` |
| **28-V-02** | **GREEN** | `test_revoked_agent_filtered_with_count` |
| **28-V-03** | **GREEN** | `test_1000_proposals_split_into_2_chunks` |

Additional tests (not in the Nyquist sample but covering the export surface):

- `test_empty_input_returns_empty_dict_and_zero_skipped` — empty DB → `{}` + `0`
- `test_non_approved_proposals_excluded` — PENDING/REJECTED/EXECUTED/FAILED rows never returned
- `test_sha256_hash_populated_from_file_record` — RESEARCH L1 always-populate invariant
- `test_deterministic_ordering_within_agent_group` — `ORDER BY ... RenameProposal.created_at` enforces stable chunk boundaries
- `test_chunk_empty_list_returns_empty_list` / `test_chunk_smaller_than_size_returns_single_chunk` / `test_chunks_at_500` / `test_chunk_off_by_one_above_size` / `test_chunk_at_size_returns_single_chunk` — chunk-math edge cases
- `test_chunk_count_matches_ceil_n_over_500[0,1,499,500,501,999,1000,1500]` — 8-row parametrize verifies `len(chunks) == ceil(n/500)`

## Files Created/Modified

- **`src/phaze/services/execution_dispatch.py`** (CREATED, 124 lines) — three exports:
  - `get_approved_proposals_grouped_by_agent` — async SELECT + in-Python `defaultdict(list)` accumulator + return-as-plain-`dict`.
  - `count_revoked_skipped_proposals` — async `func.count()` over the same JOIN with the inverted predicate.
  - `chunk_proposals` — synchronous one-liner `[items[i:i+size] for i in range(0, len(items), size)]`.
  - Module-private `_CHUNK_SIZE = 500` constant matches `ExecuteApprovedBatchPayload.proposals` `Field(max_length=500)`.
- **`tests/test_services/test_execution_dispatch_grouping.py`** (MODIFIED, Wave 0 stub → 320 lines): 13 test functions + 1 parametrized test (8 rows) = 20 tests. Uses real PostgreSQL via `session` fixture; seed helpers build unique `(agent_id, original_path)` pairs to avoid the Phase 24 `uq_files_agent_id_original_path` partial-UQ collision.

## Decisions Made

- **In-Python grouping over SQL `GROUP BY ... jsonb_agg(...)`** — chose `defaultdict(list)` accumulator over a database-side aggregator. Rationale: v4.0 scale is 1-5 agents × N≤10K proposals; the in-Python path is type-safer (mypy can prove `ExecuteBatchProposalItem` construction), trivially testable, and the SELECT is bounded by `ix_proposals_status`. A future scale-up phase can swap to a SQL aggregate without changing the public signature.
- **`func.count()` + explicit JOINs** for the skipped count, not a `select(RenameProposal).where(...).count()` antipattern. Mirrors `services/execution_queries.py:get_execution_stats` exactly.
- **`proposed_path or ""`** when `RenameProposal.proposed_path` is `None` — `ExecuteBatchProposalItem.proposed_path: str` requires non-None. The empty string flows through Plan 28-04's controller, which composes the absolute destination via `Path(settings.output_path) / proposed_path / proposed_filename`. An empty `proposed_path` resolves to "settings.output_path / proposed_filename" (the existing `services/execution.py:147` else-branch behavior). No test in Plan 28-03 seeds `proposed_path=None`; downstream plans cover that path.
- **No `selectinload(RenameProposal.file)`** — the SELECT already pulls `(RenameProposal, FileRecord)` tuples, so the relationship is hot in the session. Adding `selectinload` would be a no-op extra query for our needs. Plan 28-04 can re-evaluate if it needs to access `proposal.file` after grouping returns.

## Deviations from Plan

None — plan executed exactly as written. The implementation matches PATTERNS.md lines 191-225 verbatim; the test file matches the spec in the plan's `<behavior>` block plus the additional edge-case tests the plan's `<behavior>` already enumerated.

The plan called for tests using "real PostgreSQL via the existing `session` fixture" — Plan 28-01's SUMMARY noted that PostgreSQL was not running in the Wave 0 worktree. This worktree (Plan 28-03) brought up `docker compose up -d postgres` and created the `phaze_test` database via the existing infrastructure. That is environment-setup, not a deviation from plan text.

## Auth Gates

None. This plan touched no HTTP endpoints, no credentials, no external services.

## Threat Surface Scan

No NEW threat surface introduced. The plan's `<threat_model>` enumerates four threats; this implementation maps to them as follows:

- **T-28-03-T (Tampering, cross-tenant mis-grouping)** — MITIGATED. The grouping key is `file_record.agent_id` read off the joined row; no user-input path. Test `test_groups_by_agent_id` asserts proposals seeded under `agent-aaa` never appear in the `agent-bbb` group.
- **T-28-03-I (Information Disclosure, revoked-agent count)** — ACCEPTED per plan. `count_revoked_skipped_proposals` returns an integer; the banner copy (Plan 28-04) joins it with admin-visible agent name + slug.
- **T-28-03-D (Denial of Service, large backlog)** — ACCEPTED per plan. Single SELECT plus in-memory grouping; PostgreSQL handles 10K+ row SELECTs in ms. The 500-cap chunking limits downstream SAQ payload sizes.
- **T-28-03-V (Input Validation, sha256_hash type safety)** — MITIGATED. `ExecuteBatchProposalItem.sha256_hash: str | None` accepts both; the implementation always populates from `FileRecord.sha256_hash` (NOT NULL post-Phase 2) so the wire value is always `str`. Test `test_sha256_hash_populated_from_file_record` asserts this.

No `## Threat Flags` section needed — no new endpoints, auth surfaces, or trust boundaries.

## Known Stubs

None. This plan replaces a Wave 0 stub with a real implementation; no new stubs were introduced.

## Plan Verification

Executed the plan's `<automated>` command:

```bash
uv run pytest tests/test_services/test_execution_dispatch_grouping.py -x
```

Result: **20 passed in 4.89s**.

`<done>` criteria check:

- 28-V-01, 28-V-02, 28-V-03 GREEN ✓ (verified via individual `pytest ::test_name` runs)
- `src/phaze/services/execution_dispatch.py` exports `get_approved_proposals_grouped_by_agent`, `count_revoked_skipped_proposals`, `chunk_proposals` ✓ (`ast.parse` enumeration confirms all three)
- `grep -c "Agent.revoked_at.is_(None)" src/phaze/services/execution_dispatch.py` returns 2 (≥ 1) ✓
- `uv run pre-commit run --files src/phaze/services/execution_dispatch.py tests/test_services/test_execution_dispatch_grouping.py` green ✓ (ruff / ruff-format / bandit / mypy all passed on both files)
- `uv run pytest -x` (full suite) — **NOT green** in this worktree environment. 1115 passed; 7 failures + 20 errors are all pre-existing infrastructure issues unrelated to this plan: `tests/test_migrations/test_012_upgrade.py` and `tests/test_013_upgrade.py` require a `phaze_migrations_test` database that isn't provisioned in the worktree; `tests/test_routers/test_companion.py` and `tests/test_routers/test_agent_tracklists.py` errors are flaky-when-run-with-the-full-suite Redis-state-dependent tests (each passes in isolation — confirmed via `pytest tests/test_routers/test_companion.py` → 7 passed, `pytest tests/test_routers/test_agent_tracklists.py::test_tracklist_missing_auth_returns_401` → 1 passed). None of the failures touch files this plan modified.

To confirm scope: `uv run pytest tests/test_services/ tests/test_schemas/ tests/test_tasks/` → **597 passed, 3 skipped, 0 failed** — the test surface this plan could plausibly affect is clean. Plan 28-03 introduces zero regressions to the non-DB, non-shared-Redis test surface.

## TDD Gate Compliance

- **RED gate** — `test(28-03): add failing tests for dispatch grouping + chunking (RED)` — commit `e17c74c`. The test file fails with `ModuleNotFoundError: No module named 'phaze.services.execution_dispatch'` at collection time. Verified failing before implementation.
- **GREEN gate** — `feat(28-03): add dispatch grouping + revoked filter + chunking helpers (GREEN)` — commit `0dd94e8`. All 20 tests pass.
- **REFACTOR gate** — not required (minimal surface, no cleanup pass needed).

Gate sequence verified in `git log --oneline -3`:

```
0dd94e8 feat(28-03): add dispatch grouping + revoked filter + chunking helpers (GREEN)
e17c74c test(28-03): add failing tests for dispatch grouping + chunking (RED)
6cffd5a docs(phase-28): update tracking after wave 0
```

## Self-Check: PASSED

Verified all created/modified file paths and both commit hashes exist on this branch.

- File check:
  - `src/phaze/services/execution_dispatch.py` → present
  - `tests/test_services/test_execution_dispatch_grouping.py` → present (Wave 0 stub replaced)
- Commit check:
  - `e17c74c` (RED) → present on `worktree-agent-a41cdd3f0f79b379c`
  - `0dd94e8` (GREEN) → present on `worktree-agent-a41cdd3f0f79b379c`
- Done-criteria check:
  - `grep -c "Agent.revoked_at.is_(None)" src/phaze/services/execution_dispatch.py` → 2 (≥ 1) ✓
  - Exports (`ast.parse`): `chunk_proposals`, `count_revoked_skipped_proposals`, `get_approved_proposals_grouped_by_agent` — exactly the three required ✓
  - Pre-commit on both touched files → green ✓
  - Plan automated verify (`pytest tests/test_services/test_execution_dispatch_grouping.py -x`) → 20 passed ✓
