---
phase: 70-multi-kueue-n-clusters
plan: 04
subsystem: infra
tags: [multi-kueue, failure-isolation, drain, stage-cloud-window, mkue-03, d-07, pitfall-8]

# Dependency graph
requires:
  - phase: 70-multi-kueue-n-clusters
    plan: 03
    provides: "N concurrently-dispatched KueueBackend impls each resolving its own KubeConfig (the flaky-cluster surface this plan isolates)"
  - phase: 69-tiered-drain-scheduler
    provides: "the once-per-tick per-backend snapshot loop + pure select_backend policy the D-07 guards wrap"
  - phase: 68-backend-protocol-3-implementations
    provides: "the Backend protocol + the 'is_available never raises' no-op discipline this plan extends to the N-cluster snapshot"
provides:
  - "per-backend try/except around the once-per-tick is_available/in_flight_count snapshot: a raising/timing-out cluster -> available=False, remaining=0, logged (backend_id only); healthy backends + local still get work (D-07/MKUE-03)"
  - "a widened dispatch guard: a generic kube/S3 raise is a clean per-candidate hold (skipped, continue), distinct from the NoActiveAgentError fileserver-vanish break-all-remaining semantics (D-07)"
  - "an N>=2-backend isolation test suite proving one flaky cluster cannot poison the whole drain tick (snapshot + dispatch code paths)"
affects: [70-05, multi-kueue, drain, stage-cloud-window]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Defense-in-depth 'never-raise-out-of-the-tick': even though is_available/in_flight_count are supposed to swallow their own failures (Phase 68), the once-per-tick snapshot ALSO wraps each backend so an escaped raise degrades that backend to 0 slots instead of aborting the tick"
    - "Two-tier dispatch exception handling: NoActiveAgentError (fileserver-wide) holds ALL remaining + breaks; a generic Exception (one backend's cluster/bucket error) holds THIS candidate + continues -- the branch ORDER (specific before generic) is load-bearing"

key-files:
  created:
    - tests/analyze/tasks/test_release_awaiting_cloud.py
  modified:
    - src/phaze/tasks/release_awaiting_cloud.py

key-decisions:
  - "The snapshot guard sets available=False AND remaining=0 on a raise from EITHER probe leg (is_available OR in_flight_count) -- a backend whose in_flight_count raises is treated as fully unavailable for the tick, not merely capped, so the limit-gate excludes it entirely (matches D-07's '0 slots' wording)."
  - "The generic dispatch except branch does NOT decrement snapshot[target.id]['remaining'] (no slot was claimed) and does NOT commit -- the single post-loop commit + tick-wide advisory lock (5_000_504) stay the atomic boundary (Landmine L1 / SCHED-02 unchanged)."
  - "Logging is backend_id-only with NO exc_info (T-70-03-02): a KubeConfig/SecretStr/exception payload could carry creds, so the isolation log deliberately projects id only."
  - "Tests inject duck-typed stub backends by patching phaze.services.backends.resolve_backends (the drain's deferred-import seam), keeping the real pure select_backend policy in the loop -- the stubs are non-LocalBackend so the policy routes them as cloud backends (the Kueue role)."

patterns-established:
  - "stage_cloud_window is now hardened at BOTH the once-per-tick snapshot AND the per-candidate dispatch against a single flaky backend; the reconcile path was already per-row guarded from Phase 69, so the drain-tick failure-isolation story is complete for N clusters"

requirements-completed: [MKUE-03]

# Metrics
duration: 9min
completed: 2026-07-04
---

# Phase 70 Plan 04: Per-Cluster Failure Isolation in the Drain Tick Summary

**One flaky Kueue cluster can no longer poison the whole `*/5` drain tick: each backend's once-per-tick `is_available()`/`in_flight_count()` snapshot is wrapped in its own try/except (a raise/timeout -> 0 slots, logged by `backend_id` only), and the dispatch guard is widened so a generic kube/S3 raise is a clean per-candidate hold (skipped, continue) — distinct from the preserved `NoActiveAgentError` fileserver-vanish break-all-remaining. Every healthy cluster and local still receive work in the same tick, and the tick never aborts or raises (MKUE-03 / D-07, research Pitfall 8).**

## Performance

- **Duration:** ~9 min
- **Tasks:** 1 (tdd)
- **Files:** 1 source modified, 1 test file created

## Accomplishments

- **Task 1 (D-07, MKUE-03 / research Pitfall 8):** hardened `stage_cloud_window` at two seams:
  - **Snapshot loop:** each backend's `is_available(session)` + `in_flight_count(session)` are now wrapped in a per-backend `try/except Exception`. On a raise/timeout that backend's slot becomes `available=False, remaining=0`, is logged at `warning` with `backend_id=backend.id` (id only, T-70-03-02), and the loop `continue`s. The surrounding `limit = sum(remaining over available backends)` gate is unchanged — a 0-slot flaky backend simply contributes nothing, and every other backend proceeds normally.
  - **Dispatch guard:** the existing `except NoActiveAgentError` (hold ALL remaining candidates + `break` — the fileserver-vanish/WR-02 semantics) is retained, and a distinct `except Exception` branch is ADDED after it. A generic kube/S3 raise is now a clean hold of THIS candidate only: counted `skipped`, logged (`backend_id`), and `continue` to the next candidate. The raising path mutates no state (dispatch resolves the fileserver before any mutation), the slot is NOT decremented (no work claimed), and there is NO mid-loop commit — the advisory-lock scope + single post-loop commit are untouched (Landmine L1 / SCHED-02).
