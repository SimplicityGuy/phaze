---
phase: 58-enrich-analyze-workspaces
plan: 04
subsystem: ui
tags: [htmx, alpine, jinja2, analyze, lane-cards, cloud-cards, windowed-progress, oob-seed, pytest]

# Dependency graph
requires:
  - phase: 58-01
    provides: "single persistent /pipeline/stats chrome poll + visibilitychange shed; Phase-58 test file + _seed_file/_seed_analysis/_seed_cloud_job helpers + the two xfail analyze stubs"
  - phase: 58-02
    provides: "shared workspace partials (_workspace_scaffold.html macro, generic _file_table.html, _workspace_poll_seeds.html OOB seed host); derived-int-on-dag-dict + pre-mounted dag-seed-<key> pattern"
  - phase: 58-03
    provides: "sibling-workspace shape (scaffold + file-table) + STAGE_PARTIALS metadata/fingerprint precedent"
  - phase: 57.1
    provides: "read-only mid-flight windowed signal (analysis.fine_windows_analyzed/total increments during flight) — Phase 58 only READS it (D-04/PROG-03)"
  - phase: 57-shell-dag-rail
    provides: "#stage-workspace swap target, STAGE_PARTIALS whitelist, fragment-only /s/<stage>, $store.pipeline, dead-template AST guard"
provides:
  - "Analyze workspace (analyze_workspace.html): 3 always-render lane cards (local/A1/k8s) + the six existing v6.0 cloud cards reused verbatim + the all-in-stage per-file lane/window table"
  - "_lane_card.html: reusable always-render lane summary card; greys + labels a down lane offline vs not-configured with 0 capacity (D-05)"
  - "get_analyze_stage_files (services/pipeline.py): one degrade-safe multi-state read-only SELECT (LEFT JOIN cloud_job + analysis + metadata) with per-file lane derivation + window coverage"
  - "computeOnline: a read-only kind=compute online-agent count on the dag dict + base.html store + the dag-seed-computeOnline OOB placeholder (B1)"
  - "count_active_agents gains a read-only kind= filter (mirrors select_active_agent)"
affects: [59, 60, 61]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Composite workspace: scaffold + an include-driven reusable card partial (_lane_card.html) rendered N times by re-setting its context vars before each {% include %}"
    - "Reuse v6.0 OOB card partials VERBATIM inside a new workspace (oob unset -> quiet carrier on first render; stats_bar.html re-pushes them oob=True on the 5s poll into the same ids)"
    - "Per-file lane = DERIVED from the cloud_job sidecar (no row->local / cloud_phase NULL->a1 / set->k8s); read-only, no cloud_target file column"
    - "Mid-flight windowed progress reads the 57.1 analysis aggregate (fine_windows_analyzed/total) — in-flight rows render running + N/M, completed rows full coverage"

key-files:
  created:
    - "src/phaze/templates/pipeline/partials/_lane_card.html"
    - "src/phaze/templates/pipeline/partials/analyze_workspace.html"
  modified:
    - "src/phaze/services/pipeline.py"
    - "src/phaze/routers/pipeline.py"
    - "src/phaze/routers/shell.py"
    - "src/phaze/templates/base.html"
    - "src/phaze/templates/pipeline/partials/_workspace_poll_seeds.html"
    - "tests/test_enrich_analyze_workspaces.py"
    - "tests/test_shell_routes.py"

key-decisions:
  - "Per-file lane derivation is SOUND (RESEARCH A1 confirmed): cloud_job rows are created ONLY in cloud_staging.stage_file_to_s3 (the single writer, reached only on a cloud route). A local-routed file never enters that path, so it never carries a cloud_job row and cannot be mislabeled as a1/k8s."
  - "computeOnline added by EXTENDING count_active_agents with an optional kind= filter (mirrors enqueue_router.select_active_agent's kind seam) rather than inventing a second liveness rule; kind=None preserves every existing caller."
  - "Lane state (online/offline/not-configured) is computed render-time from cloud_target + dag.agentOnline/computeOnline + localqueue_unreachable; the capacity NUMERAL stays reactive via $store.pipeline (local/A1) while k8s uses the render-time admission counts (NOT store keys — Pitfall 2)."
  - "The six v6.0 cloud cards are placed VERBATIM below the lane grid (not restyled) so the quota-wait-vs-Inadmissible role=alert distinction is preserved by reuse, and they keep riding the existing /pipeline/stats OOB fanout."
  - "STAGE_PARTIALS['analyze'] flipped to analyze_workspace.html as a static literal (T-57-01); dag_canvas.html stays reachable via legacy dashboard.html (supersede-in-place, CUT-02/Phase 62 owns deletion) so the dead-template guard stays green."

