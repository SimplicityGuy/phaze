# Phase 85: EXECUTED-Gate Revival - Context

**Gathered:** 2026-07-10
**Status:** Ready for planning

<domain>
## Phase Boundary

Revive the ~18 dead `FileRecord.state == FileState.EXECUTED` gates so they fire against a
real *apply-outcome* predicate — `applied(f)` — instead of a `FileState` value that **nothing in
`src/` ever writes**. This turns the operator-facing **tag-writing and CUE-writing paths live for the
first time** (they are currently always-empty and de-facto dead), and does so **without reading the
retiring `FileRecord.state` column**.

Delivers requirement **READ-05**. Own PR, live-UAT-worthy, **not bundled** (per ROADMAP).

**In scope:** the `applied()` predicate (+ its module placement), swapping all `state==EXECUTED`
gate/reader/count/guard sites (functional gates + the UI badge) to `applied()`, and a pagination
guard on the now-populating unbounded operator lists.

**Out of scope (own phases):** deleting the `FileRecord.state` / `_TERMINAL_FILE_STATES` cascade and
making `proposals.status` sole authority (Phase 86); the broader Operator-UI stage-matrix/retry work
(Phase 87); dropping the `state` column / enum (Phase 90).
</domain>

<decisions>
## Implementation Decisions

### D-01: `applied()` predicate — source of truth
- **`applied(f)` ≡ `proposals.status == 'executed'`.** Chosen over `execution_log.status=='completed'`
  EXISTS and over the AND-of-both.
- **Rationale:** `ProposalStatus` (`models/proposal.py:20`) has exactly one success terminal —
  `EXECUTED = "executed"` (`APPROVED → EXECUTED` on successful apply, `APPROVED → FAILED` otherwise).
  It is a single, `file.state`-free predicate, and it is the **exact column Phase 86 promotes to sole
  authority** — so Phase 85's predicate and Phase 86's cutover align. `execution_log` is a per-operation
  **audit log** (keyed by `proposal_id`, potentially multiple rows) — more granular than a gate needs.
- **Placement:** an `applied()` helper in `services/stage_status.py` (the DB-reading service), mirroring
  the Phase 78 pattern (DB-free predicate in `enums/`, DB reader in `services/stage_status.py`). Provide
  both a reusable **SQL fragment/predicate** (for the `WHERE`-clause readers) and a **per-record Python
  helper** (for the single-file write guards in `tag_writer.py:185`, `cue.py:251`, `tags.py:336`).
- **Must NOT read `FileRecord.state`.**

### D-02: Which apply-outcomes count (edge semantics)
- **UNCHANGED files ARE included; FAILED files are excluded — both fall out of D-01 for free.**
  The file-level `MOVED` vs `UNCHANGED` distinction lives only in `file.state`; at the proposal layer
  both collapse to `proposals.status == 'executed'`. An unchanged-path file is still applied and its
  tags/CUE are legitimately writable. `FAILED` apply → `proposals.status == 'failed'` → excluded.
- **Restricting to MOVED-only is explicitly rejected** — it would force a `file.state` read, defeating
  the phase's purpose.
- **Idempotency:** re-writing already-written files is already de-duped by the existing
  `completed_subq` anti-join (`review.py:422`, `tags.py:422` exclude `TagWriteLog.status == COMPLETED`).
  Preserve that; do not re-introduce state-based de-dupe.

### D-03: Retroactive activation & rollout
- **Ship live, no feature flag.** Tag/CUE writing is **operator-triggered** (the routes only *list*
  applied files; the operator clicks to write — nothing auto-writes), so flipping the gate is a
  **display-only** change in risk. Surfacing the previously-invisible applied-file backlog IS the
  intended fix ("fixes the permanently-dead tag-writer path").
- **Add a pagination / LIMIT guard** to the now-populating **unbounded** list queries — at minimum
  `services/review.py:422` (`_records-needing-tag-write`) and `routers/tags.py:174` — so a large
  applied backlog on the live corpus doesn't blow up the view at 200K scale. Match existing pagination
  idiom in the codebase.
- **Live-UAT (this phase, deployment-gated):** after cutover the Tags/Cue operator lists populate with
  real applied files, and a single manual tag-write completes successfully end-to-end.

### D-04: Scope of the swap — uniform, including the UI badge
- **Uniform swap across all ~18 sites** to the shared `applied()` predicate — no `FileState.EXECUTED`
  reader survives Phase 85.
- **Includes the UI badge** `templates/proposals/partials/proposal_row.html:46` (currently
  `file.state == "executed"`): derive the "Executed/Applied" badge from `applied()` (pass the derived
  flag into the template context). This deliberately does **not** leave a stray `file.state` reader for
  Phase 90 to trip over. Deferring the badge to Phase 87 was rejected for that reason.

