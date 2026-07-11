# Phase 87: Operator UI — Stage Matrix, Failure Retry, Eligibility Trace & Priority - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-11
**Phase:** 87-Operator UI — Stage Matrix, Failure Retry, Eligibility Trace & Priority
**Areas discussed:** Stage matrix form & home, Failure + orphan surfacing / retry, "Why not eligible?" trace, Force-done / skip escape hatch, Priority stepper (default)

---

## Stage matrix form & home (UI-01)

### Matrix visual form

| Option | Description | Selected |
|--------|-------------|----------|
| Row of labeled pills | 6 labeled pills in stage order, colored by bucket; self-documenting, wraps; reuses pill tokens | ✓ |
| Compact colored dots | 6 unlabeled dots + legend/tooltip; densest, least self-evident | |
| Segmented progress bar | One bar split into 6 colored segments; reads as progress, weak for pinpointing a stage | |

**User's choice:** Row of labeled pills — `[Meta ✓][FP ●][Analyze —][Prop —][Appr —][Exec —]`.

### Matrix home surface

| Option | Description | Selected |
|--------|-------------|----------|
| Dedicated paginated files table | New browsable paginated list, row = path + matrix | |
| Per-file right pane only | Matrix in the existing right pane; no new table, no scannable overview | |
| Both: table + right pane | Matrix in a paginated table for scanning + expanded (trace + force-done) in right pane | ✓ |

**User's choice:** Both — paginated files table for scanning, expanded matrix (with trace + force-done) in the right pane.

---

## Failure + orphan surfacing / retry (UI-02, UI-05)

### Where failed files surface

| Option | Description | Selected |
|--------|-------------|----------|
| Filter on the new files table | Status filter ("analyze = failed") on the same paginated table | ✓ |
| 'Failed' tab per enrich workspace | Per-stage Failed tab next to that stage's triggers; three places | |
| Dedicated failures view | One aggregated Failures page; diverges from stage-centric nav | |

**User's choice:** Filter on the new files table.

### Retry granularity

| Option | Description | Selected |
|--------|-------------|----------|
| Both per-file and bulk-per-stage | Per-file retry button + "retry all failed in stage" bulk action | ✓ |
| Per-file only | One retry button per failed file; tedious at scale | |
| Bulk-per-stage only | One "retry all failed" per stage; no surgical control | |

**User's choice:** Both per-file and bulk-per-stage.

### Orphaned/stuck-work count placement

| Option | Description | Selected |
|--------|-------------|----------|
| DAG rail badge | Count badge on the rail near the affected stage; ambient, always visible | ✓ |
| Header status strip | In the global header alongside agent health; reads as system-health | |
| Card in Analyze workspace | Contextual card near recover_orphaned_work; only visible on that page | |

**User's choice:** DAG rail badge.

---

## "Why not eligible?" trace (UI-03)

### Trace trigger / display

| Option | Description | Selected |
|--------|-------------|----------|
| Per-stage in the right pane | Click a stage pill → that stage's trace; ties diagnostic to the pill | ✓ |
| Always-on trace block in pane | Full per-stage trace always rendered; densest | |
| Hover tooltip on the pill | Hover → conjunct checklist; cramped, poor on touch | |

**User's choice:** Per-stage, in the right pane.

### Trace depth

| Option | Description | Selected |
|--------|-------------|----------|
| Named conjuncts + the blocker | Pass/fail per conjunct + names the specific unmet upstream blocker | ✓ |
| Plain conjunct checklist | Four pass/fail lines, no upstream naming | |
| One-line verdict | Single computed sentence; hides the conjunct breakdown | |

**User's choice:** Named conjuncts + the blocker.

---

## Force-done / skip escape hatch (UI-04)

### Semantics — what it writes

| Option | Description | Selected |
|--------|-------------|----------|
| Distinct 'skipped' marker | Per-stage skip marker; stage-satisfied for eligibility but reported as a distinct 'skipped' pill; honest, derived | ✓ |
| Force real 'done' | Synthesize an actual output row; indistinguishable from real completion; counterfeits data | |
| You decide | Let planning pick, constrained to derived + distinguishable | |

**User's choice:** Distinct 'skipped' marker.
**Notes:** Chosen for honesty — a forced-skip must always be distinguishable from genuine completion. Implies a new per-stage marker + migration + derivation read + DERIV-04 harness extension (CONTEXT D-13).

### Guard

| Option | Description | Selected |
|--------|-------------|----------|
| Confirm dialog + reason note | Per-file, confirm step + recorded free-text reason (audit trail) | ✓ |
| Confirm dialog only | Per-file, single confirm; no reason | |
| One-click, no confirm | Immediate; easy to misfire | |

**User's choice:** Confirm dialog + reason note.

### Scope — which stages

| Option | Description | Selected |
|--------|-------------|----------|
| Enrich stages only | metadata / fingerprint / analyze — the stages that strand files | ✓ |
| All six stages | Includes propose/approve/execute; approval-bypass hazard | |
| Analyze only | Narrowest; excludes metadata/fingerprint wedges | |

**User's choice:** Enrich stages only.
**Notes:** Downstream propose/approve/execute deliberately excluded — skipping human-approval stages would move/rename files without review, violating the core "nothing moves without approval" value.

---

## Priority stepper (PRIO-01) — captured as default

| Option | Description | Selected |
|--------|-------------|----------|
| Good as-is | Priority stepper + pause/resume on the DAG rail, clarified labeling, re-wire to live endpoints | ✓ |
| Priority only, no pause/resume | Re-wire only the stepper; leave pause/resume orphaned | |
| Discuss it | Open a discussion on placement/labeling/pause-resume | |

**User's choice:** Good as-is — bring back both the priority stepper and pause/resume on the DAG rail, wired to the still-live `POST /pipeline/stages/{stage}/{priority,pause,resume}` endpoints, with a clarifying label ("▲ raises priority = lowers the number").

---

## Claude's Discretion

- Files-table default scope + filter set + pagination style (keyset vs offset), constrained to never scan the whole corpus per poll.
- Pill labels & 4-bucket color tokens; the distinct `skipped` pill visual treatment.
- Right-pane layout composition (expanded matrix + per-stage trace + force-skip controls).
- Retry response partials (reuse vs new) and bulk-vs-per-file HTMX affordances.
- Plan/PR decomposition seams.

## Deferred Ideas

- Lane / agent drill-in views → Phase 88 (DRILL-01..03).
- `files.state` column drop + `FileState` enum deletion → Phase 90.
- DENORM-01 denormalized stage-bitmap column → only if a poll-time measurement proves the derived query too slow (carried from Phase 82).
- Reviewed-not-folded todos: `analysis-completed-at-backfill.md` (resolved upstream by Phase 80's `036`); `wr-01-review-builder-limit-before-filter.md` (tag/CUE bulk-builder bug, different code path).
