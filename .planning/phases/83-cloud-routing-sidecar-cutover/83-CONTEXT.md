# Phase 83: Cloud-Routing Sidecar Cutover - Context

**Gathered:** 2026-07-09
**Status:** Ready for planning

<domain>
## Phase Boundary

Make cloud routing (`AWAITING_CLOUD` / `PUSHING` / `PUSHED` / `LOCAL_ANALYZING`) **one atomic
consistency domain** backed by the `cloud_job` sidecar (and the derived `in_flight(analyze)`), so the
drain-candidate query, the three dispatch route flips, and the `/pushed` / `/mismatch` /
`/upload-failed` CAS guards all read and write the same source — closing the missing-CAS-guard bug at
`routers/agent_s3.py:195`.

**Requirements:** SIDECAR-01.

**In scope:**
- The **go-forward `cloud_job(status='awaiting')` writer** — it does not exist today (see D-01, the
  discovery that reshapes this phase). Shared by the hold path (`routers/pipeline.py:346`) and both
  over-cap spill paths (`routers/agent_s3.py:195`, `routers/agent_push.py:261`).
- The **drain-candidate query** (`services/pipeline.py:1248` `get_cloud_staging_candidates`) cut over
  to the sidecar + derivation layer.
- The **three dispatch route flips** — `LocalBackend` / `ComputeAgentBackend` / `KueueBackend`
  `.dispatch` (`services/backends.py:210,311,423`).
- The **four callback CAS guards** — `report_pushed`, `report_push_mismatch` (`routers/agent_push.py`),
  `report_uploaded`, `report_upload_failed` (`routers/agent_s3.py`).
- A **regression test** proving a late/duplicate `/upload-failed` cannot clobber an already-advanced
  file (SC#2), and an **integration test** proving no double-dispatch / re-pick window (SC#3, a
  ROADMAP-designated hard gate, not a recommendation).
- Repair of the already-held `AWAITING_CLOUD` corpus that carries no sidecar row (D-04).

