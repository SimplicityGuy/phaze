---
phase: 67-backend-registry-config-model
plan: 05
subsystem: infra
tags: [config, registry, transitional-accessor, structlog, kueue, s3-staging, rsync-push]

# Dependency graph
requires:
  - phase: 67-backend-registry-config-model
    plan: 02
    provides: "ControlSettings transitional accessors (active_cloud_kind/active_compute_scratch_dir), cloud_enabled gate, log_effective_registry projection, backends_toml_env conftest fixture"
provides:
  - "agent_s3.py post-staging seam gates on active_cloud_kind == 'kueue' (was cloud_target == 'k8s')"
  - "agent_push.py scratch_path built from active_compute_scratch_dir (was compute_scratch_dir)"
  - "controller LocalQueue probe gates on active_cloud_kind == 'kueue' with boot-safety try/except preserved"
  - "controller.startup wires settings.log_effective_registry() — boot-time secret-free registry log (REG-04)"
affects: [67-06, 68-backend-protocol]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Class-B legacy dispatch call sites read the registry-derived ≤1-non-local transitional accessors (active_*), each marked '# TRANSITIONAL — Phase 68'"
    - "router/controller tests drive a REAL ControlSettings off a one-backend backends.toml (shared backends_toml_env fixture) so the registry accessor is exercised end-to-end instead of a duck-typed stub"

key-files:
  created: []
  modified:
    - src/phaze/routers/agent_s3.py
    - src/phaze/routers/agent_push.py
    - src/phaze/tasks/controller.py
    - tests/agents/routers/test_agent_s3.py
    - tests/agents/routers/test_agent_push.py
    - tests/shared/tasks/test_controller_startup_localqueue.py

key-decisions:
  - "the flat cloud_target 'a1' target maps to a compute backend (active_cloud_kind == 'compute') in the rewired tests — the non-kueue preservation case is now a compute registry, not a string literal"
  - "router/controller tests build a real ControlSettings via backends_toml_env rather than a SimpleNamespace/MagicMock stub, validating the real accessor derivation (D-08 bucket-ref + compute agent_ref invariants satisfied inline in each registry fixture)"
  - "startup log_effective_registry() call placed immediately after the boot banner (post configure_logging) so the projection renders through the central structlog pipeline"

requirements-completed: [REG-04]

# Metrics
duration: 35min
completed: 2026-07-03
---

# Phase 67 Plan 05: Class-B Call-Site Rewire + Boot Registry Log Summary

**The three remaining Class-B call sites — the S3 post-staging callback (`agent_s3.py`), the rsync push callback (`agent_push.py`), and the controller LocalQueue probe gate (`controller.py`) — now read the Plan-02 transitional registry accessors (`active_cloud_kind` / `active_compute_scratch_dir`) instead of the flat `cloud_target` / `compute_scratch_dir`, and `controller.startup` wires `settings.log_effective_registry()` so the resolved registry (id/kind/rank/cap only) is logged secret-free at boot (REG-04). The `cloud_target` config field is untouched (removed in Plan 06), so the tree stays green.**

## Performance
- **Duration:** ~35 min
- **Tasks:** 2 (both `type="auto"`)
- **Files modified:** 6 (3 src, 3 tests)

## Accomplishments
- `agent_s3.py`: the k8s post-staging seam now fires on `settings.active_cloud_kind == "kueue"` (the rowcount-guarded PUSHING→PUSHED flip + routed `submit_cloud_job` are behavior-preserved); the read is marked `# TRANSITIONAL — Phase 68` and every `cloud_target` docstring mention was scrubbed.
- `agent_push.py`: `scratch_path` is built from `settings.active_compute_scratch_dir`; the read is marked transitional and the `compute_scratch_dir` docstrings updated.
- `controller.py`: the LocalQueue reachability probe gates on `cfg.active_cloud_kind == "kueue"` while keeping its OWN broad try/except boot-safety block intact (D-05 — a kube/Redis blip must never abort boot); the WARNING still names only the env var, never the SA token / kube DSN.
- `controller.startup` calls `cfg.log_effective_registry()` right after the boot banner — the resolved registry is logged once at startup as an `{id, kind, rank, cap}` projection (REG-04, Pitfall 5).
- Tests rewired to drive a real `ControlSettings` off a one-backend registry via the shared `backends_toml_env` fixture: a one-kueue registry for the S3 seam, a one-compute registry (`scratch_dir`) for the push path; a new `test_startup_logs_effective_registry_secret_free` asserts the boot log carries the id/kind/rank/cap projection and that an SA token embedded in the registry never reaches the log.

