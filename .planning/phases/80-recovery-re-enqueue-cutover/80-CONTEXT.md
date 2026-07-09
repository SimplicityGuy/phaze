# Phase 80: Recovery / Re-enqueue Cutover - Context

**Gathered:** 2026-07-08
**Status:** Ready for planning

<domain>
## Phase Boundary

Cut `tasks/reenqueue.py` (`recover_orphaned_work` + its done-set helpers) and
`tasks/reconcile_cloud_jobs.py` over to derive their **done / in-flight sets** from the Phase-78
`stage_status` predicate layer + the cloud sidecar, with **zero `FileRecord.state` reads**.

This lands deliberately **before** the pending-set / counts readers (Phase 82) so recovery's
"absent from pending ⇒ done" definition is not silently redefined under it once `pending` becomes
`NOT done ∧ NOT in_flight` — the **double-negation dependency** the roadmap goal calls out.

**Requirements:** READ-03.

**Load-bearing invariants that MUST survive the cutover:**
1. The scheduling-ledger recovery contract — recovery re-enqueues exactly
   `ledger MINUS live-saq_jobs-keys MINUS domain-completed`.
2. The **"only previously-scheduled work recovers"** guarantee — a never-scheduled `discovered`
   file has no ledger row and MUST NOT be recovered (guards the 2026-06-18 44.5K-job over-enqueue
   incident class).
3. A failed **analyze** stays terminal — never auto-re-driven. Manual retry only.
4. The Phase-79 shadow-compare gate stays green after the cutover.

**Scope note (D-04):** Phase 80 owns its two named files *end-to-end* — this includes retiring the
single residual `FileRecord.state` **write** in `reconcile_cloud_jobs.py:212`. It does **not** touch
any other writer.

</domain>

<decisions>
## Implementation Decisions

### Domain-completed source (D-01)

- **D-01:** Recovery's `domain_completed` exclusion (the SECONDARY over-enqueue net, beyond the
  live-saq_jobs-key filter) is derived as **`NOT outcome-eligible`**, sourced from the Phase-78
  predicate layer — `enums/stage.py` `eligible()` / `FAILURE_IS_TERMINAL` composed over
  `services/stage_status.py` `done_clause` / `failed_clause`. **One source of truth**; the per-stage
  asymmetry falls out automatically rather than being hand-rolled:

  ```
  domain_completed(stage) = done(stage) OR (failed(stage) AND FAILURE_IS_TERMINAL[stage])

  domain_completed(analyze)     = done OR failed   (both terminal — manual retry only)
  domain_completed(metadata)    = done OR failed   (terminal)
  domain_completed(fingerprint) = done ONLY        (failed auto-retries — D-16 behavior preserved)
  ```

  The `fingerprint` asymmetry is intentional and must be encoded, not smoothed away: a failed
  fingerprint with a surviving ledger row IS genuinely re-drivable; a failed analyze is NOT.
  (Rejected hand-rolling per-stage done-set SELECTs in `reenqueue.py` against the new marker
  columns — it duplicates the terminal semantics and lets the analyze/fingerprint asymmetry drift.)

### Phase sequencing — read-cutover follows writer-cutover (D-02, D-03)

The discussion surfaced a **structural ordering hazard** in the milestone DAG, twice:

> **A reader can only safely cut over to a derived source once that source has a LIVE WRITER
> keeping it current past the one-time `032` backfill.**

Verified against `main`: the `032` migration backfilled the failure markers and the cloud sidecar
*once*, but the live writers still write only `FileRecord.state`. A reader that derives from a
backfilled-but-not-live-written source reads **stale** data for anything that changed after `032`.