patterns-established:
  - "Composite-workspace + include-driven repeated card partial (re-set vars per include)"
  - "Read-only multi-state file read with per-file lane derivation for a stage table"

requirements-completed: [WORK-03, WORK-04, WORK-05]

# Metrics
duration: ~8min
completed: 2026-06-30
---

# Phase 58 Plan 04: Analyze workspace (lane cards + per-file lane/window table) Summary

**Replaced the Phase-57 bridged Analyze placeholder (dag_canvas.html) with the real Analyze workspace — three always-present execution-lane cards (local / A1 / k8s) with offline-vs-not-configured labels and the reused v6.0 quota-wait-vs-Inadmissible cloud cards, plus ONE table of every in-stage file carrying a derived lane badge and the live 57.1 mid-flight windowed-progress signal — all riding the single chrome poll with zero backend behavior change (one read-only multi-state SELECT + one derived seed).**

## Performance
- **Duration:** ~8 min
- **Completed:** 2026-06-30
- **Tasks:** 3
- **Files:** 9 (2 created, 7 modified)

## Accomplishments
- **Task 1 — read-only data + seed plumbing:** added `get_analyze_stage_files` (one degrade-safe multi-state SELECT over FileRecord LEFT JOIN cloud_job + analysis + metadata; per-file lane derived, window coverage read, SAVEPOINT → `[]` on error so the hot poll never 500s); extended `count_active_agents` with a read-only `kind=` filter; seeded `computeOnline` (kind=compute online count) onto the dag dict; returned `analyze_files` + `cloud_target` from `build_dashboard_context`; added `computeOnline: 0` to the base.html store and the `dag-seed-computeOnline` OOB placeholder (B1 — an OOB seed lands only on a pre-existing id).
- **Task 2 — lane cards + cloud cards (WORK-03):** created `_lane_card.html` (always-render, greys + labels a down lane `offline` vs `not configured` with 0 capacity, D-05; word+glyph never hue-only) and `analyze_workspace.html` (scaffold + a `grid grid-cols-3` lane grid + the six v6.0 cloud cards included VERBATIM below, preserving the role=alert fault distinction); flipped `STAGE_PARTIALS["analyze"]` to the new workspace; converted `test_lane_cards_states` and updated the Phase-57 `test_root_renders_shell_analyze_default` to assert `#analyze-lanes`.
- **Task 3 — per-file lane + window table (WORK-04):** added the all-in-stage `_file_table` (File · Duration · Lane · State) with per-file lane badges (🖥️ local / ☁️ A1 / ⎈ k8s), completed rows showing `window {a}/{total}` full coverage and in-flight rows showing `running` **plus** the merged 57.1 mid-flight `N/M windows` signal (not a bare `running`, D-04/B2); rows inert-but-present (D-06); converted `test_analyze_file_table_lane_and_windows`; ran `just tailwind` to compile the new utility classes into the gitignored `app.css`.

## Task Commits
1. **Task 1: read-only Analyze stage query + computeOnline lane seed** — `69a8839` (feat)
2. **Task 2: 3-lane grid + reused cloud cards (WORK-03)** — `962dd2e` (feat)
3. **Task 3: all-in-stage per-file lane + window table (WORK-04)** — `8cd3b82` (feat)

