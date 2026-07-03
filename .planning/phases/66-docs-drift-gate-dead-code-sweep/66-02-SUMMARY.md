---
phase: 66-docs-drift-gate-dead-code-sweep
plan: 02
subsystem: ui
tags: [htmx, jinja2, fastapi, saq, admin-ui, feature-flag]

# Dependency graph
requires:
  - phase: 33-saq-ui-mount
    provides: the mounted /saq SAQ monitor sub-app gated by enable_saq_ui
  - phase: 62-polish-cutover
    provides: the shell Agents page that dropped the /saq link (the gap this plan restores)
provides:
  - Discreet flag-gated /saq footer link on the shell Agents/Compute page (CLEAN-01)
  - enable_saq_ui injected into admin_agents.page() template context via get_settings() call-site
  - Render tests proving conditional link visibility + href/target/rel attributes
affects: [saq, admin-ui, agents-page]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Template-context feature-flag gating via get_settings() call-site (respects test lru_cache-clear fixture)"
    - "rel=noopener on target=_blank anchors as a reverse-tabnabbing guard"

key-files:
  created: []
  modified:
    - src/phaze/routers/admin_agents.py
    - src/phaze/templates/admin/agents.html
    - tests/agents/routers/test_admin_agents.py

key-decisions:
  - "Gate the link on enable_saq_ui — the SAME flag that gates the /saq mount — so it never dangles as a 404 (D-09)"
  - "Inject the flag via get_settings() call-site (not a module-level snapshot) so the test lru_cache-clear fixture is respected"
  - "Link lives only in the full page shell, never in the polled /_table partial"

patterns-established:
  - "Feature-flag-gated Jinja partial: {% if enable_saq_ui %} mirrors the backend mount condition"
  - "New-tab anchors carry rel=noopener; asserted by the render test (T-66-05)"

requirements-completed: [CLEAN-01]

# Metrics
duration: 18min
completed: 2026-07-03
---

# Phase 66 Plan 02: Restore /saq shell link Summary

**Discreet flag-gated `/saq` SAQ-monitor footer link restored to the shell Agents page, opening in a new tab with `rel="noopener"`, gated on `enable_saq_ui` so it never dangles as a 404.**

## Performance

- **Duration:** ~18 min
- **Started:** 2026-07-03T16:52Z
- **Completed:** 2026-07-03T17:10Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- Injected `enable_saq_ui` into `admin_agents.page()` template context via the `get_settings()` call-site idiom (mirrors `pipeline_scans.py`), leaving the `/_table` poll partial untouched.
- Added a muted `{% if enable_saq_ui %}`-gated `/saq` footer anchor in `admin/agents.html`, opening in a new tab with `rel="noopener"` (T-66-05 reverse-tabnabbing guard, D-11) using the existing muted palette (D-10).
- Added three render tests: link present-with-attrs when the flag is true, absent when false (no dead 404, D-09), and never leaking into the polled partial.

## Task Commits

Each task was committed atomically:

1. **Task 1: Inject enable_saq_ui and add the flag-gated /saq footer link** - `32a9bad` (feat)
2. **Task 2: Render test for the /saq link in both flag states** - `2e41da3` (test)

## Files Created/Modified
- `src/phaze/routers/admin_agents.py` - Added `from phaze.config import get_settings`; added `"enable_saq_ui": get_settings().enable_saq_ui` to the `page()` context dict only (not `table_partial`).
- `src/phaze/templates/admin/agents.html` - Added a discreet `{% if enable_saq_ui %}` muted footer paragraph with `<a href="/saq" target="_blank" rel="noopener">` after the main content div, before the existing `<script>`.
- `tests/agents/routers/test_admin_agents.py` - Added 3 Phase-66 render tests toggling the flag via env var + `get_settings.cache_clear()`.

## Decisions Made
- None beyond the plan — followed D-09/D-10/D-11 as specified. The flag is read at the call-site (not snapshotted) so the conftest autouse lru_cache-clear fixture and the per-test env toggle are both honored.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Initial `uv run pytest` failed with `OSError: Connect call failed ... 5432` because the tests require the ephemeral integration Postgres/Redis. Resolved by running `just test-db` and exporting `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` (ports 5433/6380) per the justfile `integration-test` recipe. Not a code issue — expected for DB-backed buckets.

## Verification
- `uv run pytest tests/agents/routers/test_admin_agents.py -q` → 17 passed (13 original + 3 new + 1 registration).
- `uv run pytest tests/agents/routers/test_admin_agents.py -k saq -q` → 3 passed.
- `just test-bucket agents` → 394 passed in isolation.
- `uv run ruff check` + `uv run mypy src/phaze/routers/admin_agents.py` → clean.
- `git diff` confirms `table_partial` and `admin/partials/agents_table.html` are unchanged.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- CLEAN-01 delivered. The still-mounted `/saq` monitor is again discoverable from the shell, gated by the same flag as its mount.
- No blockers for remaining Phase 66 plans (docs-drift gate, dead-code sweep).

## Self-Check: PASSED

All modified files exist and all task/summary commits are present in git history.

---
*Phase: 66-docs-drift-gate-dead-code-sweep*
*Completed: 2026-07-03*
