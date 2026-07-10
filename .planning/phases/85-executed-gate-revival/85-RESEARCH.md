# Phase 85: EXECUTED-Gate Revival - Research

**Researched:** 2026-07-10
**Domain:** Derived-predicate cutover — reviving dead `FileState.EXECUTED` gates against `proposals.status=='executed'` (READ-05)
**Confidence:** HIGH (all findings verified against live source on branch `SimplicityGuy/phase-85`)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01: `applied(f)` ≡ `proposals.status == 'executed'`** — chosen over the `execution_log.status=='completed'` EXISTS and over the AND-of-both. Placement: an `applied()` helper in `services/stage_status.py` (DB-reading service), mirroring the Phase-78 pattern. Provide BOTH a reusable SQL fragment/predicate (for `WHERE`-clause readers) AND a per-record Python helper (for single-file write guards). **Must NOT read `FileRecord.state`.**
- **D-02: UNCHANGED files ARE included; FAILED files are excluded** — both fall out of D-01 for free. `MOVED` vs `UNCHANGED` distinction lives only in `file.state`; at the proposal layer both collapse to `proposals.status=='executed'`. Idempotency preserved via the existing `completed_subq` anti-join (do NOT re-introduce state-based de-dupe).
- **D-03: Ship live, no feature flag.** Tag/CUE writing is operator-triggered (routes only *list*; operator clicks to write). Add a pagination/LIMIT guard to the now-populating unbounded list queries. Live-UAT (deployment-gated): lists populate with real applied files, one manual tag-write completes end-to-end.
- **D-04: Uniform swap across all ~18 sites**, including the UI badge (`proposal_row.html:46`). No `FileState.EXECUTED` reader survives Phase 85. Badge derives from `applied()`.

### Claude's Discretion
- Exact name/signature of the `applied()` helper and its SQL-fragment form (align with existing `stage_status.py` conventions).
- The precise pagination page-size / parameterization (follow in-tree idiom).
- Badge label wording ("Executed" vs "Applied") — cosmetic.

### Deferred Ideas (OUT OF SCOPE)
- Making `proposals.status` the **sole** authority + deleting `FileRecord.state` / `_TERMINAL_FILE_STATES` cascade → **Phase 86** (SIDECAR-03).
- Broader operator visibility (stage matrix, retry, "why not eligible") → **Phase 87** (UI-01..05).
- Dropping `files.state` / `FileState` enum → **Phase 90** (MIG-04).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| READ-05 | The dead `state == EXECUTED` gates are revived against the real apply-outcome source — tag writing, review, tags/cue/tracklists guards fire for actually-applied files. | Root cause verified (§Central Tension): the live apply path writes `file.state='moved'` + `proposals.status='executed'` in one transaction; the ~15 gates check `state=='executed'`, which **no writer ever produces** → they match the empty set. Swapping to `applied() ≡ proposals.status=='executed'` (D-01) revives them. Exhaustive call-site inventory + placement + pagination all documented below. |
</phase_requirements>

## Summary

Every `FileRecord.state == FileState.EXECUTED` gate in the tag-write / CUE / review / tracklists paths is **permanently dead** for one concrete reason: the live apply path (`tasks/execution.py` → `routers/agent_proposals.py::patch_proposal_state`) writes `file.state = 'moved'` (or `'unchanged'`) and `proposals.status = 'executed'` **jointly in one transaction** — it **never** writes `file.state = 'executed'`. A repo-wide grep confirms **zero** writers of `state='executed'`. So gates comparing `state == 'executed'` filter an empty set, and the operator Tag-write/CUE lists are always empty. The revival swaps these gates to `applied(f) ≡ proposals.status == 'executed'` — the exact column that IS populated on a successful apply, in the same transaction that updates `file.current_path` (the path the tag/CUE writers act on).

The central research question — is `proposals.status=='executed'` (D-01) sufficient/equivalent to the ROADMAP SC#1's `execution_log`-join framing? — resolves **decisively in favor of D-01**. `proposals.status=='executed'` is not merely sufficient; it is **more correct** than the `execution_log` join for this gate, because it is transactionally coupled to `current_path`, while the `execution_log` COMPLETED patch is a separate, best-effort HTTP call whose failure is swallowed. The milestone's own Phase-79 shadow-compare (`services/shadow_compare.py:136`) already encodes this with an explicit comment: *"Proposal-status apply-outcome states (raw exists on proposals.status, NEVER execution_log — RESEARCH A1)"* and invariant #16 `MOVED ↔ proposals.status=='executed'`. **CONTEXT D-01 overrides ROADMAP SC#1.**

