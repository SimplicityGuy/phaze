# Phase 58: Enrich + Analyze workspaces - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-29
**Phase:** 58-enrich-analyze-workspaces
**Areas discussed:** Metadata/Fingerprint trigger granularity, Analyze table scope + windowed-progress, Lane-card degraded/offline states, Row-click → record pane wiring (R-1)

---

## Metadata / Fingerprint trigger granularity (WORK-02)

| Option | Description | Selected |
|--------|-------------|----------|
| ALL-only (honor no-backend-change) | Ship only EXTRACT ALL / FINGERPRINT ALL wired to the existing endpoints verbatim; drop EXTRACT SELECTED. Zero backend change. | ✓ |
| Add per-file selection (extend endpoint) | Row checkboxes; EXTRACT SELECTED POSTs a file-id list to an extended/new endpoint. Touches backend, bends the no-backend-change rule. | |
| ALL-only now, note selection for later | Same as ALL-only but explicitly park selection as a deferred idea. | |

**User's choice:** ALL-only (honor no-backend-change)
**Notes:** Existing `POST /pipeline/extract-metadata` enqueues all metadata-pending files (no subset param). Consequence captured in CONTEXT D-02: the approved UI-SPEC's `EXTRACT SELECTED` button + row-checkboxes are consciously cut; planner adds a one-line UI-SPEC reconciliation note and selection is deferred. (Effectively the "ALL-only, note for later" outcome via the Deferred Ideas section.)

---

## Analyze table scope + windowed-progress (WORK-04) — Q1: list scope/grouping

| Option | Description | Selected |
|--------|-------------|----------|
| In-flight only, one table + lane badge | Single table of currently-running files, each with a lane badge. Tightest read of WORK-04. | |
| All in-stage, one table + status+lane | One table of every Analyze-stage file (queued/running/awaiting-cloud/done) with status column + lane badge. | ✓ |
| Grouped per-lane under each card | Each lane card owns its own list (3 mini-tables). Triples markup, complicates poll fanout. | |

**User's choice:** All in-stage, one table + status+lane
**Notes:** Fuller operational picture; keeps the single-poll OOB fanout to one table fragment.

## Analyze table scope + windowed-progress (WORK-04) — Q2: progress rendering

| Option | Description | Selected |
|--------|-------------|----------|
| Simple % / windows-done bar | Compact bar / N/M windows from analysis_window rows (Phase 31). No SVG. | ✓ |
| Live BPM sparkline (reuse Phase 31/44) | Inline BPM sparkline per row. More informative, heavier markup/poll. | |
| Just a status word + window count | Text only ('analyzing — 14/32 windows'). Leanest, may feel thin. | |

**User's choice:** Simple % / windows-done bar
**Notes:** Rich multi-lane windowed timeline stays in the Phase 61 full record.

---

## Lane-card degraded/offline states (WORK-03)

| Option | Description | Selected |
|--------|-------------|----------|
| Always show 3, label the state | All 3 cards always present; unavailable lane greyed with 'offline' / 'not configured' + 0 capacity. | ✓ |
| Show only active lanes | Render only configured/online lanes; hide the rest. Layout shifts as lanes come/go. | |
| Always 3, but distinguish down vs unconfigured | All 3 shown with a deliberate visual/copy split between 'offline' (recoverable) and 'not configured'. | |

**User's choice:** Always show 3, label the state
**Notes:** Stable topology view. The finer down-vs-unconfigured copy split is left as planner discretion (acceptable, not required).

---

## Row-click → record pane wiring (UI-SPEC R-1)

| Option | Description | Selected |
|--------|-------------|----------|
| Inert-but-present (strict R-1) | Rows have the stable target + hover affordance, but click is unbound. Phase 61 wires the record. | ✓ |
| Wire click → lightweight selected-state | Click sets a visible selected state + optional placeholder pane. Pre-builds a seam Phase 61 may redesign. | |
| Defer entirely — no clickable rows | Plain rows, no target/selection. Contradicts UI-SPEC R-1; needs reconciliation. | |

**User's choice:** Inert-but-present (strict R-1)
**Notes:** No record/pane work leaks into Phase 58; click wired wholly in Phase 61 (RECORD-01). Combined with the dropped checkboxes (D-02), Phase 58 has no row-selection state at all.

---

## Claude's Discretion

- Discover "recent scans" surface — reuse existing `recent_scans_table.html` restyled to the C3 workspace table.
- Reuse of v6.0 cloud-state partials vs. fresh markup for lane sub-states.
- Exact OOB id additions (must ride the single `/pipeline/stats` poll, no second loop).
- Empty-state / trigger-response wiring detail (UI-SPEC locks the copy).
- Whether to copy-distinguish lane `offline` vs `not configured`.

## Deferred Ideas

- Per-file selection for Metadata/Fingerprint (EXTRACT SELECTED + checkboxes + subset endpoint) — future enhancement, needs backend change.
- Inline BPM sparkline / multi-lane windowed timeline per file — Phase 61 full record.
- Row-click → rich per-file record/pane — Phase 61 (RECORD-01).
- WORK-06 (cloud_phase admission-state sub-states) — future/deferred requirement.
