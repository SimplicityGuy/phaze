---
phase: 58-enrich-analyze-workspaces
plan: 02
subsystem: ui
tags: [htmx, alpine, jinja2, macro, oob-seed, scaffold, discover, pytest]

# Dependency graph
requires:
  - phase: 58-01
    provides: "single persistent /pipeline/stats chrome poll + visibilitychange shed; Phase-58 test file with foundation tests + xfail workspace stubs + _seed_* helpers"
  - phase: 57-shell-dag-rail
    provides: "#stage-workspace swap target, STAGE_PARTIALS whitelist, fragment-only /s/<stage> routes, $store.pipeline, dead-template AST guard"
provides:
  - "Shared workspace scaffold macro (_workspace_scaffold.html): one <h1 tabindex=-1> focus target + live x-text sub-count + secondary action row + caller() body; auto-includes the OOB seed-target host"
  - "Reusable generic file table (_file_table.html): columns/rows contract, empty state, inert D-06 rows (cursor-pointer, no click), font-mono+title cells, NO | safe (XSS V5)"
  - "Persistent OOB seed-target host (_workspace_poll_seeds.html): a dag-seed-<key> placeholder for every $store.pipeline key incl. a pre-mounted dag-seed-notYetEnriched + the five legacy seed ids"
  - "Discover workspace (discover_workspace.html): recent-scans surface (self-poll stripped) + live discovered/not-yet-enriched sub-count + SCAN/RECOVER (WORK-01)"
  - "notYetEnriched: a read-only derived int (discovered - metadataExtracted, clamped >=0) on the dag dict + base.html store + shell.py discover context"
affects: [58-03, 58-04]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Workspace composition via {% import %} a scaffold macro + {% call %}…{% endcall %} for the body slot, with actions passed as a block-set capture (no | safe needed — Markup under autoescape)"
    - "New reactive numerals become read-only derived int keys on the dag dict, auto-riding the existing stats_bar.html dag.items() OOB loop onto a pre-mounted dag-seed-<key> placeholder — no second poll, no new query"
    - "OOB seed targets must be pre-mounted in the swapped fragment (htmx OOB lands only on ids already in the DOM)"

key-files:
  created:
    - "src/phaze/templates/pipeline/partials/_workspace_scaffold.html"
    - "src/phaze/templates/pipeline/partials/_file_table.html"
    - "src/phaze/templates/pipeline/partials/_workspace_poll_seeds.html"
    - "src/phaze/templates/pipeline/partials/discover_workspace.html"
  modified:
    - "src/phaze/routers/shell.py"
    - "src/phaze/routers/pipeline.py"
    - "src/phaze/templates/base.html"
    - "tests/test_enrich_analyze_workspaces.py"
    - "tests/test_pipeline_dag_context.py"

key-decisions:
  - "Composed workspaces via a scaffold MACRO (import + call) rather than a context-variable include or dynamic include: dynamic {% include some_var %} yields None in jinja2.meta.find_referenced_templates and would orphan the body/action partials (dead-template guard red); import/call keeps every partial statically reachable and needs no | safe"
  - "notYetEnriched is computed in _build_dag_context from get_pipeline_stats, which is now passed through from both the dashboard and poll callers (signature gains stats=None) to avoid a duplicate query; direct test callers omit it and the builder reads it itself"
  - "_workspace_poll_seeds.html provides a dag-seed-<key> placeholder for EVERY store key (harmless extras for the legacy *-files-ready/busy-seed writes) plus an explicit dag-seed-notYetEnriched and the five legacy non-dag-seed ids"
  - "Discover renders recent scans through the generic _file_table (NOT recent_scans_table.html) because that partial self-polls — reusing it verbatim would add a second poll loop (WORK-05/Pitfall 4)"

patterns-established:
  - "Workspace scaffold macro + file-table contract reused by Plans 58-03/04"
  - "Derived-int-on-dag-dict + pre-mounted OOB seed target for any new reactive workspace numeral"

requirements-completed: [WORK-01]

# Metrics
duration: ~35min
completed: 2026-06-30
---

# Phase 58 Plan 02: Shared workspace partials + Discover workspace Summary

**Built the three shared presentation partials every v7.0 workspace composes against (scaffold macro, generic file table, persistent OOB seed-target host) and shipped the first real workspace — Discover — as a content-only fragment with live recent scans, a derived not-yet-enriched backlog count, and SCAN/RECOVER, all riding the single Plan-01 chrome poll with no second loop.**

## Performance
- **Duration:** ~35 min
- **Completed:** 2026-06-30
- **Tasks:** 3
- **Files:** 9 (4 created, 5 modified)

