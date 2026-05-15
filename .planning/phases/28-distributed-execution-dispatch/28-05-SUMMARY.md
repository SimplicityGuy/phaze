---
phase: 28
plan: 05
subsystem: agent-task / execution-dispatch
tags: [wave-2, exec-progress-post, saq-meta-retry-idempotency, tdd, error-step-classification, l6-l22-closed]
dependency_graph:
  requires:
    - "28-01 (ExecuteApprovedBatchPayload.sub_batch_index already present; Wave 0 stub at tests/test_tasks/test_execute_approved_batch_progress.py)"
    - "28-02 (PhazeAgentClient.post_exec_batch_progress + ExecBatchProgressPayload schema)"
  provides:
    - "Agent-side terminal progress POST per proposal (D-03)"
    - "_classify_failure_step helper (D-07 + RESEARCH L9 mapping)"
    - "SAQ-meta-backed execution_log_id + progress_request_id (closes L6/L22; delivers D-15)"
    - "<step>: <reason> error_message prefix (D-01 contract realized)"
    - "Backward-compat fallback for legacy ctx without job key (Phase 26 in-memory test fixtures)"
  affects:
    - src/phaze/tasks/execution.py
tech_stack:
  added: []
  patterns:
    - "Local-variable step tracking (current_step: Literal[copy|verify|delete]) for typed failure classification"
    - "SAQ Job.meta string-valued UUID persistence + Job.update(meta=...) merge-and-write"
    - "Defensive ctx.get('job') with fresh-UUID fallback (Phase 26 test-fixture compat)"
    - "Fire-and-forget D-16 progress POST (swallow + log WARNING; file ops already committed)"
key_files:
  created: []
  modified:
    - src/phaze/tasks/execution.py
    - tests/test_tasks/test_execute_approved_batch_progress.py
decisions:
  - "Cleaner upfront-meta-init choice (RESEARCH alternative): a single ``await job.update(meta=...)`` BEFORE the for-loop, not one per proposal. On first run, all proposal UUIDs are seeded in one batched write; on retry, the keys are already present and ``job.update`` is skipped entirely. This minimizes Redis HSETs (one per job lifecycle vs N per job)."
  - "Defensive ``ctx.get('job')`` fallback chosen over modifying the Phase 26 ``test_execute_approved_batch.py`` fixtures. The fall-back returns fresh UUIDs per call + emits a DEBUG log, so the legacy test surface keeps passing unchanged. The fall-back has no SAQ retry semantics (legacy callers don't have retries anyway)."
  - "Meta-key naming: ``log_id:{proposal_id}`` and ``req_id:{proposal_id}``. Strings (not UUIDs) because SAQ serializes ``meta`` as JSON; ``uuid.UUID`` objects aren't JSON-serializable. Strings are parsed back to ``uuid.UUID`` on retry."
  - "``_classify_failure_step`` keeps a sha256-mismatch override (string-match on ``'sha256 mismatch'``) even though ``current_step='verify'`` is already set before the hash check. This makes the failed-at-step contract robust against future re-orderings of the body."
  - "4-transition ``current_step`` state machine inside ``_execute_one`` body: ``'copy'`` (path-resolve) -> ``'verify'`` (sha256 check) -> ``'copy'`` (write) -> ``'delete'`` (unlink). Mirrors operator intuition: a path-resolve failure means 'the copy didn't begin'; a sha256 mismatch means 'verify failed'; an unlink failure means 'the delete failed' (even though the copy already succeeded)."
metrics:
  duration_seconds: 613
  duration_human: "~10m13s"
  tasks_completed: 1
  files_changed: 2
  commits: 2
  completed_date: "2026-05-15"
---

# Phase 28 Plan 05: Agent-side per-proposal progress POSTs + SAQ-meta retry-stable UUIDs Summary

Single-task surgical rewrite of ``src/phaze/tasks/execution.py``: every per-proposal terminal state now fires exactly one ``api.post_exec_batch_progress(...)`` call (success path: ``terminal_step="deleted"``; failure path: ``terminal_step="failed"`` + ``failed_at_step`` from a tracked ``current_step`` variable), the LAST proposal of a sub-batch sets ``sub_batch_terminal=True``, and BOTH per-proposal UUIDs (``execution_log_id`` + ``progress_request_id``) are now persisted in ``ctx['job'].meta`` via ``await ctx['job'].update(meta=...)`` so SAQ retries reuse the same UUIDs (closes the long-standing L6/L22 audit-row duplication bug + delivers D-15). 22 tests pass (12 new + 10 regression); pre-commit clean; ≥85% coverage on the touched module.