## Cloud_job Lifecycle Confirmation (RESEARCH Assumption A1)
The per-file lane derivation (no `cloud_job` → local / `cloud_phase IS NULL` → a1 / `cloud_phase` set → k8s) cannot mislabel a local file: `cloud_job` rows are created in EXACTLY ONE place — `services/cloud_staging.stage_file_to_s3` — which is reached only on a cloud route (after the duration router holds a file `AWAITING_CLOUD` and the staging cron stages it). A local-routed file never enters that path, so it never gets a `cloud_job` row and always derives to `local`. Confirmed by `grep -rn "CloudJob(" / "pg_insert(CloudJob)"` across `src/phaze/`.

## Deviations from Plan

### Adjusted Steps

**1. [Rule 3 - Blocking] Updated a Phase-57 bridge test that asserted superseded content**
- **Found during:** Task 2 (wiring `STAGE_PARTIALS["analyze"]`).
- **Issue:** `tests/test_shell_routes.py::test_root_renders_shell_analyze_default` asserted `id="pipeline-dag"` (the bridged dag_canvas content) on `GET /`. Flipping the analyze partial to `analyze_workspace.html` makes that content absent on the shell root (dag_canvas now lives only on the legacy `/pipeline/` dashboard), so the assertion broke.
- **Resolution:** Updated the assertion to `id="analyze-lanes"` (the new lane-card grid host) + refreshed the comment. This is the expected supersede-in-place consequence; the dead-template guard stays green because dag_canvas remains reachable via `dashboard.html`. Files: `tests/test_shell_routes.py`. Commit: `962dd2e`.

## Known Stubs
- **Analyze action buttons `ROUTE RULES` + `PAUSE`** (analyze_workspace.html) render as secondary buttons per the UI-SPEC Copywriting contract but are **not wired** in Phase 58. This is intentional and non-blocking: the live per-stage pause/priority controls already exist and remain functional on the DAG canvas; the workspace action row matches the prototype and will be wired in a later v7.0 phase. WORK-03/04/05 do not require these controls; the lane cards, cloud cards, and per-file table all deliver live data.

## Threat Flags
None — no new network endpoint, auth path, file-access pattern, or schema surface. `get_analyze_stage_files` is a read-only SELECT (T-58-DEGRADE mitigated by the SAVEPOINT/never-500 degrade); all interpolated values are server-computed ints or autoescaped strings, no `| safe` (T-58-XSS); `STAGE_PARTIALS['analyze']` is a static literal (T-57-01); the role=alert fault distinction is preserved by verbatim reuse (T-58-ALERT); the computeOnline OOB seed has a pre-mounted target (T-58-SEED).

## Authentication Gates
None.

## Issues Encountered
- Reused the running ephemeral test DB/Redis (ports 5433/6380); exported `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` accordingly. Test-environment setup only — no code impact.

## Verification
- Targeted: `test_lane_cards_states` + `test_analyze_file_table_lane_and_windows` + the rest of `test_enrich_analyze_workspaces.py` + `test_shell_routes.py` + `test_services/test_pipeline.py` + `test_pipeline_dag_context.py` + `test_dead_template_guard.py`: **111 passed**.
- Full suite + coverage: **2559 passed, 97.16% coverage** (≥85% gate met).
- `ruff check .` clean; `mypy .` (184 files) clean.
- `just tailwind`: `app.css` regenerated; `grid-cols-3`, `h-1.5`, `border-amber-500` confirmed compiled; `app.css` is gitignored and was NOT committed.

## Next Phase Readiness
- Phase 58 is COMPLETE — all four Enrich + Analyze workspaces (Discover/Metadata/Fingerprint/Analyze) now render as real fragments inside the v7.0 shell, live via the single chrome poll, with no backend behavior change.
- The composite-workspace pattern (scaffold + include-driven reusable card + reused OOB cards + file table) and the read-only multi-state file read are available for Phase 59 (Identify workspaces).
- The inert file rows (D-06) carry stable ids ready for Phase 61's row→record slide-in wiring.

## Self-Check: PASSED
- Both created partials (`_lane_card.html`, `analyze_workspace.html`) exist on disk; commits `69a8839`, `962dd2e`, `8cd3b82` present in `git log`.

---
*Phase: 58-enrich-analyze-workspaces*
*Completed: 2026-06-30*
