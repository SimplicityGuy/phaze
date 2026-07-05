---
phase: 71-deployment-config-docs-n-lane-ui
plan: 05
subsystem: docs
tags: [documentation, runbook, backends-registry, cloud_target, force-local, _FILE-secrets, hermetic-guard]

# Dependency graph
requires:
  - phase: 67-backend-registry-config-model
    provides: "backends.toml registry (REG-01/04/05) that replaced the flat cloud_target selector — the fact this plan documents"
  - phase: 71 (plans 01/02)
    provides: "N-lane grid (BEUI-01) + force-local route_control toggle (BEUI-02) whose operator behavior the runbook describes"
provides:
  - "docs/runbook.md — operator runbook: force-local incident revert, reading N lanes, spillover, per-backend _FILE secrets"
  - "Reconciled docs/configuration.md — cloud_target stated as REMOVED in Phase 67 + 1:1 cloud_target->backends equivalence"
  - "Hermetic BEUI-03 docs-content guard (tests/shared/core/test_docs_beui03.py)"
affects: [milestone-close, operator-deploy, future-docs-edits]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Hermetic docs-content guard: repo-root Path read, per-behavior asserts, zero phaze.* imports (mirrors test_requirements_traceability.py)"

key-files:
  created:
    - docs/runbook.md
    - tests/shared/core/test_docs_beui03.py
  modified:
    - docs/configuration.md
    - docs/cloud-burst.md
    - docs/k8s-burst.md
    - docs/README.md

key-decisions:
  - "DOCS-ONLY (D-13): no runtime code touched — there is no cloud_target shim to warn about; PHAZE_CLOUD_TARGET is already silently ignored via extra=ignore"
  - "Reconciled the FULL cloud_target contradiction, not just :131/:200-210 — the kube/S3 'Required when cloud_target=k8s' rows and the stale _enforce_*_when_k8s validators section (those validators no longer exist in config.py) were reframed against the registry submodel (_validate_registry)"
  - "1:1 equivalence table placed in the rewritten ### Cloud target section (where an operator reading an old config lands) + cross-referenced from the :131 row"
  - "Secret-value guard uses a shape denylist (PEM header, phaze_agent_ + 12+ token chars, inline password/secret_access_key/sa_token assignments) so a bare field-name reference in prose stays legal (T-71-11)"

patterns-established:
  - "New docs registered in the docs/README.md Operations index; GSD marker on line 1"

requirements-completed: [BEUI-03]

# Metrics
duration: 18min
completed: 2026-07-05
---

# Phase 71 Plan 05: Deployment/Config/Docs (BEUI-03) Summary

**New operator runbook (force-local revert, N-lane reading, spillover, per-backend `_FILE` secrets) plus a reconciled configuration.md that states `cloud_target` was removed in Phase 67 with the trivial 1:1 `cloud_target`→`backends` equivalence — all docs-only, guarded by a hermetic content test.**

## Performance

- **Duration:** ~18 min
- **Completed:** 2026-07-05
- **Tasks:** 2
- **Files modified:** 6 (2 created, 4 modified)

## Accomplishments
- Shipped `docs/runbook.md`: the force-local incident-revert procedure (pill states `CLOUD ROUTING`/`FORCED LOCAL`, durable `route_control` row, gates both drain and duration router, reversible/no-redeploy) **with the A4 held-file note** (already-held `AWAITING_CLOUD` files stay held); reading the N lanes (rank ascending = dispatch preference, `{in_flight}/{cap}`, `offline`, Kueue quota-wait vs Inadmissible); spillover by rank/cap; per-backend `_FILE` secrets referenced by name only.
- Reconciled the internal `cloud_target` contradiction in `docs/configuration.md`: the `:131` table row and the `### Cloud target` section now state it was **REMOVED in Phase 67 (no shim; `PHAZE_CLOUD_TARGET` silently ignored via `extra="ignore"`)** and carry the 1:1 `cloud_target`→`backends` equivalence table.
- Added a hermetic BEUI-03 docs-content guard (no DB, no `phaze.*` imports) locking both deliverables, incl. a T-71-11 secret-value denylist.
- Added unified-backends pointers to `cloud-burst.md`/`k8s-burst.md` without reintroducing the "one shared bucket" framing (superseded by REG-05); registered the runbook in the docs index.

## Task Commits

Each task was committed atomically:

1. **Task 1: Reconcile configuration.md + cloud-burst/k8s-burst pointers** — `a464c37` (docs)
2. **Task 2: New docs/runbook.md + hermetic docs-content guard** — `54464345` (docs)