## What Was Built

### New per-proposal terminal POST -- the source of Redis-hash motion

Every proposal in ``_execute_one`` now ends with **exactly one** ``api.post_exec_batch_progress(...)`` call:

| Path | ``terminal_step`` | ``failed_at_step`` | Where the call happens |
|------|-------------------|--------------------|----------------------|
| Success | ``"deleted"`` | ``None`` | After ``patch_proposal_state(executed)`` and before ``return True``. |
| Failure | ``"failed"`` | ``_classify_failure_step(current_step, exc)`` | After ``patch_proposal_state(failed)`` reporting and before ``return False``. |

The ``sub_batch_terminal`` field is ``True`` only on the LAST item of the sub-batch (computed by the outer loop as ``idx == len(payload.proposals) - 1``). This is what tells the Plan 28-02 controller "we've finished -- increment ``subjobs_completed`` and check for status promotion."

### ``_classify_failure_step`` (D-07 + RESEARCH L9 mapping)

```python
def _classify_failure_step(current_step: FailedAtStep, exc: BaseException) -> FailedAtStep:
    if "sha256 mismatch" in str(exc):
        return "verify"
    return current_step
```

The body of ``_execute_one`` tracks a local ``current_step: Literal["copy", "verify", "delete"]`` variable through 4 transitions:

| Code position | ``current_step`` value | Rationale |
|---------------|------------------------|-----------|
| Start of try block | ``"copy"`` | Path-resolve and the path-traversal guard are part of "the copy didn't begin" in the operator's mental model. |
| Just before ``if item.sha256_hash is not None:`` | ``"verify"`` | The sha256 check IS the verify sub-step. |
| Just before ``proposed.write_bytes(...)`` | ``"copy"`` | The actual byte-write. |
| Just before ``original.unlink()`` | ``"delete"`` | Anything that fails here is a delete failure (file is already on disk at the new location). |

The except-handler reads ``current_step`` exactly once and produces both ``failed_at_step`` for the progress POST and the ``"<step>: <reason>"`` prefix for the ExecutionLog ``error_message``.

### SAQ-meta-backed UUIDs (closes L6/L22; delivers D-15)

A new private helper ``_load_or_seed_uuids(job, proposals)`` returns ``(log_ids_by_proposal, req_ids_by_proposal, updated_meta, changed)``. On the first invocation, ``changed=True`` and the caller persists the merged meta dict via ``await job.update(meta=updated_meta)``. On a SAQ retry, the meta dict is already populated from the previous run -- ``changed=False`` and ``job.update`` is skipped. The UUIDs come back as ``uuid.UUID`` objects (parsed from the string-valued meta entries).

**Meta-key naming convention** (recorded for downstream forensics):

| Key pattern | Value type | Purpose |
|-------------|------------|---------|
| ``log_id:{proposal_id}`` | UUID string | The Phase 25 D-13 agent-supplied ExecutionLog primary key -- INSERT-on-conflict-do-nothing on the server side keeps duplicate retries a no-op. |
| ``req_id:{proposal_id}`` | UUID string | The Phase 28 D-15 ``ExecBatchProgressPayload.request_id`` -- ``SET NX EX 3600`` on the server side keeps duplicate progress POSTs a no-op. |

**SAQ retry-stable UUID lifecycle** (as verified by ``test_uuids_reused_from_job_meta_on_retry``):

1. First run: ``job.meta == {}``. ``_load_or_seed_uuids`` seeds both keys per proposal, returns ``changed=True``. ``await job.update(meta=...)`` persists the merged dict to Redis.
2. SAQ retries the job. ``job`` is reloaded from Redis with the previously-written ``meta`` intact.
3. ``_load_or_seed_uuids`` sees both keys present per proposal, returns ``changed=False``. ``await job.update(...)`` is SKIPPED.
4. ``_execute_one`` runs again with the SAME ``execution_log_id`` and ``progress_request_id`` per proposal.
5. ``post_execution_log`` is INSERT-on-conflict-do-nothing on the server (Phase 25 D-13) -- no duplicate audit row.
6. ``post_exec_batch_progress`` hits ``SET NX EX 3600`` on the server (Plan 28-02 D-15) -- duplicate POST returns 200 with no HINCRBY.