**Primary recommendation:** Add an `applied_clause()` (SQL `ColumnElement[bool]`, correlated `exists` over `proposals`, no `stage` arg — a file-level sibling of the existing `dedup_resolved_clause()`) plus an async per-record helper `is_applied(session, file_id) -> bool` in `services/stage_status.py`. Swap all 15 code sites (5 WHERE/count readers, 6 per-record guards, the UI badge, 3 tracklists guards — enumerated below). Add a bounding LIMIT/pagination to the two now-populating unbounded `services/review.py` list builders. `routers/tags.py::list_tags` is **already paginated** — verify, do not re-add.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| `applied()` predicate (SQL fragment + per-record) | API / service layer (`services/stage_status.py`) | — | Established derived-predicate home (Phase 78); consumed by both `.where(...)` readers and per-record guards |
| Apply-outcome authority (`proposals.status`) | Database / persistence | — | Written atomically with `file.current_path` by `agent_proposals.py` |
| Tag/CUE list rendering (paginated) | Frontend server (SSR — Jinja2/HTMX partials) | API/service | v7.0 shell workspaces; degrade-safe SAVEPOINT reads |
| UI badge derivation | Frontend server (template) | — | `proposal_row.html` reads `proposal.status` directly |
| Filesystem mutation (tag write / CUE write) | Operator-triggered API route → service | — | Already wired (`execute_tag_write`, `write_cue_file`); only the gate was dead |

## Central Tension RESOLVED: `proposals.status` vs `execution_log`

**Conclusion (HIGH confidence): `proposals.status == 'executed'` alone is correct, sufficient, and superior to the `execution_log` join for this gate. Use D-01. The two predicates are NOT strictly equivalent, and where they diverge, `proposals.status` is the more correct one.**

### Evidence — the live apply path

`tasks/execution.py::_execute_one` (agent side) executes per proposal in this order `[VERIFIED: src/phaze/tasks/execution.py:139-237]`:
1. POST `execution_log` status=`in_progress` (best-effort; exception swallowed → WARNING only).
2. **copy → verify → delete** (the actual filesystem mutation).
3. PATCH `execution_log` status=`completed` (best-effort; **exception swallowed → WARNING only**, lines 189-202).
4. PATCH `proposals/{id}/state` → `proposal_state="executed", file_state="moved", current_path=<proposed>` (**NOT individually wrapped** in the success branch; if it raises, control falls to the `except` which sets `proposals.status='failed'`).

The server handler `routers/agent_proposals.py::patch_proposal_state` `[VERIFIED: src/phaze/routers/agent_proposals.py:105-125]` applies the joint mutation in **one transaction, one commit**:
```python
proposal.status = new.value                    # 'executed'
...
file_record.state = new_file_state.value       # 'moved'  (via _FILE_FOLLOW: EXECUTED -> FileState.MOVED)
file_record.current_path = body.current_path   # the applied destination path
await session.commit()                          # single atomic commit
```

### Can `proposals.status=='executed'` exist when the file op actually FAILED?
**No.** The filesystem copy+delete (step 2) happens *before* the proposal PATCH (step 4). Any IO failure in step 2 raises → the `except` branch (lines 238-308) PATCHes `proposal_state='failed'`. There is **no path** where the copy/delete failed but the proposal reaches `'executed'`. `ProposalStatus` has exactly one success terminal (`EXECUTED`); ladder is `APPROVED → {EXECUTED | FAILED}` `[VERIFIED: src/phaze/models/proposal.py:20-34, routers/agent_proposals.py:41-43]`. EXECUTED is **not** reachable without a successful apply.

### Divergence cases (where the two predicates disagree)
| Case | `proposals.status` | `execution_log` | Reality | Better predicate |
|------|--------------------|-----------------|---------|------------------|
| Step-3 PATCH lost (network blip), step-4 succeeds | `executed` | stuck `in_progress` (never `completed`) | File WAS applied; `current_path` updated | **proposals** (execution_log false-negative) |
| Step-3 succeeds, step-4 PATCH fails → `except` sets failed | `failed` | `completed` | `current_path` NOT updated; original deleted | **proposals** (execution_log false-positive → tag write would target a stale/deleted path) |

