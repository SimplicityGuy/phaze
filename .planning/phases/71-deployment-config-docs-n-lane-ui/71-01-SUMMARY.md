---
phase: 71-deployment-config-docs-n-lane-ui
plan: 01
subsystem: services/backends
tags: [beui-01, backend-lane-snapshot, degrade-safe, admission, availability-probe]
requires:
  - "phaze.services.backends.resolve_backends (Phase 69, SCHED-01) — N-backend registry"
  - "phaze.models.cloud_job.CloudJob{backend_id, cloud_phase, inadmissible, status} (Phase 68/70)"
  - "_BaseBackend.in_flight_count (Phase 68, D-02) — per-backend cloud_job COUNT"
provides:
  - "get_backend_lane_snapshot(session) -> list[dict] — one rank-ascending secret-free lane per registry backend"
  - "_admission_by_backend_id / _probe_availability / _probe_one / _kind_of / _PROBE_TIMEOUT_SEC / _ZERO_ADMISSION"
affects:
  - "Plan 03 (BEUI-01 N-lane grid) — seeds build_dashboard_context + renders the lane template from this data path"
tech-stack:
  added: []
  patterns:
    - "GROUP BY backend_id with func.count().filter(...) FILTER aggregates for per-lane admission attribution (D-03)"
    - "asyncio.gather + per-probe asyncio.wait_for(_PROBE_TIMEOUT_SEC) bounded concurrent availability probes (D-02)"
    - "SP-1 never-500 degrade: [] / {} on any error with guarded double-rollback (mirrors pipeline._safe_count)"
key-files:
  created:
    - "tests/shared/services/test_lane_snapshot.py"
  modified:
    - "src/phaze/services/backends.py"
decisions:
  - "D-01/D-06: snapshot returns one dict per registry backend, sorted rank-ascending tie-broken by id, server-side"
  - "D-02: live is_available() probe per call, concurrent, per-probe ~1.5s timeout; LocalBackend short-circuited (no I/O)"
  - "D-03: per-backend_id admission attribution via GROUP BY so each Kueue lane owns its quota_wait/inadmissible counts"
  - "T-71-01: only {id,kind,rank,cap,in_flight,available,quota_wait,inadmissible} leaves the module; probe logs backend_id only"
metrics:
  duration: ~35m
  tasks: 2
  files: 2
  tests: 14
  completed: 2026-07-04
---

# Phase 71 Plan 01: Backend-Lane Snapshot Service Summary

Read-only `get_backend_lane_snapshot(session)` data path that feeds the BEUI-01 N-lane grid: one rank-ascending, secret-free dict per registry backend with live bounded availability + per-`backend_id` admission attribution, degrading to `[]` on any error so it never raises into the hot 5s `/pipeline/stats` poll.

## What was built

`src/phaze/services/backends.py` gained a self-contained BEUI-01 read section (no changes to the existing dispatch/reconcile bodies):

- **`_admission_by_backend_id(session)`** — a single `GROUP BY CloudJob.backend_id` producing `{backend_id: {quota_wait, inadmissible}}`. Generalizes the two GLOBAL pipeline predicates (`get_cloud_phase_counts`'s `QUEUED_BEHIND_QUOTA` and `get_inadmissible_count`'s `inadmissible AND status IN {SUBMITTED,RUNNING}`) into per-lane `func.count().filter(...)` FILTER aggregates (D-03). `backend_id`-NULL rows are excluded (`.where(is_not(None))`); `cloud_phase`-NULL local/compute rows contribute 0 to `quota_wait`. Degrades to `{}` with a guarded rollback.
- **`_probe_one` / `_probe_availability`** — `LocalBackend` short-circuits to `(id, True)` with no I/O; every other backend's `is_available` runs under `asyncio.wait_for(_PROBE_TIMEOUT_SEC=1.5)` inside a try/except → offline, fanned out concurrently via `asyncio.gather`. One hung Kueue cluster times out to offline for that ONE lane while the fan-out stays bounded to ~one timeout (T-71-02). Probe-failure logs carry `backend_id` only (T-71-01).
- **`_kind_of`** — `local`/`compute`/`kueue` via `isinstance` (mirrors `resolve_backends` dispatch), `unknown` fallback.
- **`get_backend_lane_snapshot(session)`** — resolves the registry, gathers admission + availability, builds one lane dict `{id, kind, rank, cap, in_flight (await in_flight_count), available, **admission}`, sorts `key=(rank, id)` (D-06), and wraps the whole body in an SP-1 try/except → `[]` with guarded double-rollback (T-71-03). Only the eight scalar keys ever leave the module — no `config`/`SecretStr`/token.

## Threat mitigations applied

- **T-71-01 (info disclosure):** lane dicts + probe logs emit only `{id,kind,rank,cap,in_flight,available,quota_wait,inadmissible}` / `backend_id` — never `config`, `SecretStr`, kube SA token, or S3 key. A test asserts the exact key-set (no secret-bearing key).
- **T-71-02 (DoS via hung probe):** per-probe `asyncio.wait_for(1.5s)` + try/except; `test_probe_timeout_isolation` proves a 5s-sleeping lane renders offline while a healthy lane stays online and the call returns in <1s.
- **T-71-03 (raise into poll):** `[]`/`{}` degrade with guarded double-rollback on any error; tests force DB-error and rollback-also-fails paths.

## Verification

- `uv run pytest tests/shared/services/test_lane_snapshot.py` → **14 passed** (shape, rank order + id tie-break, `[]`/`{}` degrade incl. rollback-also-fails, per-backend admission GROUP BY, probe-timeout isolation, local short-circuit, `_kind_of` dispatch + fallback).
- Task 1 acceptance subset `-k "admission_per_backend or probe_timeout_isolation"` → 2 passed.
- New code fully covered (isolated `--cov=phaze.services.backends` shows only pre-existing dispatch/reconcile lines missed — those are covered by the analyze-bucket suites).
- `uv run ruff check .` clean; `uv run mypy .` → Success, 192 source files.
- Backends-touching suites pass per-file in isolation (`test_backends.py` 27, `test_dispatch_snapshot.py` 8) — the mixed-run teardown errors are the known cross-bucket non-hermetic pattern (get_settings lru_cache / saq stub), not a regression from this plan (no existing code was modified).

## Deviations from Plan

None — plan executed as written. The plan's `71-PATTERNS.md` copy-from excerpts were unavailable in the worktree (the file was untracked in the main repo and not part of the plan's committed base), so the implementation was derived directly from the plan's `<interfaces>` block + the live `pipeline._safe_count` / `get_inadmissible_count` / `get_cloud_phase_counts` / drain-isolation precedents named in `<read_first>`. Output matches every stated behavior, artifact, key_link, and D-01/02/03/06 truth.

## Notes for downstream (Plan 03)

- Consume `get_backend_lane_snapshot(session)` in `build_dashboard_context`; each lane dict is render-ready and pre-sorted — the template loops verbatim.
- Availability is a LIVE probe per call (~1.5s worst case per hung lane, bounded); it is intentionally NOT cached here.
- `in_flight` is the authoritative per-backend `cloud_job` COUNT (D-02), distinct from the observational global `get_pushing_count`/`get_pushed_count` cards.

## Self-Check: PASSED

- FOUND: src/phaze/services/backends.py (get_backend_lane_snapshot present)
- FOUND: tests/shared/services/test_lane_snapshot.py
- FOUND commit b8fd5cc (test RED), 8a9bb43 (feat Task 1), cdd6aaa (feat Task 2)