## Files Created/Modified
- `docs/runbook.md` (created) — operator runbook: force-local revert, N-lane reading, spillover, `_FILE` secrets
- `tests/shared/core/test_docs_beui03.py` (created) — hermetic BEUI-03 docs-content guard (9 assertions)
- `docs/configuration.md` (modified) — `cloud_target` removed-in-Phase-67 statement + 1:1 equivalence; kube/S3 "Required when cloud_target=k8s" rows + stale validators section reframed against the registry
- `docs/cloud-burst.md` (modified) — one superseded pointer to the unified backends model
- `docs/k8s-burst.md` (modified) — one superseded pointer to the unified backends model
- `docs/README.md` (modified) — registered the runbook in the Operations index

## Decisions Made
- **Docs-only (D-13):** no runtime code — confirmed there is no `cloud_target` shim (grep: zero `cloud_target` in `src/phaze/`); `PHAZE_CLOUD_TARGET` is dropped by `extra="ignore"`, so nothing to warn about at startup.
- **Fuller reconciliation than the two named lines:** while executing Task 1 I found the kube/S3 "Required when `cloud_target=k8s`" knob rows and the `### Fail-fast startup validators` section still described a live `cloud_target` gate — and that section named `_enforce_s3_config_when_k8s` / `_enforce_kube_config_when_k8s` / `_enforce_compute_scratch_dir_when_a1`, validators that **no longer exist in config.py** (removed in Phase 67 with the flat knobs). I reframed them against the registry's `_validate_registry` per-backend submodel validation (verified present in the Backend registry section) to fully satisfy the "internal contradiction reconciled" truth. Documented below as a Rule 2 correctness fix.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Reconciled stale `cloud_target=k8s` validator/knob language beyond the two explicitly-named lines**
- **Found during:** Task 1 (configuration.md reconciliation)
- **Issue:** The plan explicitly named the `:131` row, the `:200-210` `### Cloud target` section, and the `:154-156` kube knobs. But the S3 knob rows (`:173-174`), the kube/S3 section intros, and the `### Fail-fast startup validators vs. the non-fatal runtime LocalQueue probe` section still described `PHAZE_CLOUD_TARGET=k8s` as a **live** gate and referenced three per-target validators (`_enforce_*_when_k8s` / `_when_a1`) that were removed in Phase 67. Leaving them would contradict the plan's must-have truth "the internal contradiction in configuration.md is reconciled" and assert validators that no longer exist.
- **Fix:** Reframed the kube/S3 section intros and knob rows against the `[[backends]]`/`[[buckets]]` registry, and rewrote the validators section to reference the live `_validate_registry` per-backend submodel validation (verified present in config.py + the Backend registry doc section) while keeping the still-live runtime LocalQueue/Inadmissible probe distinction.
- **Files modified:** docs/configuration.md
- **Verification:** `grep -rc cloud_target src/phaze/` == 0 (no code touched); BEUI-03 guard + docs-drift + docs-IA guards all green.
- **Committed in:** `a464c37` (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 missing-critical / correctness).
**Impact on plan:** Fully within the docs-only scope; strengthens the reconciliation the plan required. No runtime code touched, no scope creep beyond documentation accuracy.

## Issues Encountered
- `ruff-format` reformatted one multi-line assert in the new guard test on first commit attempt (hook aborted the commit); re-staged and re-committed clean. No logic change.

## Verification

- `uv run pytest tests/shared/core/test_docs_beui03.py -x` → **9 passed** (incl. `-k configuration` subset → 2 passed).
- `uv run pytest tests/shared/core/test_requirements_traceability.py` (docs-drift) → **10 passed** (unaffected).
- `uv run pytest tests/shared/core/test_docs_ia_current.py` → **5 passed** (README index edit safe).
- `grep -c "one shared bucket" docs/cloud-burst.md docs/k8s-burst.md` → **0 / 0** (framing not reintroduced).
- `grep -rc "cloud_target" src/phaze/` → **0** (docs-only; no runtime edits — D-13).
- `grep -ci "held" docs/runbook.md` → **6** (A4 held-file behavior documented).

## Next Phase Readiness
- BEUI-03 complete — the last requirement of Phase 71, the last phase of the 2026.7.1 Multi-Cloud Backends milestone. Ready for milestone close (`/gsd:complete-milestone 2026.7.1`) once the sibling Wave-1 plans (BEUI-01/02) land.
- No blockers. No user setup required (docs-only).

---
*Phase: 71-deployment-config-docs-n-lane-ui*
*Completed: 2026-07-05*