In **both** divergence directions `proposals.status` is correct, because it is the transaction that atomically updates `current_path` — exactly the field `execute_tag_write` (`file_record.current_path`, `[VERIFIED: services/tag_writer.py:189]`) and CUE generation (`Path(file_record.current_path)`, `[VERIFIED: routers/cue.py:273]`) operate on. `execution_log` has **no `file_id`** and is a per-operation audit log keyed by `proposal_id` (potentially multiple rows) — more granular than a gate needs `[VERIFIED: src/phaze/models/execution.py:24-37]`.

### Corroborating evidence — the milestone already decided this
`services/shadow_compare.py:136-143` `[VERIFIED]`:
```python
# --- Proposal-status apply-outcome states (raw exists on proposals.status, NEVER execution_log -- RESEARCH A1).
Invariant("executed", FileState.EXECUTED.value, _proposal_status("executed"), soft=False, ...)  # vacuous in prod
Invariant("moved",    FileState.MOVED.value,    _proposal_status("executed"), soft=False, ...)  # the LIVE apply outcome
```
The shadow-compare's `MOVED ↔ proposals.status=='executed'` invariant (#16) is the meaningful one in production; the `EXECUTED` invariant (#13) is vacuously true because **no row ever has `state=='executed'`**. This independently confirms both the dead-gate root cause and the D-01 choice.

> **Planner note:** ROADMAP SC#1 says `applied(f)` "joins `execution_log` through `proposals`". This is the inferior option. Per task instructions and CONTEXT D-01, implement `applied() ≡ proposals.status=='executed'`. Do **not** reuse the existing `done_clause(Stage.APPLY)` (which uses `execution_log`, `[VERIFIED: services/stage_status.py:136-142]`) — `applied()` is a distinct, new file-level predicate.

## Standard Stack

Zero new dependencies (hard milestone constraint). All work is pure application code over the existing stack: Python 3.14, SQLAlchemy 2.x (`ColumnElement[bool]` / correlated `exists`), FastAPI + Jinja2/HTMX SSR partials, `uv`-managed. See `## Package Legitimacy Audit`.

## Package Legitimacy Audit

**N/A — this phase installs no external packages.** Zero new runtime dependencies (milestone constraint, REQUIREMENTS.md "Out of Scope"). No slopcheck/registry verification required.

## Architecture Patterns

### Data flow (apply → gate → write)
```
agent tasks/execution.py::_execute_one
   copy+verify+delete (filesystem)  ─── success ──▶ PATCH /proposals/{id}/state
                                                          │  (one txn, one commit)
                                                          ▼
                              proposals.status='executed'  +  files.state='moved'  +  files.current_path=<dest>
                                                          │
                        ┌─────────────────────────────────┴───────────────────────────────┐
                        ▼                                                                   ▼
        applied_clause()  (SQL WHERE fragment)                        is_applied(session, file_id) (per-record bool)
        EXISTS(proposals WHERE file_id=files.id AND status='executed')
                        │                                                                   │
      ┌─────────────────┼──────────────────┐                          ┌─────────────────────┼──────────────────┐
      ▼                 ▼                  ▼                           ▼                     ▼                  ▼
 tags list/count   cue list/count   review list builders       tag_writer guard      cue generate guard   tracklists cue-version guards
 (+ LIMIT)         (bounded)        (+ LIMIT, D-03)             (:185)                (:251)               (:138/600/897)
                                                                                                                  │
                                                                                                          UI badge proposal_row.html:46
                                                                                                          → proposal.status == 'executed'
```

### Pattern 1: File-level correlated `exists` clause (mirror `dedup_resolved_clause`)
**What:** A `ColumnElement[bool]` correlating to `FileRecord` via `exists(...)`, taking NO `stage` argument, kept OUT of the `Stage` dispatch ladders (so it does not touch the DERIV-04 equivalence test).
**Source:** `[VERIFIED: services/stage_status.py:91-112]` — `dedup_resolved_clause()` is the exact template:
```python
def dedup_resolved_clause() -> ColumnElement[bool]:
    return exists(select(DedupResolution.id).where(DedupResolution.file_id == FileRecord.id))
```
**Recommended `applied()` SQL form:**
```python
def applied_clause() -> ColumnElement[bool]:
    """READ-05 / D-01: a file is 'applied' iff an executed proposal exists.
    File-level (no Stage arg); NEVER reads FileRecord.state; NEVER execution_log.
    """
    return exists(
        select(RenameProposal.id).where(
            RenameProposal.file_id == FileRecord.id,
            RenameProposal.status == "executed",  # ProposalStatus.EXECUTED.value
        )
    )
```
Usage in a WHERE reader: `.where(applied_clause(), FileRecord.id.not_in(completed_subq))`.

