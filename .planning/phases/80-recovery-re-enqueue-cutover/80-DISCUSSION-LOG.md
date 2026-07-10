# Phase 80: Recovery / Re-enqueue Cutover - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-08 (original) · 2026-07-10 (refresh — see Session 2 below)
**Phase:** 80-recovery-re-enqueue-cutover
**Areas discussed:** Domain-completed source, AWAITING/push sidecar map, Reconcile scope boundary, Pending-import removal

---

# Session 1 — 2026-07-08 (original, against `main` @ `76c0f3e8`)

## Area selection

| Option | Description | Selected |
|--------|-------------|----------|
| Domain-completed source | Reuse Phase-78 `eligible()`/`FAILURE_IS_TERMINAL` vs. hand-roll per-stage done sets | ✓ |
| AWAITING/push sidecar map | How `_get_awaiting_cloud_ids` / `_select_done_push_ids` rederive from the cloud sidecar | ✓ |
| reconcile scope boundary | Read-audit + regression guard vs. pulling writer retirement forward | ✓ |
| Pending-import removal | Drop the pending-set imports; derive `done` directly (anti-double-negation) | ✓ |

**User's choice:** All four.

---

## Domain-completed source

| Option | Description | Selected |
|--------|-------------|----------|
| Reuse Phase-78 terminal predicate | `domain_completed(stage) = done OR (failed AND FAILURE_IS_TERMINAL[stage])` — one source of truth; the analyze/metadata/fingerprint asymmetry falls out automatically | ✓ |
| Hand-roll per-stage in reenqueue | Keep bespoke done-set SELECTs, swapping `FileRecord.state` columns for the new marker columns without routing through the Phase-78 abstraction | |

**User's choice:** Reuse Phase-78 terminal predicate.
**Notes:** Locks the per-stage asymmetry — `analyze: done∨failed`, `metadata: done∨failed`, `fingerprint: done` only (failed auto-retries per D-16). Prevents drift between recovery's terminal semantics and the derivation layer's.

### Follow-up: the analyze-terminal ordering hazard

Surfaced during discussion: the derived `failed(analyze)` reads `analysis.failed_at`, but the analyze failure path writes `state = ANALYSIS_FAILED` and **not** `failed_at` until Phase 81 (FAIL-01). Phase 77 backfilled the marker once; there is no live writer. Cutting the read over first narrows the belt-and-suspenders secondary net guarding the 44.5K-job over-enqueue class. Same applies to metadata (`report_metadata_failed` persists nothing until FAIL-02).

| Option | Description | Selected |
|--------|-------------|----------|
| Pull failure-marker writes into 80 | Phase 80 also cuts the analyze/metadata failure writers — coupled writer touch, overlaps FAIL-01/02 | |
| Reorder: 81 before 80 | Make Phase 81 upstream of Phase 80 so `failed_at` is written before recovery derives from it | ✓ |
| Accept temporary narrowing | Rely on the primary ledger-clear net; document the rare callback-partial-failure gap as interim | |

**User's choice:** Reorder — Phase 81 before Phase 80.
**Notes:** Establishes the governing principle: *a read-cutover phase must follow the writer-cutover phase that keeps its derived source live.* Also resolves the metadata side of D-01 for free. Requires a ROADMAP `Depends on` edit.

---

## AWAITING/push sidecar map

Verified against `main` before asking: **`CloudJobStatus.AWAITING` has no live writer** (Phase 77 added the enum value, CHECK, partial index, and a one-time backfill only). Every live writer still writes `FileRecord.state = AWAITING_CLOUD` (`agent_push.py:261`, `pipeline.py:345`, `agent_s3.py:195`, `reconcile:212`). **`PUSHED` has no sidecar status at all** — the CHECK list has no `'pushed'` member. SIDECAR-01 (Phase 83) owns the live writers, and is downstream of Phase 80.

| Option | Description | Selected |
|--------|-------------|----------|
| Reorder SIDECAR-01 before 80 | Make Phase 83 upstream too — same principle as the 81 reorder | ✓ |
| Scope-exclude cloud reads from 80 | Cut only the enrich-stage reads; leave AWAITING/PUSHED state reads until Phase 83 | |
| Derive from live facts only | Derive held/push-done from in-flight `cloud_job` + ledger + analyze-terminal, no SIDECAR-01 | |
| Pull cloud writer dual-write into 80 | Phase 80 dual-writes AWAITING/PUSHED to the sidecar, then reads it | |