## Accomplishments
- Created the reusable **workspace scaffold** macro: exactly one `<h1 tabindex="-1">` focus landing target, a live `x-text` sub-count bound to `$store.pipeline`, a Phase-57 secondary-style action row, and a `caller()` body slot; it auto-includes the OOB seed-target host so every workspace's live seeds have a landing spot.
- Created the **generic file table** (`columns`/`rows` contract) modeled on `recent_scans_table.html` with the locked empty state, inert-but-present D-06 rows (`cursor-pointer`, stable id, no click), `font-mono`+`title=` path cells, and NO `| safe` (XSS V5 — verified path `<x>&` escapes to `&lt;x&gt;`).
- Created the **persistent OOB seed-target host** with a `dag-seed-<key>` placeholder for every store key plus a pre-mounted `dag-seed-notYetEnriched` (added to base.html only in this plan's Task 2 — the OOB-target-must-pre-exist rule) and the five legacy seed ids.
- Added **notYetEnriched** as a read-only derived int (`discovered - metadataExtracted`, clamped ≥0) on the dag dict; it rides the existing `dag.items()` OOB loop with zero `stats_bar.html` edits and zero new query path.
- Shipped **discover_workspace.html** (WORK-01): live `discovered / not-yet-enriched` sub-count, recent scans rendered through the generic table with the self-poll stripped, SCAN (reveals the reused Trigger Scan form → `POST /pipeline/scans`) and RECOVER (`POST /pipeline/recover` with `hx-confirm` + a `:disabled` busy-gate, R-4).
- Wired `STAGE_PARTIALS["discover"]` to a static literal (T-57-01) and converted the `test_discover_workspace` xfail stub to real WORK-01 assertions.

## Task Commits
1. **Task 1: shared scaffold + file-table + poll-seed partials** — `36984e0` (feat)
2. **Task 2: notYetEnriched derived seed + Discover stage context** — `9986a17` (feat)
3. **Task 3: Discover workspace + STAGE_PARTIALS wire + WORK-01 test** — `b54bc4c` (feat)

## Deviations from Plan

### Adjusted Steps

**1. [Rule 3 - Blocking] `just tailwind` build step is not applicable on this branch**
- **Found during:** Task 3 verification.
- **Issue:** The plan/phase constraints assume PR #181 (build-time Tailwind: `just tailwind` → `src/phaze/static/css/app.css`). This phase branch (`gsd/phase-58-…`) is based on `ef36eca` (#180), which **predates PR #181 and 57.1** — there is no `assets/src/app.css`, no `app.css`, and no `tailwind` justfile recipe. base.html still loads the **in-browser Tailwind JIT compiler** (`@tailwindcss/browser@4.3.2`).
- **Resolution:** Skipped the `just tailwind` rebuild + `app.css` grep. The in-browser JIT compiles new utility/arbitrary-value classes (`tracking-[0.15em]`, `h-9`, `hover:bg-white/5`, etc.) at runtime from the live DOM, so the workspace renders styled with no build step. No `app.css` exists to commit. (PR #181 will land on this branch at merge; the `@source` glob it adds already covers `pipeline/partials/`.)

### Auto-fixed Issues

**2. [Rule 1 - Bug] Nested Jinja comment broke scaffold parse**
- **Found during:** Task 3 (`TemplateSyntaxError: Encountered unknown tag 'endcall'`).
- **Issue:** The scaffold's doc comment contained a nested `{# … #}` example; Jinja comments don't nest, so the inner `#}` closed the outer comment early and exposed `{% endcall %}` as a real tag.
- **Fix:** Rewrote the doc comment without nested comment markers. File: `_workspace_scaffold.html`. Commit: `b54bc4c`.

**3. [Rule 1 - Bug] `<header>` tripped the bare-fragment guard**
- **Found during:** Task 3 (`test_stage_fragment_is_bare` failed in both `test_shell_routes.py` and `test_enrich_analyze_workspaces.py`).
- **Issue:** Those Phase-57 foundation tests assert the substring `"<head"` is absent from a stage fragment; the scaffold's semantic `<header>` element matches `"<head"`.
- **Fix:** Used a `<div>` for the header bar (layout role unchanged; documented inline). File: `_workspace_scaffold.html`. Commit: `b54bc4c`.

**4. [Rule 2 - Missing critical functionality] Discover context + dag-context signature**
- **Found during:** Tasks 2-3.
- **Issue:** The plan's Task 2 specified only `recent_scans` for the discover context, but a functional SCAN action needs the agent list for the reused Trigger Scan form; and `_build_dag_context` had no access to stats for the derived count.
- **Fix:** Added `agents` (non-revoked, ordered) to the discover branch in `shell.py`; extended `_build_dag_context(…, stats=None)` and passed `stats` through from both the dashboard and poll callers to reuse the existing query (no duplicate). Files: `shell.py`, `pipeline.py`. Commits: `9986a17`.

## Authentication Gates
None.

## Issues Encountered
- The local test DB/Redis (ports 5433/6380) from the 58-01 session were already running; exported `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` accordingly. Test-environment setup only — no code impact.

## Verification
- `tests/test_enrich_analyze_workspaces.py` + `test_shell_routes.py` + `test_pipeline_dag_context.py` + `test_dead_template_guard.py`: **28 passed, 3 xfailed** (the remaining 58-03/04 stubs).
- Full suite: **2520 passed, 3 xfailed** in 299s.
- `ruff check .` and `mypy .` (183 files): clean.
- Manual Jinja render with a seeded scan row confirmed: rows render with stable ids, XSS-escaped path cells, status word, files cell, exactly one `tabindex="-1"` h1, and the `dag-seed-notYetEnriched` target present.

## Next Phase Readiness
- The scaffold macro + generic file-table contract are ready for Plans 58-03 (Metadata/Fingerprint) and 58-04 (Analyze lane cards + per-file lane/windows). Any new reactive numeral they add follows the derived-int-on-dag-dict + pre-mounted `dag-seed-<key>` pattern established here.
- `_file_table.html` is reachable via the discover include chain (dead-template guard green); the metadata/fingerprint/analyze workspaces will feed it their own column sets.

## Self-Check: PASSED
- All four created partials exist on disk; commits `36984e0`, `9986a17`, `b54bc4c` present in `git log`.

---
*Phase: 58-enrich-analyze-workspaces*
*Completed: 2026-06-30*