### ``"<step>: <reason>"`` error_message prefix (D-01)

Both the failed ExecutionLog PATCH and the failed ``patch_proposal_state(failed)`` reporting now use ``f"{failed_step}: {exc!s}"[:500]`` instead of the previous raw ``str(exc)[:500]``. Audit forensics can mechanically slice failures by sub-step without parsing free-form exception text.

### D-16 fire-and-forget progress POST

Both the success-path and failure-path progress POSTs are wrapped in ``try/except Exception``: if the agent_client's tenacity retries are exhausted (5xx after 3 attempts or persistent ConnectError/Timeout), the exception is caught, a ``logger.warning("execute_approved_batch: progress POST failed for %s: %s", ...)`` is emitted, and ``_execute_one`` returns its normal True/False. The underlying file ops + ``patch_proposal_state`` PATCH have already committed, so the aggregate Redis-hash counter may be slightly under-reported in this rare case; the operator sees the discrepancy in SSE and can investigate via ``/audit/``.

### Defensive ``ctx.get("job")`` fallback

The plan's ``<done>`` block calls out a backward-compat requirement: ``tests/test_tasks/test_execute_approved_batch.py`` (Phase 26 B2 fixtures) constructs ``ctx={"api_client": api}`` -- no ``"job"`` key. The new code uses ``ctx.get("job")`` and falls back to per-call ``uuid.uuid4()`` generation + a DEBUG log entry if ``job`` is absent. The fall-back has no SAQ retry semantics (which legacy callers don't have anyway). All 10 legacy tests pass unchanged.

## TDD RED -> GREEN Sequence

- **RED commit ``9cdc782``** (``test(28-05): add failing tests for per-proposal progress POSTs + SAQ-meta UUID lift``): replaced the Wave 0 ``pytest.skip`` stub with the full 12-test suite. ``test_success_emits_one_deleted_progress_post`` failed with ``AssertionError: assert 0 == 1 ... api.post_exec_batch_progress.await_count`` -- the production code did not yet call the progress endpoint.
- **GREEN commit ``a67b00a``** (``feat(28-05): per-proposal progress POSTs + SAQ-meta retry-stable UUIDs``): rewrote ``_execute_one`` (signature widened with ``payload``, ``is_last``, ``execution_log_id``, ``progress_request_id``), added the new ``_classify_failure_step`` and ``_load_or_seed_uuids`` private helpers, rewired ``execute_approved_batch`` outer loop to load UUIDs from ``ctx['job'].meta`` before iterating. All 22 tests (12 new + 10 regression) now pass.

### REFACTOR gate

Not required. The implementation is minimal-surface and the Pydantic schemas are imported lazily already; no follow-up cleanup pass needed.

Gate sequence verified:

```
a67b00a feat(28-05): per-proposal progress POSTs + SAQ-meta retry-stable UUIDs
9cdc782 test(28-05): add failing tests for per-proposal progress POSTs + SAQ-meta UUID lift
b0e60e7 docs(phase-28): update tracking after wave 1
```

## 28-V-NN Test ID Status

| Test ID | Description | Status |
|---------|-------------|--------|
| **28-V-06** | Success path POSTs ``terminal_step="deleted"`` exactly once with ``sub_batch_terminal=True`` on single-item batch | **GREEN** |
| **28-V-07** | Failure path POSTs ``terminal_step="failed"`` + ``failed_at_step`` derived from ``current_step`` (path-traversal -> ``"copy"``) | **GREEN** |
| **28-V-08** | 3-proposal batch -> only the LAST POST has ``sub_batch_terminal=True``; all carry ``terminal_step="deleted"`` | **GREEN** |
| **28-V-09** | Regression: existing Phase 26 ``test_execute_approved_batch.py`` (10 tests) PASS unchanged | **GREEN** |
| **L6/L22 closure** | ``test_uuids_persisted_in_job_meta_on_first_run`` + ``test_uuids_reused_from_job_meta_on_retry`` (SAQ-meta retry-stable UUIDs) | **GREEN** |

Additional tests added for completeness (all GREEN):
- ``test_sha256_mismatch_maps_to_failed_at_verify`` (failed_at_step="verify" mapping)
- ``test_delete_failure_maps_to_failed_at_delete`` (failed_at_step="delete" mapping)
- ``test_progress_post_failure_logs_warning_but_does_not_raise`` (D-16 swallow + WARNING)
- ``test_error_message_uses_step_reason_prefix`` (D-01 ``"<step>: <reason>"`` contract)
- ``test_execution_log_and_progress_use_distinct_uuids`` (sanity)
- ``test_legacy_ctx_without_job_does_not_break`` (Phase 26 fixture backward-compat)
- ``test_correct_sha256_still_succeeds`` (sanity)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Tooling] ruff TC002 on the runtime ``import pytest`` line**