**User's choice:** Reorder SIDECAR-01 before 80.
**Notes:** Second application of the same principle. Phase 80's `Depends on` becomes 78, 79, 81, 83 — it moves to after all its writer-cutover dependencies.

---

## Reconcile scope boundary

Scouting found `reconcile_cloud_jobs.py`'s read side is **already** sidecar-derived (`cloud_job WHERE status IN (SUBMITTED, RUNNING)`), with zero `FileRecord.state` reads. Its only `FileRecord` coupling is the at-cap spill-back **write** at `:212`.

| Option | Description | Selected |
|--------|-------------|----------|
| Audit + regression guard only | Verification deliverable: assert zero state reads; leave the `:212` write to Phase 83 | |
| Also retire the AWAITING write here | Phase 80 retires `:212` so its two named files are fully state-free | ✓ |

**User's choice:** Also retire the AWAITING write here.
**Notes:** Widens Phase 80 slightly beyond pure reads, into the single residual write in one of its two named files.

### Follow-up: ownership deconfliction

The above answer is in tension with the area-2 reorder — if Phase 83 is upstream and migrates cloud-routing writers, it could already cover reconcile's spill-back. Asked to nail the split so the planner doesn't double-own the line.

| Option | Description | Selected |
|--------|-------------|----------|
| 80 owns reconcile; 83 excludes it | Phase 83 migrates the other cloud writers + reads; Phase 80 owns `reconcile:212` | ✓ |
| 83 owns all cloud writers incl. reconcile | Phase 80's reconcile deliverable reverts to audit + regression guard | |

**User's choice:** 80 owns reconcile; 83 excludes it.
**Notes:** "Phase 80 owns its two named files end-to-end." Requires a ROADMAP scope-exclusion note on Phase 83. The MKUE-04 clean-before-flip ordering at `:174-219` must survive the write swap byte-for-byte.

---

## Pending-import removal

The *whether* was locked by the phase goal (this is its core rationale). Discussion focused on the *shape*: deriving `done` directly inverts the set-size characteristic — today the **pending** sets are small and bounded; the **done** set is most of a 200K corpus.

| Option | Description | Selected |
|--------|-------------|----------|
| Ledger-scoped done query | Derive `done` only for the `file_id`s in the ledger rows already read this run — `O(\|ledger\|)`, never `O(200K)` | ✓ |
| Full-corpus done sets | Materialize whole-corpus done sets, mirroring today's analyze/push SELECTs | |
| Per-row correlated EXISTS | Evaluate `done` per ledger row at filter time — N+1 queries | |

**User's choice:** Ledger-scoped done query.
**Notes:** Recovery only ever asks about files that appear in the ledger, so bounding the done-set queries to `fids` preserves the existing read-once-per-run shape while keeping the inverted set small.

---

## Claude's Discretion

- Internal shape of the ledger-scoped done-set helper (one query per stage vs. a single `stage_status_case` query bucketed in Python).
- Whether the `_ANALYZE_DONE` / `_PUSH_DONE` / `_METADATA_PENDING` / `_FINGERPRINT_PENDING` key constants get renamed to reflect the done-not-pending inversion.
- Bound-parameter chunking strategy for `id IN fids` if the ledger grows large.
- Mechanism of the "zero `FileRecord.state` reads" regression guard (AST check, import guard, or source assertion) — an existing project idiom wins.
- Test-bucket placement for the new regression tests (must pass via `just test-bucket <bucket>` in isolation).

## Deferred Ideas

- **PROV-01 (N-compute-aware orphan recovery)** — design §9 non-goal; Phase 80 touches the done-set derivation, not `select_active_agent(kind="compute")`. Re-check the overlap, do not fix.
- **Phase 82 read-before-write inversion check** — whether READ-01/READ-02 hit the same D-02/D-03 hazard. Belongs to Phase 82's discussion.
- **Retiring the other cloud-routing state writers** (`agent_push.py:261`, `pipeline.py:345`, `agent_s3.py:195`) — SIDECAR-01 / Phase 83.
- **Cloud-push lane drain (`--profile drain`) quiesce** before the destructive `033` — Phase 90's rollout runbook.

---
---

# Session 2 — 2026-07-10 (refresh, against `main` @ `09cefc6d`)

**Areas discussed:** Folded todos, Push-done derivation, WR-02 twin divergence, Reconcile write swap, Backfill sequencing