### Claude's Discretion
- Exact name/signature of the `applied()` helper and its SQL-fragment form (align with existing
  `stage_status.py` conventions).
- The precise pagination page-size / parameterization on the unbounded lists (follow in-tree idiom).
- Badge label wording ("Executed" vs "Applied") — cosmetic.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone design & requirement
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` §3 (mapping table, line 228: apply outcome →
  `execution_log` + `proposals.status`), §4.1 item 2 (the dead-tag-writer diagnosis), §7 (call-site
  inventory — "Dead `EXECUTED` gates" line 380) — the authoritative scope source for this phase.
- `.planning/REQUIREMENTS.md` — **READ-05** (line 50), the requirement this phase closes.
- `.planning/ROADMAP.md` — Phase 85 entry (own PR, live-UAT-worthy, not bundled).

### Data model (predicate source)
- `src/phaze/models/proposal.py:20` — `ProposalStatus` enum (`EXECUTED = "executed"`, `FAILED`).
- `src/phaze/models/execution.py` / `src/phaze/enums/execution.py` — `execution_log` + `ExecutionStatus`
  (the rejected alternative predicate source; read to understand why proposals.status was chosen).
- `src/phaze/routers/agent_proposals.py:~115` — the apply path (sets `proposals.status` + `file.state`).

### Gate call-sites to swap (18)
- `src/phaze/services/tag_writer.py:185` (write guard) · `src/phaze/services/review.py:109,251`
- `src/phaze/routers/tags.py:44,174,179,336,422` · `src/phaze/routers/cue.py:48,89,251`
- `src/phaze/routers/tracklists.py:138,600,897`
- `src/phaze/templates/proposals/partials/proposal_row.html:46` (UI badge)

### Prior-phase pattern
- `src/phaze/services/stage_status.py` + `src/phaze/enums/stage.py` — the Phase 78 derivation pattern
  `applied()` should follow (DB-free predicate + DB-reading service).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `services/stage_status.py` — the home for `applied()`; already the established derived-predicate
  service (Phase 78+). Provides the SQL-fragment + Python-helper duality other cutover phases used.
- Existing `completed_subq` anti-joins (`review.py:422`, `tags.py:422`) already handle
  "don't re-offer already-tag-written files" — reuse, don't replace.
- `execute_tag_write` (`services/tag_writer.py:165`) is already wired to the operator route
  (`routers/tags.py:22`). The **trigger is not dead — only the gate is.** Reviving the gate makes the
  existing trigger observable; no new trigger machinery is needed.

### Established Patterns
- Phase-78 derivation: DB-free predicate in `enums/`, DB reader in `services/stage_status.py`.
- Mutating routers must `await session.commit()` themselves (memory: get_session NEVER commits).
  The tags/cue routers mutate — confirm they commit (they are existing callers, but verify after edits).

### Integration Points
- The `applied()` predicate is consumed as a `WHERE` fragment by list/count readers and as a per-record
  boolean by write guards. It joins `files` → `proposals` on `proposals.file_id`.
- Phase 86 immediately builds on this by making `proposals.status` the sole authority — keep the
  predicate expressed purely over `proposals` so Phase 86 needs no rework.

</code_context>

<specifics>
## Specific Ideas

- Predicate literally `proposals.status == 'executed'` — no `file.state`, no `execution_log`.
- Pagination guard specifically targets `review.py:422` and `tags.py:174` (the unbounded list queries
  the operator now hits with a real backlog).
- Badge derives from `applied()` and is fixed **in this phase**, not deferred.

</specifics>

<deferred>
## Deferred Ideas

- Making `proposals.status` the **sole** authority and deleting the `FileRecord.state` /
  `_TERMINAL_FILE_STATES` cascade → **Phase 86** (SIDECAR-03). Phase 85 only stops *reading* the
  EXECUTED state value; it does not remove the writer/cascade.
- Broader operator visibility of applied/tag-write status (stage matrix, retry, "why not eligible")
  → **Phase 87** (UI-01..05).
- Dropping `files.state` / the `FileState` enum → **Phase 90** (MIG-04).

### Reviewed Todos (not folded)
- `analysis-completed-at-backfill.md` — *"analyzed ⇒ analysis_completed_at — 1001 production rows will
  fail the shadow gate"* (matched score 0.6). **Not folded:** it belongs to the **analyze
  shadow-compare** (Phase 79) / destructive-migration (Phase 90) work, not the EXECUTED/apply gate — it
  matched on generic keywords (gate/phase/uat), not on this phase's subject matter.

</deferred>

---

*Phase: 85-executed-gate-revival*
*Context gathered: 2026-07-10*