### Pattern 2: Per-record async boolean helper
**What:** The write guards (`tag_writer.py:185`, `tags.py:336`, `cue.py:251`) and the tracklists guards hold a `FileRecord`/`file_id` + `session` but **not** the proposal. They need a scalar EXISTS query:
```python
async def is_applied(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """READ-05 / D-01 per-record twin of applied_clause()."""
    stmt = select(exists(select(RenameProposal.id).where(
        RenameProposal.file_id == file_id,
        RenameProposal.status == "executed",
    )))
    return bool(await session.scalar(stmt))
```
**Note (`files`↔`proposals` relationship):** `proposals.file_id` is a plain FK (not unique). The `uq_proposals_file_id_pending` partial-unique index enforces **one PENDING proposal per file** ONLY `[VERIFIED: models/proposal.py:53-59]`. A file **can** have multiple non-pending proposals (e.g. a FAILED then a re-approved EXECUTED). `exists(status=='executed')` is the correct authoritative test — if **any** proposal for the file is `executed`, the file is applied. This matches the joint-write semantics (the executed proposal is the one that moved the file).

### Pattern 3: UI badge (trivial, no helper needed)
`proposal_row.html:46` currently reads `proposal.file.state == "executed"` (dead — always False; also requires the `lazy="raise"` `proposal.file` relationship). For a proposal row, `applied()` for that proposal IS `proposal.status == 'executed'` — already in template scope `[VERIFIED: templates/proposals/partials/proposal_row.html:46-48]`:
```jinja
{% if proposal.status == "executed" %}   {# was: proposal.file.state == "executed" #}
```

### Anti-Patterns to Avoid
- **Reusing `done_clause(Stage.APPLY)`** — it uses `execution_log` (rejected by D-01). `applied()` is a separate predicate.
- **Adding `applied()` to the `Stage` dispatch ladders / `stage_status_case`** — it is file-level, not a stage; keep it a standalone function like `dedup_resolved_clause()` so it does not perturb the DERIV-04 SQL⇔Python equivalence test.
- **Reading `FileRecord.state` anywhere** — the phase's entire purpose (and Phase 86/90 dependency) is to stop reading it. Express `applied()` purely over `proposals` so Phase 86 (proposals-as-sole-authority) needs zero rework.
- **State-based de-dupe** — keep the existing `completed_subq` `TagWriteLog.status==COMPLETED` anti-join (D-02).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Pagination metadata | A new page/offset struct | `services/proposal_queries.py::Pagination(page, page_size, total)` `[VERIFIED]` | Dataclass with `total_pages/has_prev/has_next/start/end`; already used by tags/duplicates/cue/tracklists routers |
| Correlated file-level predicate | A fresh subquery per call site | `applied_clause()` + `is_applied()` single-sourced in `stage_status.py` | DERIV-01 single-source discipline; the whole milestone's thesis |
| Degrade-safe list read | New try/except in routers | Existing `session.begin_nested()` SAVEPOINT + return `[]` pattern | `get_tagwrite_review_rows` / `get_cue_review_cards` already do this |

## Exhaustive Call-Site Inventory