- **D-02 [roadmap edit]:** **Phase 81 (Per-Stage Failure Persistence) becomes an upstream dependency
  of Phase 80.** Rationale: D-01's `domain_completed(analyze)` and `domain_completed(metadata)`
  read the `failed` marker, but the analyze failure path writes `state = ANALYSIS_FAILED`
  (`routers/agent_analysis.py:329`) and **not** `analysis.failed_at` until Phase 81 (FAIL-01); the
  metadata failure path (`report_metadata_failed`) persists **nothing** until Phase 81 (FAIL-02).
  Cutting the read over first would narrow the documented belt-and-suspenders secondary net that
  stops a 4-hour analyze from being auto-re-driven in the callback-partial-failure case.
  (Rejected: pulling the failure-marker writes into Phase 80 — overlaps FAIL-01/02. Rejected:
  accepting a documented interim residual — the guard being narrowed is the over-enqueue guard.)

- **D-03 [roadmap edit]:** **Phase 83 (SIDECAR-01, cloud-routing sidecar + writers) becomes an
  upstream dependency of Phase 80.** Rationale: **there is no live writer of the `cloud_job`
  sidecar's `AWAITING` status.** Phase 77 added `CloudJobStatus.AWAITING` + the CHECK + the
  `ix_cloud_job_awaiting` partial index + a one-time backfill, but every live writer still writes
  only `FileRecord.state = AWAITING_CLOUD` (`routers/agent_push.py:261`, `routers/pipeline.py:345`,
  `routers/agent_s3.py:195`, `tasks/reconcile_cloud_jobs.py:212`). Worse, **`PUSHED` has no sidecar
  status at all** — the `cloud_job.status` CHECK list is
  `('uploading','uploaded','submitted','running','succeeded','failed','awaiting')`, with no
  `'pushed'` member. `_get_awaiting_cloud_ids` and `_select_done_push_ids` therefore cannot derive
  correctly until SIDECAR-01 lands their live writers.
  (Rejected: scope-excluding the cloud reads from Phase 80 — leaves READ-03 partially unmet.
  Rejected: deriving held/push-done from live facts only — redefines the sets. Rejected: a
  Phase-80 dual-write — overlaps SIDECAR-01.)

- **Net effect:** Phase 80's `Depends on` changes from **78, 79** → **78, 79, 81, 83**.
  See "Action Items" in `<specifics>`.

### Reconcile scope & ownership split (D-04)

- **D-04:** `reconcile_cloud_jobs.py`'s **read** side is already sidecar-derived — its in-flight
  iteration is `SELECT cloud_job WHERE status IN (SUBMITTED, RUNNING)` (via
  `KueueBackend.reconcile`), with **zero `FileRecord.state` reads** today. Its only `FileRecord`
  coupling is the at-cap spill-back **write** at `reconcile_cloud_jobs.py:212`
  (`update(FileRecord).values(state=FileState.AWAITING_CLOUD)`).

  **Phase 80 owns retiring that write** to the sidecar representation, so both of its named files
  are fully free of `FileRecord.state` coupling. **Phase 83 (SIDECAR-01) is scoped to EXCLUDE
  `reconcile_cloud_jobs.py`** and handles the sibling cloud-routing writers
  (`agent_push.py`, `pipeline.py`, `agent_s3.py`) plus the broader sidecar reads.
  This deconfliction is explicit so the two phases do not double-own the same line.

  The load-bearing **MKUE-04 clean-before-flip ordering** at that site
  (delete the staged S3 object *under the still-held* `pg_advisory_xact_lock(5_000_504)` **before**
  the commit that flips the file into a drain candidate, Pitfall 9) MUST be preserved byte-for-byte
  through the write swap. Also preserve: `cloud_job.attempts` is NOT incremented on the at-cap path
  (double-count guard), and `delete_job` stays POST-commit (D-04 status-read-vs-GC).

  Phase 80 additionally lands a **regression guard** asserting `reconcile_cloud_jobs.py` performs
  zero `FileRecord.state` reads.

### Pending-import removal — the anti-double-negation cut (D-05, D-06)

