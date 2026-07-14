---
phase: quick-260707-g84
plan: 01
subsystem: agent-worker / cloud-compute-agent
tags: [memory-safety, concurrency, saq, lanes, cloud-agent, oom]
requires:
  - "src/phaze/config.py worker_max_jobs + lane_*_concurrency knobs (PR #218)"
  - "src/phaze/tasks/agent_worker.py lane-mode resolution (PR #218)"
provides:
  - "min(lane knob, worker_max_jobs) concurrency resolution — WORKER_MAX_JOBS authoritative as a ceiling in lane mode"
  - "effective-concurrency startup log (lane + ceiling + clamped flag)"
  - "PHAZE_LANE_ANALYZE_CONCURRENCY=1 pin on the OCI A1 compute agent"
affects:
  - "OCI Ampere A1 (12 GB) compute agent — no longer OOM-kills on 4 concurrent process_file jobs"
tech-stack:
  added: []
  patterns:
    - "per-lane concurrency knob governs; WORKER_MAX_JOBS is a ceiling (min())"
key-files:
  created: []
  modified:
    - "src/phaze/tasks/agent_worker.py"
    - "tests/agents/tasks/test_agent_worker_lanes.py"
    - "docker-compose.cloud-agent.yml"
    - "tests/agents/deployment/test_cloud_agent_compose.py"
    - "docs/agent-queue-lanes.md"
decisions:
  - "Make WORKER_MAX_JOBS a ceiling (min(lane, worker_max_jobs)) rather than replacing the lane knob — preserves file-server defaults unchanged while giving memory-constrained hosts an authoritative cap."
  - "Belt-and-braces: also pin PHAZE_LANE_ANALYZE_CONCURRENCY=1 in cloud-agent compose so the cap is explicit at the knob that actually governs lane concurrency."
metrics:
  duration: "~15 min"
  completed: "2026-07-07"
  tasks: 3
  files: 5
  commits: 4
requirements_completed: [G84-01]
status: complete
---

# Quick 260707-g84: Fix Inert Compute-Agent Memory-Safety Cap Summary

Made `WORKER_MAX_JOBS` an authoritative ceiling in lane-mode SAQ concurrency
(`concurrency = min(lane knob, worker_max_jobs)`), fixing the PR #218 regression where the
per-lane knob alone governed concurrency and the OCI Ampere A1 (12 GB) compute agent silently
ran 4 concurrent ~8 GB `process_file` jobs and OOM-killed. Added a startup log of effective
concurrency, pinned the cloud-agent analyze lane to 1, and documented the ceiling semantics.

## What Was Built

**Task 1 (TDD) — worker_max_jobs ceiling + startup log**
`src/phaze/tasks/agent_worker.py`: lane-mode concurrency now resolves to
`min(getattr(settings, lane_attr), settings.worker_max_jobs)`. Captured `_lane_raw_concurrency`
and `_concurrency_clamped` at module scope; added an effective-concurrency `logger.info` inside
`startup()` (after `configure_logging`, never at import time) reporting effective concurrency,
lane, the `worker_max_jobs` ceiling, and whether it clamped. All-mode branch
(`concurrency == worker_max_jobs`) unchanged. Two new unit tests via the existing `_reload_worker`
harness prove `WORKER_MAX_JOBS=1 + lane=analyze` → 1 and default → 4.

**Task 2 — cloud-agent compose pin + guard test**
`docker-compose.cloud-agent.yml`: added `PHAZE_LANE_ANALYZE_CONCURRENCY=1` on the compute worker
alongside `PHAZE_AGENT_LANE=analyze`, with an explanatory comment. New compose-guard test asserts
the analyze lane is capped to 1.

**Task 3 — docs**
`docs/agent-queue-lanes.md`: core-budget note (concurrency = min(lane knob, worker_max_jobs)) and
compute-agent memory-safety rationale (A1 12 GB, process_file ~8 GB peak, `WORKER_MAX_JOBS=1`
inert in lane mode). gsd:doc marker on line 1 preserved.

## Verification

- `uv run pytest tests/agents/tasks/test_agent_worker_lanes.py tests/agents/deployment/test_cloud_agent_compose.py -q` → **21 passed**
- `uv run ruff check .` → **All checks passed**
- `uv run mypy .` → **Success: no issues found in 196 source files**
- TDD gate: `test(...)` RED commit (91cf3bf1, confirmed failing `assert 4 == 1`) → `feat(...)` GREEN commit (9d89293b).
- Spot: analyze + WORKER_MAX_JOBS=1 → 1; default analyze → 4; all-mode → worker_max_jobs (covered by existing `test_all_mode_preserves_todays_behavior`).

## Deviations from Plan

None — plan executed exactly as written.

## Commits

- `91cf3bf1` test(260707-g84): add failing test for lane-mode WORKER_MAX_JOBS ceiling
- `9d89293b` feat(260707-g84): make WORKER_MAX_JOBS a ceiling in lane-mode concurrency
- `3e8b2bb5` feat(260707-g84): pin cloud-agent analyze lane to 1 concurrent job
- `f3dad934` docs(260707-g84): document lane-knob-governs / WORKER_MAX_JOBS-ceiling semantics

## Deployment Note

This is code + compose only. A homelab redeploy of the cloud-agent (and the file-server lane
workers picking up the new resolution logic) lands the memory-safety cap in prod.

## Self-Check: PASSED

- Files: all 5 modified files present.
- Commits: 91cf3bf1, 9d89293b, 3e8b2bb5, f3dad934 all found in git log.
