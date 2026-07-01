# Phase 60: Review & Apply - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-30
**Phase:** 60-review-apply
**Areas discussed:** Propose node ownership, Bulk approve-high-confidence predicate + threshold, Tag-write bulk predicate, Audit/reversibility surface, Propose node render mechanics, Edit action semantics

---

## Propose node ownership (scope edge)

| Option | Description | Selected |
|--------|-------------|----------|
| Fold into Rename/Path | Propose = generate, Rename/Path = review the same RenameProposal rows | ✓ |
| Own it as its own workspace | Distinct Propose workspace + the 5 Review workspaces (6 stages) | |
| Out of scope — flag gap | Leave `propose` placeholder; risk broken rail node at ship | |

**User's choice:** Fold into Rename/Path
**Notes:** `propose` is a DAG rail node (SHELL-02) still on `_STAGE_PLACEHOLDER`, owned by no phase's goal. Folding keeps the gate coherent — both derive from `RenameProposal`.

---

## Bulk "approve all high-confidence" predicate + threshold (REVIEW-02)

| Option | Description | Selected |
|--------|-------------|----------|
| New server-predicate endpoint, conf ≥ 0.9 | Mirror reject-low; server re-queries RenameProposal.confidence ≥ 0.9 pending rows | ✓ |
| New endpoint, conf ≥ 0.8 | Same endpoint, more permissive 0.8 threshold | |
| Reuse /proposals/bulk as-is | Client id-list — IS the REVIEW-02 stale-bulk hazard | |

**User's choice:** New server-predicate endpoint, conf ≥ 0.9
**Notes:** Existing `/proposals/bulk` takes a client `proposal_ids` list (the hazard). 0.9 is conservative for an irreplaceable archive. Sanctioned backend addition — thin route over unchanged logic.

---

## Tag-write bulk predicate (no confidence field) (REVIEW-02)

| Option | Description | Selected |
|--------|-------------|----------|
| No-discrepancies = high-conf | Bulk-approve pending tag writes with zero discrepancies; discrepancy rows per-file only | ✓ |
| Exclude tags from bulk | Tag-write per-file only; no bulk | |
| Bulk-approve all pending | Single "write all pending" with no predicate | |

**User's choice:** No-discrepancies = high-conf
**Notes:** Tag-write is computed per-file (compute_proposed_tags + _build_comparison) with a discrepancies notion; no confidence score. No-discrepancies is the natural analog to a confidence gate.

---

## Audit / reversibility surface (REVIEW-05)

| Option | Description | Selected |
|--------|-------------|----------|
| Per-stage undo + reuse /audit | Per-workspace undo over existing endpoints; existing /audit page as unified log | ✓ |
| New unified Audit rail node now | Build below-the-line Audit workspace this phase | |
| You decide (planner discretion) | Lock only audit-row-per-apply + reversibility | |

**User's choice:** Per-stage undo + reuse /audit
**Notes:** Two audit tables (ExecutionLog, TagWriteLog) + duplicates undo + `/audit/` view already exist. Honors no-logic-change; the below-the-line Audit rail node is deferred to a later phase.

---

## Propose node render mechanics

| Option | Description | Selected |
|--------|-------------|----------|
| Light Propose workspace + Rename review | Propose = thin generation view (model/confidence + Generate ALL); Rename = review diff; both superseded, one source | ✓ |
| Alias propose → rename workspace | `propose` node renders the same Rename workspace fragment | |

**User's choice:** Light Propose workspace + Rename review
**Notes:** `/proposals/` already redirects to `/s/propose`; SHELL-02 lists Propose as a distinct node with a live count, so it gets a real (thin) fragment, not an alias.

---

## Edit action semantics (REVIEW-01)

| Option | Description | Selected |
|--------|-------------|----------|
| Inline edit PATCHes the proposal | New PATCH updates proposed_filename/path, re-derives target, before approve | ✓ |
| Edit = Skip + manual regenerate | No edit endpoint; Edit reduces to Skip | |
| You decide (planner discretion) | Let planner choose | |

**User's choice:** Inline edit PATCHes the proposal
**Notes:** No existing edit-proposal endpoint (only approve/reject/undo). Mirrors the status-PATCH pattern; edits the persisted row, does not re-run the LLM. Second sanctioned backend addition.

## Claude's Discretion

- Exact OOB id additions (single-poll, counts-only on the diff list).
- Rename/Move as two rail nodes over one source vs. one toggled workspace.
- "Skip" → existing `reject` status vs. a distinct skip status.
- Empty-state / trigger-response copy (locked by the 60 UI-SPEC).
- Reuse/restyle legacy partials vs. fresh fragments.
- Failed-apply error_message surfacing.

## Deferred Ideas

- REVIEW-06 — per-stage configurable thresholds + override UI (v7.0 ships fixed thresholds).
- Unified below-the-line Audit rail node — reuse existing `/audit/` this phase.
- Row-click → rich per-file record slide-in — Phase 61 (RECORD-01).
- Configurable Dedupe keeper-quality heuristics beyond existing auto-keep-highest-quality.
