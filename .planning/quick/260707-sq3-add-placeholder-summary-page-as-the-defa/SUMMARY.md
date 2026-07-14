---
task: 260707-sq3
title: Add placeholder Summary page as the default landing route
type: quick
status: complete
requirements: [SQ3-01, SQ3-02, SQ3-03]
base_commit: 5722cfe290ee9b6dad22bb6320d3b93a29ace78b
commits:
  - 714c8382 feat(260707-sq3): add Summary placeholder partial and repoint GET / to it
  - 192a5fc9 feat(260707-sq3): add the Summary rail node as the landing entry
  - 8bb2c735 test(260707-sq3): reconcile Analyze-default tests, cover Summary landing
---

# Summary — 260707-sq3

## Objective

Repoint the app's default landing route (`GET /`) from the Analyze stage to a NEW static
"Summary" placeholder stage, add a Summary rail node as the landing entry, and keep Analyze
fully reachable at `/s/analyze`. Scaffolding only — the Summary page's real content is deferred
to a future spec (no new DB queries, no metrics widgets, no store bindings).

## What was done

### Task 1 — Summary placeholder partial + repointed `GET /` (commit 714c8382)
- Created `src/phaze/templates/shell/partials/summary_placeholder.html`: a content-only fragment
  (no `<html>`/`<head>`/`{% extends %}`) that **composes the shared `_workspace_scaffold.html`**
  via the `metadata_workspace.html` idiom (`{% import ... as ws %}` + `{% call ws.workspace(title="SUMMARY") %}`).
  This is the load-bearing move: the scaffold transitively includes `_workspace_poll_seeds.html`,
  so the hidden OOB seed-target host rides onto the landing page and the chrome `/pipeline/stats`
  5s poll finds a target for every OOB fragment it emits (no `htmx:oobErrorNoTarget` spam). Body is
  an inert, centered, muted placeholder marked with `data-summary-placeholder`; `cloud_cards` left
  at its `false` default so the cloud-card + `#analyze-lanes` sinks are emitted. No `<h1>` in the
  body (the scaffold emits the single focus target). No widgets/counts/`x-text`/`hx-*`/`setInterval`/DB reads.
- `src/phaze/routers/shell.py`: added `"summary": "shell/partials/summary_placeholder.html"` as the
  FIRST key of `STAGE_PARTIALS` (static string literal — preserves T-57-01 and doubles as the
  dead-template guard entry root); repointed `shell_home` to `_render_stage(request, "summary", session)`;
  updated the module docstring + `shell_home` docstring. No `elif stage == "summary"` branch added
  (Summary renders off the base context — zero DB reads). Analyze key, its branch, and the empty-state
  swap left untouched.

### Task 2 — Summary rail node (commit 192a5fc9)
- `src/phaze/templates/shell/partials/rail.html`: inserted a Summary `<button>` as the FIRST node
  inside the `<nav aria-label="Pipeline stages">`, wired `hx-get="/s/summary"` →
  `#stage-workspace` (innerHTML, push-url). Full node contract satisfied: `title="Summary"`, a
  24×24 `aria-hidden` `currentColor` `w-5 h-5` heroicons v2 outline **chart-pie** glyph, a
  `max-lg:sr-only` (never `max-lg:hidden`) label, a `focus-visible:` ring, the `aria-current="page"`
  idiom. Label-only — Summary has no `$store.pipeline` key (locked live-vs-static rule; matches
  Track-ID). Added `mt-1.5` to the Discover node (now that it is no longer the first node). Extended
  the header node-order comment to list `summary` as node 0.

### Task 3 — Test reconciliation + new coverage (commit 8bb2c735)
- `tests/shared/core/test_shell_routes.py`:
  - `_RAIL_STAGES`: prepended `"summary"` → 13 navigable stages.
  - Renamed `test_root_renders_shell_analyze_default` → `test_root_renders_shell_summary_default`
    (updated docstring map too); dropped the `make_file` seed (Summary has no empty-state branch);
    now asserts `data-stage="summary"` + `data-summary-placeholder` present and `data-stage="analyze"`
    **absent** (the scaffold's hidden empty `#analyze-lanes` sink makes a bare substring check a false
    positive, so the stage marker is the signal).
  - Added `test_analyze_still_reachable_at_s_analyze` (SQ3-03): seeds a file, asserts `/s/analyze`
    renders `data-stage="analyze"` + `id="analyze-lanes"` and that the analyze rail node is active there.
  - Added `test_summary_stage_route_and_fragment` (SQ3-01): full shell on direct nav + bare fragment
    on `HX-Request: true` (no `<html>`/`<head>`); regression guard on `id="straggler-failed-card"`
    (OOB seed host) and no-second-poll-loop (`hx-trigger="every`/`setInterval` absent).
  - `test_rail_nodes_wired`: flipped the final active-node assertion from `analyze` to `summary`.

## Verification

All gates green (test DB on port 5433 via `just test-db`):
- Seven core suites (Task 3 verify command): **63 passed**.
- Whole `shared` bucket (`just test-bucket shared` with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`
  exported to :5433): **879 passed, 4 skipped**. (An initial run without the DSN exported errored/failed
  — those were env-not-code; re-run with the DSN exported is fully green.)
- The three plan-called-out breaking tests all resolved: `test_root_renders_shell_analyze_default`
  renamed+rewritten, `test_rail_nodes_wired` active-node regex flipped to summary, and
  `test_enrich_analyze_workspaces.py::test_shell_sinks_legacy_oob_fragments` stays green because
  Summary composes the shared scaffold (so `#straggler-failed-card` is present on `GET /`).
- `uv run ruff check .` — All checks passed.
- `uv run ruff format --check .` — 484 files already formatted.
- `uv run mypy .` — Success, no issues in 196 source files.
- `just docs-drift` — 6 passed, 4 skipped.
- `pre-commit run --all-files` — all hooks Passed.

## Notes / deviations

- No deviations from the plan. The single most important instruction (compose the shared scaffold
  rather than hand-rolling the placeholder) was followed, which fixes both the runtime OOB-target
  bug and the hidden `test_shell_sinks_legacy_oob_fragments` breakage in one move.
- No new dependencies, no new DB queries, no new context keys, no new endpoint (the `summary` stage
  is served by the pre-existing whitelisted `GET /s/{stage}` route), no summary widgets/metrics.
- Legacy `/pipeline/` still 302-redirects to `/`, which now lands on Summary — acceptable for the
  placeholder, no code change (per plan pre_flight_findings #9).
