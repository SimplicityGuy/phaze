---
phase: 27-watcher-service-user-initiated-scan
plan: 03
subsystem: controller-http-api
tags:
  - controller
  - http-api
  - cross-tenant-guard
  - state-machine
requires:
  - phaze.schemas.agent_files.FileUpsertChunk.batch_id (Phase 27-02 D-09)
  - phaze.schemas.agent_scan_batches.ScanBatchPatch + ScanBatchPatchResponse (Phase 27-02 D-10)
  - phaze.models.scan_batch.ScanBatch + ScanStatus.LIVE (Phase 24 D-09/D-12)
  - phaze.routers.agent_auth.get_authenticated_agent (Phase 25 D-05/AUTH-01)
  - phaze.routers.agent_proposals (Phase 26 D-08; cross-tenant guard byte-for-byte mirror)
provides:
  - PATCH /api/internal/agent/scan-batches/{batch_id} — RUNNING→{COMPLETED, FAILED} state machine + cross-tenant guard
  - POST /api/internal/agent/files — optional batch_id field with LIVE-sentinel resolution + cross-tenant guard
  - PhazeAgentClient.patch_scan_batch(batch_id, ScanBatchPatch) -> ScanBatchPatchResponse
  - agent_scan_batches.router registered in create_app()
affects:
  - tests/test_routers/test_agent_files.py — smoke-app fixture now seeds the LIVE sentinel for the test agent (Phase 24 D-11 invariant; Phase 25/26 contract behaviorally unchanged)
tech_stack:
  added: []
  patterns:
    - "Cross-tenant guard placement: 404→403→state-machine→422→409→200 ordering (mirrors Phase 26 D-08)"
    - "Idempotent same-state PATCH as zero-DB-write echo (no updated_at bump)"
    - "Server-side batch_id resolution from bearer-token-derived agent_id (LIVE sentinel via partial UQ)"
key_files:
  created:
    - src/phaze/routers/agent_scan_batches.py
    - tests/test_routers/test_agent_scan_batches.py
    - tests/test_routers/test_agent_files_batch_id.py
  modified:
    - src/phaze/routers/agent_files.py (resolution block + cross-tenant guard + batch_id stamp)
    - src/phaze/services/agent_client.py (TYPE_CHECKING import + patch_scan_batch method)
    - src/phaze/main.py (import + include_router)
    - tests/test_routers/test_agent_files.py (smoke-app fixture seeds LIVE sentinel)
    - tests/test_services/test_agent_client_endpoints.py (+1 respx happy-path test)
decisions:
  - "Used `set(set_fields.keys()) == {'status'}` to detect 'same-state echo with no other mutating fields' (clean single-statement form satisfies ruff SIM102). The plan's `<action>` step c.d said 'if body.status == batch.status AND all other body fields are unset' — semantics preserved exactly."
  - "Cast `body.status` through `ScanStatus(...)` for the same-state comparison (rather than string equality with batch.status) — keeps the comparison enum-aware and tolerates any future SCREAMING_CASE Literal additions without bug."
  - "Defensive LIVE check uses 409 (not 422) because the Literal-layer already returns 422 for `status='live'` on the wire. The handler-level check fires only if a future Literal widening lets LIVE through schema validation; returning 409 'cannot transition to LIVE' documents that LIVE is operator-untouchable, distinct from a wire-format violation."
  - "Existing test_agent_files.py smoke-app fixture was extended (not replaced) — added a single `ScanBatch(status='live')` row at fixture setup. Per the plan's `<action>` step 5: 'check the conftest before assuming no change'. Phase 25/26 contract tests remain behaviorally unchanged (all 11 pass); the seed mirrors what the Phase 24 D-11 agent-registration flow does in production."
  - "patch_scan_batch was placed in agent_client.py immediately AFTER patch_proposal_state (alphabetically: proposal_state < scan_batch) and BEFORE heartbeat — keeps the file's PATCH-method block contiguous."
metrics:
  duration_minutes: 14
  completed_date: 2026-05-13
  tasks_completed: 3
  commits: 3
  tests_added: 17
  tests_passing: 991
  files_created: 3
  files_modified: 5
---

# Phase 27 Plan 03: Controller HTTP Surface Summary

