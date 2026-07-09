# Phase 83: Cloud-Routing Sidecar Cutover - Research

**Researched:** 2026-07-09
**Domain:** Consistency-domain collapse — cloud-routing state (`AWAITING_CLOUD`/`PUSHING`/`PUSHED`/`LOCAL_ANALYZING`) onto the `cloud_job` sidecar + derived `in_flight(analyze)`, in an async SQLAlchemy 2.0 / asyncpg / Postgres 18 codebase
**Confidence:** HIGH (this is an internal-codebase phase; every load-bearing claim below is `[VERIFIED: <file>:<line>]` against HEAD of branch `SimplicityGuy/phaze-83`)

## Summary

This is not a library-research phase. It is a **read-the-code-and-resolve-nine-decisions** phase over a codebase whose architecture is already locked by five upstream decisions (D-05, D-06, D-09, D-10, D-12) and three prior shipped phases (77 additive schema, 78 derivation layer, 79 shadow gate, 81 failure markers). Zero new dependencies are permitted (milestone hard constraint, REQUIREMENTS "Out of Scope"). Therefore there is **no Standard Stack table, no Package Legitimacy Audit** — the stack is the existing one documented in CLAUDE.md.

The reshaping discovery (CONTEXT D-01) is verified: **no go-forward writer of `cloud_job.status='awaiting'` exists.** `trigger_analysis` holds a long file with a bare `file.state = FileState.AWAITING_CLOUD` at `routers/pipeline.py:346` `[VERIFIED: pipeline.py:346]`, and `routers/pipeline.py` never imports `CloudJob`. Only migration `032`'s one-shot `_BACKFILL_CLOUD_AWAITING` ever wrote such a row `[VERIFIED: 032:96-102]`. Because `services/shadow_compare.py:131` asserts `AWAITING_CLOUD ⇒ cloud_job(status='awaiting')` as a **hard** invariant `[VERIFIED: shadow_compare.py:131]`, every file held since `032` violates it. This phase must add the writer AND repair the corpus, or the deferred live-corpus shadow run (79 D-02) fails.

