---
phase: 72-per-entry-compute-binding-fail-fast-retirement
plan: 01
subsystem: analyze/backends
tags: [testing, characterization, golden, compute-backend, byte-identical]
requires:
  - phaze.services.backends.resolve_backends
  - phaze.services.backends.resolved_non_local_kind
  - phaze.config.ControlSettings.cloud_enabled
  - phaze.config.ControlSettings.active_compute_scratch_dir
provides:
  - D-06 golden byte-identical characterization of the ≤1-compute dispatch/resolution path
  - explicit zero-compute (implicit all-local) no-cloud-activity regression
affects:
  - Plan 02 (fail-fast retirement) — must keep this module green
  - Plan 03 (per-entry compute binding rewire) — must keep this module green
tech-stack:
  added: []
  patterns:
    - characterization/golden test authored against CURRENT code, run green pre-change (Phase-68 D-01 precedent)
    - backends_toml_env fixture for registry-from-TOML construction
    - nonexistent PHAZE_BACKENDS_CONFIG_FILE pointer to force implicit-local default_factory
key-files:
  created:
    - tests/analyze/services/test_compute_binding_golden.py
  modified: []
decisions:
  - Characterize ONLY the byte-identical matching-ref single-compute deploy (agent_ref == Agent.id); the id!=agent_ref case is Plan 03's intended behavior change and is deliberately excluded.
  - Zero-compute regression forces the implicit-local baseline via a nonexistent config pointer (hermetic) rather than mutating process env, mirroring the default-registry test's isolation.
metrics:
  duration: ~10m
  completed: 2026-07-05
  tasks: 2
  files: 1
---

# Phase 72 Plan 01: D-06 Golden ≤1-Compute Characterization Summary

Committed, green golden module pinning the observable ≤1-compute dispatch/resolution path and the
zero-compute implicit-all-local path against CURRENT production code — the byte-identical acceptance
safety net Waves 2-3 (Plans 02/03) must keep green.

## What Was Built

`tests/analyze/services/test_compute_binding_golden.py` (154 lines, 4 cells, test-only — no `src/` edits):

**Task 1 — ≤1-compute byte-identical characterization (matching-ref deploy):**
- `test_single_compute_registry_resolution_is_byte_identical` — a single-compute registry (local rank 99 + compute `oci-a1`, `agent_ref == "oci-a1"`, `scratch_dir=/srv/scratch`) resolves `cloud_enabled is True`, `resolved_non_local_kind == "compute"`, `active_compute_scratch_dir == "/srv/scratch"`, and composes the exact `/pushed` scratch-path `f"{scratch_dir}/{file_id}.{file_type}"` → `/srv/scratch/<uuid>.mp3` (the D-07 boundary agent_push.py must hold).
- `test_compute_backend_is_available_true_when_matching_ref_agent_online` — resolved `ComputeAgentBackend.is_available` is True when the compute agent whose `Agent.id == "oci-a1"` equals the registry `agent_ref` is online.
- `test_compute_backend_is_available_false_when_agent_absent_never_raises` — absent compute agent → `is_available` returns False (degrade-safe hold, never raises).

**Task 2 — explicit zero-compute (implicit all-local) regression:**
- `test_implicit_all_local_registry_has_no_cloud_activity` — with a nonexistent `PHAZE_BACKENDS_CONFIG_FILE` the `_default_local_registry` fires: `cloud_enabled is False`, `resolved_non_local_kind == "local"`, `active_compute_scratch_dir is None`, and `resolve_backends` yields exactly one `LocalBackend` with zero `ComputeAgentBackend`.

The "compute agent online but `id != agent_ref`" case is deliberately NOT characterized — that is Plan 03's intended per-entry-binding behavior change, not a byte-identical invariant.

## Verification

- `uv run pytest tests/analyze/services/test_compute_binding_golden.py -q` → 4 passed (against unchanged production code, ephemeral test Postgres on 5433).
- `uv run ruff check` + `uv run ruff format --check` → clean.
- `git diff --name-only f3c8643d~1 HEAD` → only the test file; no `src/` file modified.

## Deviations from Plan

None — plan executed exactly as written.

Environment note (not a deviation): the async `session`-fixture cells require the project's ephemeral
integration Postgres. Started it via `just test-db` (localhost:5433) and exported `TEST_DATABASE_URL`
before running — the standard project test-DB recipe, no code impact.

## Threat Model Adherence

- T-72-01-01 (mitigate): the golden is authored against CURRENT code and runs green in Wave 1 — a mis-pinned baseline would have failed the task `verify` immediately. Green confirms the baseline is real behavior.
- T-72-01-02 (accept): fixtures construct no `SecretStr` fields (compute entries carry only `agent_ref`/`scratch_dir`); no credential material.
- T-72-01-SC (mitigate): zero new dependencies; no package-manager install task exists.

## Commits

- `f3c8643d` test(72-01): golden ≤1-compute byte-identical characterization
- `a83c8fd2` test(72-01): explicit zero-compute all-local regression

## Self-Check: PASSED

- FOUND: tests/analyze/services/test_compute_binding_golden.py
- FOUND: commit f3c8643d
- FOUND: commit a83c8fd2
