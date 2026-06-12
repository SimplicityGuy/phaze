---
phase: 35-pipeline-determinism-idempotency-per-job-type-observability
plan: 05
subsystem: pipeline-observability
tags: [dag, svg, canvas, alpine, htmx, accessibility, d-01, ui]

requires:
  - "35-04 $store.pipeline per-node sub-keys (17) seeded to 0 in base.html"
  - "35-04 _build_dag_context → the `dag` dashboard/stats context (DB-truth done/total/active)"
  - "35-04 stats_bar.html dag-seed-<key> OOB poll seeds (the id contract mirrored here)"
  - "Phase-34 existing trigger endpoints: POST /pipeline/{analyze,extract-metadata,fingerprint,proposals}"
provides:
  - "dag_canvas.html — the single coherent 9-node SVG pipeline DAG (D-01) replacing stage_cards + processing_card"
  - "Anchor-derived bézier edges from one NODE_LAYOUT map; edge-honest (only Metadata+Analyze→Proposals)"
  - "Centralized Alpine `nodes` getter: one source of truth for {state,pill,pct,reason,label} feeding both the SVG chips AND the <ol> text-equivalent"
  - "Per-node gated triggers wired to EXISTING endpoints only (no net-new trigger surface, T-35-10)"
  - "Stacked <ol> = < sm phone fallback AND sr-only screen-reader text equivalent"
affects:
  - "pipeline dashboard UI (the operator-facing pipeline view is now the DAG)"

tech-stack:
  added: []
  patterns:
    - "Single-source-of-truth SVG layout: NODE_LAYOUT (Jinja {% set %} dict) + an edge list; each edge `d` is DERIVED from source.rightCenter→target.leftCenter (M sx,sy C sx+Δ,sy tx-Δ,ty tx,ty, Δ=60), never hand-typed"
    - "Centralized Alpine `nodes` getter (mk() folds done/total/active + the gate into state/pill/pct/reason/label) so the visual chips and the <ol> text-equivalent stay in lockstep — author once, two renderings"
    - "Static-once canvas + store-driven bindings: the SVG + node frames + buttons render ONCE; only counts/bars/pills/:disabled update via the existing 5s OOB poll (no canvas/button re-swap)"
    - "One <ol> doing two jobs: sm:sr-only (text equivalent at ≥ sm) + visible at < sm (phone fallback)"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/dag_canvas.html
    - tests/test_dag_canvas_render.py
  modified:
    - src/phaze/templates/pipeline/dashboard.html
    - src/phaze/templates/pipeline/partials/stats_bar.html
    - src/phaze/templates/base.html
    - src/phaze/routers/pipeline.py
    - tests/test_routers/test_pipeline_scans.py
  deleted:
    - src/phaze/templates/pipeline/partials/stage_cards.html
    - src/phaze/templates/pipeline/partials/processing_card.html
    - tests/test_template_helpers/test_stage_cards_partial.py
    - tests/test_template_helpers/test_processing_card_partial.py

decisions:
  - "Discovery 'Rescan Files' is an in-page <a href=#trigger-scan-heading> to the existing Trigger Scan card, NOT a one-click POST: the existing /pipeline/scans endpoint requires agent_id + path form fields, so a fabricated one-click enqueue is impossible and a net-new endpoint is out of scope"
  - "Scan/Search renders DISPLAY-ONLY (no 'Scan Tracklists' button) — only a per-file /tracklists/scan (file_ids form) exists, no bulk pipeline-level tracklist endpoint, so the UI-SPEC conditional resolves to display-only"
  - "Fingerprint gates on $store.pipeline.discovered (UI-SPEC L243 topology correction), NOT metadataExtracted — its only hard upstream is Discovery (reads the file on disk)"
  - "Centralized `nodes` getter rather than per-node inline x-data: avoids duplicating the gate logic across the chip and the <ol>, and keeps the LOCKED copy in one place"
  - "stats_bar.html's three 'files ready' OOB seeds are now hidden (store-write only) so the OOB swap never leaks visible 'N files ready' text onto the canvas; the hidden text node is retained for the backward-compatible polling assertion"

requirements-completed: [OBSERV]

metrics:
  duration: "~95m"
  completed: "2026-06-12"
  tasks: 3
  files: 9