15 code sites (design's "~18" includes 3 docstring/comment references). **All design-cited line numbers verified present.** No missed site found (full-repo grep of `FileState.EXECUTED` / `state == "executed"`).

| # | File:line | Role | `applied()` form |
|---|-----------|------|------------------|
| 1 | `routers/tags.py:44` | COUNT (stat card `executed_stmt`) | WHERE fragment: `.where(applied_clause())` |
| 2 | `routers/tags.py:174` | LIST WHERE (`list_tags`) — **already paginated** (offset/limit at :185, `Pagination` at :213) | swap WHERE to `applied_clause()`; pagination already present |
| 3 | `routers/tags.py:179` | COUNT (`list_tags` total) | WHERE fragment |
| 4 | `routers/tags.py:336` | Per-record write guard (`write_file_tags`) | `is_applied(session, file_id)` |
| 5 | `routers/tags.py:422` | LIST WHERE (`bulk_write_no_discrepancies`) — **unbounded loop over all qualifying** | WHERE fragment + LIMIT (D-03) |
| 6 | `routers/cue.py:48` | LIST WHERE (`_get_eligible...` eligible set) | WHERE fragment |
| 7 | `routers/cue.py:89` | LIST WHERE (missing-timestamps/gated set) | WHERE fragment |
| 8 | `routers/cue.py:251` | Per-record guard (`generate_cue`) | `is_applied(session, file_id)` (or check loaded proposal) |
| 9 | `routers/tracklists.py:138` | Per-record guard (cue-version, list loop) | `is_applied(...)` |
| 10 | `routers/tracklists.py:600` | Per-record guard (cue-version, single) | `is_applied(...)` |
| 11 | `routers/tracklists.py:897` | Per-record guard (cue-version, list loop) | `is_applied(...)` |
| 12 | `services/review.py:109` | LIST WHERE (`get_tagwrite_review_rows`) — **unbounded** (this is CONTEXT's "review.py:422", stale #) | WHERE fragment + LIMIT (D-03) |
| 13 | `services/review.py:251` | LIST WHERE (`get_cue_review_cards` gated set) — **unbounded** | WHERE fragment + LIMIT (D-03) |
| 14 | `services/tag_writer.py:185` | Per-record write guard (`execute_tag_write`, raises `ValueError`) | `is_applied(session, file_record.id)` |
| 15 | `templates/.../proposal_row.html:46` | UI badge | `proposal.status == "executed"` (in-scope) |

**Do NOT touch (other phases / other states):** `services/proposal.py:39` `_TERMINAL_FILE_STATES` (Phase 86 cascade), `services/pipeline.py:57` (Phase 82/86), `services/shadow_compare.py:139/143` (Phase 79 gate — leave the `moved`/`executed` invariants intact until Phase 90), `agent_proposals.py` (the WRITER — Phase 86). These are out of scope for READ-05.

**Import cleanup:** after the swap, `FileState` may become unused in `cue.py` / `review.py` / `tag_writer.py` / `tags.py` / `tracklists.py` — remove the now-dead import (ruff `F401`). Verify per file (some still use other `FileState` members — e.g. `pipeline.py`, but that's out of scope).

## Pagination Idiom (D-03)

**In-tree idiom `[VERIFIED]`:** `routers/tags.py::list_tags:157-225` is the reference implementation:
```python
page: int = Query(1, ge=1),
page_size: int = Query(20, ge=10, le=100),
...
offset = (page - 1) * page_size
stmt = stmt.offset(offset).limit(page_size)
pagination = Pagination(page=page, page_size=page_size, total=total)  # from services.proposal_queries
```

**Reality check on D-03's cited targets:**
- `routers/tags.py:174` (`list_tags`) is **already fully paginated** — the D-03 guard is effectively already satisfied here. Verify and leave; do not double-paginate.
- `services/review.py:422` cited by CONTEXT/design **does not exist** — `services/review.py` is only 270 lines. The intended target is `get_tagwrite_review_rows` (`review.py:90-136`, WHERE at :109) — an unbounded `select(...).order_by(...)` with a per-file compute loop, degrade-safe under `begin_nested()`. **This is the true unbounded site** and the primary pagination work.
- `get_cue_review_cards` (`review.py:202-270`, WHERE at :251) is the **second** unbounded list builder — same treatment.
- `bulk_write_no_discrepancies` (`tags.py:404-450`, WHERE at :422) iterates ALL qualifying files and writes each; it is operator-triggered one-shot (not a 5s poll) but still unbounded on a large applied backlog — consider a LIMIT/batch cap.

**Implementation note:** `get_tagwrite_review_rows(session)` / `get_cue_review_cards(session)` currently take only `session`. Threading full page/page_size through their router callers is the faithful idiom; a simpler bounded-`.limit(N)` cap is acceptable per D-03 ("follow in-tree idiom") + Claude's-discretion on page-size, given these are degrade-safe render helpers. Planner picks; recommend threading `page`/`page_size` to match `list_tags` for consistency.

## Trigger Wiring Reality (verified)

- **Tag write is live-wired, only the gate was dead.** `execute_tag_write` (`services/tag_writer.py:165`) is called from `routers/tags.py`: single write `write_file_tags` (:368), bulk `bulk_write_no_discrepancies` (:436), undo `undo_tag_write` (:474). Reviving the gate makes the existing operator trigger observable — **no new trigger machinery needed** `[VERIFIED]`.
- **CUE write is live-wired.** `POST /cue/{tracklist_id}/generate` (`routers/cue.py:238`) validates the (currently-dead) `state==EXECUTED` guard at :251, then `write_cue_file(content, audio_path)` at :276. Reviving the guard turns CUE generation on `[VERIFIED]`.

## Commit Discipline (memory: get_session NEVER commits)

| Mutating path | Commits? | Verdict |
|---------------|----------|---------|
| `routers/tags.py::write_file_tags` (:368-369) | `await session.commit()` after `execute_tag_write` | ✅ correct |
| `routers/tags.py::bulk_write_no_discrepancies` (:436-438) | `await session.commit()` after loop | ✅ correct |
| `routers/tags.py::undo_tag_write` (:474-475) | `await session.commit()` | ✅ correct |
| `routers/cue.py::generate_cue` (:238-326) | **no** `session.commit()` — writes a `.cue` file to **disk only**, no DB row mutation | ✅ correct (nothing to commit) |
| `routers/agent_proposals.py::patch_proposal_state` | single `commit` (:125) — the WRITER, out of scope | ✅ (Phase 86 owns it) |

**No commit-discipline defect introduced by this phase** — the mutating routers already commit. The `applied()` swap is read-only at every reader site; the write guards remain read-then-write with the existing commit downstream. Planner should re-verify after edits (memory landmine), but no new commit is required.

## Common Pitfalls

### Pitfall 1: Assuming the gate is dead because the *trigger* is dead
**What goes wrong:** Adding new tag/CUE write machinery.
**Reality:** The trigger is live; only the `state=='executed'` predicate is dead (matches empty set). Swap the predicate, add nothing.
**Warning sign:** Any new route/task in the plan.

### Pitfall 2: Using `execution_log` because ROADMAP SC#1 says so
**What goes wrong:** False-positive gate (execution_log `completed` but proposal `failed` → tag write targets a deleted path).
**How to avoid:** Use `proposals.status=='executed'` (D-01). See §Central Tension.

### Pitfall 3: Per-record guards have no proposal in hand
**What goes wrong:** Trying to check `applied()` from a bare `FileRecord` (no relationship loaded; `proposal.file` is `lazy="raise"`).
**How to avoid:** Use the async `is_applied(session, file_id)` EXISTS helper — never lazy-load a relationship.

### Pitfall 4: Leaving a stray `file.state` reader for Phase 90
**What goes wrong:** The UI badge (`proposal_row.html:46`) reads `proposal.file.state` — if left, Phase 90 (drop `files.state`) trips over it.
**How to avoid:** D-04 — fix the badge to `proposal.status == "executed"` in THIS phase.

### Pitfall 5: Re-introducing state-based de-dupe / breaking idempotency
**What goes wrong:** Files re-offered for tag-write after already written.
**How to avoid:** Preserve the `completed_subq` (`TagWriteLog.status==COMPLETED`) anti-join at `review.py:105/109` and `tags.py:418/422` (D-02).

### Pitfall 6: Test blindness to commit (memory landmine)
**What goes wrong:** `conftest.py` overrides `get_session` with the test's own session; assertions read uncommitted rows → a missing commit passes tests but rolls back in prod.
**How to avoid:** For any write-path test, assert from an **independent** session. (No new commit needed here, but the behavior-change test must still verify the write persists.)

## Runtime State Inventory

This is a code-only reader swap — no rename, no migration, no data mutation. Still, per-category:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | **None.** No new/changed rows. `proposals.status='executed'` already exists in prod (written by the live apply path). Prod corpus: memory notes prod is at Alembic 031, but the apply path (`patch_proposal_state`) predates this milestone entirely — executed proposals exist. | none (read-only swap) |
| Live service config | **None** — no service config references EXECUTED. | none |
| OS-registered state | **None.** | none |
| Secrets/env vars | **None.** | none |
| Build artifacts | **None** — pure `.py`/`.html` edits; no package rename. Import removals only (`F401`). | `uv run ruff check` catches dead imports |

**The canonical question — after the code swaps, what runtime systems still cache the old value?** Nothing. `applied()` reads `proposals.status`, which is already live. The change is display/gate-behavior only (D-03: operator-triggered, no auto-write), so the sole runtime effect is the Tag-write/CUE lists populating with the pre-existing applied backlog.

## Live-Corpus / UAT Considerations

- **How many applied files exist in prod?** Not directly countable from this branch (read-only research; no prod probe run here). Memory note "Prod is at Alembic 031" concerns the derived-model tables (dedup/fingerprint), NOT proposals — the apply path is old, so an applied backlog plausibly exists. The whole point of the phase is that this backlog is **currently invisible** (dead gate). **This is precisely why D-03 mandates the pagination guard** — a large first-time-visible backlog must not blow up the render at 200K scale.
- **Prod probe recipe (if the planner/operator wants a count before UAT):** memory `reference_lux_readonly_pg_probe` — `ssh datum@lux.lan`, direct `:5432`, DB `phaze`, wrap in `BEGIN TRANSACTION READ ONLY`, base64 the SQL. Count: `SELECT count(DISTINCT file_id) FROM proposals WHERE status='executed';`.
- **Live-UAT (deployment-gated, per D-03):** after cutover, Tags/Cue operator lists populate with real applied files; a single manual tag-write completes end-to-end. Defer to homelab rollout; record in phase VERIFICATION.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (via `uv run`) |
| Config | `pyproject.toml` (`[tool.pytest]`), `tests/buckets.json`, `tests/conftest.py` |
| Quick run command | `uv run pytest tests/review -x` |
| Full suite command | `uv run pytest` (or per-bucket: `just test-bucket review`) |
| Bucket mapping | Buckets are **directories** under `tests/`: `discovery metadata fingerprint analyze identify review agents integration shared` `[VERIFIED: tests/buckets.json + ls tests/]` |

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Command | Bucket / File |
|-----|----------|-----------|---------|---------------|
| READ-05 (SC#2) | An actually-applied file (`proposals.status='executed'`, no `TagWriteLog COMPLETED`) now PASSES the tag-write guard that previously always failed | integration/behavior | `uv run pytest tests/review/services/test_tag_writer.py -x` | `review` |
| READ-05 (SC#2) | An applied file appears in the CUE eligible/gated list + `generate_cue` guard admits it | behavior | `uv run pytest tests/review/routers/test_cue.py -x` | `review` |
| READ-05 (SC#1) | `applied_clause()` / `is_applied()` — executed→True; failed/approved/pending→False; multi-proposal (failed+executed)→True; **never reads `file.state`** | unit | `uv run pytest tests/shared/... -x` | `shared` (stage_status is shared) |
| D-03 | Unbounded list bounded — `get_tagwrite_review_rows` / `get_cue_review_cards` respect LIMIT/page | unit/behavior | `uv run pytest tests/review/services/... -x` | `review` |
| D-04 | Badge renders "Executed/Applied" from `proposal.status=='executed'`, not `file.state` | template/behavior | `uv run pytest tests/review/routers/test_agent_proposals.py` (or proposals row test) | `review` |
| Regression | Idempotency — a file with COMPLETED `TagWriteLog` is NOT re-offered (`completed_subq` preserved) | unit | existing tests in `tests/review` | `review` |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/review -x` (+ `tests/shared` for predicate tasks)
- **Per wave merge:** `just test-bucket review` and `just test-bucket shared` in **isolation** (per-bucket-isolation requirement — memory `reference_ci_bucket_isolation`: `get_settings` lru_cache leak + saq_jobs stub poison surface here; new tests must pass via `just test-bucket <bucket>`, not just the full suite)
- **Phase gate:** full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] Behavior-change test proving the gate flip (SC#2) — likely **new** in `tests/review/services/test_tag_writer.py` (existing file, add case) — mutation-check it: with `applied()` reverted to `state==EXECUTED`, the applied fixture must FAIL the guard (RED); restore → GREEN. (memory `feedback_mutation_test_guard_tests`: a green guard proves nothing until you watch it go red.)
- [ ] `applied_clause()`/`is_applied()` unit tests — likely **new** file under `tests/shared/`.
- [ ] Existing EXECUTED-gate tests to UPDATE (fixtures currently set `state='executed'` which never happens in prod): `tests/review/services/test_tag_writer.py`, `tests/review/routers/test_tags.py`, `tests/review/routers/test_cue.py`, `tests/identify/routers/test_tracklists.py`, `tests/integration/test_review_audit.py` `[VERIFIED: grep]`. These must be migrated to seed `proposals.status='executed'` (+ optionally `file.state='moved'`) instead of `state='executed'`.
- Framework install: none (pytest already present).

## Security Domain

`security_enforcement` is not set in `.planning/config.json` (treat as enabled), but this phase adds **no new attack surface**: it is a read-predicate swap on an internal, single-operator, private-network tool. The filesystem-mutating paths (`execute_tag_write` mutagen write, `write_cue_file`) are unchanged and already gated. The one relevant control is the path-traversal containment guard in the apply path (`tasks/execution.py::_resolve_and_check_containment`, T-26-11-S1) — **out of scope, untouched**.

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | marginal | `page`/`page_size` already validated via FastAPI `Query(ge=, le=)` — reuse the `list_tags` bounds (`ge=10, le=100`) so pagination params can't request unbounded pages |
| V4 Access Control | no (single-operator internal tool; agent routes already `Depends(get_authenticated_agent)`) | unchanged |
| V6 Cryptography | no | — |

| Threat | STRIDE | Mitigation |
|--------|--------|------------|
| Unbounded page_size DoS on the newly-populating list | Denial of Service | `Query(page_size, ge=10, le=100)` bound (existing idiom) — D-03's whole point |
| Tag write to a stale/deleted path (execution_log false-positive) | Tampering / integrity | Use `proposals.status` gate (transactionally coupled to `current_path`) — §Central Tension |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | An applied backlog (`proposals.status='executed'`) actually exists in the live prod corpus (not empty) | Live-Corpus/UAT | LOW — if empty, the lists stay empty and UAT can't demonstrate SC#2 live, but the code change and hermetic tests remain correct. Operator can confirm via the read-only PG probe. |

All other claims are `[VERIFIED]` against source on this branch. Central-tension conclusion, call-site inventory, commit discipline, pagination idiom, and trigger wiring are all verified, not assumed.

## Open Questions

1. **Should `is_applied()` be a standalone EXISTS query per call, or should per-record guards be refactored to carry the proposal?**
   - What we know: the guards currently load a bare `FileRecord`. An extra EXISTS query per guard is simplest and matches the degrade-safe style.
   - Recommendation: standalone `is_applied(session, file_id)`. The N extra queries are negligible (guards fire on single operator clicks / bounded lists, not the 5s poll). Planner's discretion.

2. **Pagination: thread page/page_size through the two `review.py` service builders, or apply a fixed `.limit(N)` cap?**
   - Recommendation: thread `page`/`page_size` to match `list_tags` exactly (consistency + the `Pagination` dataclass already renders the shell controls). Falls under D-03 "follow in-tree idiom" + Claude's-discretion on size.

## Environment Availability

Pure code/config change over the existing stack — no new external dependency. Existing dev stack (`uv`, pytest, Postgres for integration tests via `just test-db`) already provisioned. Note the `MIGRATIONS_TEST_DATABASE_URL` port footgun (memory) is irrelevant here (no migration in this phase).

## Sources

### Primary (HIGH confidence) — verified source on branch `SimplicityGuy/phase-85`
- `src/phaze/routers/agent_proposals.py:41-131` — the apply-path joint write (proposals.status + file.state + current_path, one commit)
- `src/phaze/tasks/execution.py:139-308` — agent-side ordering (copy/delete before proposal PATCH; execution_log best-effort)
- `src/phaze/models/proposal.py:20-60`, `models/execution.py:24-37`, `models/file.py:20-71` — enums + relationships
- `src/phaze/services/stage_status.py:91-142` — `dedup_resolved_clause()` template + existing `done_clause(APPLY)` (the execution_log one to NOT reuse)
- `src/phaze/services/shadow_compare.py:136-143` — the milestone's own "NEVER execution_log" apply-outcome decision (corroborates D-01)
- `src/phaze/services/review.py:90-270`, `routers/tags.py:157-450`, `routers/cue.py:238-326`, `routers/tracklists.py:134-901`, `templates/proposals/partials/proposal_row.html:46` — the 15 call sites
- `src/phaze/services/proposal_queries.py:24-63` — `Pagination` dataclass
- `tests/buckets.json` + `ls tests/` — bucket→directory mapping

### Secondary
- `.planning/phases/85-executed-gate-revival/85-CONTEXT.md` (D-01..D-04)
- `.planning/REQUIREMENTS.md` (READ-05), `.planning/ROADMAP.md` (Phase 85 SC), memory landmines (get_session-never-commits, mutation-test-guards, bucket-isolation, lux PG probe)

## Metadata

**Confidence breakdown:**
- Central tension (proposals vs execution_log): **HIGH** — traced the full apply path + corroborated by the committed shadow-compare invariant.
- Call-site inventory: **HIGH** — every design-cited line verified; exhaustive grep found no missed site (and identified stale `review.py:422` reference).
- Placement/shape of `applied()`: **HIGH** — exact `dedup_resolved_clause()` sibling template in-repo.
- Pagination idiom: **HIGH** — `Pagination` + `list_tags` reference verified.
- Commit discipline: **HIGH** — all mutating routers grep-verified to commit; cue.py writes disk-only.
- Live-corpus backlog size: **LOW** (A1) — not probed in this read-only research session.

**Research date:** 2026-07-10
**Valid until:** 2026-08-09 (stable internal code; re-verify line numbers if the branch advances before planning)