**Why a refresh.** Session 1 was gathered against `main` @ `76c0f3e8`. Phases 81 (`191a8c79`),
83 (`6855cfe2`) and 84 (`09cefc6d`) have since merged. This **resolved D-02 and D-03** (the two
reorders Session 1 argued for) and invalidated several of Session 1's verified-against-`main` facts.
Every code claim in Session 2 was re-verified against `09cefc6d`.

**What Session 1 got right and what changed:**
- `FAILURE_IS_TERMINAL` existed in no `.py` when Session 1 cited it; Phase 81 created it (`enums/stage.py:87`).
- `CloudJobStatus.AWAITING` had no live writer; Phase 83 added `hold_awaiting_cloud` (`services/backends.py:86`).
- **Still true:** `CloudJobStatus` has no `'pushed'` member. Phase 83's D-12 *deliberately* refused a
  universal pushing/pushed predicate rather than adding one.

---

## Existing context handling

| Option | Description | Selected |
|--------|-------------|----------|
| Update it | Load existing 80-CONTEXT.md and refresh against new main | ✓ |
| View it | Display without changing | |
| Skip | Leave as-is | |

**User's choice:** Update it

---

## Folded Todos

| Option | Description | Selected |
|--------|-------------|----------|
| Fold as blocking prerequisite | Backfill is a hard precondition of the cutover | ✓ |
| Fold as context only | Record as hazard; planning decides sequencing | ✓ |
| Review but don't fold | Leave as a milestone / Phase 79 follow-up | |

**User's choice:** Fold as blocking prerequisite + fold as context
**Notes:** `analysis-completed-at-backfill.md` (severity: major, found by Phase 84 UAT test 8).
Production: 1050 files at `state='analyzed'`, only 49 with `analysis_completed_at` set, **1001 NULL**.
Surfaced during discussion that the todo's framing understates the impact — it presents this as a
shadow-gate failure, but for Phase 80 it is an **over-enqueue safety regression introduced by the
cutover itself**: today `_select_done_analyze_ids` reads `state IN (ANALYZED, ANALYSIS_FAILED)` and
marks all 1050 done; afterwards `done_clause(ANALYZE)` requires `analysis_completed_at IS NOT NULL`,
so any of the 1001 still holding a ledger row is re-enqueued for a 4h re-analysis. The todo's options
2 (soft-allowlist) and 3 (accept non-zero `hard_fail_total`) repair the *gate* while leaving
`done_clause(ANALYZE)` false for all 1001 rows — only option 1 (the backfill) closes the hazard.

---

## Push-done derivation

| Option | Description | Selected |
|--------|-------------|----------|
| Compute-scoped sidecar OR analyze-done | `cloud_job.status=='succeeded' OR domain_completed_clause(ANALYZE)` | ✓ |
| Collapse into `domain_completed(analyze)` | Single predicate; loses the landed-but-not-analyzed window | |
| Row-existence past `uploading` | `status IN ('uploaded','submitted','running','succeeded')` | |

**User's choice:** Compute-scoped sidecar OR analyze-done
**Notes:** Verified `_enqueue_push_file` (`services/backends.py:154`) is called only from
`ComputeAgentBackend.dispatch`; Kueue dispatches via `_stage_file_to_s3`. So a `push_file` ledger row
*implies compute*, and 83 D-12's compute/kueue status collision — the reason no universal `pushed`
predicate exists — does not apply. No registry lookup needed, so `stage_status.py` stays config-free
per 78 D-04. Option 2 rejected: a file mid-analysis on compute scratch would re-drive and re-rsync a
large file (regressing Phase 50 D-10). Option 3 rejected: `SUBMITTED` means *still pushing* on the
compute lane.

### Follow-up: the awaiting-candidate clause

| Option | Description | Selected |
|--------|-------------|----------|
| Extract a shared named clause builder | One builder, three call sites | ✓ |
| Re-spell inline a third time | Lowest blast radius; duplicates the conjunct | |
| Consume via `get_cloud_staging_candidates` | Reuse the query, not the clause | |

**User's choice:** Extract a shared named clause builder
**Notes:** A bare `cloud_job.status == 'awaiting'` read would wrongly include files mid-local-analysis:
83 D-13 keeps the `LOCAL_ANALYZING` flip while D-14 reaps the inert `awaiting` row only at the
analyze-*terminal* seams (`agent_analysis.py:266,390`). Recovery *routes* on this set (CLOUDROUTE-02,
compute-only), so the error would send a locally-analyzing file to a compute agent. Option 3 rejected
on its face: `get_cloud_staging_candidates` takes `with_for_update(of=CloudJob, skip_locked=True)` and
a LIMIT window — recovery would take row locks and see a partial set.