---

# Phase 35 Plan 05: Pipeline DAG Canvas (D-01) Summary

**Replaced the Phase-34 `stage_cards.html` + `processing_card.html` with a single coherent 9-node SVG pipeline DAG (sketch 001 Variant B): anchor-derived bézier edges from one `NODE_LAYOUT` map with honest topology (only Metadata+Analyze converge into Proposals), stage-colored chips with live store-bound counts/bars/state-pills, per-node gated triggers wired to the EXISTING endpoints only, dark-mode throughout, and a stacked `<ol>` that doubles as the `< sm` phone fallback and the screen-reader text equivalent — all kept live by the existing 5s `/pipeline/stats` OOB poll with no new poll, no SSE, and no net-new endpoint.**

## What Was Built

### Task 1 + 2 — `dag_canvas.html` (committed together, see Deviations)
- **Layout + edges:** ONE `NODE_LAYOUT` Jinja dict (col0 Discovery · col1 {Metadata, Analyze, Fingerprint, Scan/Search} · col2 {Proposals, Scrape} · col3 {Execute, Match}) and an authoritative edge list. Each of the 9 edges emits a cubic-bézier `<path d="M sx,sy C sx+60,sy tx-60,ty tx,ty">` DERIVED from `source.rightCenter → target.leftCenter`. **Edge honesty:** `metadata→proposals` and `analyze→proposals` only; NO `fingerprint→proposals` and NO tracklist→proposals edge. Canvas is `role="group" aria-label="Pipeline stage graph"`; the SVG layer is `aria-hidden="true"`.
- **9 node chips:** 3px stage-color top-stripe, stage-name label, state pill, 18px tabular-nums count, denominator `/ {total}` — EXCEPT Scan/Search which renders a literal `/ —` with no determinate bar (Counter/Denominator rule). Every node carries light + `dark:` classes.
- **Four states + centralized gating:** a parent Alpine `nodes` getter computes `{state(complete/active/idle/disabled), pill(DONE/{N} ACTIVE/READY/WAITING/GATED), pct, reason, label}` for every node from `$store.pipeline`. Color is never the only signal — each state also carries the pill text and a status icon (check/spinner-pulse/lock/clock).
- **Triggers (existing endpoints only):** Metadata→`POST /pipeline/extract-metadata`, Analyze→`POST /pipeline/analyze`, Fingerprint→`POST /pipeline/fingerprint`, Proposals→`POST /pipeline/proposals`; Discovery→`<a #trigger-scan-heading>`; Execute→`<a /proposals/>`; Scan/Scrape/Match display-only. LOCKED `:disabled` predicates (Fingerprint on `discovered`); the disabled button's label IS the LOCKED reason string + an informative `:aria-label`; inline `Couldn't enqueue. Retry.` on transport error.
- **Full-page seeds:** in-place `dag-seed-<key>` paragraphs (mirroring the 35-04 OOB ids) for the 17 per-node keys + the 5 Phase-34 gating keys, so bindings are correct before the first poll tick.
- **`<ol>` fallback:** all 9 stages in topological order; `sm:sr-only` (text equivalent at ≥ sm) + visible at < sm (phone fallback). DOM order of triggers = topological order (Tab traversal).

### Task 3 — Dashboard swap + legacy removal (`dashboard.html`, `stats_bar.html`, deletions)
- `dashboard.html` drops the `processing_card` + `stage_cards` includes and includes `dag_canvas.html` in the `#pipeline-stages` slot; the `#pipeline-stats` 5s-poll div is kept (it still carries the per-node OOB seeds).
- `stats_bar.html` drops the `processing_card` include; the three "files ready" OOB seeds are now hidden (store-write only). Comments in `base.html` + `pipeline.py` that named the removed partials were reworded.
- `git rm` of `stage_cards.html`, `processing_card.html`, and their two dedicated render-test files.

## Verification

