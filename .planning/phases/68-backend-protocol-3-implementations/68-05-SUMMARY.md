---
phase: 68-backend-protocol-3-implementations
plan: 05
subsystem: infra
tags: [backend-protocol, config, transitional-shim-removal, dead-code, phase-acceptance-gate]

# Dependency graph
requires:
  - phase: 68-04
    provides: "Live seams rewired onto backend.dispatch()/is_available()/cap; every active_cloud_kind reader resolves through resolved_non_local_kind() — leaving config.active_cloud_kind/active_cap unreferenced by production code"
  - phase: 68-03
    provides: "resolve_backends() boot guard (the relocated >1-non-local fail-fast) + resolved_non_local_kind() helper"
provides:
  - "config.active_cloud_kind and config.active_cap deleted (D-07: the two Phase-67 transitional dispatch selectors, unreferenced after the 68-04 rewire) — the transitional shim is fully gone (BACK-01 complete)"
  - "config.cloud_enabled retained (registry on/off gate; BEUI-02 structural foundation)"
  - "config._single_non_local + the three config-VALUE accessors (active_kube/active_bucket/active_compute_scratch_dir) retained and re-tagged Phase 68 -> Phase 70 (MKUE-01) — the single-cluster kube_staging/s3_staging/agent_push reads consume them until Phase 70 (D-09)"
  - "Full phase acceptance gate green: full suite (2637) + migration 029 round-trip + D-01 golden snapshot + D-02 invariant (BACK-04)"
affects: [69-scheduler, 70-mkue, phase-71-beui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Transitional-shim removal AFTER every reader is rewired (last wave) — the selectors are deleted only now, so there is never a mid-wave import break"
    - "Retained value accessors keep _single_non_local's raise-on->1-non-local as defense-in-depth alongside the resolve_backends() boot guard"

key-files:
  created: []
  modified:
    - "src/phaze/config.py"
    - "src/phaze/services/backends.py"
    - "tests/shared/config/test_bucket_registry.py"

key-decisions:
  - "D-07: removed ONLY the two dispatch-SELECTOR accessors (active_cloud_kind/active_cap); cloud_enabled stays as the registry on/off gate"
  - "D-09: KEPT the three config-VALUE accessors (active_kube/active_bucket/active_compute_scratch_dir) + _single_non_local — the single-cluster verbatim bodies read them until Phase 70; re-tagged their stale 'removed in Phase 68' docstrings to 'retained through Phase 70 (MKUE-01)'"
  - "Grep gate required rewording two backends.py docstrings + config.py comment blocks that still named the literal selectors (68-04 left historical mentions) — the plan's own 'no active_cloud_kind/active_cap anywhere in src/phaze/' acceptance criterion is only met once the literal token is gone from ALL of src/phaze"
  - "The >1-non-local raise test re-pointed from settings.active_cloud_kind to a retained value accessor (active_compute_scratch_dir), which still reaches _single_non_local's raise — the removed selector could no longer trigger it"

requirements-completed: [BACK-01, BACK-04]

# Metrics
duration: ~22min
completed: 2026-07-03
---

# Phase 68 Plan 05: Remove the Transitional Dispatch Selectors + Phase Acceptance Gate Summary

**The two Phase-67 transitional dispatch selectors (`config.active_cloud_kind` / `config.active_cap`) are deleted now that 68-04 rewired every reader onto the `Backend` protocol — the transitional shim is fully gone (BACK-01) — while `cloud_enabled`, `_single_non_local`, and the three config-VALUE accessors stay and are re-tagged Phase 68 -> Phase 70 (MKUE-01); the full phase acceptance gate is green (2637-test suite + migration 029 round-trip + the D-01 golden snapshot unchanged + the D-02 invariant), proving the removal behavior-preserving (BACK-04).**

## Performance

- **Duration:** ~22 min
- **Completed:** 2026-07-03
- **Tasks:** 2 (Task 1 the removal + re-tag; Task 2 the confirmation-only acceptance gate)
- **Files modified:** 1 source accessor file + 1 source docstring reword + 1 test

## Accomplishments

- **Task 1 — selector removal + value-accessor retention (D-07/D-09):** deleted the `active_cloud_kind`
  (`compute`/`kueue`/None) and `active_cap` (int/None) `@property` selectors from `ControlSettings`.
  KEPT `cloud_enabled` (registry on/off gate, `config.py`), `_single_non_local` (with its
  raise-on-`>1`-non-local left intact as defense-in-depth alongside the `resolve_backends()` boot
  guard), and the three config-VALUE accessors `active_compute_scratch_dir` / `active_kube` /
  `active_bucket` (the single-cluster `agent_push` / `kube_staging` / `s3_staging` reads consume them
  verbatim until Phase 70). Re-tagged those four retained members' docstrings from
  `TRANSITIONAL — removed in Phase 68 (BACK-01)` to `TRANSITIONAL — retained through Phase 70
  (MKUE-01)`, and re-pointed the four surrounding config comment blocks (~L577/606/646/822) so they
  note the two selectors were removed in Phase 68 and the retained accessors live until Phase 70.
  `log_effective_registry`'s `{id,kind,rank,cap}` projection is unchanged (T-68-12 mitigated).
- **Task 2 — full phase acceptance gate (BACK-04):** confirmed the removal is dead-code-only and
  behavior-preserving. The D-01 golden dispatch snapshot stayed green and byte-identical (no dispatch
  behavior touched); the D-02 equivalence invariant holds; migration 029's 028->029->028 round-trip
  (`backend_id` nullable + `s3_key` nullable) is green; the full 2637-test suite passes. Final grep
  audit: zero `active_cloud_kind` / `active_cap` references anywhere in `src/phaze/`.

## Task Commits

1. **Task 1: Remove active_cloud_kind/active_cap selectors; retain + re-tag value accessors (D-07/D-09)** — `f3f75eb` (refactor)

_(Task 2 is a green-gate confirmation with no production change beyond Task 1 — no commit of its own.)_

## Verification

- Grep audit: `grep -rn 'active_cloud_kind\|active_cap' src/phaze/` -> **CLEAN** (both selectors fully removed; no literal token remains in any source docstring/comment).
- `uv run mypy src/phaze/config.py src/phaze/services/backends.py` -> **clean**; pre-commit (ruff/ruff-format/bandit/mypy) all **Passed** on the Task-1 commit.
- `uv run pytest tests/` (full suite, test-DB up) -> **2637 passed, 57 warnings** (warnings are the pre-existing AsyncMock coroutine warnings in unrelated `reenqueue`/`pipeline` mocks, documented in 68-04 — not regressions).
- BACK-04 gate: `tests/analyze/core/test_dispatch_snapshot.py` -> **8 passed** (D-01 golden snapshot unchanged/byte-identical).
- D-02 invariant: `tests/analyze/services/test_backends.py::test_in_flight_equivalence` -> **1 passed**.
- Migration 029 round-trip: `tests/integration/test_migrations/test_migration_029_backend_id.py` -> **3 passed**.
- Config/backends focused sweep (`test_bucket_registry` + dispatch snapshot + staging cron + backends + controller-startup) -> **63 passed**.

## Deviations from Plan

The plan's `files_modified` listed only `src/phaze/config.py`. Two additional edits were mechanically
required to satisfy the plan's own hard acceptance criteria (grep-clean `src/phaze/` + full suite
green). Both are Rule 3 fixes (blocking the acceptance gate); neither changes any dispatch behavior.