## Task Commits

| Task | Name | Type | Commit |
| ---- | ---- | ---- | ------ |
| 1 | Rewire S3 callback + push-scratch reads onto transitional accessors | refactor | `8007e0c` |
| 2 | Gate LocalQueue probe on registry + wire boot-time registry log | feat | `b71324a` |

## Files Modified
- `src/phaze/routers/agent_s3.py` — post-staging seam predicate + docstring scrub.
- `src/phaze/routers/agent_push.py` — `scratch_path` accessor + docstring update.
- `src/phaze/tasks/controller.py` — probe-gate predicate, boot-safety preserved, `log_effective_registry()` wired into `startup`, stale `cloud_target` comments scrubbed.
- `tests/agents/routers/test_agent_s3.py` — real ControlSettings via `backends_toml_env` (kueue / compute registries), `kind`-parametrized `_patch_settings`.
- `tests/agents/routers/test_agent_push.py` — real ControlSettings off a one-compute registry, `active_compute_scratch_dir` == `/srv/scratch`.
- `tests/shared/tasks/test_controller_startup_localqueue.py` — `_stub_collaborators`/`_stub_controller` split, probe driven off `active_cloud_kind`, new secret-free registry-log test.

## Deviations from Plan

None — plan executed as written. The tests were driven via the shared `backends_toml_env` fixture with real `ControlSettings` (per the plan action) rather than the pre-existing duck-typed stubs, which is a stronger validation of the real accessor derivation.

## Threat Surface
All four register threats mitigated as planned:
- T-67-05-01 (kube/Redis blip aborting boot) — the probe gate swap changed only the predicate source; its broad try/except boot-safety block is intact (grep-verified `try:` still wraps `get_local_queue()`).
- T-67-05-02 (secret material in the startup log) — `log_effective_registry()` projects id/kind/rank/cap only; the new test embeds an SA token in the registry and asserts it never reaches the log.
- T-67-05-03 (Backend-protocol scope creep) — transitional accessor reads only; no Backend type introduced (Pitfall 1).
- T-67-05-SC (package installs) — zero new dependencies.

No new security-relevant surface beyond the threat model.

## Known Stubs
None.

## Verification
- `uv run pytest tests/agents/routers/test_agent_s3.py` → 12 passed (fresh DB, isolation).
- `uv run pytest tests/agents/routers/test_agent_push.py` → 8 passed (fresh DB, isolation).
- `uv run pytest tests/shared/tasks/test_controller_startup_localqueue.py` → 8 passed (7 existing + 1 new registry-log test).
- `uv run pytest tests/shared/tasks/test_controller_startup_banner.py` → 4 passed (sibling test unaffected by the `log_effective_registry` call + `active_cloud_kind` gate).
- `grep -c cloud_target src/phaze/routers/agent_s3.py src/phaze/tasks/controller.py` → 0 each (code + comments/docstrings); `grep -c "settings.compute_scratch_dir" src/phaze/routers/agent_push.py` → 0.
- `grep -c log_effective_registry src/phaze/tasks/controller.py` → 1 (startup call wired).
- `uv run mypy src/phaze/routers/agent_s3.py src/phaze/routers/agent_push.py src/phaze/tasks/controller.py` → clean.
- `uv run ruff check` on all six files → clean.
- Pre-commit hooks (ruff, ruff-format, bandit, mypy) pass on both task commits — no `--no-verify`.

**Note on cross-file combined runs:** running the three target files together in one `pytest` invocation surfaces the pre-existing colima DB-isolation flake (an errored `create_all` leaves tables undropped, poisoning subsequent tests with a duplicate-`agents`-type error). Each file passes cleanly in isolation against a fresh ephemeral DB (`just test-db`), matching the project's CI-bucket isolation requirement; the flake is infra state, not a logic regression.

## Next Phase Readiness
- Plan 06 (Wave 4) can now remove the flat `cloud_target` + `compute_scratch_dir` (and the other flat cloud fields) — every Class-A/Class-B call site is rewired onto the registry accessors as of this plan.
- No blockers.

## Self-Check: PASSED

- All six modified files present on disk.
- Both task commits (`8007e0c`, `b71324a`) present in git history.

---
*Phase: 67-backend-registry-config-model*
*Completed: 2026-07-03*