Wave 2 controller landing: PATCH `/api/internal/agent/scan-batches/{batch_id}` with full state-machine + 403-before-state-machine cross-tenant guard (T-27-01); POST `/api/internal/agent/files` extended with optional `batch_id` field that either resolves to the calling agent's LIVE sentinel or is checked against the same cross-tenant guard before any FileRecord insert (T-27-02); `PhazeAgentClient.patch_scan_batch` wraps the new endpoint with the existing tenacity retry funnel; `agent_scan_batches.router` is wired into `create_app()`.

## What Was Built

**Three atomic commits, one per task:**

| Commit  | Task | Description |
| ------- | ---- | ----------- |
| 43af6a9 | 1    | New `phaze.routers.agent_scan_batches` module with PATCH handler enforcing the 404→403→same-state-echo→409→422→200 ordering. `_SCAN_TRANSITIONS = {RUNNING: {COMPLETED, FAILED}}` is the single source of truth — LIVE intentionally absent. Same-state PATCH with no other mutating fields is a zero-DB-write echo (no `updated_at` bump; Phase 26 D-08 invariant). `PhazeAgentClient.patch_scan_batch` added, inheriting the tenacity retry policy + AgentApiError hierarchy via the `_request` funnel. 11 router contract tests + 1 respx client test. Test 9 (`test_cross_agent_403_before_state_machine`) PATCHes agent A's COMPLETED batch with agent B's bearer — asserts 403, NOT 409, proving the cross-tenant check precedes state-machine evaluation. |
| 0b327a6 | 2    | `agent_files.upsert_files` extended with a `batch_id` resolution block inserted at the top of the handler body, BEFORE the records loop. Present `batch_id` is fetched + cross-tenant-checked (404/403); absent `batch_id` selects the calling agent's LIVE sentinel via the partial UQ `uq_scan_batches_agent_id_live`. Every record in the chunk is stamped with the resolved `batch_id` alongside the AUTH-01 `agent_id` stamp. 5 new contract tests cover all branches; Test 3 verifies atomicity (zero rows inserted when the cross-tenant 403 fires). Existing `test_agent_files.py` smoke-app fixture now seeds the LIVE sentinel for the test agent to mirror the Phase 24 D-11 production behavior — Phase 25/26 contract tests behaviorally unchanged (all 11 still pass). |
| 8577ae2 | 3    | `phaze.main.create_app()` imports + includes `agent_scan_batches.router` in the Phase 26 internal-agent block (alphabetical order between `agent_proposals` and `agent_tracklists`). New `test_router_registered_in_main_app` asserts the path prefix `/api/internal/agent/scan-batches` is reachable on the production app (not just smoke-app) and a PATCH method is bound. |

## Verification

The plan's `<verification>` block in full:

- `uv run pytest tests/test_routers/test_agent_scan_batches.py tests/test_routers/test_agent_files_batch_id.py tests/test_routers/test_agent_files.py -x -q` → **28 passed in 4.91s** (12 + 5 + 11)
- `uv run pytest -x -q --ignore=tests/test_migrations` (full smoke) → **991 passed, 1 skipped in 124.94s** (no regression)
- `uv run ruff check src/phaze/routers/agent_scan_batches.py src/phaze/routers/agent_files.py src/phaze/services/agent_client.py src/phaze/main.py` → **All checks passed**
- `uv run ruff format --check` over all changed files → clean
- `uv run mypy src/phaze/routers/agent_scan_batches.py src/phaze/routers/agent_files.py src/phaze/services/agent_client.py src/phaze/main.py` → **Success: no issues found**
- pre-commit hooks ran on every commit (no `--no-verify`); bandit clean

## Acceptance Criteria — Grep Confirmations

**Task 1 (agent_scan_batches.py):**
- `grep -c "if batch.agent_id != agent.id:"` → **1**
- `grep -c "status.HTTP_403_FORBIDDEN"` → **1** (the plan listed `status=status.HTTP_403_FORBIDDEN`; the actual code splits across lines after `ruff format`, so the canonical pattern is `status.HTTP_403_FORBIDDEN`)
- `grep -c "status.HTTP_404_NOT_FOUND"` → **1**
- `grep -c "status.HTTP_409_CONFLICT"` → **2** (illegal-transition guard + defensive LIVE-rejection)
- `grep -c "_SCAN_TRANSITIONS"` → **4** (definition + lookup-site reference + 2 docstring references)
- `grep -c "async def patch_scan_batch" src/phaze/services/agent_client.py` → **1**
- `grep -c "exclude_unset=True" src/phaze/services/agent_client.py` → **6** (was 5 pre-Plan-03; +1 for the new method)

