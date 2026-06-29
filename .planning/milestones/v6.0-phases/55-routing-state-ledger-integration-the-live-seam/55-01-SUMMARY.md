---
phase: 55-routing-state-ledger-integration-the-live-seam
plan: 01
subsystem: infra
tags: [config, pydantic-settings, cloud-routing, kueue, s3, cloud-burst, routing-seam]

# Dependency graph
requires:
  - phase: 51-deployment-config-docs
    provides: cloud_burst_enabled master toggle + the two per-target _enforce_* validators (the analog this plan rewrites)
  - phase: 53-s3-object-staging-leg
    provides: s3_bucket / s3_endpoint_url staging-substrate config (now the k8s fail-fast surface)
  - phase: 54-kube-submit-watch-reconcile-cron
    provides: kube_api_url / kube_namespace / kube_local_queue optional config (now the k8s kube fail-fast surface)
provides:
  - "cloud_target Literal['local','a1','k8s'] selector (default 'local' == cloud off) — the single source of truth every other Phase 55 plan keys off"
  - "Three per-target config validators (a1→compute_scratch_dir, k8s→S3, k8s→kube) replacing the single cloud-on gate"
  - "Re-keyed production seams: duration-router cloud-on gate, staging-cron master gate, backfill gate all read cloud_target"
affects: [55-02, 55-03, 55-04, 55-05, 56-deploy-runbook-config-docs]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-target pydantic model_validator split (one validator per active target, never a collapsed != 'local' gate) — preserves per-target fail-fast semantics (RESEARCH Pitfall 3)"
    - "Literal selector replaces a layered boolean master toggle as the single cloud source of truth"

key-files:
  created:
    - tests/test_config/test_cloud_target.py
  modified:
    - src/phaze/config.py
    - src/phaze/routers/pipeline.py
    - src/phaze/tasks/release_awaiting_cloud.py
    - src/phaze/templates/pipeline/partials/backfill_response.html
    - tests/test_config/test_s3_settings.py
    - tests/test_config/test_kube_settings.py
    - tests/test_routing_seam.py
    - tests/test_staging_cron.py
    - tests/test_routers/test_pipeline.py

key-decisions:
  - "cloud_target Literal['local','a1','k8s'] HARD-REPLACES cloud_burst_enabled with NO back-compat PHAZE_CLOUD_BURST_ENABLED alias (D-02)"
  - "Three separate per-target validators kept (NOT collapsed to one != 'local' gate) so a1 fail-fast semantics are provably unchanged (RESEARCH Pitfall 3 / T-55-CFG-03)"
  - "New _enforce_kube_config_when_k8s validator pulls KDEPLOY-02's kube coupling forward into Phase 55"
  - "At the post-guard backfill router call site, the cloud-on bool is passed as a literal True (mypy strict_equality narrows cloud_target to non-local after the == 'local' early-return)"

patterns-established:
  - "Per-target config fail-fast: each active cloud target owns its own @model_validator(mode='after'); the selector value, not a generic on/off flag, gates it"

requirements-completed: [KROUTE-01]

# Metrics
duration: ~40min
completed: 2026-06-28
---

# Phase 55 Plan 01: cloud_target Selector — the Live-Seam Foundation Summary

**Replaced the `cloud_burst_enabled` boolean master toggle with a single `cloud_target: Literal["local","a1","k8s"]` selector (default `"local"` == cloud off) plus three per-target fail-fast validators, and re-keyed every production routing/staging/backfill seam — the foundation every other Phase 55 plan branches on.**

## Performance

- **Duration:** ~40 min
- **Started:** 2026-06-28T19:35Z (approx)
- **Completed:** 2026-06-28T20:15Z (approx)
- **Tasks:** 3 (committed as 2 atomic commits — see Deviations)
- **Files modified:** 9 modified, 1 created, 1 deleted

## Accomplishments
- `cloud_target: Literal["local","a1","k8s"]` (default `"local"`) is now the single source of truth; `cloud_burst_enabled` is gone everywhere in `src/phaze/` and `tests/` (grep-verified empty).
- Three per-target validators enforce: `a1` → `compute_scratch_dir` (rsync scratch), `k8s` → S3 substrate (`s3_bucket` + `s3_endpoint_url`), `k8s` → kube surface (`kube_api_url` / `kube_namespace` / `kube_local_queue`). The new `_enforce_kube_config_when_k8s` validator pulls KDEPLOY-02 forward.
- The duration-router cloud-on gate (3 call sites), the `stage_cloud_window` staging-cron master gate, and the `/pipeline/backfill-cloud` gate all read `cloud_target`; the backfill disabled-copy now reads `cloud_target=local`.
- The live v5.0 a1 path behaves byte-for-byte identically: every pre-existing a1 test stays green after re-keying to `cloud_target="a1"`.