### Follow-up: where the builder lives

| Option | Description | Selected |
|--------|-------------|----------|
| `services/stage_status.py` | Beside the LOCKED builders it composes | ✓ |
| `services/backends.py` | Beside `hold_awaiting_cloud`, its writer | |
| `services/pipeline.py` | Where two of three call sites live | |

**User's choice:** `services/stage_status.py`
**Notes:** Does not violate 83 D-12's rejection of `pushing_clause`/`pushed_clause` in this module —
that turned on the `backends.toml` registry dependency, and this clause needs only a status literal.
`backends.py` rejected as the delicate end of a managed import cycle (module-top
`from phaze.tasks.reconcile_cloud_jobs import _reconcile_one`).

---

## WR-02 twin divergence

| Option | Description | Selected |
|--------|-------------|----------|
| Call-site gate on `enqueued_at` | Compare `ledger.enqueued_at` to `metadata.failed_at`; twins unchanged | ✓ |
| Clear `metadata.failed_at` at retry-enqueue | Eliminate the cell at source; reverses 81 D-11 | |
| Add `~inflight_clause` to the SQL twin | WR-02's literal suggestion — **dangerous** | |

**User's choice:** Call-site gate on `enqueued_at`
**Notes:** Investigation showed **WR-02's own proposed fix is a trap.** `inflight_clause` is
*scheduling-ledger row existence* (`stage_status.py:175`), and every recovery candidate **is** a
ledger row by construction — so adding the disjunct makes `domain_completed` return `False` for every
candidate, disabling the secondary over-enqueue net wholesale (the 44.5K incident class). It would be
a silent no-op for the drain and the count card, which already `AND ~inflight_clause`, so their tests
would stay green.

Also established the ambiguous cell exists for **metadata only**: `retry_analysis_failed`
(`routers/pipeline.py:956`) clears `analysis.failed_at` before enqueuing (Phase-81 CR-01 fix), while
`retry_metadata_failed` deliberately leaves it set (81 D-11). `SchedulingLedger.enqueued_at`
(`models/scheduling_ledger.py:63`) disambiguates an orphaned retry (`enqueued_at > failed_at`, must
re-drive) from a callback-partial-failure (`enqueued_at < failed_at`, must stay terminal).

### Follow-up: locking the rejected option

| Option | Description | Selected |
|--------|-------------|----------|
| Regression test + rejected-option docstring | Test both cells; document the trap in `domain_completed_clause` + the equivalence SCOPE comment | ✓ |
| Regression test only | Behavioral test; leave the SCOPE comment as-is | |
| Also un-exclude the seeds | Assert the twins deliberately disagree | |

**User's choice:** Regression test + rejected-option docstring

---

## Reconcile write swap

| Option | Description | Selected |
|--------|-------------|----------|
| Reuse `hold_awaiting_cloud`, clears stay inline | Fourth caller of 83's single awaiting writer | ✓ |
| Extend `hold_awaiting_cloud` with clear flags | Fully DRY; grows the shared writer for one caller | |
| Bespoke sidecar UPDATE in reconcile | No reuse; re-creates a hand-copied writer | |

**User's choice:** Reuse `hold_awaiting_cloud`, clears stay inline
**Notes:** Discovered that `reconcile_cloud_jobs.py`'s at-cap spill **violates the hard `awaiting_cloud`
shadow invariant on `main` today** — it writes `state = AWAITING_CLOUD` while stamping
`cloud_job.status = FAILED` (`shadow_compare.py:131`, `soft=False`). Phase 83 could not fix it because
Session 1's D-04 scoped this file to Phase 80. Also noted `hold_awaiting_cloud` stamps only
`status`/`attempts`/optional `cloud_phase`, so `inadmissible = False` and `staging_bucket = None` must
stay inline; and the loaded ORM object's `status` must not be pre-mutated or autoflush races the CAS's
`WHERE`.

### Follow-up: dual-write or retire?

| Option | Description | Selected |
|--------|-------------|----------|
| Keep the dual-write, gate behind CAS | Mirror `agent_push.py:307`; write dies in Phase 90 (83 D-00c) | |
| Retire the write now, as D-04 said | Zero `FileRecord.state` coupling in this file | ✓ |
| Retire the write AND add a shadow invariant | Compensate with gate tightening | |