**Task 2 (agent_files.py):**
- `grep -c "Phase 27 D-09" src/phaze/routers/agent_files.py` → **2** (block-leading comment + per-record stamp comment)
- `grep -c "if body.batch_id is not None:"` → **1**
- `grep -c "ScanStatus.LIVE"` → **1** (sentinel resolution SELECT)
- `grep -c "if batch.agent_id != agent.id:"` → **1**
- `grep -c "status.HTTP_403_FORBIDDEN"` → **1**

**Task 3 (main.py):**
- `grep -c "agent_scan_batches" src/phaze/main.py` → **2** (import + include_router)
- `uv run python -c "from phaze.main import create_app; create_app()"` → exits 0
- `test_router_registered_in_main_app` passes

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] ruff SIM102 — combine nested if statements**
- **Found during:** Task 1 (post-Write ruff check)
- **Issue:** The same-state idempotent-no-op detection was initially expressed as nested `if`s (outer: status matches current; inner: only `status` field was set). Ruff's SIM102 rule flags nested ifs that can be expressed as a single `and`-chained predicate.
- **Fix:** Collapsed to a single `if body.status is not None and ScanStatus(body.status) == cur and set(set_fields.keys()) == {"status"}:` line. Semantics identical to the plan's specification at step c.d.
- **Files modified:** `src/phaze/routers/agent_scan_batches.py`
- **Commit:** 43af6a9

