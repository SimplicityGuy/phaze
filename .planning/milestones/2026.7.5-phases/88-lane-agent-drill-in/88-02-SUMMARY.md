---
phase: 88-lane-agent-drill-in
plan: 02
subsystem: ui
tags: [htmx, jinja2, fastapi, drill-in, degrade-safe, poll-survival, cloud-job]

# Dependency graph
requires:
  - phase: 88-01
    provides: shared non-modal _detail_pane.html shell (#detail-pane innerHTML target) + lane-trigger-{id} hx-get="/pipeline/lanes/{id}"
  - phase: 71-backend-lane-ui
    provides: get_backend_lane_snapshot + _lane_card.html kind tokens the body reuses
provides:
  - GET /pipeline/lanes/{backend_id} read-only HTML-fragment endpoint (kind-adaptive, degrade-safe)
  - _lane_detail.html body slot swapped into #detail-pane (D-06 kueue-only quota/Inadmissible, N=20 completions, own 5s tick)
  - get_lane_recent_completions + get_lane_queue_depths degrade-safe lane-data helpers + LANE_RECENT_N=20
affects: [88-03-agent-activity-body]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Lane-detail body = innerHTML fragment (NOT the shell): the endpoint renders _lane_detail.html directly since the trigger hx-swap='innerHTML' targets #detail-pane inside the frozen shell — rendering the whole shell would nest a duplicate #detail-pane id"
    - "Own-tick self-removal on dismiss: an Alpine armed-guard x-effect removes the 5s tick element when the shell's inherited `open` flips false, so a dismissed pane can never re-open itself (no orphan loop)"
    - "Unknown backend_id returns 200 (not 404) so htmx actually swaps the friendly Lane-offline fragment — htmx does not swap 4xx by default"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/_lane_detail.html
    - tests/analyze/routers/test_lane_detail.py
  modified:
    - src/phaze/services/backends.py
    - src/phaze/routers/pipeline.py

key-decisions:
  - "GET /pipeline/lanes/{unknown} returns 200 + the Lane-offline HTML fragment (not 404): htmx innerHTML swaps only 2xx, so a 404 would leave the pane un-swapped. Still HTML, never JSON/HTTPException (T-88-03)."
  - "The endpoint renders _lane_detail.html DIRECTLY (the innerHTML body), not the 88-01 shell: the frozen shell hosts #detail-pane and the trigger swaps INTO it, so returning the shell would duplicate the #detail-pane id."
  - "get_lane_queue_depths(app_state, backend_id) drops the unused `session` param the plan sketched — a broker-only read; keeping session would trip ruff ARG with no purpose."

requirements-completed: [DRILL-01]

# Metrics
duration: ~40min
completed: 2026-07-11
---

# Phase 88 Plan 02: Lane Detail Body Summary

**`GET /pipeline/lanes/{backend_id}` + `_lane_detail.html` — a read-only, kind-adaptive, degrade-safe lane-detail fragment (kueue-only quota/Inadmissible, N=20 newest-first CloudJob completions, per-lane queue depths, own bounded 5s tick) swapped into the frozen 88-01 `#detail-pane` shell.**

## Performance
- **Duration:** ~40 min
- **Tasks:** 2
- **Files:** 4 (2 created, 2 modified)

## Accomplishments
- Added two bounded, degrade-safe, secret-free lane-data helpers to `services/backends.py`: `get_lane_recent_completions` (N=20 newest-first succeeded `CloudJob` rows for compute/kueue, `[]` for local per Open Question 1, `[]` on any error) and `get_lane_queue_depths` (per-lane-tier `analyze/fingerprint/meta/io` depth, each source degrading to 0), plus the `LANE_RECENT_N = 20` constant (D-07).
- Added `GET /pipeline/lanes/{backend_id}` to `routers/pipeline.py`: resolves `backend_id` by lookup-in-known-set against the degrade-safe snapshot (T-88-03), renders the kind-adaptive body for a found lane, and a friendly "Lane offline" fragment (200 HTML, never 500/JSON) for an unknown/offline id.
- Created `_lane_detail.html`: reuses the `_lane_card.html` kind tokens (glyph/color/`h-1.5` bar), gates quota-waiting + Inadmissible under `{% if lane.kind == 'kueue' %}` with `role="alert"` when `inadmissible > 0` (D-06 — no n/a fillers on local/compute), renders the N=20 completions list or the "No completions in the last 20." empty state (D-07), the per-lane queue depths, and the D-03 own-tick with a self-removal guard so a dismissed pane never re-opens.
- Wrote a 10-test DRILL-01 module (`tests/analyze/routers/test_lane_detail.py`): helper bound/order/degrade + endpoint known-lane / unknown-offline / local-empty-state / autoescape + template kueue-vs-non-kueue kind-adaptivity. All green; `ruff`/`ruff format`/`mypy` clean project-wide.

## Task Commits
1. **Task 1 (RED): lane-detail helper tests** — `8233b0d8` (test)
2. **Task 1 (GREEN): degrade-safe lane-data helpers** — `aaf94648` (feat)
3. **Task 2: endpoint + `_lane_detail.html` body** — `ef6782f0` (feat)

## Files Created/Modified
- `src/phaze/services/backends.py` — `LANE_RECENT_N`, `get_lane_recent_completions`, `get_lane_queue_depths` (co-located with `get_backend_lane_snapshot`); added `LANES` to the existing `enqueue_router` import.
- `src/phaze/routers/pipeline.py` — `GET /pipeline/lanes/{backend_id}` (`lane_detail`) + the three new backends imports.
- `src/phaze/templates/pipeline/partials/_lane_detail.html` — NEW lane-detail body slot (kind-adaptive, capacity bar, kueue admission alert, queue depths, N=20 completions, offline empty state, own 5s tick).
- `tests/analyze/routers/test_lane_detail.py` — NEW DRILL-01 test module (10 tests).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Correctness] Unknown backend_id returns 200 (not the plan's optional 404)**
- **Found during:** Task 2 (endpoint offline branch)
- **Issue:** The plan offered `status_code=404` for the offline fragment. htmx (2.0.10) does NOT swap 4xx responses by default, so a 404 would leave `#detail-pane` un-swapped — the operator would click an offline lane and see nothing render.
- **Fix:** Return 200 with the "Lane offline" HTML fragment (still HTML, never JSON/HTTPException — T-88-03 preserved). The friendly-empty-fragment discipline (record.py precedent) is kept; only the status code differs so htmx renders it.
- **Files:** `src/phaze/routers/pipeline.py`, `src/phaze/templates/pipeline/partials/_lane_detail.html`
- **Committed in:** `ef6782f0`

**2. [Rule 2 - Correctness] Own-tick self-removes on dismiss (the frozen shell does not clear the pane)**
- **Found during:** Task 2 (D-03 own-tick)
- **Issue:** D-03 requires the 5s tick be "removed with the pane on dismiss (no orphan loop)", but the frozen 88-01 shell's `hide()` only sets `open=false` — it does not clear `#detail-pane`. A surviving tick would keep polling and, worse, re-swap the body every 5s whose `hx-on::after-swap` re-fires `onLoaded()` and re-opens the pane the operator just closed.
- **Fix:** The tick element (inside the shell's inherited Alpine scope) carries `x-data="{armed:false}"` + `x-init` (`$nextTick` → armed) + `x-effect="if (armed && !open && window.htmx) htmx.remove($el)"`. `armed` skips the pre-open initTree pass; once opened, a later `open→false` (✕/Esc) removes the tick element so polling stops and the pane cannot self-re-open.
- **Files:** `src/phaze/templates/pipeline/partials/_lane_detail.html`
- **Committed in:** `ef6782f0`

**3. [Rule 3 - Blocking] `get_lane_queue_depths(app_state, backend_id)` drops the sketched unused `session` param**
- **Found during:** Task 1 (queue-depth helper)
- **Issue:** The plan sketched `get_lane_queue_depths(session, app_state, backend_id)`, but the queue-depth read is broker-only (SAQ `Queue.count`) and never touches the DB session — an unused param trips ruff `ARG`.
- **Fix:** Signature is `get_lane_queue_depths(app_state, backend_id)` (the plan granted "or reuse the get_queue_activity idiom" discretion). No behavior change.
- **Files:** `src/phaze/services/backends.py`
- **Committed in:** `aaf94648`

**Total deviations:** 3 auto-fixed (2 correctness, 1 blocking). No scope creep — the DRILL-01/D-06/D-07/D-00b/D-03 contract is intact.

## Known Stubs
None. Every value is a real, bounded, degrade-safe read; the lane detail is fully wired to `get_backend_lane_snapshot` + `CloudJob` + the SAQ per-lane queues.

## Threat Flags
None. The endpoint is read-only over the pre-existing secret-free snapshot + CloudJob status/timestamp scalars; no new network endpoint beyond the planned `GET /pipeline/lanes/{id}`, no new auth/file/schema surface. All operator-declared ids/kinds stay Jinja-autoescaped (T-88-05).

## Issues Encountered
- **Local full-suite colima flake (documented):** `just test-bucket analyze` reported 354 passed + 220 setup ERRORs under xdist VM pressure. Every erroring file passes in isolation and in a 29-test group without full-bucket concurrency — this is the known DB-connection-pressure flake, not a regression. The new DRILL-01 module (10) and the wave-1 survival module (5) + the `test_no_raw_state_render` guard all pass green in isolation.

## User Setup Required
None.

## Next Phase Readiness
- 88-03 (agent-activity body) implements `GET /admin/agents/{agent_id}/_activity` + `_agent_activity.html` against the same `#detail-pane` shell using the identical fragment-into-shell + own-tick-self-removal patterns established here.

---
*Phase: 88-lane-agent-drill-in*
*Completed: 2026-07-11*

## Self-Check: PASSED
- Created files verified on disk: `_lane_detail.html`, `test_lane_detail.py`, `88-02-SUMMARY.md`
- Modified files verified: `backends.py` (LANE_RECENT_N + 2 helpers), `pipeline.py` (lane_detail endpoint)
- Task commits verified: `8233b0d8` (RED test), `aaf94648` (GREEN helpers), `ef6782f0` (endpoint + body)
- Verification gate: 10/10 DRILL-01 tests green; guard + survival modules green; `ruff check .` / `ruff format --check .` / `mypy .` clean project-wide
