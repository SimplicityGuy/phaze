---
phase: 88-lane-agent-drill-in
plan: 03
subsystem: ui
tags: [htmx, jinja2, fastapi, sqlalchemy, group-by, drill-in, degrade-safe, a11y]

# Dependency graph
requires:
  - phase: 88-lane-agent-drill-in
    plan: 01
    provides: shared non-modal _detail_pane.html shell (#detail-pane innerHTML swap target) + agent-row drill-in trigger (hx-get /admin/agents/{id}/_activity)
  - phase: 82-derived-stage-status
    provides: stage_status_case single derivation + _safe_bucket_counts GroupingError-safe GROUP BY template
  - phase: 29-admin-agents
    provides: admin_agents router + classify liveness idiom + _kind_badge/_status_pill partials
provides:
  - GET /admin/agents/{agent_id}/_activity read-only HTML-fragment endpoint (DRILL-02)
  - _agent_stage_buckets — per-agent GROUP BY stage_status_case aggregate (one-conjunct clone of _safe_bucket_counts, D-04/D-00a)
  - get_agent_lane_depths + get_agent_recent_scans — bounded, degrade-safe per-agent reads (D-05/D-00b)
  - _agent_activity.html — stacked agent-activity body (liveness -> 6-stage COUNT matrix -> per-lane queue depths -> recent scans, own 5s tick)
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-agent stage aggregate: one-conjunct clone of _safe_bucket_counts (inner-subquery materializes stage_status_case(stage) label FIRST, then GROUP BY the scalar label to dodge Postgres GroupingError) + .where(FileRecord.agent_id == agent_id)"
    - "Stage×bucket NUMERAL grid: reuse _stage_pill.html colour tokens per COUNT cell (WORD+GLYPH+aria-label) instead of the single-bucket pill-include; Appr=review / Exec=apply remap"
    - "Wave-2 body fragment innerHTML-swapped into the frozen 88-01 #detail-pane; endpoint returns the BODY directly (shell is a static host, no pane_body slot), body carries its own hx-trigger=every 5s"

key-files:
  created:
    - src/phaze/templates/admin/partials/_agent_activity.html
    - tests/integration/test_agent_stage_buckets.py
    - tests/agents/routers/test_agent_activity.py
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/admin_agents.py

key-decisions:
  - "The endpoint returns the _agent_activity.html BODY fragment directly (not _detail_pane.html with a pane_body slot). The frozen 88-01 shell is a STATIC host with an innerHTML #detail-pane swap target and has NO pane_body mechanism — the trigger's hx-swap='innerHTML' lands the body there. Rendering the shell per-drill would double-nest the region and re-litigate wave-1. This matches the plan's own <interfaces> ('body renders in the shell's body slot') and the 88-01 Known Stubs."
  - "_agent_stage_buckets is a literal one-conjunct clone of _safe_bucket_counts (never a fresh CASE); the agent_id filter is mutation-checked (removing it turns test_agent_id_conjunct_is_load_bearing RED)."

patterns-established:
  - "Agent drill-in twin of the lane drill-in (88-02): the single genuinely-new query is a one-line diff on the DERIV-04-locked stage_status_case aggregate."

requirements-completed: [DRILL-02, DRILL-03]

# Metrics
duration: 40min
completed: 2026-07-11
---

# Phase 88 Plan 03: Agent-Activity Pane Body Summary

**`GET /admin/agents/{agent_id}/_activity` renders a stacked, degrade-safe agent-activity body into the frozen 88-01 `#detail-pane` shell — liveness header → per-agent 6-stage COUNT matrix (one indexed `GROUP BY stage_status_case` per stage scoped to `agent_id`) → per-lane queue depths → recent scan batches, with its own bounded 5s tick.**

## Performance
- **Duration:** ~40 min
- **Tasks:** 2 (Task 1 TDD RED→GREEN)
- **Files:** 5 (3 created, 2 modified)

## Accomplishments
- **Task 1 (TDD):** added `_agent_stage_buckets(session, agent_id, stage)` to `services/pipeline.py` — a verbatim one-conjunct clone of `_safe_bucket_counts` (the GroupingError-safe inner-subquery-then-`GROUP BY`-scalar-label shape) with the single addition of `.where(FileRecord.agent_id == agent_id)` (D-04/D-00a). Reuses the LOCKED `stage_status_case` derivation (never a fresh CASE), degrades to all-zero on any query error. Real-PG integration test (`tests/integration/test_agent_stage_buckets.py`, 5 tests) asserts counts against the derived truth, the sum-to-agent-total invariant, downstream-stage GroupingError-freedom, and the **mutation-checked** `agent_id` conjunct.
- **Task 2:** added the `GET /{agent_id}/_activity` endpoint to `routers/admin_agents.py` returning the new `_agent_activity.html` body fragment (swapped into `#detail-pane` by the 88-01 trigger). Built the D-05 stacked body: liveness header (`classify` transient `_status` + `_kind_badge` + `_status_pill` + last-seen) → the 6-stage COUNT matrix (stage×bucket NUMERAL grid reusing `_stage_pill.html` colour tokens, Appr=review / Exec=apply remap) → per-lane queue depths → recent scan batches, plus the D-03 own 5s tick. Added `get_agent_lane_depths` + `get_agent_recent_scans` (bounded, degrade-safe). Router smoke test (`tests/agents/routers/test_agent_activity.py`, 7 tests, real-PG) covers all sections, the remap, unknown/zero-file empty states, queue-depth degrade, and no-raw-state render.

## Task Commits
1. **Task 1 — RED (failing per-agent aggregate test):** `45125e0d` (test)
2. **Task 1 — GREEN (_agent_stage_buckets):** `72cdb03d` (feat)
3. **Task 2 — endpoint + body + service helpers + test:** `2a16aa34` (feat)

## Files Created/Modified
- `src/phaze/services/pipeline.py` — NEW `_agent_stage_buckets` (D-04 aggregate), `get_agent_lane_depths`, `get_agent_recent_scans`; imports `LANES`
- `src/phaze/routers/admin_agents.py` — NEW `GET /{agent_id}/_activity` endpoint + `_ACTIVITY_STAGES` 6-stage tuple
- `src/phaze/templates/admin/partials/_agent_activity.html` — NEW stacked agent body (D-05) + own 5s tick (D-03)
- `tests/integration/test_agent_stage_buckets.py` — NEW real-PG per-agent GROUP BY test (mutation-checked)
- `tests/agents/routers/test_agent_activity.py` — NEW real-PG endpoint smoke test

## Decisions Made
See `key-decisions` frontmatter. The load-bearing one: the endpoint returns the body fragment directly because the frozen 88-01 shell is a static host with an innerHTML `#detail-pane` swap target and has no `pane_body` slot (see Deviations #1).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Endpoint returns the body fragment directly, not `_detail_pane.html` with a `pane_body` slot**
- **Found during:** Task 2 (wiring the endpoint render).
- **Issue:** The plan's Task 2 action text said "Render `_detail_pane.html` with `pane_body="admin/partials/_agent_activity.html"`". The frozen 88-01 `_detail_pane.html` shell (which I must NOT modify) is a STATIC host: it renders pane chrome + a resting empty state inside an innerHTML `#detail-pane` swap target and has NO `pane_body` include mechanism. The agent-row trigger already carries `hx-target="#detail-pane" hx-swap="innerHTML"`, so the endpoint must return the body fragment ONLY.
- **Fix:** The endpoint renders `admin/partials/_agent_activity.html` directly; the trigger's innerHTML swap lands it in `#detail-pane`. This matches the plan's own `<interfaces>` ("This body renders in the shell's body slot") and the 88-01 SUMMARY Known Stubs ("the wave-2 bodies are swapped into `#detail-pane` by the trigger `hx-get`").
- **Files modified:** `src/phaze/routers/admin_agents.py`
- **Verification:** `test_agent_activity.py` asserts the response is a body fragment (`<html` absent) that innerHTML-targets `#detail-pane`; `test_no_raw_state_render.py` green.
- **Committed in:** `2a16aa34`

**Total deviations:** 1 (blocking reconciliation with the frozen wave-1 contract). No scope change — the delivered DRILL-02 contract is intact.

## Known Stubs
None. All four sections render live data (per-agent aggregate, queue depths, recent scans, liveness). The "queue depths unavailable" / "no recent scan batches" / "owns no files yet" copy are intentional degrade/empty states (D-00b), not stubs — the endpoint always passes a full all-zero lane dict on a degrade, so the "unavailable" fallback only shows on a truly empty dict.

## Known Behavior (inherited from the frozen 88-01 shell)
- The D-03 own-tick re-fetches into `#detail-pane` (per the wave-1 `<interfaces>` contract), so the shell's `#detail-pane` `hx-on::after-swap` re-runs `onLoaded()` (re-parking focus on the pane heading) every 5s. This is a property of the frozen shell's swap wiring, not the body; changing it would require editing wave-1 shell files (out of scope, forbidden). The heading is a non-interactive `tabindex="-1"` target, so focus stays inside the pane region.

## Verification
- `uv run pytest tests/agents/routers/test_agent_activity.py tests/integration/test_agent_stage_buckets.py -x` — **12 passed** (real PG 5433).
- `just test-bucket integration` — **262 passed** (isolation).
- `just test-bucket agents` — **460 passed**, 2 errors in `test_agent_heartbeat.py` that are GREEN in isolation and GREEN when run right after `test_agent_activity.py` (the documented full-suite ordering flake: get_settings lru_cache leak / saq_jobs stub poison, MEMORY) — NOT caused by this plan's files.
- Mutation-check performed: removing `FileRecord.agent_id == agent_id` turns `test_agent_id_conjunct_is_load_bearing` RED (agent A would read 7 metadata-done instead of 2); restored.
- `uv run ruff check` / `ruff format --check` / `uv run mypy` — clean on all changed sources.
- `tests/shared/test_no_raw_state_render.py` — green (no raw `FileRecord.state` render, T-88-10).

## Threat Surface
No new surface beyond the plan's `<threat_model>`. The new endpoint `GET /admin/agents/{id}/_activity` and its reads are exactly T-88-07..T-88-10 (IDOR guard via `session.get` + friendly 404 fragment; bounded/degrade-safe reads; Jinja autoescape; derived-only, never `FileRecord.state`). No `threat_flag` needed.

## Next Phase Readiness
- DRILL-02 is fully wired: clicking an agent row opens the activity pane with the derived per-agent stage matrix, queue depths, recent scans, and liveness; unknown/zero-file agents render friendly empty fragments; the body self-refreshes on its own 5s tick.
- The lane-detail twin (88-02) is the sibling wave-2 body against the same shell; both share the `#detail-pane` innerHTML contract.

---
*Phase: 88-lane-agent-drill-in*
*Completed: 2026-07-11*

## Self-Check: PASSED
- Created files verified on disk: `_agent_activity.html`, `test_agent_stage_buckets.py`, `test_agent_activity.py`, `88-03-SUMMARY.md`
- Task commits verified: `45125e0d` (RED), `72cdb03d` (GREEN), `2a16aa34` (Task 2)
- Verification gate: 12 targeted tests green; integration bucket 262 passed; agents bucket 460 passed (2 known full-suite flakes green in isolation); ruff/format/mypy clean; no-raw-state guard green