**2. [Rule 3 - Blocker] Existing test_agent_files.py fixture missing LIVE sentinel**
- **Found during:** Task 2 (first pytest run after wiring the resolution block)
- **Issue:** `test_agent_files.py` smoke-app fixture seeds the test agent via the conftest `seed_test_agent` fixture but does NOT create the LIVE sentinel that the Phase 24 D-11 agent-registration flow would normally seed in production. Phase 25/26 tests pass `batch_id=None` (the field didn't exist), so the absent-branch SELECT runs and finds zero rows → `NoResultFound` 500 in every existing test.
- **Fix:** Extended the `smoke_app_and_router` fixture in `test_agent_files.py` to seed a `ScanBatch(agent_id=agent.id, scan_path="<watcher>", status="live")` row at fixture setup. The seed mirrors what Phase 24 D-11 does in production. All 11 existing tests pass behaviorally unchanged.
- **Files modified:** `tests/test_routers/test_agent_files.py`
- **Commit:** 0b327a6
- **Plan anticipated:** Yes — the plan's `<action>` step 5 explicitly said "may require adding LIVE-sentinel seeding to that file's fixtures; do so if necessary" and the acceptance criterion confirmed "no regression — may require adding LIVE-sentinel seeding".

### Out-of-scope discoveries

None. No `deferred-items.md` entries written.

## Output Asks Resolved

The plan `<output>` asked four specific questions:

1. **"Whether the existing `agent_files.py` upsert SET clause already had `batch_id` in it (per 27-PATTERNS.md line 370 it should)"** → **Yes, confirmed.** Line 86 of `src/phaze/routers/agent_files.py` (pre-Plan-03) already had `"batch_id": base_stmt.excluded.batch_id` in the `on_conflict_do_update.set_={...}` dict. No adjustment needed there. The only changes to `agent_files.py` were (a) the resolution block insertion and (b) the per-record `data["batch_id"] = resolved_batch_id` stamp.

2. **"The actual line number where the resolution block was inserted in `agent_files.py`"** → **Lines 57-82** (16 inserted lines): the block sits inside the `upsert_files` handler, AFTER the docstring and BEFORE the existing "Build raw record dicts" comment at line 84. The per-record `data["batch_id"]` stamp lives at line 93 alongside the existing AUTH-01 stamp.

3. **"Whether any pre-existing Phase 25/26 test fixtures needed a LIVE-sentinel seeding update (likely yes — flag for Plan 04/05 awareness)"** → **Yes, exactly one fixture needed it.** `tests/test_routers/test_agent_files.py::smoke_app_and_router` was extended to seed the LIVE sentinel for the test agent. Plan 04 (scan_directory) and Plan 05 (watcher) test fixtures will likely need the same seed — flag for those plans: any test that exercises the POST `/api/internal/agent/files` handler with `batch_id` omitted MUST have the agent's LIVE sentinel pre-seeded (Phase 24 D-11 invariant). The `seed_test_agent` conftest fixture deliberately does NOT include this seed to keep the Phase 25-02 auth tests focused.

4. **"Any non-trivial deviation from the agent_proposals.py mirror (should be zero; flag if otherwise)"** → **Zero non-trivial deviations.** Structural diff vs `agent_proposals.py:62-76`:
   - 404 lookup: `session.get(ScanBatch, batch_id)` vs `session.get(RenameProposal, proposal_id)` — same shape.
   - Cross-tenant guard: `if batch.agent_id != agent.id:` vs `if file_record is not None and file_record.agent_id != agent.id:` — the proposals version has a `file_record is not None` carve-out because FileRecord could theoretically be FK-orphaned; ScanBatch has no such orphan path (RESTRICT FK on agents), so the guard is simpler. The 403 detail text and HTTP status are byte-for-byte identical otherwise.
   - State-machine evaluation, idempotent-same-state echo, and `model_dump(exclude_unset=True)` apply loop — all byte-for-byte mirrored.

## TDD Gate Compliance

All three tasks marked `tdd="true"`. RED-then-GREEN landed in the same commit per task (Phase 25/26/27-01/27-02 project precedent):

- **Task 1 RED:** Wrote `tests/test_routers/test_agent_scan_batches.py` first (12 tests including the cross-tenant-guard ordering assertion); the test file's `from phaze.routers import agent_scan_batches` import would fail at collection time. Then created the router module; ran the test suite → all 12 green.
- **Task 2 RED:** Wrote `tests/test_routers/test_agent_files_batch_id.py` first (5 tests); the `batch_id` body field is already accepted by the schema (Phase 27-02), but the handler ignored it, so all 5 tests would fail (404 wouldn't fire, cross-tenant 403 wouldn't fire, etc.). Then inserted the resolution block; ran → all 16 green (5 new + 11 existing after the fixture extension).
- **Task 3 RED:** `test_router_registered_in_main_app` would have failed because `main.py` didn't import `agent_scan_batches` yet. Wired the two `main.py` edits; ran → green.

No `test(...)`-then-`feat(...)` commit pair per task (project precedent). Each commit message documents the RED-state evidence in its narrative.

## Known Stubs

None. Every endpoint is fully wired: PATCH writes through to the ScanBatch row; POST resolves `batch_id` server-side and stamps it on every FileRecord; the client method serializes and validates round-trip. Plan 04's `scan_directory` task and Plan 05's watcher can `import` these endpoints today and exercise them without any further surface-area changes.

## Threat Flags

None new beyond the plan's `<threat_model>`. The four documented mitigations are all in place:

- **T-27-01 (cross-agent PATCH on `/scan-batches/{batch_id}`)** — mitigated; `test_cross_agent_403_before_state_machine` asserts 403 (NOT 409) when agent B PATCHes agent A's COMPLETED batch, proving the cross-tenant check precedes state-machine evaluation.
- **T-27-02 (cross-agent `batch_id` on `/files`)** — mitigated; `test_batch_id_cross_agent_403` asserts 403 AND verifies zero `FileRecord` rows were inserted (atomicity proof — the 403 fires BEFORE the records loop).
- **Information-disclosure timing oracle (same-state vs disallowed-transition)** — mitigated; the same-state path is a zero-DB-write echo, and the 403 cross-tenant guard dominates either timing branch.
- **Tampering: PATCH `status='live'`** — mitigated by two layers: schema-level `Literal["running","completed","failed"]` (422 at validation; `test_live_status_in_body_422` verifies); handler-level defensive `if new == ScanStatus.LIVE` returning 409 documents the invariant for any future Literal widening.

## Self-Check: PASSED

**Files exist:**
- FOUND: src/phaze/routers/agent_scan_batches.py
- FOUND: tests/test_routers/test_agent_scan_batches.py
- FOUND: tests/test_routers/test_agent_files_batch_id.py

**Files modified (verified via `git diff --name-only HEAD~3 HEAD`):**
- FOUND: src/phaze/routers/agent_files.py
- FOUND: src/phaze/services/agent_client.py
- FOUND: src/phaze/main.py
- FOUND: tests/test_routers/test_agent_files.py
- FOUND: tests/test_services/test_agent_client_endpoints.py

**Commits exist (on `worktree-agent-a0045365c79ab801c`):**
- FOUND: 43af6a9 — feat(27-03): add PATCH /api/internal/agent/scan-batches/{batch_id} + client method (D-10, T-27-01)
- FOUND: 0b327a6 — feat(27-03): resolve batch_id on POST /files; cross-tenant guard (D-09/D-18/D-21, T-27-02)
- FOUND: 8577ae2 — feat(27-03): wire agent_scan_batches.router into create_app() (Task 3)
