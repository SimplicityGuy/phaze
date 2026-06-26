---
phase: quick-260614-sg8
plan: 01
subsystem: tracklists-router
tags: [bugfix, saq, enqueue, dead-letter, scan_live_set]
requires:
  - phaze.schemas.agent_tasks.ScanLiveSetPayload
  - phaze.models.file.FileRecord
  - phaze.services.enqueue_router.resolve_queue_for_task
provides:
  - trigger_scan builds the complete ScanLiveSetPayload per enqueue
affects:
  - POST /tracklists/scan
tech-stack:
  added: []
  patterns:
    - "Mirror pipeline.py _enqueue_scan_jobs: full payload via model_dump(mode=\"json\"), no explicit key"
    - "cast(\"str\", routed.agent_id) to narrow str | None for AGENT_TASK payloads"
key-files:
  created: []
  modified:
    - src/phaze/routers/tracklists.py
    - tests/test_routers/test_tracklists.py
decisions:
  - "Set scan_progress total to len(job_ids) (jobs actually enqueued) so the poll reaches done when ids are skipped"
  - "Skip non-UUID strings and ids with no FileRecord rather than 500 or dead-letter"
metrics:
  duration: ~25m
  completed: 2026-06-14
---

# Phase quick-260614-sg8 Plan 01: Fix trigger_scan Dead-Letter Enqueue Summary

Fixed the `POST /tracklists/scan` dead-letter bug by building the complete `ScanLiveSetPayload(file_id, original_path, agent_id)` per enqueue — mirroring the Phase-40 `pipeline.py::_enqueue_scan_jobs` producer — so `scan_live_set` jobs validate against the strict (`extra="forbid"`) schema instead of dead-lettering (the v4.0.8 payload-incident class).

## What Changed

### Task 1 — `trigger_scan` builds the full payload (`src/phaze/routers/tracklists.py`)
- Added `from phaze.schemas.agent_tasks import ScanLiveSetPayload` and `cast` to imports.
- After the (unchanged) `resolve_queue_for_task` call and `NoActiveAgentError` empty-state branch:
  - Parse each submitted `file_ids` string via `uuid.UUID(...)`, skipping non-UUID strings (`except ValueError: continue`) — never a 500.
  - Load matching rows with `select(FileRecord).where(FileRecord.id.in_(parsed_ids))` into a `{id: record}` lookup.
  - `agent_id = cast("str", routed.agent_id)` (AGENT_TASK always resolves non-None), mirroring `pipeline.py`.
  - Iterate parsed ids in submission order; for each id present in the lookup, build `ScanLiveSetPayload(file_id=record.id, original_path=record.original_path, agent_id=agent_id)` and `await routed.queue.enqueue("scan_live_set", **payload.model_dump(mode="json"))`. No explicit `key=` (central deterministic key applied by the Phase-35 before_enqueue hook).
  - Skip ids with no FileRecord.
  - `total` context now `len(job_ids)` (jobs actually enqueued), not `len(file_ids)`, so the poll reaches `done` when some ids are skipped. All other context keys unchanged.
- Commit: `86af180`

### Task 2 — Regression tests (`tests/test_routers/test_tracklists.py`)
- Imported `ScanLiveSetPayload`.
- Updated `test_trigger_scan`: seeds a real `FileRecord` via `_make_file` + `session.add` + `flush`, posts its id, and asserts exactly one captured `("scan_live_set", payload)` with `file_id` (== str id), `original_path` (== record path), `agent_id` (== "nox"), plus `ScanLiveSetPayload.model_validate(payload)` succeeds. Kept the `agent_id=nox` poll-URL assertion.
- Added `test_trigger_scan_skips_file_id_without_record`: random uuid with no record → `captured == []` (no dead-letter).
- Added `test_trigger_scan_skips_malformed_file_id`: `"not-a-uuid"` → 200, `captured == []` (covers the `except ValueError` skip, lines 236-237).
- `test_trigger_scan_no_active_agent` left unchanged.
- Commit: `027bd79`

## Verification

- `uv run pytest tests/test_routers/test_tracklists.py` — 65 passed.
- `phaze.routers.tracklists` module coverage: **93.01%** (≥85%).
- Full suite: **1807 passed**, total coverage **97.59%**.
- `uv run ruff check .` — clean. `uv run ruff format --check .` — clean (336 files).
- `uv run mypy .` — Success, no issues (155 source files).
- `pre-commit run --all-files` — all hooks Passed (no `--no-verify`).

## Deviations from Plan

None of substance. One in-scope addition beyond the plan's two named test cases: a `test_trigger_scan_skips_malformed_file_id` test was added to cover the non-UUID skip branch that the plan's `<behavior>` block specifies (lines 236-237), keeping the new code path covered. This is a test-only addition within the plan's stated behavior.

## Threat Model Outcome

- **T-sg8-01 (DoS / dead-letter):** mitigated — every enqueue now carries the full schema-valid payload.
- **T-sg8-02 (Tampering / form input):** mitigated — non-UUID and unknown ids are dropped, never enqueued, never a 500.
- **T-sg8-SC (supply chain):** accepted — no package installs in this change.

No new threat surface introduced (no new endpoints, auth paths, or schema changes).

## Known Stubs

None.

## Self-Check: PASSED
- FOUND: src/phaze/routers/tracklists.py
- FOUND commit: 86af180 (fix: trigger_scan full payload)
- FOUND commit: 027bd79 (test: full-payload regression)
