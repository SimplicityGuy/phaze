---
phase: 58-enrich-analyze-workspaces
plan: 03
subsystem: ui
tags: [htmx, alpine, jinja2, scaffold, file-table, metadata, fingerprint, bulk-trigger, pytest]

# Dependency graph
requires:
  - phase: 58-01
    provides: "single persistent /pipeline/stats chrome poll + visibilitychange shed; Phase-58 test file with foundation tests + xfail workspace stubs"
  - phase: 58-02
    provides: "shared workspace partials (_workspace_scaffold.html macro, generic _file_table.html, _workspace_poll_seeds.html) + discover_workspace.html sibling-shape reference"
  - phase: 57-shell-dag-rail
    provides: "#stage-workspace swap target, STAGE_PARTIALS whitelist, fragment-only /s/<stage> routes, $store.pipeline (metadataBusy/fingerprintBusy/metadataDone/fingerprintDone), dead-template AST guard"
provides:
  - "Metadata workspace (metadata_workspace.html): scaffold + metadata-pending queue table + ONE EXTRACT ALL trigger wired VERBATIM to POST /pipeline/extract-metadata"
  - "Fingerprint workspace (fingerprint_workspace.html): the sibling -- FINGERPRINT ALL wired VERBATIM to POST /pipeline/fingerprint"
  - "shell.py STAGE_PARTIALS metadata/fingerprint -> the new partials (static literals) + per-stage queue context (metadata_files / fingerprint_files) via existing pending-set reads"
  - "real test_metadata_trigger_all_wired (WORK-02 / D-01 / D-02): verbatim endpoints + R-4 guard + NO selected/checkbox + no second poll"
affects: [58-04]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Sibling enrich workspaces: identical scaffold+file-table shape, differing only by title / sub-count copy / endpoint / column set -- composed against the Plan-02 shared partials, no per-workspace chrome"
    - "ALL-only bulk trigger idiom: hx-post (static, VERBATIM endpoint) + hx-target an empty #<stage>-trigger-response div + hx-confirm + :disabled busy-gate on $store.pipeline.<stage>Busy (R-4)"
    - "Live sub-count arithmetic inline in the x-text JS template literal (Math.max(metadataTotal-metadataDone,0)) -- no new store key, no new query, rides the single chrome poll"

key-files:
  created:
    - "src/phaze/templates/pipeline/partials/metadata_workspace.html"
    - "src/phaze/templates/pipeline/partials/fingerprint_workspace.html"
  modified:
    - "src/phaze/routers/shell.py"
    - "tests/test_enrich_analyze_workspaces.py"

key-decisions:
  - "metadata_files = get_metadata_pending_files (the EXACT set EXTRACT ALL enqueues -- every music/video file, D-01); fingerprint_files = get_fingerprint_pending_files (METADATA_EXTRACTED + failed-retry, deduped). Reused existing shared pending-set helpers -- no new service fn, no enqueue change"
  - "Trigger-response div uses empty:hidden so it occupies no space until the bulk-trigger fills it; this is a NEW Tailwind variant, so `just tailwind` regenerated the gitignored app.css (verified .empty\\:hidden compiled)"
  - "Pending-row enrichment cells (Existing tags on Metadata; Duration/Chromaprint/AcoustID on Fingerprint) render a neutral '—': pending files are not yet enriched/fingerprinted (semantically empty) and FileRecord.file_metadata is lazy=noload -- the rich per-file record is Phase 61 (D-06/R-1)"

patterns-established:
  - "Sibling-workspace composition (scaffold + file-table + single ALL-trigger) reusable by any future bulk-enqueue stage workspace"

requirements-completed: [WORK-02, WORK-05]

# Metrics
duration: ~25min
completed: 2026-06-30
---

# Phase 58 Plan 03: Metadata + Fingerprint workspaces Summary

**Replaced the Phase-57 placeholders for the Metadata and Fingerprint stages with their real workspaces — sibling content-only fragments of identical shape (scaffold + queue table + a single ALL-only bulk trigger), each wired VERBATIM to its existing enqueue endpoint with the R-4 double-enqueue guard, live via the one chrome poll, with zero backend behavior change.**

## Performance
- **Duration:** ~25 min
- **Completed:** 2026-06-30
- **Tasks:** 2
- **Files:** 4 (2 created, 2 modified)