- **Found during:** Pre-commit on the RED commit.
- **Issue:** The test file uses ``pytest.MonkeyPatch`` and ``pytest.LogCaptureFixture`` only as type annotations. With ``from __future__ import annotations`` enabled at the top of the file, annotations are evaluated as strings -- so a runtime ``import pytest`` triggers ruff's ``TC002 Move third-party import 'pytest' into a type-checking block``.
- **Fix:** Moved ``import pytest`` into the ``if TYPE_CHECKING:`` block alongside ``from pathlib import Path``. The existing ``test_execute_approved_batch.py`` keeps its runtime ``import pytest`` because that file uses ``pytest.raises(...)`` at runtime; the new file does not, so TYPE_CHECKING-only is the right home.
- **Files modified:** ``tests/test_tasks/test_execute_approved_batch_progress.py`` (import block reorder).
- **Commit:** ``9cdc782`` (RED commit, pre-commit autofix applied).

**2. [Rule 1 - Tooling] ruff-format reflowed the docstring + helper layouts**

- **Found during:** Pre-commit on the RED commit.
- **Issue:** ruff-format normalized blank-line spacing between the helper functions and added a blank line after the section-comment dividers. Functional behavior is identical.
- **Fix:** Re-staged the reformatted file. No semantic change.
- **Files modified:** ``tests/test_tasks/test_execute_approved_batch_progress.py``.
- **Commit:** ``9cdc782``.

### Deviation from RESEARCH skeleton

The plan's ``<action>`` block offered two SAQ-meta-persistence shapes (per-proposal incremental ``job.update`` vs upfront single ``job.update``). I implemented the **upfront single-write** shape (RESEARCH's "alternative simpler shape"). Rationale recorded in the frontmatter ``decisions`` block: on first run we do one batched ``HSET`` to Redis with N keys per proposal; on retry we skip the write entirely. The per-proposal incremental shape would have done up to N ``HSET`` writes on the first run.

The other RESEARCH skeleton suggestion preserved verbatim:
- ``_classify_failure_step(current_step, exc)`` signature uses ``current_step`` + ``exc`` (not just ``exc``) -- matches RESEARCH L9 "track step in a local variable that the except-handler reads."
- The 4-transition ``current_step`` state machine (copy -> verify -> copy -> delete) matches the ``<interfaces>`` block's resolved convention.

No Rule 2 (missing critical functionality), Rule 3 (blocker), or Rule 4 (architectural) deviations occurred.

## Auth Gates

None. The new ``post_exec_batch_progress`` agent-client method inherits the existing ``PhazeAgentClient`` bearer token (Phase 26 D-09). No new credentials, no new external services, no operator-action gates.

## Threat Surface Scan

No NEW threat surface introduced beyond what the plan's ``<threat_model>`` enumerates. All declared mitigations are now implemented:

- **T-28-05-S** (agent forging its own ``agent_id`` in progress POST) -- the agent constructs the payload with ``payload.agent_id`` straight from ``ExecuteApprovedBatchPayload``; the controller's Plan 28-02 endpoint compares ``body.agent_id != agent.id`` and 403s. Tested server-side by Plan 28-02's ``test_cross_tenant_agent_id_mismatch_403_before_state_read``.
- **T-28-05-T1** (duplicate ExecutionLog rows on retry) -- ``test_uuids_reused_from_job_meta_on_retry`` proves the same ``execution_log_id`` is sent on retry; Phase 25 INSERT-on-conflict-do-nothing keeps the audit log clean.
- **T-28-05-T2** (duplicate progress HINCRBYs on retry) -- same test proves the same ``progress_request_id`` is sent; Plan 28-02's ``SET NX EX 3600`` keeps the Redis counters clean.
- **T-28-05-I** (bearer token leak) -- the new method routes through ``_request`` which never logs the Authorization header (Phase 26 D-13 hardening preserved).
- **T-28-05-D** (progress POST failure cascade) -- ``test_progress_post_failure_logs_warning_but_does_not_raise`` proves the swallow-and-log behavior; the file op is already committed before the POST fires.
- **T-28-05-V (V12 ASVS Files & Resources)** -- the existing ``_resolve_and_check_containment`` (Phase 26 T-26-11-S1) is UNCHANGED. Path-traversal failures map to ``failed_at_step="copy"`` per RESEARCH L9.
- **T-28-05-V13 (V13 ASVS API)** -- ``ExecBatchProgressPayload`` (Plan 28-02) has ``extra="forbid"``; the agent constructs the payload from typed code so unknown fields are structurally impossible.