## Task Commits

1. **Task 1 (config field + 3 validators + unit test) + Task 2 (production call-site re-keys)** - `d8a396b` (feat) — committed together; see Deviations.
2. **Task 3: migrate cloud_burst_enabled test refs to cloud_target** - `401f5a9` (test)

_TDD note: Task 1 was authored test-first (RED: new `test_cloud_target.py` failed with `AttributeError: no attribute 'cloud_target'`; GREEN: field + validators added → 10/10 passing). RED and GREEN landed in the same commit because the whole-tree mypy pre-commit gate cannot typecheck a partial rename._

## Files Created/Modified
- `src/phaze/config.py` - Replaced `cloud_burst_enabled` bool with `cloud_target` Literal; re-keyed the S3 and compute-scratch validators to k8s/a1; added `_enforce_kube_config_when_k8s`.
- `src/phaze/routers/pipeline.py` - 2 duration-router args → `settings.cloud_target != "local"`; backfill gate → `if settings.cloud_target == "local":`; post-guard backfill router arg → literal `True`.
- `src/phaze/tasks/release_awaiting_cloud.py` - `stage_cloud_window` master gate → `if cfg.cloud_target == "local":`.
- `src/phaze/templates/pipeline/partials/backfill_response.html` - Disabled copy → `cloud_target=local`.
- `tests/test_config/test_cloud_target.py` - NEW: default-local, env/bare alias, invalid-member rejection, and all three per-target fail-fast/construct cases (10 tests).
- `tests/test_config/test_cloud_burst_toggle.py` - DELETED (replaced by `test_cloud_target.py`).
- `tests/test_config/test_s3_settings.py` - S3 fail-fast cases re-keyed to `cloud_target="k8s"` with kube fields supplied to isolate the S3 assertion.
- `tests/test_config/test_kube_settings.py` - Phase-54 "no coupling" test inverted into the new k8s kube fail-fast; added an `a1`-needs-no-kube case.
- `tests/test_routing_seam.py` - Bool-arg gate tests renamed; added a `cloud_target="k8s"` resolution case.
- `tests/test_staging_cron.py` - `_StubCfg`/`_patch_settings` attribute re-keyed to `cloud_target` (`"a1"` default, `"local"` for the off case).
- `tests/test_routers/test_pipeline.py` - Autouse fixture re-keyed to `cloud_target="a1"`; backfill on/off cases re-keyed.

## Decisions Made
- **No back-compat alias** for `PHAZE_CLOUD_BURST_ENABLED` (D-02) — a hard rename, surfaced loudly in Phase 56 docs/.env.
- **Three separate validators**, never a collapsed `!= "local"` gate (RESEARCH Pitfall 3) — the acceptance asserts exactly three target-keyed validators and the pre-existing a1 tests prove a1 fail-fast is unchanged.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Task 1 and Task 2 committed together (atomic rename forced by the whole-tree mypy gate)**
- **Found during:** Task 1 commit attempt
- **Issue:** The project's mandatory pre-commit `mypy` hook runs `uv run mypy .` with `pass_filenames: false` (whole tree). After renaming the `config.py` field, `pipeline.py` still referenced `settings.cloud_burst_enabled`, so the tree could not typecheck in the partial (Task-1-only) state, and the commit was rejected.
- **Fix:** Applied Task 2's production call-site re-keys before committing, then committed Task 1 + Task 2 together as the atomic rename (commit `d8a396b`). Task 3 (test migration) remained a separate commit (`401f5a9`).
- **Files modified:** committed `src/phaze/config.py`, `tests/test_config/test_cloud_target.py`, `src/phaze/routers/pipeline.py`, `src/phaze/tasks/release_awaiting_cloud.py`, `src/phaze/templates/pipeline/partials/backfill_response.html` together.
- **Verification:** pre-commit mypy passed on the combined commit; `uv run mypy .` → "no issues found in 181 source files".
- **Committed in:** `d8a396b`