## Accomplishments
- **Task 1 — per-stage queue context (Pitfall 5):** extended `shell._render_stage` with `metadata` and `fingerprint` branches (neither had DB context before — only analyze/discover did). Metadata loads `get_metadata_pending_files` (the EXACT set EXTRACT ALL enqueues); Fingerprint loads `get_fingerprint_pending_files` (METADATA_EXTRACTED ∪ failed-retry, deduped). Both reuse the existing shared pending-set helpers — no new service function, no enqueue change. `oob_counts` stays False; T-57-01 static-whitelist invariant preserved.
- **Task 2 — the two workspaces + wire + test:**
  - `metadata_workspace.html`: scaffold (one `<h1 tabindex="-1">`), live sub-count "{metadataDone} extracted · {pending} pending · manual stage" (pending computed inline), ONE secondary-style **EXTRACT ALL** posting VERBATIM to `POST /pipeline/extract-metadata` → `#metadata-trigger-response` (consumes `trigger_response.html` branches), R-4 guard (`hx-confirm` + `:disabled="$store.pipeline.metadataBusy > 0"`), and the metadata queue via `_file_table.html` (File · Format · Size · Existing tags · State), empty state "Nothing pending".
  - `fingerprint_workspace.html`: the sibling — **FINGERPRINT ALL** → `POST /pipeline/fingerprint` → `#fingerprint-trigger-response`, busy-gate on `fingerprintBusy`, columns File · Duration · Chromaprint · AcoustID, empty state "Nothing to fingerprint".
  - `STAGE_PARTIALS["metadata"]` / `["fingerprint"]` flipped from the placeholder to the new partials (static literals).
  - Converted `test_metadata_trigger_all_wired` from an xfail stub to real WORK-02/D-01/D-02 assertions covering both fragments.
- **D-02 honored:** NO `EXTRACT SELECTED` button, NO `type="checkbox"`, NO row-selection state anywhere. **WORK-05 honored:** neither fragment carries `hx-trigger="every"` / `setInterval` — both ride the single chrome poll. **D-06 honored:** inert-but-present rows via the shared file table.

## Task Commits
1. **Task 1: Metadata + Fingerprint per-stage queue context in shell.py** — `d0c0ccc` (feat)
2. **Task 2: workspaces + STAGE_PARTIALS wire + WORK-02 test** — `6a1ffc7` (feat)

## Deviations from Plan

### Adjusted Steps

**1. [Rule 3 - Blocking] `just tailwind` rebuild WAS required (PR #181 is now on this branch)**
- **Found during:** Task 2.
- **Issue:** Unlike Plan 58-02 (which predated PR #181), the branch is now current with origin/main, so build-time Tailwind is live (`/static/css/app.css` present, in-browser JIT gone). The workspaces introduce one new utility class (`empty:hidden` on the trigger-response divs).
- **Resolution:** Ran `just tailwind` (binary already cached) to regenerate the gitignored `app.css`; verified `.empty\:hidden` compiled. `app.css` is gitignored (`git check-ignore` confirmed) and was NOT committed. All other classes reuse the existing scaffold/file-table/`_btn` set (no rebuild needed for those).

### Auto-fixed Issues

**2. [Rule 1 - Lint] RUF003 ambiguous unicode in a code comment**
- **Found during:** Task 1 `ruff check`.
- **Issue:** A `∪` (set-union) glyph in a shell.py comment tripped `RUF003` (ambiguous-unicode).
- **Fix:** Rewrote the comment to use the word "plus". File: `shell.py`. Commit: `d0c0ccc`.

## Authentication Gates
None.

## Issues Encountered
- Reused the running ephemeral test DB/Redis (ports 5433/6380) from prior sessions; exported `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` accordingly. Test-environment setup only — no code impact.

## Known Stubs
The pending-row enrichment cells render a neutral `—` placeholder — this is **intentional and not a blocking stub**:
- **Metadata "Existing tags"** and **Fingerprint "Duration / Chromaprint / AcoustID":** these files are by definition *pending* (not yet extracted/fingerprinted), so the values are genuinely empty; and `FileRecord.file_metadata` is `lazy="noload"`, so surfacing them would require a new query/join — forbidden by the no-backend-change rule. The rich per-file record (with these values populated) is **Phase 61** (D-06 / R-1). The plan's goal (WORK-02: queue + existing manual trigger, no selection) is fully met.

## Threat Flags
None — no new network endpoint, auth path, file access, or schema surface introduced (both ALL buttons reuse existing dedup-keyed endpoints verbatim; T-58-ENQ mitigated by the R-4 busy-gate + confirm).

## Verification
- `tests/test_enrich_analyze_workspaces.py` + `test_shell_routes.py` + `test_dead_template_guard.py`: **11 passed, 2 xfailed** (the remaining 58-04 lane-card / analyze-table stubs).
- Targeted plan verification (`test_metadata_trigger_all_wired` + `test_single_poll_discipline` + dead-template guard): **3 passed**.
- `ruff check .`: clean. `mypy .` (184 files): clean.
- `just tailwind`: `app.css` regenerated, `.empty\:hidden` confirmed present.

## Next Phase Readiness
- The sibling-workspace pattern (scaffold + file-table + single ALL-trigger) is established. Plan **58-04** (Analyze lane cards + per-file lane/windows) is the last wave — its `test_lane_cards_states` and `test_analyze_file_table_lane_and_windows` remain xfail stubs ready to convert.

## Self-Check: PASSED
- Both created partials exist on disk; commits `d0c0ccc`, `6a1ffc7` present in `git log`.

---
*Phase: 58-enrich-analyze-workspaces*
*Completed: 2026-06-30*