- **D-05:** `reenqueue.py` **drops its imports of `get_metadata_pending_files` and
  `get_fingerprint_pending_files`** (`services/pipeline.py`) entirely, and derives enrich `done`
  **directly** via `done_clause`. This is the phase's core rationale: today `is_domain_completed`
  treats *"absent from the pending set"* as done. Once Phase 82 redefines pending as
  `NOT done ∧ NOT in_flight`, "absent from pending" silently becomes `done ∨ in_flight` — which
  would wrongly classify a genuinely-orphaned in-flight-ledger file as domain-completed and
  **stop recovering it**. Cutting to a direct `done` derivation *before* Phase 82 closes that
  double-negation. `is_domain_completed`'s metadata/fingerprint branches flip from
  `fid not in pending_set` → `fid in done_set`.

- **D-06:** The derived done-sets are **ledger-scoped**, not full-corpus. Deriving `done` directly
  inverts the set-size characteristic (today the *pending* sets are small and bounded; the *done*
  set is most of a 200K corpus). Recovery only ever asks about files that appear in the ledger, so
  scope every done-set query to the ledger's `file_id`s read earlier in the same run:

  ```python
  rows = await get_ledger_rows(session)
  fids = {_natural_id(r) for r in rows} - {None}

  # done sets scoped to the ledger's files only — O(|ledger|), never O(200K)
  done[analyze]  = SELECT id WHERE id IN fids AND (done_clause | failed_clause)
  done[metadata] = SELECT id WHERE id IN fids AND (done_clause | failed_clause)
  done[fingerprint] = SELECT id WHERE id IN fids AND done_clause
  ```

  (Rejected full-corpus done-sets — `O(200K)` UUIDs in memory + a full scan per recovery run.
  Rejected per-row correlated `EXISTS` at filter time — N+1 queries.)

### Carried forward from prior phases (locked — do not re-litigate)

- **Phase 78 D-01 (written decision record, `services/stage_status.py` module docstring):** the
  **authoritative** `in_flight` source is `scheduling_ledger`. `saq_jobs` is **corroborating-only**
  and NEVER flips the `in_flight` boolean; it is read-only, detail-only, SAVEPOINT-isolated, and
  degrades to a safe default. The naked `saq_jobs ∪ ledger` union was **rejected**.
- **Phase 78:** `done_clause` / `failed_clause` / `inflight_clause` / `stage_status_case` are the
  reuse targets. Do not reinvent predicates; the DERIV-04 equivalence test locks SQL⇔Python.
- `FAILURE_IS_TERMINAL[analyze] = true` is load-bearing (design §3 warning +
  `reenqueue.py:179-186` + quick-260707-d79).
- `_in_flight_cloud_job_ids` (`reenqueue.py:212`) **already** reads the `cloud_job` sidecar, not
  `FileRecord.state` — the SCHED-05 single-recovery-owner exclusion. **No change needed.**
- Phase-32 routing pitfalls, `lane_for_task` per-row lane derivation, and the
  `NoActiveAgentError` cold-boot skip-not-raise behavior are all preserved unchanged.

### Claude's Discretion

- The exact internal shape of the ledger-scoped done-set helper (one query per stage vs. a single
  `stage_status_case` query bucketed in Python), and whether `_ANALYZE_DONE` / `_PUSH_DONE` /
  `_METADATA_PENDING` / `_FINGERPRINT_PENDING` key constants are renamed to reflect the
  done-not-pending inversion.
- The chunking strategy for the `id IN fids` bound-parameter list if the ledger ever grows past a
  comfortable single-statement bind count.
- The precise mechanism of the "zero `FileRecord.state` reads" regression guard (AST check, import
  guard, or grep-style source assertion) — an existing project idiom, if one exists, wins.
- Exact test-bucket placement for the new regression tests (must pass via
  `just test-bucket <bucket>` **in isolation**, per the CI bucket-isolation constraint).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase requirements & roadmap
- `.planning/ROADMAP.md` §"Phase 80: Recovery / Re-enqueue Cutover" — goal, 3 success criteria.
  **NOTE:** its `Depends on` line needs the D-02/D-03 edit (78, 79 → 78, 79, 81, 83).
