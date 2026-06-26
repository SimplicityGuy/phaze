---
phase: 50-push-pipeline
plan: 00
subsystem: testing
tags: [nyquist, test-scaffolding, push-pipeline, wave-0]
requires: []
provides:
  - "tests/test_push_pipeline.py (argv/exit_code/janitor selectors)"
  - "tests/test_process_file_scratch.py (sha256/cleanup selectors)"
  - "tests/test_staging_cron.py (≤N window-math selectors)"
  - "tests/test_routing_seam.py (AWAITING_CLOUD routing selectors)"
affects:
  - "50-03 (push_pipeline impl), 50-04 (process_file scratch impl), 50-06 (staging cron + routing seam)"
tech-stack:
  added: []
  patterns:
    - "Wave 0 Nyquist test stubs: collectible skip-marked tests reserving -k selectors before production code exists"
key-files:
  created:
    - tests/test_push_pipeline.py
    - tests/test_process_file_scratch.py
    - tests/test_staging_cron.py
    - tests/test_routing_seam.py
  modified: []
decisions:
  - "Push-pipeline selectors split across two disjoint files (test_push_pipeline.py for 50-03, test_process_file_scratch.py for 50-04) so the two Wave 2 plans never write the same test file in parallel."
  - "Stub bodies use pytest.skip with a Wave 0 reason string (not xfail) — they cannot assert-pass falsely while reserving the named selectors."
  - "Imports restricted to stdlib + pytest; no production modules (push.py, scratch read-path, staging cron, routing seam) are imported because they do not exist yet — guarantees collection succeeds."
metrics:
  duration: ~6 min
  completed: 2026-06-26
  tasks: 2
  files: 4
---

# Phase 50 Plan 00: Push-Pipeline Nyquist Test Stubs Summary

Created the four Wave 0 Nyquist test-stub files so every downstream Phase 50 implementation task has a concrete, collectible `<automated>` verify target (a real `-k` selector) before any production code is written.

## What Was Built

Four new test files under `tests/`, each a set of skip-marked stub functions that reserve the exact `-k` selectors the implementation plans verify against:

| File | Selectors reserved | Owning plan | Requirements |
|------|--------------------|-------------|--------------|
| `tests/test_push_pipeline.py` | `argv`, `exit_code`, `janitor` | 50-03 | CLOUDPIPE-02/-04 |
| `tests/test_process_file_scratch.py` | `sha256`, `cleanup` | 50-04 | CLOUDPIPE-03/-04 |
| `tests/test_staging_cron.py` | window-math (default file run) | 50-06 | CLOUDPIPE-01/-05 |
| `tests/test_routing_seam.py` | AWAITING_CLOUD routing (default file run) | 50-06 | CLOUDPIPE-01 |

All 11 stub functions skip with a `Wave 0 stub — implemented in 50-0X` reason. Imports are stdlib + pytest only, so pytest collection succeeds and the suite stays green.

## Tasks

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Push-pipeline + process-file-scratch stubs | 009bd9c | tests/test_push_pipeline.py, tests/test_process_file_scratch.py |
| 2 | Staging-cron + routing-seam stubs | a56cb0f | tests/test_staging_cron.py, tests/test_routing_seam.py |

## Verification

`uv run pytest tests/test_push_pipeline.py tests/test_process_file_scratch.py tests/test_staging_cron.py tests/test_routing_seam.py -q` → **11 skipped, 0 errors**.

Per-file `def test_` counts: push_pipeline 3, process_file_scratch 2, staging_cron 4, routing_seam 2 (all ≥ required).

Selector resolution confirmed: `-k argv`, `-k janitor`, `-k sha256`, `-k cleanup` each collect ≥1 test. All pre-commit hooks (ruff, ruff-format, bandit, mypy) passed on both commits.

## Deviations from Plan

None — plan executed exactly as written.

## Self-Check: PASSED

- FOUND: tests/test_push_pipeline.py
- FOUND: tests/test_process_file_scratch.py
- FOUND: tests/test_staging_cron.py
- FOUND: tests/test_routing_seam.py
- FOUND commit: 009bd9c
- FOUND commit: a56cb0f
