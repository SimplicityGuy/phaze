---
phase: 61-full-record-k-agents
plan: 02
subsystem: web-ui
tags: [htmx, alpinejs, focus-trap, x-trap, record, slide-in, fragment, file-table, snapshot, uuid-scoping]

# Dependency graph
requires:
  - phase: 61-full-record-k-agents (plan 01)
    provides: "@alpinejs/focus x-trap dep + the 4 RED record tests + conftest seed_file_with_windows"
  - phase: 60-review-and-apply
    provides: "_diff_row.html + the approve/edit/undo routes the record's pending-approval cluster reuses; row_detail.html identity"
  - phase: 58-workspaces
    provides: "_file_table.html (the shared file table the row->record trigger extends) + get_analyze_stage_files"
provides:
  - "GET /record/{file_id} — a typed-uuid, strictly file_id-scoped, read-only BARE fragment composing the file's timeline/diff/identity/pending-approvals/history (RECORD-01 / D-01)"
  - "record_host.html — the persistent x-trap.inert.noscroll slide-in host (sibling of cmdk_modal.html, survives rail swaps); #record-body re-inits Alpine after each HTMX swap (Pitfall 3)"
  - "record_not_found.html — the friendly 404 fragment (T-61-05)"
  - "_file_table.html row_file_ids param — rows open the record via hx-get=/record/{file_id} + record:open dispatch (the ⌘K Files group reuses the same contract)"
affects: [61-03-palette, 61-04-agents, 61-05-empty-state, 62-cutover]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Read-only compose route mirrors proposals.proposal_timeline (T-31-06-02 file_id scoping) — no new query semantics"
    - "Persistent chrome host OUTSIDE #stage-workspace + per-host hx-on::after-swap Alpine.initTree (the shell's global re-init covers only #stage-workspace)"
    - "Reused <td>-emitting partials (analysis_timeline.html / row_detail.html) wrapped in a 1-row table so their own reveal scripts key on the wrapper id"
    - "Shared table partial stays backward-compatible: row->record binding emits ONLY when the optional row_file_ids parallel list is supplied"

key-files:
  created:
    - "src/phaze/routers/record.py"
    - "src/phaze/templates/record/record_body.html"
    - "src/phaze/templates/record/record_not_found.html"
    - "src/phaze/templates/shell/partials/record_host.html"
  modified:
    - "src/phaze/main.py"
    - "src/phaze/templates/shell/shell.html"
    - "src/phaze/templates/pipeline/partials/_file_table.html"
    - "src/phaze/templates/pipeline/partials/analyze_workspace.html"
    - "src/phaze/services/pipeline.py"
    - "tests/test_enrich_analyze_workspaces.py"

key-decisions:
  - "record_body.html + record_not_found.html were created in Task 1 (plan listed record_body under Task 2) — Task 1's verify renders record_body.html, so the route cannot return a 200 fragment without it. Task 2 then only added the host + shell wiring."
  - "Added FileRecord.id -> row dict 'file_id' (str) in get_analyze_stage_files — the Analyze row dict carried no file id, and row_file_ids needs one. Read-only additive column; the degrade-safe SAVEPOINT path is unchanged."
  - "record.py does NOT use `from __future__ import annotations` (matches proposals.py) so FastAPI can resolve `AsyncSession`/`Request` at runtime for Depends — the future import triggers ruff TC002 wanting them in a TYPE_CHECKING block, which would break DI (the PEP 649 hazard CLAUDE.md documents)."
  - "History composed from ExecutionLog (joined via its proposal.file_id) + TagWriteLog (direct file_id) — the cleanest read-only per-file event source (UI-SPEC §7 Discretion)."
  - "The dialog aria-label starts 'File record' and is refined to the loaded file name after swap via h2.textContent (inherently XSS-safe — no |e/|tojson needed in the host JS attr)."

requirements-completed: [RECORD-01]

# Metrics
duration: ~40min
completed: 2026-07-01
---

# Phase 61 Plan 02: Full-record slide-in (RECORD-01) Summary

**Built `GET /record/{file_id}` — a typed-uuid, strictly file_id-scoped, read-only bare HTMX fragment composing the file's windowed timeline, metadata diff, identity, inline-approvable pending approvals, and history into a persistent `x-trap` focus-trapped slide-in over the shell, opened from Analyze file rows (and, by contract, ⌘K).**

## Performance
- **Duration:** ~40 min
- **Completed:** 2026-07-01
- **Tasks:** 3
- **Files:** 4 created + 6 modified (+ this SUMMARY)

