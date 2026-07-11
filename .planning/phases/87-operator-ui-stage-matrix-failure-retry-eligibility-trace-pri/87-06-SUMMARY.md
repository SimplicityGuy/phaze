---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
plan: 06
subsystem: operator-ui
tags: [force-skip, stage-skip, eligibility-trace, htmx, alpine, x-trap, sanitize-pg-text, ui-03, ui-04]

requires:
  - phase: 87-01
    provides: stage_skip table + StageSkip ORM model (the force-skip marker this writer persists)
  - phase: 87-02
    provides: Status.SKIPPED + resolve_status/eligible skipped threading (the derivation the trace reads)
  - phase: 87-04
    provides: routers/pipeline.py paginated files table + _stage_matrix pill scaffold built on here
provides:
  - "POST /pipeline/files/{file_id}/skip/{stage} — enrich-only, additive, sanitized, committed force-skip writer"
  - "GET /pipeline/files/{file_id}/trace/{stage} — single-row eligibility trace (resolve_status + real eligible())"
  - "_eligibility_trace.html — verdict + 4 named conjuncts (done? / in-flight? / upstream met? / terminal fail?) with the blocker highlighted"
  - "_force_skip_dialog.html — Alpine x-trap enrich-only confirm dialog with a required reason textarea"
  - "record_body.html right pane — 6 keyboard-accessible trace triggers + enrich-only force-skip controls"
affects:
  - "Phase 90 (downstream unblock): the trace surfaces the OQ-1 scope-minimal gate (a skipped upstream still gates propose)"
  - "87-07/87-08 (rail priority re-wire + orphan badge) compose into the same record slide-in host"

tech-stack:
  added: []
  patterns:
    - "mutating router commits itself (get_session never auto-commits); writer tests read from an INDEPENDENT session"
    - "additive-only marker writer: never clears a failure marker, so the Phase-79 shadow-compare gate stays green"
    - "sanitize_pg_text before persisting operator free text (NUL-abort footgun); reason never echoed back (T-87-21)"
    - "single-row trace: per-stage file_id-scoped resolve_status + REAL eligible(), never a corpus scan; degrade-safe"
    - "HTMX↔Alpine dialog bridge: @htmx:after-request closes on 2xx; hx-on::before-swap force-swaps the 422 validation body"
    - "self-contained per-enrich-stage force-skip island with a STATIC hx-post URL (HTMX-friendly, no reactive attr)"

key-files:
  created:
    - tests/analyze/test_force_skip_writer.py
    - src/phaze/templates/pipeline/partials/_eligibility_trace.html
    - src/phaze/templates/pipeline/partials/_force_skip_dialog.html
    - tests/shared/test_eligibility_trace.py
  modified:
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/record/record_body.html

key-decisions:
  - "Trace verdict is the REAL eligible() (the scheduler's source of truth) — a divergent trace would hide the deadlock UI-03 exists to expose"
  - "A SKIPPED upstream is rendered as still-gating downstream (honest), NOT 'satisfied' — matching the OQ-1 SCOPE-MINIMAL resolution (downstream unblock deferred to Phase 90). This deviates from the plan's stale 'skipped renders as satisfied' note."
  - "Right-pane pills are trace triggers WITHOUT per-stage status colour: buckets come from record.py's context, which is outside this plan's file ownership; the colored status matrix is the files-table's job (Plan 04/05)"
  - "422 for both the enrich-only guard (JSON detail) and the empty-reason validation (inline HTML fragment); a per-enrich static hx-post keeps the dialog HTMX-safe"

patterns-established:
  - "INDEPENDENT-session read as the commit-teeth guard for a mutating router (mutation-verified: dropping the commit → 3 RED)"
  - "trace conjunct model: {question, ok(✓/✗), phrase, blocker} → one highlighted blocker per non-eligible verdict"

requirements-completed: [UI-03, UI-04]

duration: ~70min
completed: 2026-07-11
---

# Phase 87 Plan 06: Right-pane eligibility trace + enrich-only force-skip Summary

**The per-file right pane: a single-row eligibility trace that names the ONE blocker keeping a stage
out of the pending set (UI-03), plus the correctness-sensitive force-skip escape hatch — enrich-only,
additive, sanitized, committed — that lets the `failed` bucket converge honestly as `skipped`, never
counterfeit `done` (UI-04).**

## Performance