- **Isolation test suite (`tests/analyze/tasks/test_release_awaiting_cloud.py`, new):** four N-backend tests over duck-typed stub backends (injected via the `resolve_backends` seam; the real `select_backend` policy stays in the loop):
  1. `is_available` raises on backend A → tick survives, A gets 0 slots, both candidates route to healthy backend B.
  2. `in_flight_count` raises on backend A → A treated as remaining=0, B absorbs every candidate.
  3. a generic `dispatch` raise → clean per-candidate hold (`{"staged": 0, "skipped": 2}`), `dispatch` invoked TWICE (loop continued), files stay AWAITING_CLOUD with no `cloud_job`.
  4. a `NoActiveAgentError` from `dispatch` → holds ALL remaining + breaks (`dispatch` invoked ONCE), preserved semantics.

## Task Commits

1. **Task 1 (RED):** `14077b7` (test — the 4 isolation tests; verified 3 failing / 1 passing pre-impl, the 1 pass being the already-handled NoActiveAgentError preservation case)
2. **Task 1 (GREEN):** `e1392d4` (feat — the snapshot per-backend try/except + the widened dispatch guard)

_TDD note: the isolation tests landed RED first (`14077b7`) — is_available raise, in_flight_count raise, and generic-dispatch raise all propagated out of the un-guarded tick — then GREEN in the source commit (`e1392d4`)._

## Files Created/Modified

**Source:**
- `src/phaze/tasks/release_awaiting_cloud.py` — the once-per-tick snapshot loop now wraps each backend's `is_available`/`in_flight_count` in a per-backend `try/except` (raise → `available=False, remaining=0` + `backend_id`-only log + `continue`); the candidate loop's dispatch guard gains a generic `except Exception` branch (clean per-candidate hold: `skipped += 1`, log, `continue`) after the retained `except NoActiveAgentError` (hold-all + `break`).

**Tests:**
- `tests/analyze/tasks/test_release_awaiting_cloud.py` (new) — `_StubBackend` (controllable `raise_on` per lifecycle call) + `_IsoCfg` + `_patch_backends` (patches `phaze.services.backends.resolve_backends` + the drain's `get_settings`); 4 `stage_cloud_window`-`isolation` tests covering both snapshot legs, the generic-dispatch hold-and-continue, and the preserved NoActiveAgentError hold-all-and-break.

## Deviations from Plan

None — plan executed exactly as written. The plan named the test file `tests/analyze/tasks/test_release_awaiting_cloud.py` (it did not previously exist; the prior `stage_cloud_window` coverage lives in `tests/analyze/core/test_staging_cron.py`), so it was created new as specified.

## Authentication Gates

None.

## Known Stubs

None — the change is a pure hardening of the existing drain tick; no placeholder/empty-data paths introduced. (`_StubBackend` is a test double, not a runtime stub.)

## Threat Flags

None — no new network endpoints, auth paths, or trust-boundary schema beyond the plan's `<threat_model>`. The register mitigations are honored: T-70-03 (one flaky cluster poisoning the tick) is the exact behavior this plan mitigates via the per-backend snapshot + dispatch try/except; T-70-03-02 (info disclosure) is honored by logging `backend_id` ONLY with no `exc_info`/payload; T-70-03-03 (a mid-loop commit re-opening the over-stage window) is honored — the generic hold branch `continue`s with no commit, and the advisory-lock scope + single post-loop commit are unchanged.

## Verification

- `uv run pytest tests/analyze/tasks/test_release_awaiting_cloud.py` → **4 passed**.
- `uv run pytest tests/analyze/tasks/ -k "stage_cloud_window and isolation"` → **4 passed** (acceptance grep).
- `uv run pytest tests/analyze/core/test_staging_cron.py tests/analyze/core/test_dispatch_snapshot.py` → **30 passed** (the drain-tick regression suite — behavior unchanged for the healthy path).
- `uv run pytest tests/analyze tests/agents tests/discovery` → **988 passed**, 56 setup ERRORS in DB-heavy `tests/agents/*` under colima VM pressure (the documented full-suite connection-flake). Re-running the erroring subset in isolation (`tests/agents/routers/test_agent_s3.py tests/agents/services/test_agent_bootstrap.py`) → **19 passed** — confirmed infra flake, not a regression.
- `uv run ruff check .` → All checks passed. `uv run mypy .` (project-wide via the pre-commit hook on both commits) → Passed. Every commit ran the full pre-commit suite (ruff, ruff-format, bandit, mypy) with no `--no-verify`.

_Test DB: `localhost:5433` (`TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test`)._

## Self-Check: PASSED

- `70-04-SUMMARY.md` exists on disk.
- `tests/analyze/tasks/test_release_awaiting_cloud.py` exists on disk.
- `src/phaze/tasks/release_awaiting_cloud.py` carries both guards (verified by the passing acceptance tests).
- Commits `14077b7` (test-RED) and `e1392d4` (feat-GREEN) present in git history.

---
*Phase: 70-multi-kueue-n-clusters*
*Completed: 2026-07-04*