- `uv run pytest tests/test_dag_canvas_render.py` — 22 passed (topology / render / gating / integration)
- Full suite: **1715 passed, 0 failures** (with `TEST_DATABASE_URL` → 5433, `MIGRATIONS_TEST_DATABASE_URL` → 5433/phaze_migrations_test, `PHAZE_REDIS_URL` → 6380). The 11 migration failures + Redis errors seen on the first run were purely env-config (wrong DB/Redis ports) and pass once the URLs are set.
- `uv run mypy .` — clean (150 source files); `uv run ruff check .` — clean; `pre-commit run --files <touched>` — all hooks pass.
- Acceptance greps: both legacy partials removed; `grep -rc "stage_cards.html\|processing_card.html" src/phaze/templates src/phaze/routers` == 0; `grep -c dag_canvas.html dashboard.html` == 1; exactly 4 `hx-post="/pipeline/` targets; Fingerprint gate reads `discovered`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Corrected the worktree base to the wave-2-merged commit**
- **Found during:** Task 3 (integration tests referenced a `dag` context that did not exist in the worktree).
- **Issue:** The agent-startup branch check's `git reset --hard 2e2802fc` (the canonical wave-2 base carrying 35-04) was not applied, so the worktree sat on `30cf158` (v4.0.11) which lacks 35-04's store extension, `_build_dag_context`, and the `dag-seed` OOB seeds. Tasks 1–2 were built on that wrong base.
- **Fix:** Saved the two new files, `git reset --hard 2e2802fc55…` to the declared base (which has 35-04), restored the files, re-ran tests (green), and re-committed.
- **Commit:** a576a14 (re-applied on the correct base).

**2. [Rule 3 - Blocking] Discovery 'Rescan Files' is an anchor to the existing scan card, not a POST**
- **Issue:** The plan/UI-SPEC describe Discovery → "existing scan trigger (POST scan)", but `/pipeline/scans` requires `agent_id` + path form fields — a meaningful one-click POST is impossible and a net-new endpoint is out of scope.
- **Fix:** Discovery's Rescan is a real focusable `<a href="#trigger-scan-heading">` that jumps to the already-rendered Trigger Scan card. Honors "no net-new endpoint", stays always-actionable (root).

**3. [Rule 1 - Test contract] Updated the store-driven `:disabled` test to the canvas binding**
- **Issue:** `test_button_disabled_binds_to_store_not_frozen_literal` asserted the old `stage_cards` inline `:disabled="loading || $store.pipeline.discovered === 0 || …"` strings; the DAG canvas binds via the centralized `nodes.<node>.blocked` getter.
- **Fix:** Re-pointed the assertions at `:disabled="loading || nodes.analyze.blocked"` / `nodes.proposals.blocked` plus the store-reading gate predicates, and the no-frozen-literal guard. D-01 consequence of replacing `stage_cards`.

### Other notes
- **Scan/Search display-only:** confirmed only a per-file `/tracklists/scan` (file_ids form) exists — no bulk pipeline-level tracklist endpoint — so the UI-SPEC conditional resolves to display-only and the `Scan Tracklists` label is omitted (as specified).
- **Tasks 1 + 2 share one commit (a576a14):** the original separate Task-1 commit aborted when a pre-commit reformat modified files; the content was then squashed into the Task-2 stage, and the base-correction reset consolidated both onto the correct base in a single commit.

## Known Stubs

None. Scrape, Match (per-tracklist triggers live on `/tracklists`) and Scan/Search (no bulk endpoint) are **display-only by design per the UI-SPEC**, not stubbed data — they bind to live DB-truth counts from the 35-04 `dag` context like every other node.

## Threat Flags

None new. Triggers POST ONLY to the existing Phase-34 endpoints and the Execute node merely navigates to `/proposals/` — no net-new unauthenticated trigger surface (T-35-10 mitigated). All dynamic values are server-computed ints rendered through Jinja autoescape into numeric `x-init` store writes; the SVG `d` strings derive from a server-side layout map, not user input (T-35-11 mitigated).

## Self-Check: PASSED

- FOUND: src/phaze/templates/pipeline/partials/dag_canvas.html
- FOUND: tests/test_dag_canvas_render.py
- FOUND: src/phaze/templates/pipeline/dashboard.html (dag_canvas included; legacy includes removed)
- REMOVED: stage_cards.html, processing_card.html (+ their render tests)
- FOUND commits: a576a14 (dag_canvas Tasks 1+2), 0270bbc (dashboard swap + legacy removal, Task 3)
