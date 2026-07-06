---
phase: 76-compute-push-hardening
plan: 03
subsystem: routers
tags: [security, input-validation, http-boundary, fastapi, agent-id]
requires:
  - "Agent.id_charset DB CHECK (models/agent.py:36) — canonical agent-id shape"
  - "CLI AGENT_ID_RE (cli/__init__.py:44) — same shape, do-not-weaken"
provides:
  - "Pattern + max_length=128 validation on scan_status agent_id (GET /tracklists/scan/status)"
  - "Annotated Query pattern + max_length=128 validation on agent_roots_swap agent_id (GET /pipeline/scans/agent-roots)"
  - "Malformed-agent_id -> 422 regression tests for both endpoints"
affects:
  - "src/phaze/routers/tracklists.py"
  - "src/phaze/routers/pipeline_scans.py"
tech-stack:
  added: []
  patterns:
    - "FastAPI Query(pattern=..., max_length=...) request-boundary validation"
    - "Annotated[str, Query(...)] for a query param that also needs a Depends sibling"
key-files:
  created: []
  modified:
    - "src/phaze/routers/tracklists.py"
    - "src/phaze/routers/pipeline_scans.py"
    - "tests/shared/routers/test_pipeline_scans.py"
    - "tests/identify/routers/test_tracklists.py"
decisions:
  - "Placed scan_status 422 tests in tests/identify/routers/test_tracklists.py (where the existing scan_status tests already live, reusing install_fake_queues) and agent_roots_swap 422 tests in tests/shared/routers/test_pipeline_scans.py (alongside the existing agent_roots_swap tests) — per the plan's explicit test-placement discretion (D-Discretion)."
metrics:
  duration: "~15m"
  completed: "2026-07-06"
  tasks: 2
  files: 4
requirements: [HARD-03]
---

# Phase 76 Plan 03: agent_id HTTP-boundary validation Summary

HARD-03 hardens both unvalidated `agent_id` query-param boundaries with the canonical agent-id
shape (`pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$"`, `max_length=128`) so a malformed id returns 422 at
the HTTP boundary instead of a silently-empty 200, closing AR-30-03 / Phase-30 REVIEW IN-01.

## What Was Built

- **`routers/tracklists.py::scan_status` (`GET /tracklists/scan/status`)** — the `agent_id`
  `Query(...)` gained `pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128`. `Query` was already
  imported. A malformed id (e.g. `Bad_ID!`) now 422s before the handler touches
  `task_router.queue_for(agent_id)` (previously it reached the handler and returned a silent empty
  200 poll — or, absent a task_router, a 500).
- **`routers/pipeline_scans.py::agent_roots_swap` (`GET /pipeline/scans/agent-roots`)** — the bare
  `agent_id: str` query param became
  `agent_id: Annotated[str, Query(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)]`, and
  `Query` was added to the `from fastapi import (...)` line (it was not previously imported there).
  A malformed id now 422s before `session.get(Agent, agent_id)` instead of silently rendering the
  empty picker.
- **Regression tests** — four new tests (malformed → 422, well-formed → passes) for each endpoint:
  `test_scan_status_malformed_agent_id_returns_422` / `_well_formed_agent_id_passes_validation` in
  `tests/identify/routers/test_tracklists.py`, and
  `test_agent_roots_swap_malformed_agent_id_returns_422` /
  `_well_formed_agent_id_passes_validation` in `tests/shared/routers/test_pipeline_scans.py`.

## Task-by-Task

| Task | Name | Type | Commit |
| ---- | ---- | ---- | ------ |
| 1 (RED) | Add failing 422 regression tests for both endpoints | test | `6c94b4bd` |
| 1 (GREEN) | Validate agent_id at both HTTP boundaries | feat | `140dc64b` |
| 2 | Quality gate (ruff, mypy, docs-drift, targeted suite, no-dep-change) | verify | (no code change) |

TDD cycle: RED confirmed both malformed tests fail (agent_roots returned 200; scan_status reached
the handler and errored on the missing task_router), GREEN made all four pass. No REFACTOR needed —
the fix is two minimal signature edits plus one import addition.

## Verification

- `GET /tracklists/scan/status?...&agent_id=Bad_ID!` → **422**; well-formed `test-agent-01` → 200.
- `GET /pipeline/scans/agent-roots?agent_id=Bad_ID!` → **422**; well-formed `test-agent` → 200.
- `grep` confirms the exact pattern + `max_length=128` on both params; `Query` present in the
  `pipeline_scans.py` fastapi import.
- `uv run ruff check` + `uv run ruff format --check` on all four touched files: **pass**.
- `uv run mypy src/phaze/routers/tracklists.py src/phaze/routers/pipeline_scans.py`: **Success**.
- `just docs-drift`: **10 passed** (green).
- Full targeted modules: `test_pipeline_scans.py` 53 passed; `test_tracklists.py` 67 passed.
- `git diff bb31c76d..HEAD -- pyproject.toml uv.lock`: **empty** (no dependency files changed, D-10).

## Deviations from Plan

None — plan executed as written.

Two within-discretion choices (explicitly permitted by the plan's D-Discretion and the Task 1
`<action>` note):
1. The scan_status 422 tests were added to `tests/identify/routers/test_tracklists.py` (where the
   existing scan_status tests already live and where the `install_fake_queues`/`client` fixtures
   are), rather than to `tests/shared/routers/test_pipeline_scans.py`. This touches a second test
   module beyond the one listed in the plan frontmatter — the plan's acceptance criteria
   anticipated this ("plus any tracklists test module touched").
2. During GREEN a transient DB `ConnectionError` flake (the known colima full-suite flake) errored
   four DB-touching tests in a combined run; re-running the affected subset in isolation passed
   6/6, confirming infra-not-regression per the standing MEMORY guidance.

## Authentication Gates

None.

## Known Stubs

None.

## Threat Flags

None — the change reduces the input surface at existing boundaries; no new endpoints, auth paths,
file access, or schema changes were introduced. The threat register's AR-30-03 / Phase-30 REVIEW
IN-01 mitigation (`mitigate` disposition) is now implemented at both boundaries.
