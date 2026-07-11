# Phase 86: Proposals Cutover - Research

**Researched:** 2026-07-10
**Domain:** SQLAlchemy async writer-deletion / state-cascade retirement (Python 3.14, FastAPI, Postgres)
**Confidence:** HIGH (every claim verified against live source in this worktree)

## Summary

Phase 86 is a **writer-deletion seam**: delete the four `proposal → FileRecord.state` cascade
writers so `proposals.status` (+ `execution_log`) is the sole authority for review decisions
(approve/reject) and apply outcomes (moved/unchanged/executed). The readers were already cut over
(Phase 82 removed `get_pipeline_stats`' `GROUP BY state`; Phase 85 revived the EXECUTED gates onto
`applied()`/`is_applied()` over `proposals.status`). The bug being dissolved lives at
`services/proposal.py:372-373`: `store_proposals` guards its `file.state` write with
`_TERMINAL_FILE_STATES` (`:39`), a frozenset that **omits `MOVED` and `UNCHANGED`** — so a
stale/re-run batch on an already-applied file yanks `state` back to `PROPOSAL_GENERATED`.

**All CONTEXT.md line numbers verified accurate against live source** (rare — usually they drift).
Every one of the seven listed sites is exactly where CONTEXT.md says. The completeness-sweep claim
("zero remaining readers of `file.state` for approved/rejected/proposal_generated/moved/unchanged")
is **CONFIRMED** — the only hits are the four writer sites themselves, plus the `shadow_compare`
INVARIANTS registry (which must stay untouched, D-04) and code comments. Templates read
`proposal.status`/`tracklist.status`, never `file.state`; `stats.proposal_generated` derives from
`done("proposals")` via `_derive_stats` (`routers/pipeline.py:163`), not `file.state`.

**Primary recommendation:** Delete the four writer limbs + the `_TERMINAL_FILE_STATES` frozenset +
now-unused `FileState` imports; rework the apply-PATCH to echo the request `file_state` (never read
`file_record.state`); prove the fix with one integration test (stale batch on an executed file → row
untouched, `is_applied` stays True, asserted from an independent session) plus a mutation-verified
AST source-scan guard modeled on `tests/shared/test_reenqueue_reconcile_source_scan.py`. Update the
three existing test files whose `file.state` assertions will break.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**D-01: Deletion scope — all four proposal→`file.state` cascade writers.** Delete all four sites now.
1. `services/proposal.py:373` — `store_proposals` write of `FileState.PROPOSAL_GENERATED`, **and** the
   `_TERMINAL_FILE_STATES` frozenset (`:39`) + its guard block (the `select(FileRecord)` load at
   `:370-373` exists *only* to write state — delete the whole block). **This is where the
   MOVED-regression bug lives.**
2. `services/proposal_queries.py:166,168` — `update_proposal_status` writes of `APPROVED`/`REJECTED`.
3. `services/proposal_queries.py:186-189` — `bulk_update_status` `FileRecord` UPDATE (also reached via
   `approve_pending_above_confidence`).
4. `routers/agent_proposals.py:115` — apply-outcome write of `MOVED`/`UNCHANGED` (`_FILE_FOLLOW`).
- **`current_path` must survive** at site 4 (`agent_proposals.py:118`). Delete only the
  `file_record.state =` limb.

**D-02: Apply PATCH HTTP contract — echo the request, never read `file.state`.**
- `ProposalStateResponse.file_state` echoes the request's `body.file_state` without reading
  `file_record.state`. Wire contract stays byte-identical. Only caller (`tasks/execution.py:205,269`)
  discards the response → zero-risk.
- The same-state idempotent no-op branch (`agent_proposals.py:84-95`, reads `file_record.state` at
  `:88`) must also stop reading the retiring column — echo request-derived value (or `None` on replay).
- The request field `file_state` STAYS (`schemas/agent_proposals.py:27`): load-bearing for the
  `current_path`-required validator (`:39`). Only the state-mirror side effect is removed.

**D-03: Regression proof — behavioral + mutation-verified.** Integration test: `store_proposals` with
a stale batch on an already-applied file (`proposals.status=='executed'`); assert `is_applied(f)` stays
True / the executed proposal row is untouched / the file row is not touched — from an **INDEPENDENT
session**. Apply-PATCH test: PATCH no longer writes `file.state` but still writes `current_path` and
echoes request `file_state` (moved + unchanged + idempotent replay). Mutation-verify every new guard.