- **Duration:** ~70 min
- **Completed:** 2026-07-11T06:28Z
- **Tasks:** 3
- **Files modified:** 6 (2 modified, 4 created)

## Accomplishments

- **Force-skip writer** (`POST /pipeline/files/{id}/skip/{stage}`): enrich-only 422 guard (mirrors
  `pipeline_stages._validate_stage`), required reason → inline validation, `sanitize_pg_text` before
  persist, additive-only (never clears `analysis.failed_at`), commits itself. Five behaviors locked,
  every DB assertion read from an INDEPENDENT session (mutation-verified: dropping the commit → RED).
- **Eligibility trace** (`GET /pipeline/files/{id}/trace/{stage}`): one file's per-stage scalars →
  `resolve_status` + the REAL `eligible()` → a verdict + four named conjuncts (`done?` · `in-flight?` ·
  `upstream met?` · `terminal fail?`) with the single unmet blocker highlighted. Strictly file_id-scoped
  single-row reads (mutation-verified no-corpus-scan guard), degrade-safe to "Trace unavailable this tick."
- **Right pane** (`record_body.html`): 6 keyboard-accessible stage trace triggers (real `<button>`s that
  hx-get the trace and reveal `_eligibility_trace.html` beneath the clicked pill), with the enrich-only
  `_force_skip_dialog.html` (Alpine `x-trap` focus-trap, required reason, accent-cyan confirm, Cancel
  default focus). Propose/review/apply carry NO skip affordance (D-10, render-asserted with teeth).

## Task Commits

1. **Task 1: Force-skip writer endpoint** - `fcd5fe72` (feat)
2. **Task 2: Single-row eligibility trace endpoint + `_eligibility_trace.html`** - `54d63cb8` (feat)
3. **Task 3: Right-pane expanded matrix (trace triggers) + enrich-only force-skip dialog** - `3ec34f49` (feat)

## Files Created/Modified

- `src/phaze/routers/pipeline.py` - `force_skip_stage` writer + `eligibility_trace` endpoint + the
  `_one_stage_scalars` / `_has_approved_proposal` / `_eligibility_trace_context` single-row helpers.
- `src/phaze/templates/pipeline/partials/_eligibility_trace.html` - verdict + 4-conjunct trace, blocker bold.
- `src/phaze/templates/pipeline/partials/_force_skip_dialog.html` - enrich-only Alpine x-trap confirm dialog.
- `src/phaze/templates/record/record_body.html` - right-pane "Stage eligibility" section (trace triggers + enrich force-skip).
- `tests/analyze/test_force_skip_writer.py` - 5 behaviors (422 non-enrich, empty-reason no-write, committed
  independent-session read, NUL sanitize round-trip, additive failed_at unchanged).
- `tests/shared/test_eligibility_trace.py` - 8 tests (blocker naming, single-row/no-corpus, enrich vacuous
  upstream, skipped-upstream honest gating, degrade-safe, + 3 record_body render assertions).

## Decisions Made

- **Trace verdict = real `eligible()`.** The trace is a diagnostic of scheduler eligibility; computing a
  divergent verdict would let it claim a stage is eligible when the scheduler permanently gates it — the
  exact hidden-deadlock class UI-03 exists to expose.
- **Right-pane pills are trace triggers, not colored status pills.** Per-stage `buckets` originate in
  `record.py`'s context, which is outside this plan's declared file ownership (parallel-executor rule), so
  I did not modify it. The colored status matrix is delivered by the files-table (Plan 04/05); this pane is
  the diagnostic + escape hatch. All Task-3 acceptance criteria (6 keyboard trace triggers, enrich-only
  force-skip, required reason, verbatim copy) are met without status colour.

## Deviations from Plan

**1. [Rule 1 — Correctness/Honesty] A skipped upstream renders as still-gating, NOT "satisfied"**
- **Found during:** Task 2.
- **Issue:** The plan's interface note + Task-2 acceptance say "a skipped upstream counts as met
  (`stage_satisfied = done OR skipped`) … renders as satisfied." But the shipped derivation
  (`enums/stage.py:eligible`) checks downstream upstreams STRICTLY `== Status.DONE`, and `eligible_clause`
  is enrich-only — and the authoritative **OQ-1 resolution is SCOPE-MINIMAL** (RESEARCH 509-512): a
  force-skip converges the `failed` bucket + enrich pending sets + recovery but **does NOT unblock
  downstream** (deferred to Phase 90). So a force-skipped `metadata` genuinely keeps `propose` gated.
