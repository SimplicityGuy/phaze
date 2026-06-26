---
phase: 50-push-pipeline
plan: 04
subsystem: agent-compute
tags: [cloud-push, integrity, scratch-cleanup, saq]
requires:
  - "50-01: ProcessFilePayload.expected_sha256/scratch_path fields + agent_push schemas"
  - "50-03 (same wave, at merge): PhazeAgentClient.report_push_mismatch client method"
provides:
  - "process_file scratch read-path swap + off-loop sha256 verify + finally cleanup"
  - "enqueue_process_file expected_sha256/scratch_path kwargs"
affects:
  - "50-05: control-side push-mismatch handling + re-push attempt cap"
tech-stack:
  added: []
  patterns:
    - "asyncio.to_thread(compute_sha256, ...) off-loop integrity verify (scan.py pattern)"
    - "finally-unlink(missing_ok=True) bounds scratch disk to the in-flight set"
key-files:
  created: []
  modified:
    - src/phaze/tasks/functions.py
    - src/phaze/services/analysis_enqueue.py
    - tests/test_process_file_scratch.py
    - tests/test_services/test_analysis_enqueue.py
decisions:
  - "report_push_mismatch reached via the Any-typed ctx['api_client'] (not via the PhazeAgentClient-annotated local) so functions.py typechecks before the parallel 50-03 client method merges, and stays clean afterward (no type:ignore to go unused under warn_unused_ignores)."
  - "Mismatch branch unlinks explicitly (delete-before-report per threat model) AND the universal finally unlinks; both missing_ok so double-unlink is a safe no-op."
metrics:
  duration: "~12m"
  tasks: 2
  files-modified: 4
  completed: 2026-06-26
---

# Phase 50 Plan 04: Compute-Agent Scratch Verify + Cleanup Summary

`process_file` now integrity-verifies and analyzes the ephemeral pushed scratch copy (sha256 checked off the event loop before any analysis), deletes the scratch file in a `finally` on every exit path, and `enqueue_process_file` can pin the scratch path + expected sha256 for a cloud file while the bulk local producer stays byte-identical.

## What Was Built

**Task 1 — `process_file` scratch read-path (CLOUDPIPE-03 / -04)** (`src/phaze/tasks/functions.py`)
- `read_path = payload.scratch_path or payload.original_path` — the path-agnostic analyzer now consumes the scratch copy when one is pinned, else `original_path` (existing behavior).
- Gated on `payload.scratch_path and payload.expected_sha256`: `actual = await asyncio.to_thread(compute_sha256, Path(scratch_path))`. On mismatch the scratch file is unlinked, `report_push_mismatch(file_id)` is awaited, and a `{"status": "push_mismatch"}` result returns with **no analysis** (corrupt transfer is never trusted, T-50-corrupt).
- The whole analysis body is wrapped so a `finally` runs `Path(scratch_path).unlink(missing_ok=True)` on **every** exit: success, `TimeoutError`, `ProcessExpired`, generic re-raise, and the mismatch early-return (T-50-scratch-dos).

**Task 2 — producer threading** (`src/phaze/services/analysis_enqueue.py`)
- Added keyword-only `expected_sha256: str | None = None` and `scratch_path: str | None = None`, threaded into the `ProcessFilePayload(...)` construction (mirroring `fine_cap`/`coarse_cap`). The deterministic key and `timeout=7200`/`retries=2` policy are untouched; a call without the new kwargs serializes both as `None`, so the bulk `_enqueue_analysis_jobs` producer is byte-identical under `extra="forbid"`.

## How It Works

The control plane (50-05) pins `expected_sha256` (from `FileRecord.sha256_hash`) and the per-job `scratch_path` when enqueuing a cloud file. The compute agent reads only that ephemeral copy, hashes it off the event loop (so the event loop stays free for the pebble analysis pool), and refuses to analyze a copy whose digest does not match. Cleanup is unconditional via `finally`, so the scratch dir can never accumulate orphans regardless of how the job ends.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Stale payload key-set assertion in `test_analysis_enqueue.py`**
- **Found during:** Task 2 (running the existing enqueue suite).
- **Issue:** 50-01 added `expected_sha256`/`scratch_path` to `ProcessFilePayload` but left `test_enqueue_process_file_complete_payload_and_policy` asserting the old 7-field `model_dump` key-set, so the test was already failing on the wave-2 base.
- **Fix:** Updated the assertion to the finalized 9-field set and added `is None` checks for the two new fields — the contract this plan's producer completes.
- **Files modified:** `tests/test_services/test_analysis_enqueue.py`
- **Commit:** c44a10f

### Cross-plan integration note (not a deviation)

`report_push_mismatch` is the client method added by the parallel wave-2 plan **50-03** (which owns `agent_client.py`). To keep `functions.py` typechecking before that file merges — and to avoid a `# type: ignore` that would become an *unused* ignore (CI-red under `warn_unused_ignores=true`) once 50-03 lands — the mismatch report is invoked through the `Any`-typed `ctx["api_client"]` rather than the `PhazeAgentClient`-annotated `api` local. `agent_client.py` was **not** modified (it is 50-03's file), preserving the disjoint-file parallel-execution invariant.

## Verification

- `uv run pytest tests/test_process_file_scratch.py -q -k "sha256 or cleanup"` — pass (match→analyze; mismatch→unlink+report+no-analyze; finally-unlink on success/timeout/crash/error/mismatch).
- `uv run pytest tests/test_process_file_scratch.py -q -k "enqueue or scratch"` — pass (payload carries the fields when supplied, `None` otherwise; key/policy unchanged).
- `uv run pytest tests/test_process_file_scratch.py tests/test_task_split.py -q` — 18 passed (`functions.py` still Postgres-free).
- `uv run pytest tests/test_services/test_analysis_enqueue.py -q` — pass.
- `uv run ruff check .` — all checks passed.
- `uv run mypy .` — no issues in 166 source files.
- Grep gates: `to_thread(compute_sha256` and `report_push_mismatch` present in `functions.py`; `expected_sha256` and `scratch_path` present in `analysis_enqueue.py`.

## Threat Model Coverage

| Threat ID | Mitigation in this plan |
|-----------|-------------------------|
| T-50-corrupt | Off-loop sha256 verify before analysis; mismatch → delete + report + no-analyze. |
| T-50-scratch-dos | `finally` unlink on every exit path bounds scratch disk to the in-flight set. |
| T-50-loop | Out of scope here — re-push attempt cap is enforced control-side (50-05); this plan only reports the mismatch. |

No new threat surface introduced beyond the plan's `<threat_model>`.

## Commits

- `8f9ee34` test(50-04): failing tests for scratch sha256 verify + cleanup + producer threading (RED)
- `5ce6604` feat(50-04): scratch read-path + off-loop sha256 verify + finally cleanup in process_file
- `c44a10f` feat(50-04): thread expected_sha256 + scratch_path through enqueue_process_file (+ Rule 1 fix)

## Known Stubs

None.

## Self-Check: PASSED
- FOUND: src/phaze/tasks/functions.py (to_thread(compute_sha256) + report_push_mismatch + finally cleanup)
- FOUND: src/phaze/services/analysis_enqueue.py (expected_sha256/scratch_path kwargs)
- FOUND: tests/test_process_file_scratch.py (real tests replace Wave-0 stubs)
- FOUND: commits 8f9ee34, 5ce6604, c44a10f