- `.planning/REQUIREMENTS.md` — READ-03 (full text); also READ-01/READ-02 (Phase 82, the
  pending/counts cutover this phase deliberately precedes), FAIL-01/FAIL-02 (Phase 81, now
  upstream per D-02), SIDECAR-01 (Phase 83, now upstream per D-03).
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` —
  §2.2 (`in_flight` + the D-01 open question, resolved in Phase 78),
  §2.3 (per-stage failure markers + the `report_metadata_failed`-records-nothing latent bug),
  §3 (the `eligible` predicate + the `FAILURE_IS_TERMINAL` table + the ⚠️ **load-bearing**
  analyze-terminal warning citing `reenqueue.py:179-186` and the 44.5K over-enqueue incident),
  §6.2 (two-step migration + shadow-compare gate + the quiesce requirement),
  §7 (call-site inventory — names `reenqueue.py`'s five readers explicitly),
  §8 (constraints: uv only, ruff/mypy strict, 90% coverage, per-bucket test isolation, `:5433`
  test DB, migrations never reference `saq_jobs`),
  §9 (non-goals — **"Not fixing PROV-01 … though `reenqueue.py` is heavily touched here, so
  re-check the overlap during planning"**),
  §10 (open decisions D-01/D-04).

### Upstream phases (the derived model this phase consumes)
- `.planning/phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` — the
  predicate-module decisions; `done(metadata)` = row present AND `failed_at IS NULL`; the two-module
  split; the DERIV-04 SQL⇔Python equivalence test.
- `.planning/phases/79-shadow-compare-gate-live-corpus/79-CONTEXT.md` — the standing gate this phase
  must keep green (SC-3); D-06's `{FINGERPRINTED, LOCAL_ANALYZING}` soft allowlist.
- `.planning/phases/77-additive-schema-rescan-wipe-fix-migration-032/77-CONTEXT.md` — what `032`
  backfilled vs. skipped; the failure markers; `CloudJobStatus.AWAITING`; the partial indexes.
- `alembic/versions/032_add_derived_status_schema.py` — the additive schema being read.

### Existing code — the predicates to REUSE (never reinvent)
- `src/phaze/services/stage_status.py` — `done_clause` / `failed_clause` / `inflight_clause` /
  `stage_status_case` `ColumnElement[bool]` builders. **Read the D-01 DECISION RECORD in the module
  docstring** — it fixes `scheduling_ledger` as the authoritative `in_flight` source.
- `src/phaze/enums/stage.py` — DB-free `Stage`/`Status` enums, `ELIGIBILITY_DAG`,
  `FAILURE_IS_TERMINAL`, `resolve_status()`, `eligible()`. The D-01 composition source.
- `src/phaze/services/shadow_compare.py` — the Phase-79 gate (`_cloud_awaiting` reads
  `CloudJob.status == "awaiting"`); must stay green.

### Existing code — the cutover targets
- `src/phaze/tasks/reenqueue.py` — **the primary target.** Module docstring carries THREE reframes
  that must be honored: the Phase-42 durability reframe, the **Phase-45 ledger reframe** (the
  operator spec for the over-enqueue incident), and the per-stage domain-completed predicate
  contract. Cutover sites: `_build_done_sets` (:136), `_select_done_analyze_ids` (:177),
  `_select_done_push_ids` (:190), `_get_awaiting_cloud_ids` (:200), `is_domain_completed` (:242).
  `_in_flight_cloud_job_ids` (:212) needs **no** change.
- `src/phaze/tasks/reconcile_cloud_jobs.py` — reads already sidecar-derived; the single write to
  retire is at **:212**, inside `_handle_no_callback_terminal`. Preserve the MKUE-04
  clean-before-flip ordering documented at :174-219.
- `src/phaze/services/scheduling_ledger.py` — `get_ledger_rows`, `insert_ledger_if_absent`.
- `src/phaze/services/pipeline.py` — `count_inflight_jobs`, `get_live_job_keys` stay;
  `get_metadata_pending_files` / `get_fingerprint_pending_files` imports are **removed** (D-05).
- `src/phaze/services/backends.py` — `IN_FLIGHT`, `resolve_backends`, `KueueBackend.reconcile`
  (the per-row advisory-lock owner).
- `src/phaze/models/cloud_job.py` — `CloudJobStatus` (note: **no `'pushed'` member**),
  the status CHECK constraint, `ix_cloud_job_awaiting`.

### Test harness conventions
- `tests/buckets.json` + `tests/shared/test_partition_guard.py` — one bucket per file; new tests must
  pass via `just test-bucket <bucket>` **in isolation**, not merely in the full suite.
- `tests/test_recovery.py` — the existing totality test asserting
  `_DOMAIN_COMPLETED_STAGES` XOR live-keys-only against `_KEY_BUILDERS` (T-45-17). The SC-2
  never-scheduled-`discovered`-file regression test belongs alongside it.
- `tests/test_task_split.py` — enforces the control-only import boundary; `reenqueue.py` must never
  be importable from the agent worker.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`services/stage_status.py` + `enums/stage.py` (Phase 78)** — the entire derivation layer. D-01
  composes `eligible()`/`FAILURE_IS_TERMINAL` over `done_clause`/`failed_clause`. This is the
  primary reuse and the reason Phase 80 is a thin cutover rather than new logic.
- **Phase 77 partial indexes** — `ix_analysis_completed`, `ix_analysis_failed`, `ix_metadata_failed`,
  `ix_fprint_success`, `ix_cloud_job_awaiting`. The ledger-scoped done queries (D-06) will hit these;
  they are `IS NOT NULL`-shaped, never `status IN (...)`.
- **`_in_flight_cloud_job_ids` (`reenqueue.py:212`)** — already the correct shape: a sidecar read,
  no `FileRecord.state`. It is the SCHED-05 single-recovery-owner exclusion and the template for
  what the other three helpers should become.
- **`get_ledger_rows`** — already read once per recovery run; D-06's `fids` set is free from it.

### Established Patterns
- **Read-once-per-run**: `_build_done_sets`, `_get_awaiting_cloud_ids`, `_in_flight_cloud_job_ids`
  are each read exactly once alongside `live` / `rows`, then used as in-memory set membership.
  D-06 keeps this shape, only bounding the sets to the ledger's files.
- **Fingerprint "done" spelling** `status IN ('success','completed')` (Phase-59 WR-02 / PR #189) —
  already encoded in `done_clause(fingerprint)`; renders as `= ANY (ARRAY[...])` matching
  `ix_fprint_success`.
- **SAVEPOINT-isolated, degrade-to-safe-default** reads for anything touching `saq_jobs`
  (`services/pipeline.py:466-497` idiom). Alembic **never** references `saq_jobs`.
- **Never a raw random-key enqueue** — every replay goes through `_replay_row` with `key=row.key`,
  so the deterministic-key dedup collapses a still-live item to a skipped no-op (the Phase-32
  doubling backstop).
- **Stored job policy replay** — `row.timeout` / `row.retries` are replayed when present so a
  recovered long `process_file` keeps its 7200s/retries=2 bound (the recover-button timeout-loss
  bug). Untouched by this phase, but do not regress it.

### Integration Points
- `recover_orphaned_work` is called by **both** the controller startup hook and the manual "Recover"
  button (`force=True`), by design (D-03) so the automatic and manual paths cannot drift. Any change
  to the done-set semantics changes **both** at once.
- The `force=True` path bypasses **only** the no-op DETECT gate — never the per-item deterministic-key
  dedup. Preserve this exactly.
- `reconcile_cloud_jobs` dispatches per-backend (`for b in resolve_backends(cfg)`), and the per-row
  advisory lock is acquired at the top of each `KueueBackend.reconcile` unit. The :212 write swap
  happens *inside* that held lock.
- `services/backends.py` does a module-top `from phaze.tasks.reconcile_cloud_jobs import _reconcile_one`,
  so `reconcile_cloud_jobs` imports `resolve_backends` **function-locally** to break the cycle. Do not
  hoist that import.

</code_context>

<specifics>
## Specific Ideas

### Action Items — roadmap edits required before this phase executes

These fall out of D-02 and D-03 and are **prerequisites**, not suggestions:

1. **`.planning/ROADMAP.md` — Phase 80 `Depends on`:** `Phase 78, Phase 79` → **`Phase 78, Phase 79,
   Phase 81, Phase 83`**. Phase 80 moves after both of its writer-cutover dependencies.
2. **`.planning/ROADMAP.md` — Phase 83 (SIDECAR-01) scope:** add an explicit exclusion —
   `tasks/reconcile_cloud_jobs.py`'s spill-back write is owned by **Phase 80** (D-04), not 83.

### The governing principle this discussion established

> **A read-cutover phase must follow the writer-cutover phase that keeps its derived source live.**

The `032` migration backfilled the derived sources **once**. Any reader that cuts over before its
source has a live writer reads data frozen at backfill time. This bit Phase 80 twice (analyze/metadata
failure markers → Phase 81; cloud-routing sidecar → Phase 83).

**Worth checking during Phase 82's discussion:** the same inversion may affect READ-01/READ-02
(pending sets + `get_pipeline_stats` counts), which likewise derive from `failed` markers and the
cloud sidecar. Not acted on here — flagged only.

### Verified-against-`main` facts that motivated the reorders

- `cloud_job.status` CHECK members are exactly
  `('uploading','uploaded','submitted','running','succeeded','failed','awaiting')`
  (`models/cloud_job.py:114`) — **there is no `'pushed'` status.**
- `CloudJobStatus.AWAITING` has **no live writer**; the only code that reads `status == "awaiting"`
  is `services/shadow_compare.py:82` (the Phase-79 gate, reading `032`-backfilled rows).
- Live `state = AWAITING_CLOUD` writers: `routers/agent_push.py:261`, `routers/pipeline.py:345`,
  `routers/agent_s3.py:195`, `tasks/reconcile_cloud_jobs.py:212`.

### Non-negotiables carried into planning

- The SC-2 regression test — **a never-scheduled `discovered` file is not recovered** — is the
  headline guard. It belongs next to the existing `_DOMAIN_COMPLETED_STAGES` totality test in
  `tests/test_recovery.py`.
- A companion regression asserting **a failed analyze is never produced by any automatic recovery
  path** (the `FAILURE_IS_TERMINAL[analyze]` encoding, ELIG-03's twin at the recovery layer).
- `reenqueue.py`'s three module-docstring reframes (Phase-42 durability, Phase-45 ledger, the
  domain-completed contract) must be **updated in place**, not deleted — they are the institutional
  memory for two production incidents.

</specifics>

<deferred>
## Deferred Ideas

- **PROV-01 — N-compute-aware orphan recovery.** Design §9 explicitly non-goals this, while noting
  *"`reenqueue.py` is heavily touched here, so re-check the overlap during planning."* Phase 80
  touches `recover_orphaned_work`'s done-set derivation, **not** its compute-agent selection
  (`select_active_agent(session, kind="compute")` at :382 stays single-active-compute). Deferred to
  v2 per the 2026.7.2 close-out. Re-check, do not fix.
- **Phase 82 read-before-write inversion check** — whether READ-01/READ-02 hit the same D-02/D-03
  hazard. Belongs to Phase 82's discussion.
- **Retiring the other cloud-routing `state` writers** (`agent_push.py:261`, `pipeline.py:345`,
  `agent_s3.py:195`) — SIDECAR-01 / Phase 83, explicitly excluding `reconcile_cloud_jobs.py` per D-04.
- **Cloud-push lane drain (`--profile drain`) quiesce** before the destructive `033` — Phase 90's
  rollout runbook (carried forward from 79-CONTEXT).

None else — discussion stayed within phase scope.

</deferred>

---

*Phase: 80-recovery-re-enqueue-cutover*
*Context gathered: 2026-07-08*