**D-04: Gate untouched; `_TERMINAL_FILE_STATES` fully deleted.** Do NOT modify
`services/shadow_compare.py` INVARIANTS — the six proposal-outcome invariants go historical-only but
stay green (implication `state=X ⇒ derived`). Fully delete `_TERMINAL_FILE_STATES` + its guard + the
now-unused `FileState` import in `proposal.py` if nothing else uses it.

### Claude's Discretion
- Exact placement/wording of the request-derived echo on the idempotent replay branch (`None` vs
  echoing `body.file_state`) — cosmetic, both honest.
- Test file placement / bucket, following in-tree idiom (must pass via `just test-bucket <bucket>` in
  isolation).
- Whether `store_proposals` still needs to `select` the `FileRecord` at all after the state write is
  gone (it does NOT — delete the dead load; see verification below).

### Deferred Ideas (OUT OF SCOPE)
- Enrich/cloud/dedup/ingestion `.state=` writers + dropping `files.state` / the `FileState` enum /
  `ix_files_state` → **Phase 90** (MIG-04).
- Operator UI derivation of review/apply status from `proposals` (stage matrix, retry, "why not
  eligible") → **Phase 87** (UI-01..05).
- Whether `store_proposals` can insert a duplicate pending proposal on an already-executed file — NOT a
  Phase 86 concern (the derived propose-pending set prevents the call; `is_applied()` is unaffected).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SIDECAR-03 | Review decisions (approve/reject) and apply outcomes are read from `proposals.status` + `execution_log`; `FileRecord.state` is no longer a redundant, drift-prone mirror of proposal state (fixes the `store_proposals` MOVED-regression bug). | The four writer sites are enumerated and verified below (Writer Deletion Map). The reader side is already complete: `applied_clause()`/`is_applied()` (`stage_status.py:117-166`) and `_derive_stats` (`routers/pipeline.py:137-163`) derive purely from `proposals`. The completeness sweep confirms zero surviving `file.state` readers of proposal-lifecycle values. `uq_proposals_file_id_pending` (alembic 019) structurally protects the non-pending proposal row, which is why deleting the state write is safe. |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Review decision authority (approve/reject) | Database (`proposals.status`) | — | `update_proposal_status`/`bulk_update_status` already set `proposals.status`; the `file.state` mirror is pure redundancy being deleted. |
| Apply-outcome authority (moved/unchanged/executed) | Database (`proposals.status` + `execution_log`) | — | The agent copy→verify→delete path transactionally couples `proposals.status`; `is_applied()` reads it. |
| Apply-PATCH HTTP contract | API / Backend (`routers/agent_proposals.py`) | — | The `/state` endpoint is the one contract-adjacent edit; response shape stays byte-identical (D-02). |
| Anti-drift enforcement | Test tier (AST source-scan guard) | — | A DB-free AST scan is the established idiom (`test_*_source_scan.py`) for proving a column is no longer read/written. |

## Standard Stack

No new packages. This is a deletion/refactor phase within the existing stack (SQLAlchemy 2.0 async,
FastAPI, asyncpg, Postgres, pytest + pytest-asyncio). Per CLAUDE.md: Python 3.14, `uv run` prefix on
every command, ruff line-length 150, mypy strict (excludes `tests/`), 90% coverage min, pre-commit
must pass.

**Package Legitimacy Audit:** N/A — phase installs zero external packages.
**Environment Availability:** N/A — code/config-only change; no new external tools/services. Existing
test infra (Postgres test DB on 5433, `just test-bucket`) already present.

## Writer Deletion Map (verified against live source)

| # | File:Line | What to delete | Verified nuance |
|---|-----------|----------------|-----------------|
| 1a | `services/proposal.py:39` | `_TERMINAL_FILE_STATES` frozenset | Referenced ONLY at `:372`. After deleting the guard block it is fully dead. Delete entirely (D-04). |
| 1b | `services/proposal.py:370-373` | The `select(FileRecord)` load + `scalar_one_or_none()` + guarded `file_record.state = FileState.PROPOSAL_GENERATED` | **Confirmed:** the `select(FileRecord)` at `:370` exists ONLY to write state — `file_record` is used nowhere else in the loop. Delete the whole block (Claude's discretion resolved: dead load, remove it). |
| 1c | `services/proposal.py:18` import | `FileState` in `from phaze.models.file import FileRecord, FileState` | After 1a+1b, `FileState` is unused in the module. `FileRecord` is STILL used (`build_file_context` typing `:139`, `load_companion_contents` `:407`) — keep `FileRecord`, drop only `FileState`. mypy `warn_unused_ignores`/ruff `F401` will catch a stale import. |
| 2 | `proposal_queries.py:164-168` | `update_proposal_status` — the `if new_status == APPROVED: proposal.file.state = ...` / `elif REJECTED: ...` block | Keep `proposal.status = new_status.value` (`:163`) and the re-select tail. Note `proposal.file` is `lazy="noload"`/`selectinload`-eager; deleting the write removes the only `.file.state` touch here. |
| 3 | `proposal_queries.py:185-189` | `bulk_update_status` — `file_state = ...` + the `select(RenameProposal.file_id)` subquery + `update(FileRecord)...values(state=...)` | Keep the `update(RenameProposal)...values(status=...)` (`:183`) and `return int(cursor_result.rowcount)`. `approve_pending_above_confidence` (`:194`) reuses this and needs no other change. |
| 4 | `agent_proposals.py:110-121` | The `file_record.state = new_file_state.value` limb only | **KEEP** `current_path` write (`:117-119`) and its `response_current_path`. Rework so `response_file_state` echoes `body.file_state` (D-02) rather than the written `new_file_state.value`. `_FILE_FOLLOW` (`:47-50`) is now dead (only `FileState` consumer besides `:31` import) — safe to delete; verify no other reference. |
| 5 | `agent_proposals.py:84-95` | The idempotent same-state branch reading `file_record.state` at `:88` | Stop reading `file_record.state`. Per discretion, echo `None` (honest — no request outcome on replay) or `body.file_state`. `current_path` echo at `:89` may stay (it is NOT part of the state cascade — it is a real path). |
| 6 | `agent_proposals.py:31` import | `FileState` in `from phaze.models.file import FileRecord, FileState` | `FileRecord` still used (`session.get(FileRecord, ...)` `:71`, `.agent_id` guard `:72`). After deleting sites 4+5 and `_FILE_FOLLOW`, `FileState` is unused — drop it. |

**Sites deliberately UNTOUCHED (verified):**
- `services/shadow_compare.py:123-152` INVARIANTS registry — implication anti-joins (`state=X AND NOT
  <derived>`), NOT equality. Confirmed at `shadow_compare.py:11-13` docstring and the `run_shadow_compare`
  body. Freezing `file.state` cannot turn these red; the six proposal-outcome invariants become
  historical-only but stay green. **Leave untouched (D-04).**
- `schemas/agent_proposals.py` — `ProposalStatePatch` request shape (incl. `file_state` field `:27` +
  `_require_path_when_moved` validator `:31-41`) and `ProposalStateResponse` field set stay
  byte-identical. No schema edit needed; only the handler's *value source* for `file_state` changes.
- `services/agent_client.py:493-506` `patch_proposal_state` wrapper — unchanged (wire contract stable).
- `tasks/execution.py:205,269` — the only caller; **confirmed it discards the response** (both call
  sites `await api.patch_proposal_state(...)` with no assignment). D-02 zero-risk confirmed.

## Why deleting the state write is safe (the load-bearing invariant)

`uq_proposals_file_id_pending` (alembic `019_add_proposals_pending_unique_index.py:73`) is a **partial
unique index** `ON (file_id) WHERE status='pending'`. `store_proposals` upserts with
`on_conflict_do_update(index_elements=["file_id"], index_where=(RenameProposal.status == "pending"))`
(`proposal.py:349-354`). The conflict only fires against a PENDING row, so an
APPROVED/EXECUTED/REJECTED/FAILED proposal for the same file is **never a conflict target** — the
proposal ROW is structurally protected. The `file.state` guard was a *second* mirror trying to protect
the same fact at the FILE level; it is the redundant limb whose omission of MOVED/UNCHANGED is the bug.
Deleting it removes the drift surface entirely, leaving the already-correct partial index as the sole
protection.

## Reader Completeness Sweep (CONFIRMED zero surviving readers)

Grep for `.state` compared against `approved|rejected|proposal_generated|moved|unchanged` across
`src/phaze` (excluding `proposal.status`/`ProposalStatus`) returns ONLY:
- The four writer sites (deleted by this phase).
- `shadow_compare.py` INVARIANTS (implication; leave untouched, D-04).
- Docstrings/comments (invisible to an AST scan — Pitfall 1).

**Templates:** `proposal_row.html`, `filter_tabs.html`, `status_badge.html`, `_diff_row.html` read
`proposal.status` / `tracklist.status`, never `file.state`. `stats_bar.html:23` reads
`stats.proposal_generated`, which `_derive_stats` (`routers/pipeline.py:163`) computes as
`done("proposals")` from stage-progress — **not** `file.state`. No reader-cutover risk. Any UI
derivation of review/apply status from `proposals` is Phase 87 (out of scope).

## Reusable Assets (verified signatures)

- `services/stage_status.py:117 applied_clause() -> ColumnElement[bool]` — SQL predicate,
  `exists(proposals WHERE file_id==FileRecord.id AND status=='executed')`. For query-level use.
- `services/stage_status.py:148 async is_applied(session, file_id: uuid.UUID) -> bool` — per-record
  twin, single scalar EXISTS. **This is the predicate the D-03 integration test should call** (it holds
  a `file_id` + `session`, not a query). Both NEVER read `file.state` or touch `execution_log`.
- `tests/shared/core/test_proposals_upsert.py` — existing `store_proposals` upsert harness (`_seed_file`,
  `_batch`, `_count`, independent `select(...)` assertions in the `shared` bucket). The D-03 stale-batch
  test belongs here (same bucket, same idioms).
- `tests/shared/test_reenqueue_reconcile_source_scan.py` — the **template** for the anti-drift AST
  source-scan guard: walks `Call.args` AND `Call.keywords`, matches `ast.Attribute .attr=="state"` in
  `Load` context off any `FileRecord`-bound name, plus `FileState.<member>` occurrences and
  `getattr(_, "state")`. Its negative tests mutate crafted source STRINGS (DB-free, hermetic).

## Runtime State Inventory

> This phase deletes writers but does NOT migrate stored data. The `files.state` COLUMN and its
> existing values persist untouched until Phase 90 (MIG-04). Enumerated per the cutover discipline.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | Production `files.state` rows already carrying `proposal_generated`/`approved`/`rejected`/`moved`/`unchanged`/`executed` (prod is at Alembic 031 per project memory — column exists, populated). After this phase NO new writer produces these values; existing rows **freeze** at their last value. | None — freezing is intended and gate-safe. The authoritative lifecycle moves to `proposals.status` (already backfilled/authoritative, no migration). Column drop is Phase 90. |
| Live service config | None. | None — verified: no external service stores proposal state. |
| OS-registered state | None. | None — no scheduler/pm2 tasks reference proposal state. |
| Secrets/env vars | None. | None. |
| Build artifacts / installed packages | None — no package rename; `FileState` enum members stay defined (only two imports of it are dropped). | None. Orphaned enum members (`PROPOSAL_GENERATED`, `APPROVED`, `REJECTED`, `MOVED`, `UNCHANGED` as *write targets*) are informational only — dropping the enum is Phase 90, NOT this phase. |

**Frozen-state consequence (call out to planner):** after this phase a file that gets a proposal / is
approved / is applied keeps its `file.state` at its last enrich value (e.g. `ANALYZED`) while
`proposals.status` tracks the real lifecycle. `shadow_compare` invariants stay green because a file
frozen at an earlier `state` still satisfies its weaker implication.

## Test Impact Map (existing tests that WILL break — must be updated in this phase)

| File | Line(s) | Assertion | Action |
|------|---------|-----------|--------|
| `tests/shared/core/test_proposals_upsert.py` | 157-159 | `test_fresh_insert_stamps_pk` asserts `file_record.state == FileState.PROPOSAL_GENERATED` | **Delete** the state assertion (`:157-159`). Keep the PK/status/path assertions. |
| `tests/shared/core/test_proposals_upsert.py` | 185-203 | `test_rerun_does_not_regress_terminal_file_state` — premise (state forward-only guard) is deleted | **Replace** with the D-03 test: seed an executed proposal, run a stale `store_proposals` batch, assert the executed proposal row untouched + `is_applied()` True from an independent session. (The old "approved-row survives" test at `:117-139` asserts the PROPOSAL ROW via the partial index — that STAYS GREEN and is the real safety guarantee.) |
| `tests/review/services/test_proposal_queries.py` | 260-264, 269-273, 302-316 | asserts `result.file.state == FileState.APPROVED/REJECTED` and bulk `f.state == APPROVED` | **Delete** the `.file.state`/`.state` assertions; keep the `proposals.status` assertions. Rename tests to drop the "transitions FileRecord.state (APR-02)" framing. |
| `tests/review/routers/test_agent_proposals.py` | 84, 92, 108, 114 | `:84`/`:108` assert `body["file_state"] == "moved"/"unchanged"` (STAY — response echo); `:92`/`:114` assert `f.state == FileState.MOVED/UNCHANGED.value` (BREAK) | **Keep** `:84`/`:108`. **Delete** `:92`/`:114`. Add an assertion that `f.state` is UNCHANGED from its seeded value (proves the cascade write is gone — this is a positive guard, not just a deletion). |
| `tests/review/routers/test_agent_proposals.py` | ~118-135 | same-state idempotent replay test (both return 200) | If it asserts `body["file_state"]` on replay, update to the new request-derived/`None` echo (discretion). Status-code assertions unchanged. |

**Note:** `tests/agents/services/test_agent_client_endpoints.py:91,100` and
`tests/shared/test_applied_clause.py` seed `file_state="moved"`/`state=FileState.MOVED` as *fixtures*
(inputs), not cascade assertions — they do NOT break. `test_applied_clause.py:123` explicitly seeds
`state=FileState.MOVED  # deliberately NOT 'executed'` to prove `applied` reads proposals not state —
this test is the existing proof that the reader is already cut over; keep it.

## Common Pitfalls

### Pitfall 1: Line-grep guards are toothless on multi-line SQLAlchemy (project memory `feedback_mutation_test_guard_tests`)
**What goes wrong:** A regex/line guard asserting "no `FileRecord.state` write" misses a
`.values(state=...)` splat or a multi-line `select(...).where(...)`. Phase 83 shipped two toothless
guards this way.
**How to avoid:** Use the AST source-scan idiom (`test_reenqueue_reconcile_source_scan.py`), which walks
both positional `Call.args` and `Call.keywords` and matches `ast.Attribute` nodes. **Mutation-verify:**
inject each syntactic form (`f.state = X`, `.values(state=X)`, `getattr(f,"state")`,
`update(FileRecord).values(state=...)`) into a crafted STRING → guard RED → confirm; and check a
legitimate `.status`/`.id` read is NOT flagged (false-positive check).

### Pitfall 2: `get_session` override reads uncommitted rows (project memory `project_get_session_never_commits`)
**What goes wrong:** `conftest.py:216` overrides `get_session` so the test and the handler share a
session — a test asserting on the same session sees uncommitted writes and passes spuriously.
**How to avoid:** The D-03 assertion (proposal row untouched / `is_applied` True) must read from an
**INDEPENDENT session**. The router must `await session.commit()` itself (it does, `agent_proposals.py:125`).

### Pitfall 3: Deleting the wrong limb at site 4 (dropping `current_path`)
**What goes wrong:** `current_path` (`agent_proposals.py:117-119`) is the real move destination, not part
of the state cascade. Deleting it silently loses the moved-file path.
**How to avoid:** Delete ONLY `file_record.state = new_file_state.value` (`:115`). Keep the
`if body.current_path is not None: file_record.current_path = body.current_path` block and its
`response_current_path`. A test must assert `current_path` still persists on a `moved` PATCH.

### Pitfall 4: Stale unused imports fail CI, not silently
**What goes wrong:** After deleting the state writes, `FileState` (both files), `_FILE_FOLLOW`, and the
`select` import in `proposal.py` (if `store_proposals` was its only user) may go unused. ruff `F401`
+ mypy strict will fail the commit.
**How to avoid:** Confirm each: `FileRecord` STAYS in both files; `select` STAYS in `proposal.py`
(used by `load_companion_contents:402,407` and `store_proposals` upsert still needs `func`,`pg_insert`,
not `select` for the deleted load — but `select` is used elsewhere in the module, verify). Run
`uv run ruff check .` + `uv run mypy .` before commit.

### Pitfall 5: The idempotent replay branch is a second, easy-to-miss reader
**What goes wrong:** Deleting site 4 but leaving `agent_proposals.py:88` (`file_state_str =
file_record.state`) means the endpoint STILL reads the retiring column on the same-state path — the
source-scan guard would (correctly) go red, or worse, a line-grep guard would miss it.
**How to avoid:** D-02 explicitly requires site 5 too. The AST guard over `agent_proposals.py` catches
any surviving `file_record.state` Load.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (async) |
| Config | `pyproject.toml` / `pytest.ini` in repo; buckets in `tests/buckets.json` (directory-based) |
| Buckets relevant | `shared` (store_proposals), `review` (proposal_queries + agent_proposals router), `agents` (agent_client) |
| Quick run | `uv run pytest tests/shared/core/test_proposals_upsert.py -x` |
| Bucket run (isolation) | `just test-bucket shared` / `just test-bucket review` |
| Full suite | `uv run pytest` (flakes under colima VM pressure — re-run failed subset in isolation, project memory `reference_local_fullsuite_colima_flake`) |

### Phase Requirements → Test Map (behavioral seams)
| Seam | Behavior | Test Type | Command | Exists? |
|------|----------|-----------|---------|---------|
| MOVED-not-re-proposed (the bug) | `store_proposals` on a stale batch where the file has an `executed` proposal → executed proposal row untouched, `is_applied()` True, file row not written | integration | `just test-bucket shared` | ❌ Wave 0 — new test in `test_proposals_upsert.py` (replaces `test_rerun_does_not_regress_terminal_file_state`) |
| PATCH still writes current_path | `moved` PATCH persists `file_record.current_path = body.current_path` | integration | `just test-bucket review` | ⚠️ adapt existing `test_agent_proposals.py` (assert current_path, drop `f.state`) |
| PATCH echoes request file_state | success `moved`/`unchanged` responses return `body["file_state"]` without reading `file.state` | integration | `just test-bucket review` | ✅ `:84`/`:108` already assert echo — keep; add "f.state unchanged from seed" positive guard |
| Idempotent replay path | same-state PATCH returns 200 and does NOT read `file.state` | integration | `just test-bucket review` | ⚠️ adapt existing replay test to new `None`/request-derived echo |
| Anti-drift (no state write survives) | AST scan over `proposal.py`, `proposal_queries.py`, `agent_proposals.py` finds zero `FileRecord.state` Store/Load and zero `FileState` write-target occurrences | source-scan (DB-free) | `just test-bucket shared` (or `review`) | ❌ Wave 0 — new `test_proposals_cutover_source_scan.py` modeled on `test_reenqueue_reconcile_source_scan.py` |

### Sampling Rate
- **Per task commit:** the touched bucket's quick run (`uv run pytest tests/<bucket>/... -x`).
- **Per wave merge:** `just test-bucket shared` + `just test-bucket review` in isolation (catches
  non-hermetic leakage, project memory `reference_ci_bucket_isolation`).
- **Phase gate:** full suite green before `/gsd:verify-work`; 90% coverage floor.

### Wave 0 Gaps
- [ ] `tests/shared/core/test_proposals_upsert.py` — new D-03 stale-batch/executed test (replaces the
  terminal-state test); delete the `PROPOSAL_GENERATED` assertion in `test_fresh_insert_stamps_pk`.
- [ ] `tests/shared/test_proposals_cutover_source_scan.py` (or `review/`) — new AST guard, mutation-verified.
- [ ] `tests/review/services/test_proposal_queries.py` — drop `.file.state` assertions (3 tests).
- [ ] `tests/review/routers/test_agent_proposals.py` — drop `f.state` assertions, add current_path +
  echo + "state unchanged" guards, adapt replay test.

## Security Domain

Internal agent-only endpoint; no auth/session/crypto change in scope. Relevant controls stay intact:

| ASVS Category | Applies | Standard Control (unchanged by this phase) |
|---------------|---------|-------------------------------------------|
| V4 Access Control | yes | Cross-tenant guard `agent_proposals.py:71-76` (rejects a proposal whose file belongs to another agent, 403 before state-machine logic) — **do not touch**. |
| V5 Input Validation | yes | `ProposalStatePatch` `extra="forbid"` + `_require_path_when_moved` validator — **stays** (D-02 keeps the request shape). |
| V6 Cryptography | no | — |

No new threat surface: the change removes a write, keeps the same request/response bytes, and the only
caller discards the response.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| — | (none) | — | All claims verified against live source in this worktree. No `[ASSUMED]` claims. |

## Open Questions

1. **Exact new test-file placement for the D-03 stale-batch test** — recommendation: extend
   `tests/shared/core/test_proposals_upsert.py` (same bucket, same harness) rather than a new file, so
   `_seed_file`/`_batch`/`_count` are reused. Non-blocking (Claude's discretion in CONTEXT.md).
2. **Idempotent replay echo value** (`None` vs `body.file_state`) — both honest per D-02; recommend
   `None` (no outcome was requested on a pure replay, and the request `file_state` may be absent).
   Non-blocking.

## Sources

### Primary (HIGH confidence — live source, this worktree)
- `src/phaze/services/proposal.py:18,39,370-373` — `_TERMINAL_FILE_STATES` + `store_proposals` cascade + import.
- `src/phaze/services/proposal_queries.py:163-191` — `update_proposal_status` / `bulk_update_status` / `approve_pending_above_confidence`.
- `src/phaze/routers/agent_proposals.py:31,47-50,84-95,110-131` — apply-PATCH + idempotent branch + `_FILE_FOLLOW`.
- `src/phaze/schemas/agent_proposals.py:21-50` — request/response schemas.
- `src/phaze/tasks/execution.py:205,269` — only caller, discards response.
- `src/phaze/services/agent_client.py:493-506` — `patch_proposal_state` wrapper.
- `src/phaze/services/stage_status.py:117-166` — `applied_clause()` / `is_applied()`.
- `src/phaze/services/shadow_compare.py:1-152` — INVARIANTS registry (implication, leave untouched).
- `src/phaze/routers/pipeline.py:137-163` — `_derive_stats` (`proposal_generated` from `done("proposals")`).
- `alembic/versions/019_add_proposals_pending_unique_index.py` — `uq_proposals_file_id_pending`.
- `tests/shared/core/test_proposals_upsert.py`, `tests/review/services/test_proposal_queries.py`,
  `tests/review/routers/test_agent_proposals.py`, `tests/shared/test_reenqueue_reconcile_source_scan.py`,
  `tests/shared/test_applied_clause.py`, `tests/buckets.json`.
- `.planning/REQUIREMENTS.md:56` — SIDECAR-03.

### Secondary
- CONTEXT.md (86), 85/82/83 CONTEXT.md, PARALLEL-ENRICH-DAG-DESIGN.md §4/§6.1/§7 (design authority).
- Project memory: `feedback_mutation_test_guard_tests`, `project_get_session_never_commits`,
  `reference_ci_bucket_isolation`, `project_prod_alembic_031_unreleased`,
  `reference_local_fullsuite_colima_flake`.

## Metadata

**Confidence breakdown:**
- Writer deletion map: HIGH — every line number verified against live source (all matched CONTEXT.md).
- Reader completeness sweep: HIGH — grep + template scan confirm zero surviving readers.
- Safety invariant (partial index): HIGH — read alembic 019 + the upsert `index_where`.
- Test impact map: HIGH — read the exact failing assertions.
- shadow_compare left-green: HIGH — read the implication-based anti-join and INVARIANTS registry.

**Research date:** 2026-07-10
**Valid until:** ~2026-08-09 (30 days; stable internal refactor, but re-verify line numbers if other
phases merge into these files first — e.g. Phase 87 UI work touches the proposals surface).
