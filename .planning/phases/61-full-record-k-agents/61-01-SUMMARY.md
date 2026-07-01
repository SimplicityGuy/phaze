---
phase: 61-full-record-k-agents
plan: 01
subsystem: testing
tags: [alpinejs, focus-trap, sri, cdn, pytest, conftest, htmx, cloudjob, analysis-window]

# Dependency graph
requires:
  - phase: 60-review-and-apply
    provides: "_diff_row.html approve/edit/undo routes the record's pending-approval cluster reuses; the SRI test + conftest async-factory pattern"
  - phase: 57-shell
    provides: "shell.html standalone head block + cmdk_modal.html skeleton the focus plugin + palette build on"
provides:
  - "@alpinejs/focus@3.15.12 loaded (SRI-pinned, before Alpine core) in BOTH shell.html and base.html — the x-trap dep for the record slide-in (D-01) and the ⌘K palette (D-04)"
  - "SRI gate extended to scan shell.html (parametrized over both templates) — closes RESEARCH Pitfall 1"
  - "tests/test_record_palette_agents.py — 11 RED behavior tests encoding RECORD-01..04 + the cross-cutting single-poll fragment guard"
  - "conftest factories: seed_file_with_windows, seed_distinct_artists, seed_cloud_jobs"
affects: [61-02-record, 61-03-palette, 61-04-agents, 61-05-empty-state]

# Tech tracking
tech-stack:
  added: ["@alpinejs/focus@3.15.12 (CDN, SRI SHA-384)"]
  patterns:
    - "Parametrized SRI guard over every head-bearing template (base.html + shell.html)"
    - "Deferred (in-test) import of not-yet-existing symbols so a RED scaffold COLLECTS clean"
    - "Skip-on-404 cross-surface fragment test so sequential Wave-2 plans each pass their own per-task verify"

key-files:
  created:
    - "tests/test_record_palette_agents.py"
  modified:
    - "src/phaze/templates/shell/shell.html"
    - "src/phaze/templates/base.html"
    - "tests/test_base_html_sri.py"
    - "tests/conftest.py"

key-decisions:
  - "Palette grouped endpoint contract fixed to GET /search/ (HX branch) — the existing search router's HX results branch is the grouped-results seam (RESEARCH diagram)"
  - "distinct_artists() contract placed in phaze.services.search_queries; classify_compute_lanes() in phaze.services.agent_liveness — imported deferred in tests so 61-03/61-04 conform to the written signatures"
  - "Empty-state tests target the bare /s/analyze HX fragment (not the full shell) so the 'not scan-live-sets' + 'no free-text input' negatives cannot collide with the palette's scan-live-sets command living in shell chrome"
  - "Empty-state guide root must carry a data-empty-state marker — the branch discriminator both the present/suppressed tests key on"

patterns-established:
  - "Every template with its own <head> CDN block is guarded by the parametrized SRI test"
  - "RED Wave-0 scaffolds use deferred imports + real assertions (never assert True) and encode the exact route/ARIA/endpoint contract downstream plans satisfy"

requirements-completed: []

# Metrics
duration: ~12min
completed: 2026-07-01
---

# Phase 61 Plan 01: Focus-plugin + Wave-0 scaffold Summary

**Landed @alpinejs/focus@3.15.12 (SRI-pinned, before Alpine core) in both shell.html and base.html, extended the SRI gate to shell.html, and stood up the 11-test RED behavior scaffold + conftest factories that Plans 02-05 turn green.**

## Performance

- **Duration:** ~12 min
- **Completed:** 2026-07-01
- **Tasks:** 3
- **Files modified:** 4 (1 created, 3 modified) + 1 SUMMARY

## Accomplishments
- Added the one new CDN dep (`@alpinejs/focus@3.15.12`, the `x-trap` focus-trap plugin) as `<script defer>` immediately before the Alpine core line in BOTH head blocks, SRI SHA-384 pinned + full-semver URL + first-party publisher (mitigates T-61-02 supply-chain).
- Extended `tests/test_base_html_sri.py`: `_extract_cdn_scripts(template)` + `_ALL_TEMPLATES` parametrization over base.html AND shell.html on the version-pin and network SRI checks — closes RESEARCH Pitfall 1 (a stale/missing focus hash in shell.html, where the traps actually run, was previously invisible). The live network SRI check validated the focus-plugin hash against jsdelivr.
- Created `tests/test_record_palette_agents.py` — 11 async behavior tests encoding the RECORD-01..04 contract + the cross-cutting single-poll fragment guard, RED until the surfaces land, collecting clean now via deferred imports.
- Added three conftest async-factory fixtures (`seed_file_with_windows`, `seed_distinct_artists`, `seed_cloud_jobs`) mirroring the existing `make_file`/`seed_pending_proposal` shape.