No ``## Threat Flags`` section needed.

## Known Stubs

None. This plan implements the full D-03/D-15/D-16/D-01 contract for the agent-side execution lifecycle. Every code path described in the threat model and the counter-math table from Plan 28-02 is exercised by at least one test. The Wave 0 ``pytest.skip`` stub at ``tests/test_tasks/test_execute_approved_batch_progress.py`` is now replaced by the full 12-test implementation.

## Plan Verification

Executed the plan's ``<automated>`` command verbatim:

```bash
uv run pytest tests/test_tasks/test_execute_approved_batch_progress.py \
              tests/test_tasks/test_execute_approved_batch.py -x
```

Result: **22 passed, 0 failed, 0 skipped** in 0.09s.

``<done>`` criteria verification:

| Criterion | Required | Actual | Status |
|-----------|----------|--------|--------|
| ``grep -c "post_exec_batch_progress" src/phaze/tasks/execution.py`` | >= 2 | **2** | PASS |
| ``grep -c "ctx\[.job.\]" src/phaze/tasks/execution.py`` | >= 2 | **3** | PASS |
| ``grep -c "_classify_failure_step" src/phaze/tasks/execution.py`` | >= 1 | **3** | PASS |
| ``grep -c "current_step" src/phaze/tasks/execution.py`` | >= 4 | **13** | PASS |
| ``uv run pre-commit run --files <2 files>`` | green | green | PASS |
| Regression: legacy ``test_execute_approved_batch.py`` PASS unchanged | green | green | PASS |
| 28-V-06 / 28-V-07 / 28-V-08 GREEN | green | green | PASS |

Wider non-DB test surface: ran ``uv run pytest tests/test_tasks/ tests/test_schemas/ tests/test_services/test_agent_client.py tests/test_services/test_agent_client_exec_batch_progress.py -x`` -> **227 passed, 9 warnings** in 43.10s. The 9 RuntimeWarnings are pre-existing ``AsyncMockMixin._execute_mock_call was never awaited`` issues in unrelated ``tasks/tracklist.py`` and ``services/ingestion.py``; not introduced by this plan.

Full-suite ``uv run pytest -x`` was NOT run because the worktree environment has no running PostgreSQL container (DB-backed integration tests at ``tests/test_routers/test_pipeline_scans.py``, ``tests/test_services/test_proposal_queries.py``, etc. require ``localhost:5432``). Per Plan 28-01's and 28-02's SUMMARYs, these are pre-existing DB-infrastructure failures not introduced by Phase 28 work. The plan-relevant test surface (task + schema + agent_client) is fully green.

## TDD Gate Compliance

- **RED gate** (``test(28-05): ...`` commit ``9cdc782``): replaced Wave 0 ``pytest.skip(allow_module_level=True)`` stub with 12 failing tests. Pre-implementation ``pytest`` failed with ``AssertionError`` on the first test (``api.post_exec_batch_progress.await_count == 0 != 1``). PASS.
- **GREEN gate** (``feat(28-05): ...`` commit ``a67b00a``): rewrote ``_execute_one`` + ``execute_approved_batch`` + added ``_classify_failure_step`` + ``_load_or_seed_uuids``. All 22 tests (12 new + 10 regression) now pass. PASS.
- **REFACTOR gate:** not required -- the implementation is minimal-surface; no follow-up cleanup pass needed.

## Self-Check: PASSED

Verified both file paths and both commit hashes exist on this branch.

**File check** (both ``git ls-files``-tracked):

- ``src/phaze/tasks/execution.py`` -- MODIFIED (191 insertions / 14 deletions; 410 lines total).
- ``tests/test_tasks/test_execute_approved_batch_progress.py`` -- Wave 0 stub REPLACED (501 lines total; 12 tests).

**Commit check:**

- ``9cdc782`` (RED): present on ``worktree-agent-adfc88948163abb39``.
- ``a67b00a`` (GREEN): present on ``worktree-agent-adfc88948163abb39``.