**Out of scope:**
- `tasks/reconcile_cloud_jobs.py` — **Phase 80** owns it (ROADMAP scope exclusion; 80-CONTEXT D-04).
- `tasks/reenqueue.py` — **Phase 80** owns it *end-to-end* (80-CONTEXT D-04: "Phase 80 owns its two
  named files end-to-end … zero `FileRecord.state` reads"). This covers `_select_done_push_ids`
  (`:190`) and `_get_awaiting_cloud_ids` (`:200`), both of which are cloud-routing `state` reads.
  Confirmed against `SimplicityGuy/phase-80` during discussion — **no overlap**.
- The three enrich pending sets and `get_pipeline_stats` — **Phase 82** (READ-01 / READ-02).
- The destructive `FileState` column drop — **Phase 90**.
- `get_pushing_count` / `get_pushed_count` — an **unowned gap**; see Deferred Ideas.

**Sequencing:** rewired off Phase 82 to break the `80 → 83 → 82 → 80` cycle. 83 runs BEFORE 80 and 82.
It needs 78 (derivation layer), 79 (shadow gate), and 81 (the analyze failure marker that
`domain_completed(analyze)` reads — the conjunct D-05 depends on).

</domain>

<decisions>
## Implementation Decisions

### Upstream contract (carried forward — do not re-litigate)

- **D-00a:** `AWAITING_CLOUD` is a `cloud_job` row with `status='awaiting'` (`s3_key`/`upload_id`
  NULL) on the **existing** sidecar — no new table, no new column. `LOCAL_ANALYZING` gets **no
  sidecar row**; it is exactly `in_flight(analyze)`. (77-CONTEXT D-04 / D-05 / D-06.)
- **D-00b:** `in_flight` is authoritative from `scheduling_ledger`; `saq_jobs` never flips the
  boolean. (78-CONTEXT D-01 / D-02.)
- **D-00c:** **Writers dual-write.** `FileRecord.state` keeps being stamped; only *reliance* on it is
  replaced. The `state` write dies in Phase 90. SC#1 forbids `FileRecord.state` **routing reads**, not
  writes and not display reads. (81-CONTEXT D-05.)
- **D-00d:** The Phase-79 shadow-compare gate must stay green (SC#3). Its contract is **implication,
  not equality** (79-CONTEXT D-04) — a file may be `LOCAL_ANALYZING` *and* carry an `awaiting` row
  without violating anything, because `awaiting_cloud`'s invariant is one-directional
  (`state == AWAITING_CLOUD ⇒ row exists`). `LOCAL_ANALYZING` is soft-allowlisted (79 D-06);
  `awaiting_cloud` / `pushing` / `pushed` are **hard** (`services/shadow_compare.py:131,133,134`).

### The missing awaiting writer (D-01 — a discovery, not a choice)

- **D-01:** ⚠ **There is no go-forward writer of `cloud_job.status='awaiting'`.** `trigger_analysis`
  holds a long file with a bare `file.state = FileState.AWAITING_CLOUD`
  (`routers/pipeline.py:341-346`); `routers/pipeline.py` never imports `CloudJob`. Only migration
  `032`'s one-shot backfill ever wrote an `awaiting` row.

  **Consequence:** the *hard* shadow invariant `AWAITING_CLOUD ⇒ cloud_job(status='awaiting')`
  (`services/shadow_compare.py:131`, `soft=False`) is violated by **every file held since `032`** —
  which the deferred live-corpus run (79 D-02) would surface. This phase is **not a pure reader
  cutover**; it must add that writer.

  **Locked:** a go-forward writer MUST exist and MUST be **shared** by the hold path and both over-cap
  spill paths. Its call-site shape is Claude's discretion (see below).

### Drain-candidate cutover (D-05, D-06 — LOCKED)

- **D-05:** **The exclusion mechanism is a predicate conjunct, not row deletion.** The drain selects:

  ```
  cloud_job.status = 'awaiting'
    AND NOT inflight_clause(Stage.ANALYZE)
    AND NOT domain_completed_clause(Stage.ANALYZE)
  ```

  `LocalBackend.dispatch` stays a **no-`cloud_job`-row writer** (`services/backends.py:210-241`).

  **Why not deletion.** Today the *only* thing removing a locally-dispatched file from the candidate
  set is its `state = LOCAL_ANALYZING` flip, and the cutover deletes that guard's reader. Row deletion
  looks like the symmetric fix (compute/kueue promote the row via `on_conflict_do_update`) but it
  **fails on the rollback path**: `stage_cloud_window` rolls back the whole tick on a poisoned txn
  (`tasks/release_awaiting_cloud.py:264`), while `process_file`'s ledger row was already committed
  independently by the `before_enqueue` hook's own session. A rolled-back tick therefore leaves a
  queued job **plus** a restored `awaiting` row — tick *N+1* re-picks a file with analysis in flight
  and can dispatch it to a **cloud** backend. That is exactly the double-dispatch SC#3 forbids.
  The conjunct survives this: the committed ledger row alone re-excludes the file.

  `domain_completed(analyze)` (Phase 81 D-17) is what excludes a **terminally-failed** local analyze —
  `FAILURE_IS_TERMINAL[analyze] = True`, so a failed analyze is domain-complete and never re-picked.
  This is precisely the 81 dependency the ROADMAP dep-note names.

  **Note:** the re-pick hazard is **sequential-tick**, not concurrent. `stage_cloud_window` already
  takes a fixed transaction-scoped advisory lock (`release_awaiting_cloud.py:135`, WR-04/SCHED-02)
  that serializes overlapping ticks under a single post-loop commit.

- **D-06:** **`with_for_update(of=CloudJob, skip_locked=True)`.** The candidacy predicate now lives on
  `cloud_job`, so lock *that* table — Postgres re-evaluates the locked table's `WHERE` after acquiring
  the lock (EvalPlanQual). Locking only `files` would leave the deciding column on an unlocked table
  and readable stale, and the tick's advisory lock does **not** cover the callback routers
  (`/uploaded`, `/pushed`) or the reconcile cron, all of which mutate `cloud_job` concurrently.

### `/upload-failed` CAS guard (D-09, D-10 — LOCKED)

- **D-09:** **The CAS anchor is `cloud_job.status`, not `FileRecord.state`.**

  ```python
  update(CloudJob).where(CloudJob.file_id == file_id,
                         CloudJob.status.in_(['uploading', 'uploaded']))
  ```

  `rowcount == 0` → idempotent no-op. The `FileRecord` dual-write is **gated behind that rowcount**.
  Makes the sidecar the single CAS domain (the phase's "collapse"), survives Phase 90's state-write
  removal, and satisfies SC#1 — a `state` CAS is still a `FileRecord.state` routing read.

  Covers the named bug: an already-`ANALYZED` file's `cloud_job` reads `RUNNING` or `SUCCEEDED`, so
  the CAS matches 0 rows and cannot clobber it back to `AWAITING_CLOUD`.
  (Rejected: mirroring `report_push_mismatch`'s `state == PUSHING` guard — maximum symmetry, but it
  keeps a routing read this phase exists to remove and Phase 90 must redo it.)

- **D-10:** **On `rowcount == 0`, `report_upload_failed` does a FULL no-op** — no `cloud_job` write, no
  `FileRecord` write, **no multipart abort, no `delete_staged_object`**, no ledger clear. Commit,
  return `200 cleared=False`. Mirrors `report_push_mismatch`'s no-op exactly.

  **Safe because** `_delete_staged_object_if_cloud` owns the staged object on **both** analyze-terminal
  paths — `put_analysis` (`routers/agent_analysis.py:264`) and `report_analysis_failed` (`:381`). So
  `/upload-failed` is not the last line of defense against a leak (KSTAGE-04 still holds).

  **And it is the only safe option:** keeping the S3 cleanup on the no-op path would let a
  late/duplicate callback on a `cloud_job = RUNNING` file delete the object a live Kueue job is
  mid-download on, failing an analysis that was about to succeed.

### PUSHING / PUSHED derivation (D-12 — LOCKED)

- **D-12:** **No universal PUSHING/PUSHED predicate. Each callback CAS's on its own backend kind's
  `cloud_job.status`.**

  The two lifecycles collide on status values, so no status→state map works without knowing the
  backend kind:

  | | dispatch | after callback | then |
  |---|---|---|---|
  | **compute** (rsync, `agent_push`) | `state=PUSHING`, `cloud_job=SUBMITTED` | `/pushed` → `state=PUSHED`, `cloud_job=SUCCEEDED` | `process_file` runs on compute |
  | **kueue** (S3, `agent_s3`) | `state=PUSHING`, `cloud_job=UPLOADING` | `/uploaded` → `state=PUSHED`, `cloud_job=UPLOADED` | → `SUBMITTED` → `RUNNING` → `SUCCEEDED` |

  `SUBMITTED` = "still pushing" for compute but "already pushed" for kueue. `SUCCEEDED` = "pushed,
  analysis running" for compute but "the k8s Job finished" for kueue. This is why Phase 79 loosened
  both invariants to bare row-existence (`shadow_compare.py:133-134`).

  **No universal predicate is needed**, because every callback is already backend-kind-exclusive:
  - `report_pushed` / `report_push_mismatch` — compute only → CAS on `status == 'submitted'`
    (compute's single in-flight status, `backends.py` D-10). A kueue file cannot reach `/pushed`:
    `resolve_compute_backend` returns `None` for a kueue `backend_id` and the handler already returns
    a clean `200` hold (`agent_push.py:107`).
  - `report_uploaded` — kueue only → CAS on `status == 'uploading'` (already present at `:109`).
  - `report_upload_failed` — kueue only → CAS on `status IN ('uploading','uploaded')` (D-09).

  (Rejected: `pushing_clause()` / `pushed_clause()` in `services/stage_status.py` resolving kind via
  the Phase-67 registry — it would make the predicate module depend on `backends.toml`, breaking the
  DB-free **and** config-free purity that 78 D-04 / 26 D-03 established for the agent import boundary.
  Rejected: a new orthogonal `cloud_job.push_done_at` column — 77 D-04 explicitly chose "reuse the
  existing sidecar", and it adds migration-renumber churn.)

  The only reader that genuinely wants a universal distinction is the pair of UI count cards — see
  Deferred Ideas.

### Claude's Discretion

The operator delegated the following. Each carries **binding constraints** surfaced during discussion;
research and planning may choose the mechanism but must honor the constraint and the recommendation
where one is given.

- **D-02 — the awaiting writer's call site.** A shared `services/` helper (stamping `file.state` +
  upserting the `awaiting` row in the caller's txn, never committing — the `backends.py` dispatch
  discipline) vs. inline in `trigger_analysis` vs. a bulk post-loop upsert.
  **Constraint:** one writer, reused by the hold path *and* both spill paths — not three hand-written
  copies. **Recommended:** the shared helper.

- **D-03 — the spilled file's `cloud_job` row.** Today the spill paths write `cloud_job = FAILED`
  (with `attempts` marked spent) while the file goes to `AWAITING_CLOUD`.
  **Constraints:** (a) "keep `FAILED`, widen the drain to `status IN ('awaiting','failed')`" is
  **ruled out** — it breaks the hard shadow invariant `AWAITING_CLOUD ⇒ status='awaiting'`;
  (b) "terminalize the old row and insert a fresh awaiting row" is **ruled out** by
  `uq_cloud_job_file_id` (one row per file, 77 D-04). **Recommended:** re-stamp to `status='awaiting'`,
  retaining `attempts = cloud_submit_max_attempts` as the budget-spent marker that `select_backend`
  reads to route to local. Verify `'awaiting' ∉ IN_FLIGHT` (`services/backends.py:76`) so per-backend
  in-flight accounting is unaffected — it currently is not in that set.

- **D-04 — repairing the already-held corpus** (files with `state = AWAITING_CLOUD` and no sidecar
  row). Options: a repair migration `034` re-running `032`'s `INSERT … SELECT … 'awaiting'` with
  `ON CONFLICT DO NOTHING`; an idempotent upsert in the drain; or relying on the rollout quiesce.
  **Constraints:** the drain-upsert option makes the drain a writer of what it reads *and* keeps a
  `FileRecord.state` read in the drain — violating SC#1. The quiesce option is unsound: `--profile
  drain` empties `PUSHING`/`uploading`, **not** the parked `AWAITING_CLOUD` set, which every
  `trigger_analysis` refills, and Phase 83 lands long before Phase 90's rollout.
  **Recommended:** the repair migration. **Note:** it takes `034`, so Phase 90's destructive migration
  renumbers `034 → 035`, repeating the doc churn 81 D-08 already accepted (`.planning/ROADMAP.md`,
  `.planning/REQUIREMENTS.md` MIG-02/MIG-04, `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md`).
  Per 77 D-01 precedent, mirror any constraint into the ORM `__table_args__` so
  `alembic revision --autogenerate` still produces an empty diff.

- **D-07 — the FIFO key and the parked-row staleness clock.** Today: `ORDER BY FileRecord.created_at
  ASC`, and `services/backend_selection.py:30` reads `file.updated_at` as the lane-entry timestamp for
  the `cloud_route_max_wait_sec` spill (RESEARCH A3: "no writer touches the parked row").
  **Constraint (must address):** once Phase 90 removes the dual-written `file.state = AWAITING_CLOUD`,
  nothing stamps `file.updated_at` at lane entry and the staleness clock silently measures the wrong
  thing. Do not hand that landmine to a destructive-migration phase.
  **Recommended:** keep FIFO on `FileRecord.created_at` (immutable discovery order — byte-identical to
  today) and move the staleness clock to `cloud_job.updated_at`. Moving FIFO to `cloud_job.created_at`
  **changes ordering**: a file discovered months ago but held today gets a fresh row and sorts to the
  back, where today it sorts to the front.

- **D-08 — the SC#3 gate's shape.** **Constraint:** ROADMAP designates this a *hard gate*, not a
  recommendation. **Recommended:** mirror 79 D-01/D-02 — a hermetic pytest in the `integration` bucket
  driving two sequential `stage_cloud_window` ticks across the three outcomes (local dispatch;
  rolled-back tick with a committed ledger row; terminally-failed local analyze), asserting each file
  is dispatched exactly once and never to a cloud backend after a local dispatch; the live-corpus run
  deferred to the next homelab rollout and recorded in VERIFICATION. Must pass via
  `just test-bucket integration` **in isolation** (CI bucket isolation). Consider asserting the plan
  uses `ix_cloud_job_awaiting` rather than a seq scan, given the two new `EXISTS` conjuncts on a `*/5`
  cron over a 200K corpus.

- **D-11 — the missing `pg_advisory_xact_lock` on `/upload-failed`'s attempt RMW.** `/upload-failed`
  reads → `+1` → writes back `s3_upload_attempt` on the `s3_upload:<file_id>` ledger payload
  (`agent_s3.py:176-180, 241-242`) with **no serialization**. Its sibling `/mismatch` takes
  `pg_advisory_xact_lock(hashtext(ledger_key))` for the identical pattern (`agent_push.py:240`),
  specifically because a row lock would self-deadlock against the `before_enqueue` hook's own session.
  `/upload-failed`'s under-cap path calls `cloud_staging.redrive_upload` → `stage_file_to_s3`, which
  enqueues on that same `s3_upload:<file_id>` key — so the hook upserts the same ledger row from its
  own session. **The exact self-deadlock hazard and the exact remedy both apply.** Two concurrent
  `/failed` callbacks can each read `attempt = N` and lose an increment, letting a file exceed its
  bounded upload budget.
  **Recommended: in scope, WITH the concurrency regression test.** SIDECAR-01 says the CAS-guard
  behavior of `/pushed`, `/mismatch` and `/upload-failed` is "preserved or **strengthened**"; same
  endpoint, same transaction, exact donor, and this phase already rewrites that handler's guard.
  **Explicitly ruled out:** "add the lock, skip the test" — shipping an untested mitigation is a
  failure mode this project has hit before. If planning judges it out of scope, record it as a
  deferred idea naming the donor, not as silently absent.

- **D-13 — `LocalBackend.dispatch`'s `file.state = LOCAL_ANALYZING` flip** (`backends.py:234`). Its
  only consumer was the old drain predicate, which D-05 replaces.
  **Constraint:** "keep the flip *and* delete the awaiting row" is **ruled out** — D-05 rejected row
  deletion. **Recommended: keep the flip** (dual-write, D-00c). Dropping it leaves the file
  `state = AWAITING_CLOUD` while it analyzes locally, which visibly inflates the operator's
  "awaiting cloud" card (`get_awaiting_cloud_count`) for no gain. Keeping it is safe under 79 D-04's
  implication-not-equality contract (D-00d).

- **D-14 — who reaps the inert `awaiting` row.** A consequence of choosing the conjunct (D-05) over
  deletion: a locally-dispatched file keeps its `awaiting` row forever. It is inert for **correctness**
  (`'awaiting' ∉ IN_FLIGHT`; `backend_id` NULL on a hold; reconcile reads only `SUBMITTED`/`RUNNING`)
  but **not for performance**: `ix_cloud_job_awaiting` is a partial index on `status = 'awaiting'`
  alone, so every long file ever analyzed locally stays in it permanently and the `*/5` drain tick
  scans a monotonically growing dead set, filtering it with two `EXISTS` conjuncts. At 200K this
  degrades. **Constraint (must address): the index-growth hazard.**
  **Recommended:** reap at the analyze-terminal seams — `put_analysis`
  (`routers/agent_analysis.py:264`) and `report_analysis_failed` (`:381`) `DELETE` the file's
  `cloud_job` row `WHERE status = 'awaiting'`, in the transaction they already open. These are the exact
  two seams that already call `_delete_staged_object_if_cloud`; the analyze-terminal path already owns
  cloud cleanup. A cloud-analyzed file's row is `SUCCEEDED`/`RUNNING`, so the status filter leaves it
  untouched. (Rejecting the drain-side reap: it makes the drain a writer of what it reads, and only
  reaches rows inside the tick's `LIMIT` window — it never catches up once the dead set outgrows it.)

- **D-15 — the three cloud-lane count cards.** See Deferred Ideas for the full gap statement.
  **Recommended:** **83 closes `get_awaiting_cloud_count` only**, deriving it from the *same* clause
  builder the drain uses so the card and the drain cannot disagree. **Ruled out:** taking all three —
  counting `pushing`/`pushed` requires resolving the compute/kueue status collision, resurrecting the
  universal predicate D-12 deliberately rejected.

- Not raised, left to research: whether `report_uploaded`'s now-redundant `FileRecord.state == PUSHING`
  guard (`agent_s3.py:128`) should also move to the sidecar anchor for symmetry; whether the awaiting
  writer's helper lives in `services/backends.py` or a new module; whether migration `034` and the
  Phase-90 renumber land in this PR or its own; whether the shadow gate gains a new invariant now that
  a go-forward `awaiting` writer exists.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase requirements & roadmap
- `.planning/ROADMAP.md` §"Phase 83: Cloud-Routing Sidecar Cutover" (line 400) — goal, 3 success
  criteria, the `reconcile_cloud_jobs.py` scope exclusion, the deps-rewire note, and the
  research-at-plan-time flag on the drain-re-pick hazard.
- `.planning/REQUIREMENTS.md` — SIDECAR-01 (line 54, full text). READ-01/READ-02 (lines 46-47) define
  Phase 82's boundary; MIG-02/MIG-04 name the destructive migration by the number D-04 renumbers.
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` — §4/D-03 (lines 232-242: the `cloud_job`
  sidecar decision, `LOCAL_ANALYZING` = `in_flight(analyze)`, `AWAITING_CLOUD` needs a row);
  **line 264-265** (the `agent_s3.py:195` missing-CAS bug, stated verbatim); §6.1 (lines 225, 321-322:
  the state→derivation-source table); §6.2 (the shadow-gate invariant list + the quiesce requirement);
  §10 D-03 (line 423).

### Upstream phase context (locked decisions this phase inherits)
- `.planning/phases/77-additive-schema-rescan-wipe-fix-migration-032/77-CONTEXT.md` — **D-04** (the
  `awaiting` status member, not a new table), **D-05** (`LOCAL_ANALYZING` has no sidecar row),
  **D-06** (`032` ensures a row exists for `PUSHING`/`PUSHED`).
- `.planning/phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` — **D-01**
  (`in_flight` from `scheduling_ledger`), **D-02** (degrade-safe boolean), **D-04** (two-module split;
  `enums/stage.py` is DB-free — D-12 keeps it config-free too).
- `.planning/phases/79-shadow-compare-gate-live-corpus/79-CONTEXT.md` — **D-01/D-02** (the hermetic
  pytest gate + the deferred live run; the shape D-08 mirrors), **D-04** (implication-not-equality;
  comprehensive invariant scope), **D-06** (the soft allowlist: `FINGERPRINTED`, `LOCAL_ANALYZING`).
- `.planning/phases/81-per-stage-failure-persistence-retry-paths/81-CONTEXT.md` — **D-05** (the
  dual-write discipline), **D-15/D-17** (`FAILURE_IS_TERMINAL`, `domain_completed()` + its SQL twin —
  the predicate D-05's conjunct consumes).
- **Phase 80's context is NOT in this worktree.** It lives at
  `.planning/phases/80-recovery-re-enqueue-cutover/80-CONTEXT.md` on branch `SimplicityGuy/phase-80`;
  read it with `git show SimplicityGuy/phase-80:<path>`. Its **D-04** ("Phase 80 owns its two named
  files end-to-end") is what puts `tasks/reenqueue.py` and `tasks/reconcile_cloud_jobs.py` out of
  Phase 83's scope. Its **D-01** defines the same `domain_completed` formula D-05 relies on.

### Research (read before planning the drain query)
- `.planning/research/PITFALLS.md` — the drain / over-enqueue classes.
- `.planning/research/ARCHITECTURE.md` — the `domain_completed` formula (line 493); RESEARCH A3, cited
  by `services/backend_selection.py:30` for the parked-row `updated_at` staleness clock (D-07).

### Code the phase touches
- `src/phaze/routers/agent_s3.py:149-246` — `report_upload_failed`; **`:195` is the unguarded write
  (the SC#2 bug)**; `:176-180, 241-242` the unserialized attempt RMW (D-11); `:105-134`
  `report_uploaded`'s two existing rowcount guards (the CAS donor).
- `src/phaze/routers/agent_push.py:126` — `report_pushed`'s `state == PUSHING` CAS; **`:240`** the
  `pg_advisory_xact_lock` donor for D-11; **`:258-272`** `report_push_mismatch`'s guarded spill + full
  no-op — the D-10 donor.
- `src/phaze/routers/pipeline.py:341-346` — `trigger_analysis`'s `AWAITING_CLOUD` hold. **The file
  never imports `CloudJob`** — this is the D-01 gap.
- `src/phaze/services/pipeline.py:1248` — `get_cloud_staging_candidates`, the drain-candidate query
  (D-05/D-06). `:1113, 1207, 1225` — the three cloud-lane count cards (D-15 / Deferred).
- `src/phaze/services/backends.py:210-241` (`LocalBackend.dispatch`, the `LOCAL_ANALYZING` flip, D-13);
  `:311-360` (`ComputeAgentBackend.dispatch`, the `pg_insert(...).on_conflict_do_update` upsert at
  `:326-334` that promotes an `awaiting` row); `:423-441` (`KueueBackend.dispatch`); **`:74-80`** the
  `IN_FLIGHT` status tuple (`'awaiting'` is correctly absent — D-03 must keep it so).
- `src/phaze/tasks/release_awaiting_cloud.py:135` (the tick advisory lock), `:173` (the candidate
  call), `:257-264` (single post-loop commit / whole-tick rollback — the D-05 rationale).
- `src/phaze/services/stage_status.py:150` (`inflight_clause`), `:170` (`domain_completed_clause`) —
  the two builders D-05's conjunct composes. **Reuse; do not re-spell.**
- `src/phaze/enums/stage.py:87` (`FAILURE_IS_TERMINAL`), `:186` (`domain_completed`).
- `src/phaze/models/cloud_job.py:50` (`AWAITING`), `:112-122` (the status CHECK + `ix_cloud_job_awaiting`
  partial index — the D-14 growth hazard).
- `src/phaze/routers/agent_analysis.py:264, 381` — the two `_delete_staged_object_if_cloud` seams
  (the D-10 safety argument and the D-14 reaper site).
- `src/phaze/services/backend_selection.py:30, 113` — the parked-row staleness clock (D-07).
- `src/phaze/services/shadow_compare.py:82, 131, 133-134, 151` — `_cloud_awaiting`, the three hard cloud
  invariants, and the `LOCAL_ANALYZING` soft allowlist.
- `src/phaze/services/scan_deletion.py:110` — `delete(CloudJob).where(...)`, the row-deletion precedent
  (relevant to D-14's reaper, not to D-05).
- `alembic/versions/032_add_derived_status_schema.py:98, 144-153` — the one-shot `'awaiting'` backfill
  and the CHECK/index the D-04 repair migration re-runs against.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`report_push_mismatch`'s over-cap branch (`agent_push.py:258-293`)** — a complete, correct guarded
  spill: CAS on the current state → `rowcount == 0` → clean idempotent 200 with *no* terminalization
  and *no* ledger clear. D-09/D-10's donor; the only change is the anchor column.
- **`pg_advisory_xact_lock(hashtext(ledger_key))` (`agent_push.py:240`)** — the serialized-RMW donor
  for D-11, complete with the written rationale for why a row lock self-deadlocks against the
  `before_enqueue` hook's session.
- **`services/stage_status.py`'s `inflight_clause` / `domain_completed_clause`** — D-05's conjunct
  composes these verbatim. They are the drift-locked twins of `enums/stage.py`'s pure resolver
  (78 D-04's parametrized equivalence test); re-spelling either predicate breaks that lock.
- **`ComputeAgentBackend.dispatch`'s `on_conflict_do_update` upsert (`backends.py:326-334`)** — already
  promotes an existing `awaiting` row to `SUBMITTED`. **No change needed** for the cloud dispatch
  paths' awaiting-row retirement; only `LocalBackend` lacks an equivalent (D-05 resolves that with the
  conjunct instead of a write).
- **`_delete_staged_object_if_cloud` (`agent_analysis.py:110`)** — already invoked on both
  analyze-terminal paths. The D-14 reaper joins a transaction that already exists.
- **`032`'s `INSERT … SELECT gen_random_uuid(), f.id, 'awaiting' …`** (`:98`) — the exact statement the
  D-04 repair migration re-runs, plus `ON CONFLICT DO NOTHING`.

### Established Patterns
- **`dispatch` never commits** — it mutates in the caller's session; the drain owns the single
  post-loop commit under the tick advisory lock (`backends.py:147-152`, `release_awaiting_cloud.py:189`).
  Any new awaiting-row write on a dispatch/spill path must honor this.
- **Record-don't-rederive** — `/pushed` and `/mismatch` resolve the file's backend from the RECORDED
  `cloud_job.backend_id` via `resolve_compute_backend`, never `select_active_agent(kind="compute")`
  (Phase 72/73 D-06/D-07). It is also what makes `/pushed` naturally safe against a kueue file (D-12).
- **`AUTH-01`** — `file_id` travels on the URL PATH; agent identity comes from the token dependency;
  request bodies carry no identity and keep `extra='forbid'`.
- **Ledger-clear in the same transaction as the terminal write** (Phase 45 L-02).
- **String-backed `CloudJobStatus`** — new members need only the CHECK membership list, never a
  Postgres enum-type migration (77 D-04).
- **`enums/stage.py` is DB-free** (78 D-04) — D-12 extends that to **config-free**: no `backends.toml`
  dependency may leak into the predicate module.

### Integration Points
- The drain query is consumed by `tasks/release_awaiting_cloud.py:173` inside the advisory-locked tick;
  it returns `FileRecord` entities that `Backend.dispatch(file, session, task_router)` mutates.
  Selecting the entity is fine — SC#1 forbids reading `FileRecord.state` as a *predicate*.
- `select_backend` (`services/backend_selection.py`) reads `cloud_job.attempts` to exclude cloud after a
  spill, and `file.updated_at` as the staleness clock (D-07).
- `services/backends.py:76` `IN_FLIGHT` gates per-backend `in_flight_count`. `'awaiting'` must stay out
  of it (D-03).
- `services/shadow_compare.py` is the standing CI gate — every decision above is chosen so no file's
  *derived* status changes, except where D-01's new writer **repairs** an invariant that is currently
  violated.

</code_context>

<specifics>
## Specific Ideas

- The phrase in SIDECAR-01 — CAS behavior "preserved or **strengthened**" — was read as licensing D-11
  (the missing advisory lock), not just the named `agent_s3.py:195` guard.
- SC#1's "no `FileRecord.state` **routing** read" was read literally and narrowly: routing = the
  drain-candidate predicate, the dispatch route flips, and the four callback guards. Display reads
  (count cards) and recovery reads (`reenqueue.py`) are **not** routing reads — which is what keeps
  Phase 83 from colliding with Phases 80/82.
- The operator chose the **predicate conjunct over row deletion** on the strength of the rolled-back-tick
  argument specifically: the drain's whole-tick rollback vs. the `before_enqueue` hook's independently
  committed ledger row. This is the single most load-bearing decision in the phase — the deletion
  variant looks more symmetric and is wrong.
- ⚠ `.planning/sketches/MANIFEST.md` exists with no packaged findings skill. Its sketch (001,
  pipeline-dag-view) concerns the DAG dashboard, not cloud routing — likely irrelevant here, but run
  `/gsd:sketch --wrap-up` if planning wants it available.
- `.planning/codebase/` does not exist; the scout used targeted greps rather than pre-built maps.

</specifics>

<deferred>
## Deferred Ideas

- **`get_pushing_count` / `get_pushed_count` are an unowned gap.** Both read `FileRecord.state`
  (`services/pipeline.py:1207, 1225`) and feed the `staged_pushing_card` / `analyzing_cloud_card`
  partials. **No requirement in any phase names them:** READ-02 names `get_pipeline_stats`
  specifically, and Phase 82's SC#2 is about four-bucket *per-stage* counts (metadata / fingerprint /
  analyze). Phase 87's UI-01..05 concern the file-row state matrix and the eligibility trace.
  **Phase 90 drops the column these two read.** Counting them requires resolving the compute/kueue
  status collision (D-12) — i.e. a universal PUSHING/PUSHED predicate. This is a **hard Phase-90
  blocker** and should be assigned by the milestone audit (`/gsd:audit-milestone`) or an explicit
  ROADMAP amendment. `get_awaiting_cloud_count` is the third of the trio and D-15 recommends Phase 83
  absorb it, because **this phase is what makes it disagree with the drain**.

- **The Phase-90 staleness-clock hazard (D-07).** If the clock stays on `file.updated_at`, Phase 90's
  removal of the dual-written `state` silently breaks `cloud_route_max_wait_sec`. Recorded here so it
  is not lost even if D-07's research picks the minimal-diff option.

- **`ix_cloud_job_awaiting` unbounded growth (D-14).** If no reaper ships, the partial index accretes
  one dead row per long file ever analyzed locally, and the `*/5` drain tick's scan degrades at 200K.

- **`report_uploaded`'s redundant `FileRecord.state == PUSHING` guard** (`agent_s3.py:128`). Once D-09
  makes `cloud_job.status` the CAS domain, this second guard is belt-and-braces on a column Phase 90
  removes. Retiring it for symmetry is a candidate for this phase or Phase 90; not decided.

- **A shadow-gate invariant for the new awaiting writer.** With a go-forward writer, the converse
  implication (`cloud_job(status='awaiting') ⇒ state == AWAITING_CLOUD`) becomes *nearly* assertable —
  except D-13 keeps `LOCAL_ANALYZING` files carrying an awaiting row until D-14's reaper runs. Adding
  the converse would require encoding that exception; deliberately not done here (79 D-04's contract is
  implication in one direction only).

- **`MAX_FINGERPRINT_ATTEMPTS`** and the **mixed-engine fingerprint retry hole** — inherited unchanged
  from 81-CONTEXT's deferred list. Untouched by this phase.

</deferred>

---

*Phase: 83-Cloud-Routing Sidecar Cutover*
*Context gathered: 2026-07-09*
</content>