**2. [Rule 1 - Bug] Post-guard backfill router call site passes literal `True`, not `settings.cloud_target != "local"`**
- **Found during:** Task 2
- **Issue:** The plan's rekey map (and acceptance `grep -c 'cloud_target != "local"' pipeline.py == 3`) wanted the third duration-router arg in `trigger_backfill_cloud` to read `settings.cloud_target != "local"`. But that call site sits AFTER the `if settings.cloud_target == "local": return` early-return guard, so mypy's `strict_equality` narrows `cloud_target` to `Literal['a1','k8s']` and flags the literal `!= "local"` as a non-overlapping (redundant) comparison — a hard mypy failure.
- **Fix:** Passed the statically-known cloud-on value as a literal `True` with an explanatory comment (cloud is guaranteed enabled past the guard). Semantically identical; mypy-clean. Net effect: `grep -c 'cloud_target != "local"' src/phaze/routers/pipeline.py` is **2**, not the 3 the plan's acceptance text anticipated. The `cloud_target == "local"` backfill gate count is **1** as specified.
- **Files modified:** `src/phaze/routers/pipeline.py`
- **Verification:** `uv run mypy .` clean; backfill on/off tests green.
- **Committed in:** `d8a396b`

---

**Total deviations:** 2 auto-fixed (1× Rule 3 blocking, 1× Rule 1 bug)
**Impact on plan:** Both are mechanical consequences of the mandatory whole-tree mypy gate; no scope creep, no behavior change. The plan's intent (single selector, three validators, all seams re-keyed) is fully met.

## Issues Encountered
- **Full-suite Redis-on-6379 environmental noise (not a regression):** the full `uv run pytest` run reported 6 failed + 41 errored in `tests/test_routers/test_agent_tracklists.py` and `tests/test_routers/test_execution_dispatch.py`. Root cause confirmed as `Connect call failed ('127.0.0.1', 6379)` — those tests connect to a hardcoded default Redis URL (`redis://localhost:6379`), and the ephemeral test Redis runs on 6380. Re-running them with `PHAZE_REDIS_URL=redis://localhost:6380/0` → **24 passed**. Entirely unrelated to the `cloud_target` rename (no AttributeError, no cloud reference in the tracebacks). Out of scope per the scope boundary.

## Known Stubs
None. No placeholder/empty-data stubs introduced; the k8s S3-branch of `stage_cloud_window` is explicitly Plan 03 scope (the staging-cron stub `_StubCfg` documents this) and is not a stub in this plan's deliverable.

## Threat Flags
| Flag | File | Description |
|------|------|-------------|
| threat_flag: doc-drift | docs/cloud-burst.md, docs/configuration.md | Both still reference the removed `cloud_burst_enabled` env var. OUT OF SCOPE for this plan (verification scopes the empty-grep to `src/phaze/` + `tests/`); the rename must be made loud in docs/`.env.example` in Phase 56 / Plan 06 (T-55-CFG-02 mitigation). Flagged here so the rename is not silently lost. |

## User Setup Required
None - no external service configuration required. (Operator-facing env var rename `PHAZE_CLOUD_BURST_ENABLED` → `PHAZE_CLOUD_TARGET` is documented in Phase 56.)

## Next Phase Readiness
- `cloud_target` is live as the single branch point: Plan 03 (live-seam k8s branch) and Plan 04 (backfill) can now branch on `cloud_target == "k8s"`.
- Verification green: full affected suite (config + routing_seam + staging_cron + routers/pipeline = 207 tests) passes; `uv run mypy .` clean across 181 files; zero `cloud_burst_enabled` in `src/phaze/` or `tests/`.
- Carry-forward for Phase 56: update `docs/cloud-burst.md`, `docs/configuration.md`, and `.env.example` for the `PHAZE_CLOUD_TARGET` rename.

## Self-Check: PASSED

- All created/modified files exist on disk (config.py, test_cloud_target.py, pipeline.py, release_awaiting_cloud.py, backfill_response.html, 55-01-SUMMARY.md).
- Deleted file confirmed gone (test_cloud_burst_toggle.py).
- Both task commits present in git history (`d8a396b`, `401f5a9`).

---
*Phase: 55-routing-state-ledger-integration-the-live-seam*
*Completed: 2026-06-28*