## Task Commits

Each task was committed atomically:

1. **Task 1: focus plugin in both head blocks + SRI gate extended to shell.html** - `732cc9c` (feat)
2. **Task 2: conftest record/palette/agents/empty-state fixtures** - `7c54962` (test)
3. **Task 3: RED Wave-0 test scaffold (11 behavior tests)** - `4eed688` (test)

## Files Created/Modified
- `tests/test_record_palette_agents.py` - 11 RED behavior tests (record fragment, ⌘K grouped palette, Agents two sections, compute-lane liveness, empty state, cross-cutting single-poll guard).
- `src/phaze/templates/shell/shell.html` - loads `@alpinejs/focus@3.15.12` before Alpine core (record slide-in + ⌘K focus-traps).
- `src/phaze/templates/base.html` - same focus-plugin insertion (legacy pages).
- `tests/test_base_html_sri.py` - `_SHELL_HTML` constant + parametrized `_extract_cdn_scripts`/version-pin/network checks over both templates.
- `tests/conftest.py` - `seed_file_with_windows` (AnalysisResult + fine/coarse AnalysisWindow rows), `seed_distinct_artists` (FileMetadata + Tracklist, shared name + None per table), `seed_cloud_jobs` (running / submitted+inadmissible mix, distinct file_id per row).

## Decisions Made
- **Palette endpoint = `GET /search/` (HX branch).** The RESEARCH data-flow diagram maps the ⌘K grouped results to "search.py grouped branch"; the existing router already returns an HX results fragment there. The scaffold's palette tests query `/search/` so 61-03 extends that branch rather than adding a parallel route.
- **Query-service homes fixed by the scaffold:** `distinct_artists()` → `phaze.services.search_queries`, `classify_compute_lanes()` → `phaze.services.agent_liveness`. Imported deferred inside the tests (RED via ImportError now) so 61-03/61-04 implement to the written signatures.
- **Empty-state tests use the bare `/s/analyze` HX fragment.** This keeps the "not `scan-live-sets`" and "no free-text path input" negatives from colliding with the palette's (correct) `scan-live-sets` command, which lives in shell chrome outside the fragment.
- **`data-empty-state` marker** is the branch discriminator the present/suppressed empty-state tests both key on — a stable contract for 61-05.

## Deviations from Plan

None - plan executed exactly as written. No auto-fixes required (Rules 1-4 not triggered); no architectural decisions or auth gates.

## Issues Encountered
None. The Task-1 network SRI check ran (internet available) and confirmed the focus-plugin hash matches jsdelivr's served content — the pinned hash in the plan interfaces was correct.

## Requirements Note
This plan's frontmatter lists RECORD-01..04, but Plan 61-01 is the Wave-0 foundation: it lays the RED scaffold + the load-bearing focus dep, it does NOT deliver the surfaces. `requirements-completed` is intentionally empty — RECORD-01..04 are satisfied by Plans 61-02..05, which turn these tests green. The orchestrator should not mark RECORD-01..04 complete from this plan.

## Next Phase Readiness
- Wave-2 plans (61-02 record, 61-03 palette, 61-04 agents, 61-05 empty state) can now attach their `<automated>` verifies to concrete, already-written tests.
- `test_new_fragments_single_poll_clean` is skip-on-404 resilient, so 61-02/61-03/61-05 each pass their per-task verify against their own just-built surface without spurious executor retries; the full three-surface assertion holds once 61-05 lands and in the post-wave full suite.
- Fixtures cover every read-model Plans 02-05 verify against (windows, distinct artists, CloudJob liveness; empty-DB is the default clean session).

## Self-Check: PASSED
- All created/modified files present on disk (5 code files + SUMMARY).
- All three task commits present in git log (`732cc9c`, `7c54962`, `4eed688`).
- Task-1 SRI test: 6 passed (incl. live network hash validation). Task-2 conftest imports clean. Task-3 scaffold collects 11 tests, exit 0. Full suite collects 2606 tests with no import breakage from the conftest changes.

---
*Phase: 61-full-record-k-agents*
*Completed: 2026-07-01*