### Auto-fixed

**1. [Rule 3 — blocking gate] Reworded two `backends.py` docstrings that still named the literal selectors**
- **Found during:** Task 1 (grep audit)
- **Issue:** 68-04 left two historical docstring mentions of `active_cloud_kind` in
  `src/phaze/services/backends.py` (`Backend` protocol docstring L110 "the `if active_cloud_kind == …`
  fork"; `resolved_non_local_kind` docstring L390 "replacement for the deleted `active_cloud_kind`
  accessor"). The plan's own Task-1 verify (`grep … && exit 1`) and Task-2 acceptance criterion
  ("no `active_cloud_kind`/`active_cap` anywhere in `src/phaze/`") match the whole tree, so those
  comment references would trip the gate even though they are not property usages.
- **Fix:** reworded to `the ``if kind == …`` cloud-target fork` and `the deleted config
  dispatch-selector accessor` — meaning preserved, literal token gone. Also reworded the four
  `config.py` comment blocks that named the selectors/accessors for the same reason.
- **Files modified:** `src/phaze/services/backends.py`, `src/phaze/config.py`
- **Commit:** `f3f75eb`

**2. [Rule 1/3 — test adaptation] Updated `test_bucket_registry.py` assertions that called the removed selectors**
- **Found during:** Task 1
- **Issue:** four assertions in `tests/shared/config/test_bucket_registry.py` exercised
  `settings.active_cloud_kind` / `settings.active_cap` directly on a real `ControlSettings` — they
  would `AttributeError` once the properties were deleted. One of them (`_accessor_raises`) used
  `settings.active_cloud_kind` to trigger the `>1`-non-local `ValueError`.
- **Fix:** dropped the two removed-selector assertions from the compute/kueue/implicit-local accessor
  tests (the retained `active_compute_scratch_dir` / `active_kube` / `active_bucket` / `cloud_enabled`
  assertions still cover the behavior); re-pointed the raise test to `settings.active_compute_scratch_dir`,
  a retained value accessor that still reaches `_single_non_local`'s raise (match=`Phase 69`). No
  behavioral assertion changed.
- **Files modified:** `tests/shared/config/test_bucket_registry.py`
- **Commit:** `f3f75eb`

## Known Stubs

None. This is dead-code removal — no placeholder/empty-value stub introduced.

## Threat Flags

None. No new network endpoint, auth path, or trust-boundary surface. `log_effective_registry`'s
secret-free `{id,kind,rank,cap}` projection is unchanged (T-68-12 mitigated); the `>1`-non-local
fail-fast is preserved (boot-time `resolve_backends()` guard + `_single_non_local`'s retained raise as
defense-in-depth — T-68-13 mitigated); zero package installs (T-68-SC N/A).