## Accomplishments
- **Task 1 — the route (`ce4fd8a`):** `src/phaze/routers/record.py` with `GET /{file_id}` (typed `uuid.UUID` path param — closes the template-path/BAC surface, T-61-03). Every read (AnalysisWindow rows split fine/coarse, the 1:1 AnalysisResult, this file's pending RenameProposal rows, ExecutionLog+TagWriteLog history) is filtered strictly by `file_id`, mirroring `proposals.proposal_timeline` (T-31-06-02). A missing/de-duplicated file returns HTTP 404 rendering `record_not_found.html` (friendly copy, `text/html`, no stack trace — T-61-05), never a raised `HTTPException` (which would JSON-serialize). Registered `record.router` next to `shell.router` in `main.py`.
- **Task 2 — the persistent host (`258b9ac`):** `shell/partials/record_host.html`, a persistent `{% include %}` sibling of `cmdk_modal.html` OUTSIDE `#stage-workspace` (survives rail swaps). `role="dialog"` + `aria-modal="true"` + `x-trap.inert.noscroll="open"` on the right-anchored `w-[760px] inset-y-4 right-4` panel; `@record:open.window` records the opener for focus-return; Esc/backdrop/✕ close. `#record-body` carries `hx-on::after-swap` → `Alpine.initTree` (re-inits the `_diff_row.html` x-data islands — RESEARCH Pitfall 3) and refines the dialog `aria-label` to the loaded file name via `textContent`. Included in `shell.html` next to the cmdk modal.
- **Task 3 — the row trigger (`5748ab0`):** `_file_table.html` gained an OPTIONAL parallel `row_file_ids` list; when supplied, each `<tr>` emits `hx-get="/record/{file_id}"` → `#record-body` + `@click="$dispatch('record:open', {el:$el})"` (click-UNBOUND otherwise — backward compatible for Discover/Metadata/Fingerprint/Track-ID workspaces). `get_analyze_stage_files` now surfaces `file_id`, and `analyze_workspace.html` passes `row_file_ids`. The ⌘K Files group (Plan 03) reuses the identical `/record/{file_id}` contract.
- **`record_body.html`** composes all seven UI-SPEC §Surface-1 sections top→bottom: sticky Jura header + mono path + ✕; `grid-cols-4` facts (Format/Duration/sha256/Lane); the windowed multi-lane timeline (reuses `analysis_timeline.html` verbatim); a `grid-cols-2` metadata + identity row (identity reuses `row_detail.html`); the amber inline-approvable "pending approvals for this file" box (reuses `_diff_row.html` wired to the Phase 60 routes); and a mono History list. JS-attribute contexts use `|tojson` (inherited from `_diff_row.html`) — no `|e` in any new JS attr (T-61-01 / SP-5).

## Task Commits
1. **Task 1: GET /record/{file_id} route + 404 fragment + composed body + registration** — `ce4fd8a` (feat)
2. **Task 2: persistent record_host.html chrome + shell wiring** — `258b9ac` (feat)
3. **Task 3: wire Analyze rows -> record slide-in (+ file_id read + stale-test update)** — `5748ab0` (feat)

## Deviations from Plan

### Auto-fixed / auto-adjusted

**1. [Rule 3 - Blocking] Created `record_body.html` + `record_not_found.html` in Task 1**
- **Found during:** Task 1
- **Issue:** The plan lists `record_body.html` under Task 2's files, but Task 1's `<verify>` renders `record/record_body.html` (the route returns it) — the three Task-1 tests cannot reach 200 without it.
- **Fix:** Built the full composed `record_body.html` + the `record_not_found.html` 404 fragment in Task 1; Task 2 was then scoped to the persistent host + shell wiring only.
- **Files:** `src/phaze/templates/record/record_body.html`, `record_not_found.html`
- **Commit:** `ce4fd8a`

**2. [Rule 3 - Blocking] Added `file_id` to `get_analyze_stage_files`**
- **Found during:** Task 3
- **Issue:** The plan says "pass `row_file_ids` (the file ids backing `ns.rows`)", but the Analyze row dict carried NO file id (the interfaces note flagged "there is NO file_id passed per row yet").
- **Fix:** Added `FileRecord.id` to the existing read-only SELECT and `"file_id": str(file_id)` to the row dict. Additive, read-only; the degrade-safe SAVEPOINT/`[]`-on-error path is unchanged. No test asserted the dict's exact key set.
- **Files:** `src/phaze/services/pipeline.py`
- **Commit:** `5748ab0`

**3. [Rule 1 - Stale test] Updated `test_analyze_file_table_lane_and_windows`**
- **Found during:** Task 3
- **Issue:** This Phase-58 test asserted `"hx-get" not in tbl` (the click-unbound row invariant). Its own docstring scoped that: "row→record wiring is Phase 61". My Task-3 change (correctly, per the plan) adds that binding, so the assertion was superseded.
- **Fix:** Replaced the negative assertion with positive checks that the row now carries `hx-get="/record/`, `hx-target="#record-body"`, and `record:open` (and still no `aria-selected` / no self-poll). Updated the docstring.
- **Files:** `tests/test_enrich_analyze_workspaces.py`
- **Commit:** `5748ab0`

**4. [Rule 3 - Blocking] Dropped `from __future__ import annotations` in `record.py`**
- **Found during:** Task 1 (pre-commit ruff)
- **Issue:** With deferred annotations, ruff TC002 wants `AsyncSession`/`Request` moved into a `TYPE_CHECKING` block — but FastAPI resolves them at runtime for `Depends`, so that move would break DI (the PEP 649 hazard CLAUDE.md documents).
- **Fix:** Removed the future import to match the sibling `proposals.py` router. `dict[str, Any]`/`list[...]` are native on Python 3.14 without it.
- **Files:** `src/phaze/routers/record.py`
- **Commit:** `ce4fd8a`

No architectural changes (Rule 4 not triggered); no authentication gates.

## Known Stubs
- **`record_body.html` Lane badge is hardcoded `🖥️ local`.** Per-file lane derivation (local / ☁️ A1 / ⎈ k8s) and the D-02 lane-badge OOB refresh are NOT in this plan's cut — the record body is a snapshot (D-02), and the lane badge is cosmetic. It does not gate RECORD-01 (all seven sections render, file_id-scoped; the pending-approval cluster is fully wired to the Phase 60 routes). A future plan can bind it off the same `/pipeline/stats` fanout behind the `oob_counts` gate.
- **`record_body.html` metadata-diff left card is informational.** The load-bearing, inline-approvable before→after diff is the amber "pending approvals for this file" box (reusing `_diff_row.html` + the Phase 60 approve/edit/undo routes — the RECORD-01 correctness core). The left metadata card points the operator to it rather than duplicating a second diff surface.

## Threat Flags
None. The only new surface is the planned `GET /record/{file_id}` (already in the plan's `<threat_model>`: typed `uuid.UUID`, strict file_id scoping, friendly 404). No new endpoints, auth paths, file access, or schema changes.

## Verification
- In-scope suite (single clean invocation, fresh schema): **25 passed** —
  `test_record_fragment_bare_and_scoped`, `test_record_missing_file_404_fragment`, `test_record_pending_approvals_wired`, `test_new_fragments_single_poll_clean`, `tests/test_base_html_sri.py`, `tests/test_shell_routes.py`, `tests/test_enrich_analyze_workspaces.py`.
- No regression: `tests/test_identify_workspaces.py` + `tests/test_routers/test_pipeline_scans.py` = 69 passed (the shared `_file_table.html` stays click-unbound where `row_file_ids` is absent).
- `ruff check` + `mypy` clean on all changed Python; pre-commit (ruff/ruff-format/bandit/mypy) passed on every commit (never `--no-verify`).
- Out of scope (Plans 61-03/04/05): the palette / agents / empty-state tests in `test_record_palette_agents.py` remain RED — they are turned green by the later Wave-2 plans, not this one.

## Next Plan Readiness
- The `record_host.html` + `GET /record/{file_id}` contract is live: Plan 61-03's ⌘K Files group opens records by dispatching `record:open` + `hx-get="/record/{file_id}"` into `#record-body` (no new route, no duplicated logic).
- `_file_table.html`'s `row_file_ids` is available to any workspace that wants row→record (Metadata/Fingerprint/Track-ID can opt in by passing their own file-id lists).

## Self-Check: PASSED
- Created files present on disk: `src/phaze/routers/record.py`, `templates/record/record_body.html`, `templates/record/record_not_found.html`, `templates/shell/partials/record_host.html`.
- Task commits present in git log: `ce4fd8a`, `258b9ac`, `5748ab0`.
- In-scope verification: 25 passed; no-regression suite: 69 passed.

---
*Phase: 61-full-record-k-agents*
*Completed: 2026-07-01*