**Primary recommendation:** Implement all nine delegated decisions along the CONTEXT.md recommendations — they are internally consistent and each is verified sound below. The single sharpest risk is the drain-candidate re-pick window (SC#3), which the ROADMAP designates a *hard gate*: build the D-08 integration test (two sequential `stage_cloud_window` ticks) before committing the drain query. Migration `034` (repair) is confirmed available (alembic head is `033`).

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Route decision (long-file hold, spill-to-local) | API/control (`routers/pipeline.py`, `services/backend_selection.py`) | — | Routing is a control-plane policy; agents are Postgres-free and never decide routing |
| Cloud-routing state of record | Database (`cloud_job` sidecar) | — | The phase's thesis: one atomic consistency domain lives in `cloud_job.status` + `scheduling_ledger` (for `in_flight`) |
| Drain candidacy | API/control cron (`tasks/release_awaiting_cloud.py` + `services/pipeline.py` query) | Database (partial index + row lock) | Candidacy predicate reads the sidecar; Postgres enforces atomicity via `FOR UPDATE OF cloud_job SKIP LOCKED` |
| Dispatch route flips | API/control (`services/backends.py` three `dispatch` impls) | Database (in-txn `cloud_job` write) | Each backend writes its own sidecar row in the caller's uncommitted txn; drain owns the single commit |
| Callback CAS guards | API/control (`routers/agent_s3.py`, `routers/agent_push.py`) | Database (rowcount-guarded UPDATE) | Agent reports outcome via URL-path `file_id` (AUTH-01); the control plane CAS's on `cloud_job.status` |
| Corpus repair | Database (migration `034`) | — | One-shot backfill of the missing sidecar rows; SQL-only, sync migration |

## User Constraints (from CONTEXT.md)

### Locked Decisions (DO NOT re-open — research honored these as given)

- **D-00a:** `AWAITING_CLOUD` = a `cloud_job` row `status='awaiting'` (s3_key/upload_id NULL) on the existing sidecar — no new table/column. `LOCAL_ANALYZING` = no sidecar row, exactly `in_flight(analyze)`.
- **D-00b:** `in_flight` is authoritative from `scheduling_ledger`; `saq_jobs` never flips the boolean.
- **D-00c:** Writers dual-write `FileRecord.state`; only *reliance* on it is replaced. SC#1 forbids `FileRecord.state` **routing reads**, not writes and not display reads. The `state` write dies in Phase 90.
- **D-00d:** The Phase-79 shadow gate must stay green; its contract is **implication, not equality** (79 D-04). `LOCAL_ANALYZING` is soft-allowlisted; `awaiting_cloud`/`pushing`/`pushed` are hard.
- **D-05:** Drain exclusion is a **predicate conjunct**, NOT row deletion: `cloud_job.status='awaiting' AND NOT inflight_clause(ANALYZE) AND NOT domain_completed_clause(ANALYZE)`. `LocalBackend.dispatch` stays a no-`cloud_job`-row writer.
- **D-06:** `with_for_update(of=CloudJob, skip_locked=True)` — lock the table the candidacy predicate lives on.
- **D-09:** `cloud_job.status` is the CAS anchor for `report_upload_failed`; the `FileRecord` dual-write is gated behind its rowcount.
- **D-10:** On `rowcount == 0` → FULL no-op (no S3 ops, no ledger clear). Commit, return `200 cleared=False`.
- **D-12:** Per-endpoint kind-specific CAS; NO universal PUSHING/PUSHED predicate. `enums/stage.py` stays DB-free AND config-free.

### Claude's Discretion (the nine to resolve — recommendations + binding constraints)

D-02, D-03, D-04, D-07, D-08, D-11, D-13, D-14, D-15 — each carries a recommendation and a binding constraint. See the dedicated `## Resolving the Nine Delegated Decisions` section below, which verifies each recommendation against the code and endorses or refines it.

### Deferred Ideas (OUT OF SCOPE)

- `get_pushing_count` / `get_pushed_count` — unowned gap; a hard Phase-90 blocker, not this phase.
- `report_uploaded`'s redundant `FileRecord.state == PUSHING` guard (`agent_s3.py:128`) — retiring for symmetry is a candidate but not decided; see the "left to research" note below.
- A shadow-gate converse invariant for the new awaiting writer — deliberately not added (implication one-directional).
- `MAX_FINGERPRINT_ATTEMPTS` / mixed-engine fingerprint retry hole — untouched.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SIDECAR-01 | Cloud-routing status represented via `cloud_job` sidecar (and/or derived `in_flight(analyze)`), with `/pushed`, `/mismatch`, `/upload-failed` CAS behavior preserved or strengthened; closes `agent_s3.py:195` bug | The drain-query cutover (D-05/D-06), the three dispatch flips, the four callback guards, the go-forward awaiting writer (D-01/D-02), the corpus repair (D-04), and the D-11 advisory-lock strengthening are all mapped to verified code sites below. SC#2 regression test + SC#3 integration test defined in `## Validation Architecture`. |

## Ground-Truth Verification of CONTEXT Assertions

Every factual claim CONTEXT.md rests on was re-verified against HEAD. Results:

| Assertion | Verdict | Evidence |
|-----------|---------|----------|
| Alembic head is `033` (Phase 81); `034` is free | **CONFIRMED** | `alembic/versions/033_add_analysis_completed_xor_failed.py` `revision="033" down_revision="032"`; no `034*.py` exists `[VERIFIED: ls alembic/versions]` |
| `FAILURE_IS_TERMINAL`, `domain_completed()` shipped in `enums/stage.py` | **CONFIRMED** | `FAILURE_IS_TERMINAL = {ANALYZE: True, METADATA: True, FINGERPRINT: False}` at `stage.py:87`; `domain_completed()` at `stage.py:186` `[VERIFIED: enums/stage.py:87,186]` |
| `domain_completed_clause()` (SQL twin) shipped in `services/stage_status.py` | **CONFIRMED** | `stage_status.py:170`; `inflight_clause` at `:150`; both correlate on `FileRecord.id` `[VERIFIED: stage_status.py:150,170]` |
| No go-forward `cloud_job.status='awaiting'` writer; `pipeline.py` never imports `CloudJob` | **CONFIRMED** | `trigger_analysis` sets bare `file.state = FileState.AWAITING_CLOUD` at `pipeline.py:346`; `grep CloudJob src/phaze/routers/pipeline.py` → no match `[VERIFIED: pipeline.py:346]` |
| Hard shadow invariant `AWAITING_CLOUD ⇒ cloud_job(status='awaiting')` at `shadow_compare.py:131`, `soft=False` | **CONFIRMED** | `Invariant("awaiting_cloud", ..., _cloud_awaiting, soft=False)`; `_cloud_awaiting` checks `status == "awaiting"` exactly `[VERIFIED: shadow_compare.py:82,131]` |
| The SC#2 bug: unguarded `FileRecord.state = AWAITING_CLOUD` write | **CONFIRMED** | `agent_s3.py:195` — `update(FileRecord)...values(state=FileState.AWAITING_CLOUD)` with no CAS predicate on the current status `[VERIFIED: agent_s3.py:195]` |
| `IN_FLIGHT` status tuple excludes `'awaiting'` | **CONFIRMED** | `backends.py:76` = `(UPLOADING, UPLOADED, SUBMITTED, RUNNING)` — no AWAITING `[VERIFIED: backends.py:74-81]` |
| `ix_cloud_job_awaiting` is a partial index on `(file_id) WHERE status='awaiting'` | **CONFIRMED** | `models/cloud_job.py:122` mirrors `032:153` byte-for-byte `[VERIFIED: cloud_job.py:110-123]` |
| `_BACKFILL_CLOUD_AWAITING` INSERT shape reusable for `034` | **CONFIRMED** | `INSERT INTO cloud_job (id, file_id, status) SELECT gen_random_uuid(), f.id, 'awaiting' FROM files f WHERE f.state = 'awaiting_cloud' ON CONFLICT (file_id) DO NOTHING` `[VERIFIED: 032:96-102]` |
| `_delete_staged_object_if_cloud` on both analyze-terminal seams | **CONFIRMED** | `put_analysis` → `agent_analysis.py:264`; `report_analysis_failed` → `agent_analysis.py:381`; guarded on a `cloud_job` row existing (all-local = zero S3 calls) `[VERIFIED: agent_analysis.py:110,264,381]` |
| `report_uploaded` gates on `resolved_non_local_kind(settings)=="kueue"` | **CONFIRMED** | `agent_s3.py:122` `[VERIFIED: agent_s3.py:122]` |
| `pg_advisory_xact_lock(hashtext(ledger_key))` donor exists on `/mismatch`, absent on `/upload-failed` | **CONFIRMED** | present `agent_push.py:240`; the `/upload-failed` RMW at `agent_s3.py:176-180,241-242` has NO serialization `[VERIFIED: agent_push.py:240; agent_s3.py:176-180,241-242]` |
| `ComputeAgentBackend.dispatch` already promotes an `awaiting` row via `on_conflict_do_update` | **CONFIRMED** | `backends.py:326-338` upserts on `file_id` conflict, `set_={backend_id, status=SUBMITTED}` `[VERIFIED: backends.py:326-338]` |
| Doc lines for the `034→035` renumber have shifted since 81 D-08 | **CONFIRMED** | see `## Migration 034` — 81 D-08's "line 485" is now ROADMAP:497 |

## Resolving the Nine Delegated Decisions

Each recommendation below is verified sound against the code and **endorsed** unless noted.

### D-02 — the awaiting writer's call site — ENDORSE: shared `services/` helper

A single helper (stamp `file.state = AWAITING_CLOUD` + upsert the `cloud_job` `status='awaiting'` row in the caller's session, **never committing**) reused by three sites:
1. hold path — `trigger_analysis` at `pipeline.py:346` (today the bare `file.state=` write) `[VERIFIED: pipeline.py:346]`;
2. `report_upload_failed` over-cap spill — `agent_s3.py:190-195` `[VERIFIED: agent_s3.py:190-195]`;
3. `report_push_mismatch` over-cap spill — `agent_push.py:261,279-283` `[VERIFIED: agent_push.py:261,279-283]`.

**Verified constraint:** the dispatch discipline — `dispatch` mutates in the caller's session and NEVER commits; the drain owns the single post-loop commit under the advisory lock `[VERIFIED: backends.py:147-152; release_awaiting_cloud.py:257]`. The helper MUST honor this (no commit inside the helper). The helper's upsert must use `pg_insert(CloudJob)...on_conflict_do_update(index_elements=["file_id"], set_={"status": ..., "attempts": ...})` because `uq_cloud_job_file_id` makes a plain INSERT unsafe when a terminalized row already exists (the spill case) `[VERIFIED: cloud_job.py:76]`.

**Placement (the "left to research" note):** `services/backends.py` already imports `pg_insert`, `CloudJob`, `CloudJobStatus`, `FileState` and is imported by both `pipeline.py` and the two routers' call graph without a cycle — it is the natural home. A new module is unnecessary. **Recommendation: `services/backends.py` (or a thin `services/cloud_routing.py` if `backends.py` growth is a concern; either is acceptable, no cycle risk since `backend_selection.py` imports `backends`, not vice-versa)** `[VERIFIED: backend_selection.py:35,44]`.

### D-03 — the spilled file's `cloud_job` row — ENDORSE: re-stamp to `status='awaiting'`, retain `attempts` spent

Verified: `'awaiting' ∉ IN_FLIGHT` `[VERIFIED: backends.py:74-81]`, so re-stamping a spilled row to `awaiting` does NOT inflate any backend's `in_flight_count` (which counts `backend_id == self.id AND status IN {UPLOADING,UPLOADED,SUBMITTED,RUNNING}`) `[VERIFIED: backends.py:178-190]`. Verified: `select_backend` reads `cloud_attempts` (from `cloud_job.attempts` via `_cloud_attempts_for`) and excludes cloud when `attempts >= cloud_submit_max_attempts` `[VERIFIED: backend_selection.py:97-99; release_awaiting_cloud.py:88-95]`. So retaining `attempts = cloud_submit_max_attempts` correctly routes the re-stamped `awaiting` row to **local** on the next drain tick.

Both ruled-out alternatives confirmed unsound: widening the drain to `status IN ('awaiting','failed')` breaks the hard `AWAITING_CLOUD ⇒ status='awaiting'` invariant `[VERIFIED: shadow_compare.py:82,131]`; terminalize-then-insert-fresh is blocked by `uq_cloud_job_file_id` `[VERIFIED: cloud_job.py:76]`. **The re-stamp is exactly the D-02 helper applied on the spill path** (this is why D-02's helper must be reused here, not hand-copied). Note the current spill writes `status=FAILED, cloud_phase=None, attempts=spent` `[VERIFIED: agent_s3.py:190-194; agent_push.py:279-283]`; the change is `FAILED → awaiting` (keep `cloud_phase=None`, keep `attempts` spent).

### D-04 — repairing the already-held corpus — ENDORSE: repair migration `034`

`034` re-runs `032`'s exact `_BACKFILL_CLOUD_AWAITING` statement `[VERIFIED: 032:96-102]` with its native `ON CONFLICT (file_id) DO NOTHING` (idempotent; skips files that already have any cloud_job row). SQL-only, **sync** migration (`def upgrade()`, plain `op.execute(...)`; only `env.py` is async) `[VERIFIED: 032 is sync]`. Both alternatives confirmed unsound: the drain-upsert makes the drain a writer of what it reads AND keeps a `FileRecord.state` read (violates SC#1); the quiesce is unsound because `--profile drain` empties `PUSHING`/`uploading`, not the parked `AWAITING_CLOUD` set, and `trigger_analysis` refills it continuously `[VERIFIED: pipeline.py:346]`.

**No CHECK/index change is needed for `034`** — the `'awaiting'` CHECK value and `ix_cloud_job_awaiting` already landed in `032` `[VERIFIED: 032:144-153; cloud_job.py:114,122]`. Therefore `034` touches **no ORM-mapped schema** (pure data backfill), so the 77 D-01 empty-autogenerate-diff concern does **not** bite here (there is nothing new to mirror into `__table_args__`). Confirm autogenerate stays empty after `034` as a belt-and-braces check anyway.

**Landing choice (the "left to research" note):** `034` + the Phase-90 renumber can land in this PR (small, mechanical) or its own. Recommendation: **land `034` in this phase's PR** — it is the corpus-repair half of D-01's writer addition and belongs with it; the Phase-90 renumber is a doc-only edit (see `## Migration 034`).

### D-07 — FIFO key + staleness clock — ENDORSE: FIFO on `FileRecord.created_at`, move staleness clock to `cloud_job.updated_at`

Verified: the drain today orders `ORDER BY FileRecord.created_at ASC` `[VERIFIED: pipeline.py:1260]` and `select_backend` reads `file.updated_at` as the lane-entry timestamp `[VERIFIED: backend_selection.py:29-31,108]`. The binding constraint is real: once Phase 90 removes the dual-written `file.state = AWAITING_CLOUD`, nothing stamps `file.updated_at` at lane entry, silently breaking `cloud_spill_to_local_after_seconds` `[VERIFIED: backend_selection.py:108]`. Keep FIFO on `FileRecord.created_at` (immutable discovery order — byte-identical to today, preserves the "held months ago sorts first" property) and pass `cloud_job.updated_at` (or a value derived from it) into `select_backend` instead of `file.updated_at`. The D-02 helper's upsert stamps `cloud_job.updated_at` at hold time (TimestampMixin `onupdate`), giving the staleness clock a Phase-90-durable home.

**Implementation note:** `select_backend`'s signature currently reads `file.updated_at` directly `[VERIFIED: backend_selection.py:108]`. The cleanest minimal change is to pass the awaiting row's `updated_at` explicitly (like `cloud_attempts` is already passed explicitly per the RESEARCH signature note) rather than reading it off `file`. The drain loop already fetches per-candidate cloud data via `_cloud_attempts_for` `[VERIFIED: release_awaiting_cloud.py:205]` — extend that read (or the candidate query's returned columns) to also surface `cloud_job.updated_at`.

### D-08 — SC#3 gate shape — ENDORSE: hermetic pytest in the `integration` bucket, live run deferred

Mirror 79 D-01/D-02. Two sequential `stage_cloud_window` ticks across the three outcomes; assert each file dispatched exactly once and never to a cloud backend after a local dispatch. See `## Validation Architecture` for the full test map. The live-corpus run is deferred to the next homelab rollout and recorded in VERIFICATION (79 D-02 precedent). **Bucket: `integration`** (`tests/integration/`) — verified that `test_shadow_compare.py` and the DERIV-04 equivalence test live there `[VERIFIED: ls tests/integration]`. Must pass via `just test-bucket integration` in isolation. Consider an `EXPLAIN`-based assertion that the plan uses `ix_cloud_job_awaiting` — but see D-14 for why the reaper, not the assertion, is the real defense.

### D-11 — advisory lock on `/upload-failed`'s attempt RMW — ENDORSE: in scope, WITH the concurrency regression test

Verified exact self-deadlock hazard: `/upload-failed`'s under-cap path calls `cloud_staging.redrive_upload → stage_file_to_s3` which enqueues on the same `s3_upload:<file_id>` key `[VERIFIED: agent_s3.py:224; cloud_staging.py:146]`; that enqueue's `before_enqueue` hook opens its own session and upserts the same ledger row, so a `with_for_update` row lock here self-deadlocks — identical to the documented `/mismatch` rationale `[VERIFIED: agent_push.py:230-239]`. Remedy: copy `agent_push.py:240`'s `select(func.pg_advisory_xact_lock(func.hashtext(ledger_key)))` verbatim, placed BEFORE the RMW read at `agent_s3.py:176` `[VERIFIED: agent_push.py:240]`. The donor concurrency test is `report_push_mismatch`'s T-73-13 (see `## Validation Architecture`). **Explicitly ruled out by CONTEXT:** "add the lock, skip the test" — this project has shipped untested mitigations before (memory `project_security_closed_not_tested`). If planning judges D-11 out of scope, it must be recorded as a deferred idea naming the T-73-13 donor, not silently dropped.

### D-13 — `LocalBackend.dispatch`'s `LOCAL_ANALYZING` flip — ENDORSE: keep the flip (dual-write)

Verified: the flip `file.state = FileState.LOCAL_ANALYZING` at `backends.py:234` `[VERIFIED: backends.py:234]`; its only consumer was the old drain predicate (`state == AWAITING_CLOUD`), which D-05 replaces with the conjunct. Keeping it is D-00c dual-write and is safe under 79 D-04's implication-not-equality: `LOCAL_ANALYZING` is soft-allowlisted at `shadow_compare.py:151` `[VERIFIED: shadow_compare.py:151]`. Dropping it would leave the file `state=AWAITING_CLOUD` while analyzing locally, inflating `get_awaiting_cloud_count` (D-15) for no gain. "Keep the flip AND delete the awaiting row" is ruled out (D-05 rejected deletion). **Note the interaction with D-14:** after this flip the file still carries its `awaiting` cloud_job row (LocalBackend writes no row and deletes none), which the conjunct correctly excludes via `NOT inflight_clause(ANALYZE)` — but that inert row is the D-14 index-growth hazard.

### D-14 — reap the inert `awaiting` row — ENDORSE: reap at the two analyze-terminal seams

Verified the growth hazard: `ix_cloud_job_awaiting` is partial on `status='awaiting'` alone `[VERIFIED: cloud_job.py:122]`, and the ORDER BY is on `files.created_at` (a column NOT in the index), so the drain must scan every `awaiting` cloud_job row, join to files, sort, and limit each `*/5` tick. Without a reaper, every long file ever analyzed locally accretes a permanent `awaiting` row (LocalBackend deletes nothing) and the dead set grows monotonically. Recommendation: at `put_analysis` (`agent_analysis.py:264`) and `report_analysis_failed` (`agent_analysis.py:381`) — the two seams that already call `_delete_staged_object_if_cloud` and already open a txn — add `DELETE FROM cloud_job WHERE file_id = :fid AND status = 'awaiting'` `[VERIFIED: agent_analysis.py:264,381]`. A cloud-analyzed file's row is `SUCCEEDED`/`RUNNING`, so the `status='awaiting'` filter leaves it untouched. The row-deletion precedent is `scan_deletion.py:110` `delete(CloudJob).where(...)` `[VERIFIED: scan_deletion.py:110]`. **This reaper — not an EXPLAIN assertion — is the real defense against the D-14 degradation.** Rejecting the drain-side reap: it makes the drain a writer of what it reads and only reaches rows inside the tick's LIMIT window (never catches up once the dead set outgrows it).

### D-15 — the three cloud-lane count cards — ENDORSE: close `get_awaiting_cloud_count` only

Verified `get_awaiting_cloud_count` reads `FileRecord.state == AWAITING_CLOUD` at `pipeline.py:1121` `[VERIFIED: pipeline.py:1112-1123]` — a display read, not a routing read (so SC#1 permits leaving it, but D-15 recommends closing it because *this phase is what makes it disagree with the drain*). Derive it from the SAME clause the drain uses so card and drain cannot diverge: `COUNT(cloud_job) WHERE status='awaiting' AND NOT inflight_clause(ANALYZE) AND NOT domain_completed_clause(ANALYZE)` — i.e. the count of genuinely-parked (non-locally-dispatched) awaiting rows. Keep it `_safe_count`-wrapped (degrade-safe, poll never 500s) `[VERIFIED: pipeline.py:1119-1123]`. Ruled out: taking all three — `get_pushing_count`/`get_pushed_count` require resolving the compute/kueue status collision (D-12's rejected universal predicate); they are an unowned Phase-90 blocker `[VERIFIED: pipeline.py:1206-1236]`.

## Architecture Patterns

### System data flow (the one atomic consistency domain)

```
                        ┌──────────────────────────────────────────────┐
   long file discovered │  trigger_analysis (routers/pipeline.py:346)  │
   (duration >= thresh) │  D-02 helper: file.state=AWAITING_CLOUD       │
                        │            + upsert cloud_job status='awaiting'│  <-- NEW WRITER (D-01)
                        └───────────────────────┬──────────────────────┘
                                                │ (commit)
                                                ▼
                        ┌──────────────────────────────────────────────┐
    */5 cron            │  stage_cloud_window tick (advisory-locked)    │
    (release_awaiting)  │  snapshot each backend cap/availability       │
                        │  get_cloud_staging_candidates(limit):         │
                        │    SELECT files JOIN cloud_job                 │
                        │    WHERE cloud_job.status='awaiting'           │  <-- D-05 conjunct
                        │      AND NOT inflight_clause(ANALYZE)          │      (replaces state read)
                        │      AND NOT domain_completed_clause(ANALYZE)  │
                        │    ORDER BY files.created_at LIMIT :n          │  <-- D-07 FIFO
                        │    FOR UPDATE OF cloud_job SKIP LOCKED         │  <-- D-06 lock
                        └───────────────┬──────────────────────────────┘
                                        │ select_backend(rank-first, D-07 clock=cloud_job.updated_at)
                 ┌──────────────────────┼───────────────────────────┐
                 ▼                      ▼                           ▼
        LocalBackend.dispatch   ComputeAgentBackend.dispatch   KueueBackend.dispatch
        state=LOCAL_ANALYZING   state=PUSHING                  state=PUSHING
        NO cloud_job write      upsert cloud_job=SUBMITTED     stage S3 + cloud_job=UPLOADING
        (D-13 keep flip)        (promotes awaiting row)        (existing upsert)
        [awaiting row stays,    backend_id=self.id             backend_id + staging_bucket
         reaped at analyze-     │                              │
         terminal, D-14]        ▼ push_file                    ▼ presigned PUT
                         compute agent rsync             kueue agent S3 upload
                                │                              │
                                ▼ POST /pushed                 ▼ POST /uploaded, /failed
                    agent_push.report_pushed         agent_s3.report_uploaded / report_upload_failed
                    CAS status=='submitted'          CAS status=='uploading' (/uploaded)
                    (D-12 compute-kind)              CAS status IN('uploading','uploaded') (/upload-failed, D-09)
                                                     rowcount==0 -> FULL no-op (D-10)
                                                     + pg_advisory_xact_lock on attempt RMW (D-11)
                                        │
                                        ▼ analyze terminal (put_analysis / report_analysis_failed)
                                  DELETE cloud_job WHERE status='awaiting'  <-- D-14 reaper
                                  + _delete_staged_object_if_cloud (existing)
```

### Pattern 1: rowcount-guarded idempotent CAS on `cloud_job.status`
**What:** UPDATE gated on the current status, then branch on `res.rowcount`.
**When:** every callback guard (`/uploaded`, `/upload-failed`; `/pushed`, `/mismatch` mirror it on `FileRecord.state` today and stay compute-kind per D-12).
**Example (the D-09 anchor for `report_upload_failed`, replacing the unguarded `agent_s3.py:195`):**
```python
# Source: pattern donor agent_push.py:258-272 (report_push_mismatch over-cap CAS) [VERIFIED]
res = cast("CursorResult[Any]", await session.execute(
    update(CloudJob)
    .where(CloudJob.file_id == file_id, CloudJob.status.in_([CloudJobStatus.UPLOADING.value, CloudJobStatus.UPLOADED.value]))
    .values(status=CloudJobStatus.AWAITING.value, cloud_phase=None, attempts=settings.cloud_submit_max_attempts)  # D-03 re-stamp
))
if res.rowcount == 0:
    await session.commit()   # D-10 FULL no-op: no FileRecord write, no S3 abort/delete, no ledger clear
    return UploadFailedResponse(file_id=file_id, cleared=False)
# rowcount != 0: NOW do the FileRecord dual-write, S3 cleanup, ledger clear (all gated behind the rowcount)
```

### Pattern 2: dispatch never commits; the drain owns the single post-loop commit
**What:** `Backend.dispatch` mutates in the caller's uncommitted session under the tick advisory lock; a mid-loop commit would drop the lock and re-open the over-stage class (Landmine L1).
**Applies to:** the D-02 helper (must not commit) and any new awaiting write on a dispatch/spill path `[VERIFIED: backends.py:147-152; release_awaiting_cloud.py:257-264]`.

### Pattern 3: correlated `exists(...)` clause reuse — never re-spell a predicate
**What:** `inflight_clause` / `domain_completed_clause` are the drift-locked SQL twins of the DB-free `enums/stage.py` resolver (DERIV-04 equivalence test). Compose them verbatim in the drain's `.where(...)`; do NOT hand-write the ledger/analysis EXISTS `[VERIFIED: stage_status.py:150,170; enums/stage.py:186]`.

### Anti-Patterns to Avoid
- **Row deletion to exclude a locally-dispatched file** — the symmetric-looking fix that D-05 rejects: `stage_cloud_window` rolls back the whole tick on a poisoned txn `[VERIFIED: release_awaiting_cloud.py:257-264]` while `process_file`'s ledger row was already committed by the `before_enqueue` hook's own session → a rolled-back tick restores the deleted `awaiting` row while a job is queued → tick N+1 re-picks and can cloud-dispatch a file with analysis in flight. That is the double-dispatch SC#3 forbids.
- **Locking only `files`** — leaves the deciding column (`cloud_job.status`) on an unlocked table, readable stale; the tick's advisory lock does not cover the callback routers or the reconcile cron that mutate `cloud_job` concurrently (D-06).
- **Leaking `backends.toml` into `enums/stage.py`** — a universal PUSHING/PUSHED predicate would break the DB-free AND config-free purity 78 D-04 / 26 D-03 established (D-12).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Exclude in-flight/terminally-failed analyze from the drain | A fresh ledger/analysis EXISTS subquery | `inflight_clause(Stage.ANALYZE)` + `domain_completed_clause(Stage.ANALYZE)` | Drift-locked twins; re-spelling breaks the DERIV-04 equivalence test `[VERIFIED: stage_status.py:150,170]` |
| Serialize the `/upload-failed` attempt RMW | `with_for_update` on the ledger row | `pg_advisory_xact_lock(hashtext(key))` | A row lock self-deadlocks against the `before_enqueue` hook's own session (D-11) `[VERIFIED: agent_push.py:230-240]` |
| Promote an `awaiting` row on cloud dispatch | A DELETE + fresh INSERT | `pg_insert(...).on_conflict_do_update(index_elements=["file_id"])` | `uq_cloud_job_file_id` forbids two rows; the upsert already exists `[VERIFIED: backends.py:326-338; cloud_job.py:76]` |
| Repair the un-sidecar'd corpus | An idempotent upsert in the drain | Migration `034` re-running `032`'s `_BACKFILL_CLOUD_AWAITING` | Drain-upsert violates SC#1 (writer of what it reads + a state read) `[VERIFIED: 032:96-102]` |
| Delete the staged S3 object on the CAS-miss path | Keep the abort/delete on the no-op | FULL no-op (D-10) | A late callback could delete an object a live Kueue job is mid-download on; `_delete_staged_object_if_cloud` owns cleanup on both analyze-terminal paths `[VERIFIED: agent_analysis.py:264,381]` |

**Key insight:** almost every building block this phase needs already exists in-tree (the CAS donor, the advisory-lock donor, the upsert, the backfill SQL, the two clause builders, the reaper precedent). The phase is primarily **wiring and anchor-swapping**, not new construction — which is exactly why the risk concentrates in the drain-query correctness (SC#3), not in novel code.

## Runtime State Inventory

This is a rename/representation-cutover phase, so the inventory applies.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | **`cloud_job` rows missing for every `AWAITING_CLOUD` file held since `032`** — the D-01 corpus gap. `pipeline.py:346` has written bare `file.state=AWAITING_CLOUD` with no sidecar row since `032` shipped `[VERIFIED: pipeline.py:346]` | **Data migration `034`** (backfill) + **code edit** (D-02 writer) — both required, they are different tasks |
| Live service config | None affecting cloud routing. The `*/5` `stage_cloud_window` cron is code-registered on the control worker (`PHAZE_ROLE=control`), not stored external config `[VERIFIED: release_awaiting_cloud.py:108-110]` | None |
| OS-registered state | None — no Task Scheduler / systemd / pm2 state embeds cloud-routing status | None (verified: cloud routing is entirely in-DB) |
| Secrets/env vars | None renamed. `cloud_submit_max_attempts`, `push_max_attempts`, `cloud_spill_to_local_after_seconds` are read by name and unchanged `[VERIFIED: backend_selection.py:97,108; agent_s3.py:183]` | None |
| Build artifacts | None — no package rename, no egg-info churn. `034` touches no ORM-mapped schema (CHECK/index already in `032`), so no autogenerate drift `[VERIFIED: 032:144-153]` | None (confirm empty autogenerate diff after `034`) |

**The canonical question — after every file in the repo is updated, what runtime systems still have the old representation?** Answer: the live Postgres corpus. Every file parked in `AWAITING_CLOUD` since `032` has no `cloud_job(status='awaiting')` row. Migration `034` repairs it; the D-02 writer prevents new gaps. This is the whole reason the phase is "not a pure reader cutover."

## Common Pitfalls

### Pitfall 1: The rolled-back-tick double-dispatch (the phase's sharpest regression)
**What goes wrong:** a file is locally dispatched (ledger row committed by the `before_enqueue` hook's own session), then the drain tick rolls back on a later poisoned statement, and next tick re-picks the file and cloud-dispatches it with analysis in flight.
**Why it happens:** the drain's whole-tick rollback vs. the hook's independently-committed ledger row `[VERIFIED: release_awaiting_cloud.py:257-264]`.
**How to avoid:** the D-05 predicate conjunct (`NOT inflight_clause(ANALYZE)`) survives the rollback because the committed ledger row alone re-excludes the file — this is precisely why D-05 chose the conjunct over row deletion.
**Warning signs:** SC#3 integration test's "rolled-back tick with a committed ledger row" case dispatches a file twice or to a cloud backend.

### Pitfall 2: `FOR UPDATE OF cloud_job` on an outer join / stale-read of the deciding column
**What goes wrong:** locking `files` instead of `cloud_job` leaves the candidacy column (`cloud_job.status`) unlocked and readable stale under READ COMMITTED against concurrent callback/reconcile mutations.
**Why it happens:** the candidacy predicate moved from `files.state` to `cloud_job.status`; the lock must move with it (D-06).
**How to avoid:** `select(FileRecord).join(CloudJob, CloudJob.file_id == FileRecord.id)` (INNER join — a candidate must have an awaiting row) with `.with_for_update(of=CloudJob, skip_locked=True)`. INNER (not outer) join is required so `FOR UPDATE OF cloud_job` names a guaranteed-present row; Postgres rejects `FOR UPDATE` against the nullable side of an outer join.
**Warning signs:** `EXPLAIN` shows a seq scan on `cloud_job`, or a `FOR UPDATE cannot be applied to the nullable side of an outer join` error.

### Pitfall 3: Cartesian blow-up from mis-correlated EXISTS inside the join
**What goes wrong:** `inflight_clause`/`domain_completed_clause` fail to correlate to the outer `FileRecord` once a `.join(CloudJob)` is added.
**Why it doesn't happen (verified):** both builders correlate by the referenced ORM column `FileRecord.id` inside `exists(select(...).where(X.file_id == FileRecord.id))` `[VERIFIED: stage_status.py:98,167]`, not positionally. Adding a `.join(CloudJob)` to the outer query does not change which entity the subquery's `FileRecord.id` reference resolves to. **[MEDIUM confidence — verify with the SC#3 test's `EXPLAIN` that no cartesian appears and row counts match expectations at fixture scale.]**

### Pitfall 4: The `s3_upload:<file_id>` ledger row is never cleared on success
**What goes wrong:** assuming `report_uploaded` clears the `s3_upload` ledger row (it does not — only the `/upload-failed` over-cap terminal path calls `clear_ledger_entry` at `agent_s3.py:204`) `[VERIFIED: grep clear_ledger_entry]`.
**Why it matters here:** D-10's FULL no-op path must NOT clear the ledger (matching that the success path also doesn't manage it), and D-11's advisory lock is keyed on `hashtext("s3_upload:"+file_id)` — the same key `stage_file_to_s3` enqueues on. This is a pre-existing lifecycle quirk, not introduced by this phase; do not "fix" it in scope.
**Warning signs:** a plan task that adds a ledger clear to `report_uploaded` or to the D-10 no-op path.

### Pitfall 5: Non-hermetic test failure in isolation (documented project class)
**What goes wrong:** a new test passes in the full suite but fails via `just test-bucket integration` in isolation.
**Why it happens:** `get_settings` `lru_cache` leak and `saq_jobs` stub poison (memory `reference_ci_bucket_isolation`).
**How to avoid:** run the new SC#2/SC#3 tests via `just test-bucket integration` and `just test-bucket agents`/`just test-bucket analyze` in isolation before committing; export both DB URLs (see Environment Availability — `MIGRATIONS_TEST_DATABASE_URL` footgun).

## Migration 034: repair migration + the Phase-90 renumber

**`034` (this phase) — additive/data-only, sync:**
```python
# Source: re-run of 032's _BACKFILL_CLOUD_AWAITING [VERIFIED: 032:96-102]
op.execute("""
INSERT INTO cloud_job (id, file_id, status)
SELECT gen_random_uuid(), f.id, 'awaiting'
FROM files f
WHERE f.state = 'awaiting_cloud'
ON CONFLICT (file_id) DO NOTHING
""")
# downgrade(): no-op or DELETE FROM cloud_job WHERE status='awaiting' AND ... — document lossiness.
```
Head is `033` `[VERIFIED: 033 down_revision="032"]`, so `034` chains `down_revision="033"`. Touches no ORM-mapped schema (the `'awaiting'` CHECK value and `ix_cloud_job_awaiting` shipped in `032`) → no `__table_args__` mirroring needed, autogenerate stays empty.

**Phase-90 destructive-migration renumber `034 → 035` — doc-only edits (line numbers re-verified against HEAD, they shifted since 81 D-08):**

| Doc | Current lines referencing `034` as the destructive migration | Verified |
|-----|--------------------------------------------------------------|----------|
| `.planning/ROADMAP.md` | 21, 25, 36, 281, 497, 504, 506 | `[VERIFIED: grep 034 ROADMAP.md]` (81 D-08's "485" is now 497) |
| `.planning/REQUIREMENTS.md` | 98 (MIG-04) | `[VERIFIED: grep 034 REQUIREMENTS.md]` |
| `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` | 352, 356 | `[VERIFIED: grep 034 DESIGN.md]` |

This is the same doc churn 81 D-08 already accepted. The renumber is mechanical; the risk is missing a reference — grep `\b034\b` across `.planning/` before Phase 90.

## Code Examples

### The D-05 drain query (cut over from `pipeline.py:1257-1263`)
```python
# Source: composed from stage_status.py:150,170 clause builders [VERIFIED] + D-05/D-06/D-07
from phaze.enums.stage import Stage
from phaze.services.stage_status import inflight_clause, domain_completed_clause

stmt = (
    select(FileRecord)                                   # return the entity (SC#1 permits selecting it)
    .join(CloudJob, CloudJob.file_id == FileRecord.id)   # INNER: candidate must have an awaiting row
    .where(
        CloudJob.status == CloudJobStatus.AWAITING.value,          # D-05 conjunct 1
        ~inflight_clause(Stage.ANALYZE),                           # D-05 conjunct 2 (reuse, do not re-spell)
        ~domain_completed_clause(Stage.ANALYZE),                   # D-05 conjunct 3 (Phase 81 twin)
    )
    .order_by(FileRecord.created_at.asc())               # D-07 FIFO on immutable discovery order
    .limit(limit)
    .with_for_update(of=CloudJob, skip_locked=True)      # D-06 lock the candidacy table
)
```

### The D-02 shared awaiting writer (no commit)
```python
# Source: dispatch discipline backends.py:147-152 [VERIFIED] + upsert backends.py:326-338 [VERIFIED]
async def hold_awaiting_cloud(session: AsyncSession, file: FileRecord, *, attempts: int = 0) -> None:
    """Stamp AWAITING_CLOUD + upsert the cloud_job awaiting row in the CALLER'S txn. NEVER commits."""
    file.state = FileState.AWAITING_CLOUD  # D-00c dual-write; dies in Phase 90
    stmt = pg_insert(CloudJob).values(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.AWAITING.value, attempts=attempts)
    stmt = stmt.on_conflict_do_update(index_elements=["file_id"],
                                      set_={"status": stmt.excluded.status, "attempts": stmt.excluded.attempts})
    await session.execute(stmt)
    # spill callers pass attempts=cloud_submit_max_attempts (D-03 budget-spent marker)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Cloud routing read from `FileRecord.state` (routing predicate) | Read from `cloud_job.status` + derived `in_flight(analyze)` | This phase (83) | SC#1 — no `FileRecord.state` routing read; state write survives to Phase 90 |
| Drain candidacy = `state == AWAITING_CLOUD`, lock `files` | `cloud_job.status='awaiting'` conjunct, lock `cloud_job` | This phase (D-05/D-06) | Survives the rolled-back-tick double-dispatch |
| `report_upload_failed` unguarded `FileRecord.state` write | CAS on `cloud_job.status`, FULL no-op on miss | This phase (D-09/D-10) | Closes the `agent_s3.py:195` clobber bug |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `with_for_update(of=CloudJob, skip_locked=True)` composes legally with the INNER join + `~exists(...)` conjuncts + ORDER BY + LIMIT under SQLAlchemy 2.0/asyncpg, and Postgres EvalPlanQual re-checks the locked-table qual | Pitfall 2/3 | If EPQ semantics differ from expectation, a concurrent callback could momentarily admit a stale candidate — but the sequential-tick advisory lock (D-05 note, `[VERIFIED: release_awaiting_cloud.py:135]`) is the primary serializer, not EPQ. The SC#3 test must assert behavior, not rely on EPQ nuance. `[ASSUMED]` — established SQLAlchemy/Postgres behavior but not executed in this session |
| A2 | Passing `cloud_job.updated_at` into `select_backend` instead of `file.updated_at` is a clean minimal change to the drain loop | D-07 | If the candidate query can't cheaply surface `cloud_job.updated_at`, the staleness-clock move needs an extra per-candidate read (like `_cloud_attempts_for`) — a small cost, not a blocker. `[ASSUMED]` |
| A3 | `security_enforcement` is unset in config → treat as enabled, but this phase introduces no new external input/auth surface | Security Domain | Low — the phase is internal state representation; AUTH-01 (path-only file_id, `extra='forbid'` bodies) is preserved unchanged `[VERIFIED: agent_s3.py:170; agent_push.py:202]` |

## Open Questions

1. **Should `report_uploaded`'s redundant `FileRecord.state == PUSHING` guard (`agent_s3.py:128`) move to the sidecar anchor now?**
   - What we know: once D-09 makes `cloud_job.status` the CAS domain, this second guard is belt-and-braces on a column Phase 90 removes `[VERIFIED: agent_s3.py:128]`.
   - What's unclear: whether symmetry justifies touching a working guard in this PR vs. deferring to Phase 90.
   - Recommendation: leave it (it is harmless dual-guarding and Phase 90 removes it anyway); note it as a deferred idea (already is). Do not expand scope.

2. **Does `034` + the Phase-90 renumber land in this PR or its own?**
   - Recommendation: land `034` here (it is the repair half of D-01); the renumber is doc-only and can ride along or be a follow-up — planner's call, low risk either way.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Postgres (ephemeral test DB) | integration/agents/analyze bucket tests + migration test | ✓ | via `just test-db` on port **5433** (NOT 5432) | none — required |
| `MIGRATIONS_TEST_DATABASE_URL` | `034` migration test | ⚠ must be exported | defaults to 5432 (wrong); `just test-bucket` does NOT export it | export `postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test` explicitly `[VERIFIED: justfile:215]` |
| `uv` | all commands | ✓ | project constraint — never bare `pip`/`pytest`/`mypy` | none |
| Redis (ephemeral) | task-queue-touching tests | ✓ | `just test-db` provisions on 6380 | none |

**Missing dependencies with fallback:** the `MIGRATIONS_TEST_DATABASE_URL` port footgun (memory `reference_migrations_test_db_port`) — migration tests fail *in isolation* looking like the colima flake but aren't. Export both DB URLs before running the `034` migration test.

## Validation Architecture

nyquist_validation is enabled (`workflow.nyquist_validation: true` `[VERIFIED: config.json]`).

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio, via `uv run pytest` (CLAUDE.md) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` + `tests/buckets.json` (CI shard map) |
| Quick run command | `uv run pytest tests/integration/test_<new>.py -x` |
| Bucket run (isolation) | `just test-bucket integration` / `just test-bucket agents` / `just test-bucket analyze` |
| Full suite command | `just test-db && uv run pytest` (export both DB URLs) |

### Phase Requirements → Test Map
| Req / SC | Behavior | Test Type | Automated Command | Bucket | File Exists? |
|----------|----------|-----------|-------------------|--------|-------------|
| SC#2 (SIDECAR-01) | Late/duplicate `/upload-failed` on an already-advanced file (cloud_job RUNNING/SUCCEEDED) matches 0 rows → FULL no-op, no clobber to AWAITING_CLOUD | regression | `just test-bucket agents` | agents | ❌ Wave 0 — extend `tests/agents/routers/test_agent_s3.py` |
| SC#2 (D-11) | Two concurrent `/upload-failed` for one file cannot each read the same attempt and lose an increment (advisory lock) | concurrency regression | `just test-bucket agents` | agents | ❌ Wave 0 — donor: `report_push_mismatch` T-73-13 in `tests/agents/routers/test_agent_push.py` |
| SC#3 | Two sequential `stage_cloud_window` ticks across (a) local dispatch, (b) rolled-back tick with committed ledger row, (c) terminally-failed local analyze — each file dispatched exactly once, never cloud after a local dispatch | integration (hard gate) | `just test-bucket integration` | integration | ❌ Wave 0 — new file in `tests/integration/`; drive via `tests/analyze/tasks/test_release_awaiting_cloud.py` + `test_staging_cron.py` fixtures |
| SC#1 | Drain query, three dispatch flips, four callback guards read/write `cloud_job` (or `in_flight`), no `FileRecord.state` routing read | static + behavioral | `just test-bucket analyze` + grep audit | analyze | ⚠ partial — `tests/analyze/services/test_backends.py`, `test_dispatch_snapshot.py` exist; extend |
| SC#3 (shadow green) | Shadow-compare gate stays green; the new awaiting writer + `034` repair make `awaiting_cloud` invariant pass on backfilled fixtures | integration | `just test-bucket integration` | integration | ⚠ extend `tests/integration/test_shadow_compare.py` |
| D-04 | `034` backfill is idempotent (ON CONFLICT DO NOTHING) and repairs the un-sidecar'd corpus | migration test | `MIGRATIONS_TEST_DATABASE_URL=...5433... uv run pytest <migration test>` | (migration) | ❌ Wave 0 |
| D-15 | `get_awaiting_cloud_count` derives from the drain clause; card and drain cannot disagree | unit | `just test-bucket analyze`/`shared` | analyze/shared | ❌ Wave 0 — extend `tests/shared/routers/test_pipeline.py` |

### Sampling Rate
- **Per task commit:** the touched bucket's quick run (e.g. `just test-bucket agents` for a callback-guard task) — must pass **in isolation** (documented non-hermetic class).
- **Per wave merge:** `just test-bucket integration` + `agents` + `analyze` + `shared`.
- **Phase gate:** full suite green (`just test-db && uv run pytest`) before `/gsd:verify-work`; per-module coverage floor 90; overall gate 95.

### Manual-Only / Deployment-Gated
- **Live-corpus shadow-compare run** (the D-01 repair proof against real data) is **deferred to the next homelab rollout** and recorded in VERIFICATION — the 79 D-02 precedent. The hermetic SC#3 test is the in-CI gate; the live run confirms the corpus repair on the real 200K set.
- **`EXPLAIN` index-usage assertion** (drain uses `ix_cloud_job_awaiting`, not seq scan) — optional in-test; the real defense against degradation is the D-14 reaper. If added, guard it as MEDIUM-value (plan shapes vary by row count / stats).

### Wave 0 Gaps
- [ ] `tests/integration/test_<sc3_drain>.py` — SC#3 two-tick double-dispatch gate
- [ ] `tests/agents/routers/test_agent_s3.py` additions — SC#2 CAS no-op + D-11 concurrency (donor T-73-13)
- [ ] `034` migration test (idempotent backfill) — export `MIGRATIONS_TEST_DATABASE_URL` on port 5433
- [ ] `tests/integration/test_shadow_compare.py` additions — `awaiting_cloud` invariant green post-writer + `034`
- [ ] `tests/shared/routers/test_pipeline.py` additions — `get_awaiting_cloud_count` derives from the drain clause
- [ ] fixture reuse from `tests/analyze/core/test_staging_cron.py` / `test_dispatch_snapshot.py` / `tests/analyze/tasks/test_release_awaiting_cloud.py` for driving `stage_cloud_window` (fake agents, `task_router` stubs)

## Security Domain

`security_enforcement` is unset → treated as enabled. This phase introduces **no new external input, no new endpoint, no auth-surface change** — it re-anchors internal consistency-domain writes. Applicable controls:

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V4 Access Control | yes (preserved) | AUTH-01: `file_id` on the URL PATH; agent identity from the token dependency; request bodies carry no identity and keep `extra='forbid'` `[VERIFIED: agent_s3.py:170; agent_push.py:202]` |
| V5 Input Validation | yes (preserved) | Pydantic request models; `body.detail` bounded diagnostic; migration `034` is parameter-free static SQL (no interpolation) `[VERIFIED: 032:96-102 shape]` |
| V6 Cryptography | no | — |

| Threat pattern | STRIDE | Standard Mitigation |
|----------------|--------|---------------------|
| Late/duplicate/spoofed callback clobbers an advanced file back to AWAITING_CLOUD | Tampering | The D-09 CAS on `cloud_job.status` + D-10 FULL no-op — this IS the phase's security deliverable (SC#2) |
| `/mismatch` reporter mis-attributes another agent's file | Spoofing | D-07 reporter-authorization (`agent.id == backend.agent_ref`) — preserved, never re-stamp `backend_id` from the token `[VERIFIED: agent_push.py:216-223]` |
| SQL injection via migration/backfill | Tampering | `034` is static parameter-free SQL; the drain uses ORM columns + bound params only |

## Sources

### Primary (HIGH confidence) — codebase at HEAD of `SimplicityGuy/phaze-83`
- `src/phaze/enums/stage.py:87,186` — `FAILURE_IS_TERMINAL`, `domain_completed()` (Phase 81 shipped)
- `src/phaze/services/stage_status.py:150,170` — `inflight_clause`, `domain_completed_clause`
- `src/phaze/services/pipeline.py:1112-1123,1206-1236,1248-1264` — count cards + drain query
- `src/phaze/services/backends.py:74-81,178-190,210-241,310-356,405-442,532-569` — `IN_FLIGHT`, three dispatch impls, resolvers
- `src/phaze/routers/agent_s3.py:105-146,149-247` — `report_uploaded`, `report_upload_failed` (SC#2 bug at :195)
- `src/phaze/routers/agent_push.py:100-170,173-299` — `report_pushed`, `report_push_mismatch` (D-09/D-10/D-11 donors)
- `src/phaze/routers/pipeline.py:339-359` — the D-01 hold path
- `src/phaze/tasks/release_awaiting_cloud.py:88-95,108-268` — the drain tick + advisory lock
- `src/phaze/services/backend_selection.py:20-119` — `select_backend`, the D-07 staleness clock
- `src/phaze/services/shadow_compare.py:70-152` — the hard/soft invariants
- `src/phaze/routers/agent_analysis.py:110,264,381` — the two analyze-terminal seams (D-14 reaper + D-10 safety)
- `src/phaze/models/cloud_job.py:38-123` — `CloudJobStatus.AWAITING`, CHECK, `ix_cloud_job_awaiting`
- `alembic/versions/032_add_derived_status_schema.py:96-102,144-156` — `_BACKFILL_CLOUD_AWAITING`, CHECK/index
- `alembic/versions/033_*` — confirmed head; `034` free
- `justfile:117-118,134-215` — `test-bucket`, `test-db`, `MIGRATIONS_TEST_DATABASE_URL`
- `.planning/` — CONTEXT.md, ROADMAP.md §Phase 83, REQUIREMENTS.md SIDECAR-01, PARALLEL-ENRICH-DAG-DESIGN.md §4/§6

### Secondary (MEDIUM confidence)
- SQLAlchemy 2.0 `with_for_update(of=...)` + `skip_locked` + LIMIT queue semantics; Postgres EvalPlanQual re-qual under READ COMMITTED — established behavior, not executed this session (Assumption A1)

### Tertiary (LOW confidence)
- None. This phase required no web/library research (zero new dependencies, internal cutover).

## Metadata

**Confidence breakdown:**
- Decision resolutions (the nine): HIGH — each recommendation verified against the exact code site it depends on
- Drain-query composition (D-05/D-06): HIGH on clause reuse and lock placement; MEDIUM on EvalPlanQual nuance (A1) — SC#3 test is the safety net
- Corpus repair `034` + renumber: HIGH — backfill statement and doc lines re-verified
- Pitfalls: HIGH — the rolled-back-tick and lock-placement pitfalls trace to verified rollback/commit boundaries

**Research date:** 2026-07-09
**Valid until:** ~2026-08-08 (30 days; stable internal codebase, but re-verify alembic head and the `034`-renumber doc lines if Phase 80/81/82 land intervening migrations or ROADMAP edits before planning)
