# Phase 86: Proposals Cutover - Context

**Gathered:** 2026-07-10
**Status:** Ready for planning

<domain>
## Phase Boundary

Make `proposals.status` (+ `execution_log` for the audit trail) the **sole authority** for the
proposal lifecycle — review decisions (approve/reject) and apply outcomes (moved/unchanged/executed) —
by **deleting the entire redundant `FileRecord.state` proposal cascade**. This dissolves the
`store_proposals` MOVED-regression bug (`_TERMINAL_FILE_STATES` omitted `MOVED`/`UNCHANGED`, so a
stale/re-run batch could regress an applied file's `state` back to `PROPOSAL_GENERATED`).

Delivers requirement **SIDECAR-03**. This is a **writer-deletion seam** — the proposal-lifecycle
*readers* are already cut over (Phase 82 removed `get_pipeline_stats`' `GROUP BY state`; Phase 85
revived the EXECUTED gates onto `applied(f) ≡ proposals.status=='executed'`). A completeness sweep
found **zero** remaining readers (templates or routers) of `file.state` for
`approved`/`rejected`/`proposal_generated`, so deleting these writers has no reader-cutover risk.

**In scope:** delete the four proposal→`file.state` writer sites (below); rework the apply PATCH
response to stop reading the retiring column; a behavioral + mutation-verified regression test proving
the MOVED-clobber is gone.

**Out of scope (own phases):** the enrich/cloud/dedup/ingestion `.state=` writers and dropping the
`files.state` column / `FileState` enum → **Phase 90** (MIG-04); operator UI stage-matrix/retry →
**Phase 87**.
</domain>

<decisions>
## Implementation Decisions

### D-01: Deletion scope — all four proposal→`file.state` cascade writers
- **Delete all four sites now**, making `proposals.status` truly sole authority — no proposal-lifecycle
  `file.state` writer survives into Phase 90. One coherent "proposals" seam (one shippable PR).
  1. `services/proposal.py:373` — `store_proposals` write of `FileState.PROPOSAL_GENERATED`, **and** the
     `_TERMINAL_FILE_STATES` frozenset (`:39`) + its guard block (the `select(FileRecord)` load at
     `:370-373` exists *only* to write state — delete the whole block, not just the assignment). **This
     is where the MOVED-regression bug lives.**
  2. `services/proposal_queries.py:166,168` — `update_proposal_status` writes of `APPROVED`/`REJECTED`.
  3. `services/proposal_queries.py:186-189` — `bulk_update_status` `FileRecord` UPDATE of
     `APPROVED`/`REJECTED` (also reached via `approve_pending_above_confidence`).
  4. `routers/agent_proposals.py:115` — apply-outcome write of `MOVED`/`UNCHANGED` (`_FILE_FOLLOW`).
- **`current_path` must survive** at site 4 (`agent_proposals.py:118`) — it is the real move
  destination, not part of the state cascade. Delete only the `file_record.state =` limb.
- **Rejected:** "core-only, defer apply-outcome to Phase 90." Chosen against because the ROADMAP headline
  is *sole* authority, the apply-outcome write is the same drift-prone mirror, and doing the whole
  cascade in one seam is cleaner. Accepted cost: this phase touches the agent PATCH contract (D-02).

### D-02: Apply PATCH HTTP contract — echo the request, never read `file.state`
- **`ProposalStateResponse.file_state` echoes the request's `body.file_state`** (`'moved'`/`'unchanged'`)
  — the outcome the agent asked for — **without reading `file_record.state`**. Wire contract stays
  byte-identical (`proposal_state` / `file_state` / `current_path` all still present). The only caller,
  `tasks/execution.py:205`, **discards the response**, so this is zero-risk.
- **The same-state idempotent no-op branch** (`agent_proposals.py:84-95`, currently reads
  `file_record.state` at `:88`) must **also stop reading the retiring column** — echo the same
  request-derived value (or `None` on the replay path) instead.
- **The request field `file_state` stays** (`schemas/agent_proposals.py:27`): it is load-bearing —
  it drives the `current_path`-required validator (`:39`) and tells the handler whether/where to write
  `current_path`. Only the *state-mirror side effect* is removed, not the request contract.
- **Rejected:** deriving the field from `proposals.status` via `_FILE_FOLLOW` (re-reconstructs the
  MOVED/UNCHANGED distinction that only `file.state` carried — awkward, field is unused); dropping the
  field (agent-facing schema change for no functional gain).

### D-03: Regression proof — behavioral + mutation-verified
- **Integration test** (the bug's real manifestation, not per-site units): run `store_proposals` with a
  stale batch containing an already-applied file (`proposals.status=='executed'`) and assert
  **`applied(f)` stays True / the executed proposal row is untouched / the file row is not touched** —
  asserted from an **INDEPENDENT session** (`conftest` `get_session` override reads uncommitted rows;
  see `[[project_get_session_never_commits]]`).
- **Apply-PATCH test:** the PATCH no longer writes `file.state` but **still writes `current_path`** and
  **echoes the request `file_state`** (both success `moved` and `unchanged` paths + the same-state
  idempotent replay).
- **Mutation-verify every new guard** (break the code → watch it go RED → restore) and record it in the
  verification doc — a GREEN guard proves nothing until it has failed once
  (`[[feedback_mutation_test_guard_tests]]`).

### D-04: Gate untouched; `_TERMINAL_FILE_STATES` fully deleted
- **Do NOT modify `services/shadow_compare.py` INVARIANTS.** The six proposal-outcome invariants
  (`proposal_generated`/`approved`/`rejected`/`moved`/`unchanged`/`executed`) become **historical-only**
  after this phase — no writer produces those `state` values anymore, so they can only cover pre-cutover
  rows — but they **stay green** (the gate asserts `state=X ⇒ derived`; a file frozen at an earlier
  `state` still satisfies its weaker implication) and still guard the frozen backfill until Phase 90
  drops the column. The historical-only nature is inherent, not a defect. Minimal touch to the standing
  gate.
- **Fully delete `_TERMINAL_FILE_STATES`** (`proposal.py:39`), its guard, and the now-unused
  `FileState` import in `proposal.py` if nothing else in the module uses it. No repurpose.

### Claude's Discretion
- Exact placement/wording of the request-derived echo on the idempotent replay branch (`None` vs
  echoing `body.file_state`) — cosmetic, both honest.
- Test file placement / bucket, following in-tree idiom (must pass via `just test-bucket <bucket>` in
  isolation — `[[reference_ci_bucket_isolation]]`).
- Whether `store_proposals` still needs to `select` the `FileRecord` at all after the state write is
  gone (it likely does not — delete the dead load).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone design & requirement
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` §4 (line 227-228: `APPROVED/REJECTED` →
  `proposals.status`; `EXECUTED/FAILED/MOVED/UNCHANGED` → `execution_log` + `proposals.status`),
  §4.1 item 5 (the `store_proposals` MOVED-regression diagnosis, `proposal.py:39`), §6.1 (backfill
  table — proposal states need **no** backfill, already authoritative), §7 (call-site inventory).
- `.planning/REQUIREMENTS.md` — **SIDECAR-03** (line 56), the requirement this phase closes.
- `.planning/ROADMAP.md` — Phase 86 entry.

### Prior-phase decisions this phase builds on
- `.planning/phases/85-executed-gate-revival/85-CONTEXT.md` — D-01 established
  `applied(f) ≡ proposals.status=='executed'` in `services/stage_status.py`, expressed **purely over
  `proposals`** specifically so Phase 86 needs no rework. Reuse `applied()` in the D-03 test.
- `.planning/phases/82-counts-pending-set-cutover/82-CONTEXT.md` — removed `get_pipeline_stats`
  (`GROUP BY state`); confirms the proposal-lifecycle count/pending readers are already derived.
- `.planning/phases/83-cloud-routing-sidecar-cutover/83-CONTEXT.md` — the sibling SIDECAR-01 writer-
  cutover pattern (delete-the-redundant-writer + CAS-guard collapse).

### Writer sites to delete (verified)
- `src/phaze/services/proposal.py:39` (`_TERMINAL_FILE_STATES`), `:370-373` (`store_proposals` cascade)
- `src/phaze/services/proposal_queries.py:164-168` (`update_proposal_status`), `:185-189`
  (`bulk_update_status`)
- `src/phaze/routers/agent_proposals.py:110-119` (apply-outcome limb; keep `current_path`), `:84-95`
  (same-state idempotent branch — stop reading `file_record.state`)

### HTTP contract (do not change the request shape)
- `src/phaze/schemas/agent_proposals.py` — `ProposalStatePatch` (request; `file_state` field + validator
  stay) / `ProposalStateResponse` (response; `file_state` now request-echoed).
- `src/phaze/tasks/execution.py:205,269` — the only caller; discards the response.
- `src/phaze/services/agent_client.py:497-506` — `patch_proposal_state` client wrapper.

### Standing gate (leave untouched — read to confirm it stays green)
- `src/phaze/services/shadow_compare.py` — INVARIANTS registry (implication direction; six proposal
  invariants go historical-only) + `run_shadow_compare`.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `applied(f) ≡ proposals.status=='executed'` in `services/stage_status.py` (Phase 85) — the derived
  apply-outcome predicate; reuse it in the D-03 regression assertions instead of reading `file.state`.
- `uq_proposals_file_id_pending` partial unique index (`ON (file_id) WHERE status='pending'`) already
  protects an approved/executed proposal row from being overwritten by a re-run `pg_insert`'s
  `ON CONFLICT` (which only fires against `pending` rows). This is why deleting the state write is safe:
  the proposal row itself is already guarded.

### Established Patterns
- Milestone principle: **readers-before-writers, one shippable PR per seam, small blast radius**
  (hard requirement). Phase 86 is the writer half — readers were done in 82/85.
- Mutating routers/services must `await session.commit()` themselves; tests must assert from an
  independent session (`[[project_get_session_never_commits]]`).
- Shadow-compare is an **implication** (`state=X ⇒ derived`), never equality — freezing `file.state`
  cannot turn it red.

### Integration Points
- Agent PATCH `/api/internal/agent/proposals/{id}/state` (`agent_proposals.py`) is the apply seam — the
  one contract-adjacent edit. Keep the wire response byte-identical (D-02).
- After this phase, a file that gets a proposal / is approved / is applied **freezes its `file.state`**
  at its last enrich value (e.g. `ANALYZED`) while `proposals.status` tracks the real lifecycle. This is
  expected and gate-safe; called out so the Phase 87 UI derives review/apply status from `proposals`,
  not `file.state`.

</code_context>

<specifics>
## Specific Ideas

- Delete the *whole* `store_proposals` file-load-and-guard block (`:370-373`), not just the assignment —
  the `select(FileRecord)` exists only to write state.
- Request-echo the response `file_state`; never read `file_record.state` anywhere in `agent_proposals.py`
  after this phase (including the idempotent replay branch at `:88`).
- The D-03 test must reproduce the *actual* bug scenario (stale batch on an applied file), asserted from
  an independent session, and be mutation-verified.

</specifics>

<deferred>
## Deferred Ideas

- Enrich/cloud/dedup/ingestion `.state=` writers + dropping `files.state` / the `FileState` enum /
  `ix_files_state` → **Phase 90** (MIG-04).
- Operator UI derivation of review/apply status from `proposals` (stage matrix, retry, "why not
  eligible") → **Phase 87** (UI-01..05).
- Whether `store_proposals` can insert a *duplicate pending* proposal if ever called on an
  already-executed file (the `ON CONFLICT` only matches pending rows) — **not a Phase 86 concern**: the
  derived propose-pending set (`done(propose)` = a proposals row exists, Phase 82) prevents the call, and
  `applied(f)` is unaffected either way. Noted as a truth for the researcher, not scoped work.

### Reviewed Todos (not folded)
- `analysis-completed-at-backfill.md` — *"analyzed ⇒ analysis_completed_at — 1001 production rows will
  fail the shadow gate"* (score 0.6). **Not folded:** belongs to the analyze shadow-compare / destructive
  migration (Phase 79/90); matched on generic keywords, not this phase's subject. (Per memory, Phase
  80's migration `036` already backfills `analysis_completed_at` — `[[project_analyzed_invariant_red_on_deploy]]`.)
- `wr-01-review-builder-limit-before-filter.md` — *"Tag/CUE bulk builders apply .limit() before the
  qualifying-change filter — 200K starvation risk"* (score 0.6). **Not folded:** it targets the tag/CUE
  bulk-write builders (Phase 85/87 operator surface), not the proposal-status cascade.

</deferred>

---

*Phase: 86-proposals-cutover*
*Context gathered: 2026-07-10*