- **Fix:** The trace's `upstream met?` conjunct STRICTLY mirrors `eligible()` (upstream must be DONE), and
  the verdict is the REAL `eligible()`. A skipped upstream renders honestly as a gating blocker
  (`metadata skipped — downstream stays gated (Phase 90)`). Rendering it "satisfied" would make the trace
  claim `propose` is eligible when the scheduler will never schedule it — recreating a hidden deadlock.
- **Files modified:** `src/phaze/routers/pipeline.py` (trace context), `tests/shared/test_eligibility_trace.py`.
- **Verification:** `test_skipped_upstream_still_gates_downstream` asserts the honest blocker;
  mutation-verified (flipping to the lenient `done OR skipped` rule → RED).
- **Committed in:** `54d63cb8` (Task 2 commit).

**2. [Rule 3 — Blocking/Tooling] Force-skip dialog reworked to satisfy the semgrep JSF autoescape rule**
- **Found during:** Task 3.
- **Issue:** The PostToolUse semgrep hook (a Java/JSF `autoescape-disabled` XSS rule) false-positived on the
  literal `@keydown.escape.window="open = false"` (matching `escape … = false`).
- **Fix:** Routed dialog close through an Alpine `hide()` method so no `false` sits on the `.escape` line;
  also dropped a redundant `event.detail.isError = false` (HTMX 2.x swaps on `shouldSwap = true` alone).
- **Files modified:** `src/phaze/templates/pipeline/partials/_force_skip_dialog.html`.
- **Verification:** hook passes; the 3 record_body render assertions pass.
- **Committed in:** `3ec34f49` (Task 3 commit).

---

**Total deviations:** 2 (1 correctness/honesty, 1 tooling). **Impact:** No scope creep. Deviation 1 is a
substantive correctness call — the trace reflects the shipped scheduler, not a stale plan note; flagged for
the orchestrator below.

## Issues Encountered

- The `client` fixture overrides `get_session` with the shared test session (which sees uncommitted rows),
  so a same-session read cannot prove the writer committed. Writer tests read from an INDEPENDENT
  `async_sessionmaker(async_engine)` session (project memory rule) — mutation-verified.
- A stray interactive `cp -i` during a mutation-test restore left the file mutated; caught immediately via a
  post-restore `grep` and fixed with a direct edit. Both mutation guards (commit-teeth, skipped-lenient,
  enrich-only) were confirmed to go RED then restored GREEN.

## Threat Register Coverage

- **T-87-18** (approval bypass via force-skip): mitigated — enrich-only 422 (`STAGE_TO_FUNCTION` allowlist)
  + no skip affordance on propose/review/apply (render-asserted); backstopped by the Plan-01 DB CHECK.
- **T-87-19** (NUL aborts PG txn): mitigated — `sanitize_pg_text(reason)` before persist; NUL round-trip test.
- **T-87-20** (writer tidies `failed_at`): mitigated — additive-only writer; `test_skip_never_clears_analysis_failed_at`.
- **T-87-21** (XSS via reason): mitigated — reason NEVER echoed into any response; all rendered values autoescape.
- **T-87-22** (missing/blank reason): mitigated — `reason.strip()` required → inline validation, no write.
- **T-87-23** (trace becomes a corpus scan): mitigated — single-row file_id-scoped reads; `test_trace_is_single_row_no_corpus_scan` (every SELECT has WHERE, no COUNT); degrade-safe.

## Next Phase Readiness

- Right-pane slide-in host is ready for 87-07 (rail priority re-wire) + 87-08 (orphan badge).
- **Flag for orchestrator (Phase 90):** the trace now makes the OQ-1 scope-minimal gate VISIBLE — a
  force-skipped enrich upstream still gates its downstream (`propose`). If/when Phase 90 wires
  downstream-unblock (`stage_satisfied = done OR skipped` into the propose reader + a downstream
  eligibility clause), the trace's strict `upstream met?` conjunct should be revisited in lockstep.

## Self-Check: PASSED

All 6 key files present on disk; all 3 task commits (`fcd5fe72`, `54d63cb8`, `3ec34f49`) found in git
history. Regression check green: `test_force_skip_writer` (5) + `test_eligibility_trace` (8) +
`test_stage_endpoints` + `test_files_page` = 23 passed; `ruff check src/phaze/` clean; router + app import OK.

---
*Phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri*
*Completed: 2026-07-11*