**User's choice:** Retire the write now, as D-04 said
**Notes:** Chosen **against** the initial recommendation, which favored 83 D-00c's dual-write pattern.
Verified safe before recording: at reconcile time a kueue file sits at `state = PUSHED`
(`agent_s3.py:128`); the `pushed` shadow invariant is `_cloud_job_exists` — any `cloud_job` row, any
status (`shadow_compare.py:68`) — so an `awaiting` row satisfies it and the gate stays green. The
drain and the "Awaiting cloud" card both derive from `cloud_job`, so the spilled file is still picked
up and counted. `file.updated_at` no longer being bumped is harmless: 83 D-07 already moved the
lane-entry staleness clock to `cloud_job.updated_at`, which the CAS bumps. Accepted cost: display
reads of `FileRecord.state` show `PUSHED` until Phase 90, and reconcile becomes the first
cloud-routing writer to stop dual-writing.

---

## Backfill sequencing

| Option | Description | Selected |
|--------|-------------|----------|
| Inside Phase 80's PR as `036` | Cutover + its data land atomically | ✓ |
| Standalone migration first, own PR | Smallest per-PR blast radius; adds a deploy prerequisite | |
| Backfill + reclassify together | Also soft-allowlist `analyzed` | |

**User's choice:** Inside Phase 80's PR as `036`
**Notes:** Migrations run to `035`, so `036` is next free. Mirrors Phase 81's precedent (migration
`033` shipped in its own PR alongside the writers depending on it). Established that `033`'s
constraint is a **NAND, not a strict XOR** despite its name —
`NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)` (`models/analysis.py:56`) — so
both-NULL is legal and the backfill must carry a `failed_at IS NULL` guard or it aborts on every
analyze-failed row.

### Follow-up: the numbering drift

| Option | Description | Selected |
|--------|-------------|----------|
| De-number the docs, one-time correction | Stop hardcoding; fix the `034` collision in passing | ✓ |
| Renumber to `037`, keep hardcoding | Follow 81 D-08 literally | |
| Defer the sweep to Phase 90 | Smallest diff now; collision stays live | |

**User's choice:** De-number the docs, one-time correction
**Notes:** `034` currently names BOTH Phase 83's shipped `034_backfill_cloud_awaiting.py` (recorded at
`ROADMAP.md:416`) and Phase 90's planned destructive migration (`ROADMAP.md:21,36,535,542`;
`REQUIREMENTS.md:98`). Third renumber in the milestone: 81 D-08 applied `033→034`; 83 D-04 predicted
`034→035` but never applied it; Phase 84 then took `035`. `just docs-drift` does not check migration
numbers, so nothing catches this class of drift.

---

## Claude's Discretion (Session 2)

- Internal shape of the ledger-scoped done-set helper; whether `_ANALYZE_DONE` / `_PUSH_DONE` /
  `_METADATA_PENDING` / `_FINGERPRINT_PENDING` constants are renamed for the done-not-pending inversion.
- Chunking strategy for the `id IN fids` bind list.
- Mechanism of the "zero `FileRecord.state` reads" regression guard.
- The name of the extracted awaiting-candidate clause builder.
- Whether `036` and the D-14 doc de-numbering land in the same commit.
- Whether `analysis.updated_at` or `created_at` is the better backfill source (verify `TimestampMixin` first).
- Test-bucket placement for the new regression tests (must pass via `just test-bucket <bucket>` in isolation).

## Deferred Ideas (Session 2)

- **PROV-01** — N-compute-aware orphan recovery. Design §9 non-goals it; re-check the overlap, do not fix.
- **Clearing `metadata.failed_at` at retry-enqueue** — the considered alternative to D-10; reverses 81 D-11
  and drops retried files from the failed-metadata UI count during the retry window.
- **Un-excluding the `*_inflight` equivalence seeds** by asserting deliberate disagreement.
- **Phase 82 read-before-write inversion check** for READ-01/READ-02.
- **Soft-allowlisting `analyzed`** / accepting a non-zero `hard_fail_total` — belongs to Phase 79/90 gate design.
- **`report_uploaded`'s redundant `state == PUSHING` guard** (`agent_s3.py:128`) — 83 left the symmetry to research.
- **Phase 79's deferred live-corpus shadow-compare run (SC-3)** — `036` is a precondition for it going green.
- **Dropping the unused `FileState` import** from `reconcile_cloud_jobs.py:45` once D-04 lands.

## Superseded from Session 1

- "Cloud-push lane drain quiesce before the destructive `033`" — the destructive migration is no longer
  `033` (Phase 81 took it). Per D-14, planning docs now refer to it by role, not number.
- "Retiring the other cloud-routing state writers" — **done**, shipped by Phase 83.
