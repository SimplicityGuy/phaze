# Phase 80: Recovery / Re-enqueue Cutover - Context

**Gathered:** 2026-07-08
**Refreshed:** 2026-07-10 (re-derived against `main` @ `09cefc6d` — Phases 81, 83, 84 shipped)
**Status:** Ready for planning

<domain>
## Phase Boundary

Cut `tasks/reenqueue.py` (`recover_orphaned_work` + its done-set helpers) and
`tasks/reconcile_cloud_jobs.py` over to derive their **done / in-flight sets** from the Phase-78
`stage_status` predicate layer + the `cloud_job` sidecar, with **zero `FileRecord.state` reads**.

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

**Dependency status:** `Depends on: Phase 78, Phase 79, Phase 81, Phase 83` — **all four are now
complete.** D-02 and D-03 (below) are RESOLVED; they are retained as the written record of why the
roadmap was rewired, and because their reasoning still governs Phase 82's discussion.

</domain>

<decisions>
## Implementation Decisions

### Domain-completed source (D-01)

- **D-01:** Recovery's `domain_completed` exclusion (the SECONDARY over-enqueue net, beyond the
  live-saq_jobs-key filter) is derived from the Phase-78/81 predicate layer — **not** hand-rolled.
  Phase 81 shipped both twins, so this is now a direct reuse rather than a composition:

  ```
  domain_completed(stage) = done(stage) OR (failed(stage) AND FAILURE_IS_TERMINAL[stage])

  domain_completed(analyze)     = done OR failed   (both terminal — manual retry only)
  domain_completed(metadata)    = done OR failed   (terminal; see D-10 for the retry cell)
  domain_completed(fingerprint) = done ONLY        (failed auto-retries — behavior preserved)
  ```

  SQL twin: `services/stage_status.py:195` `domain_completed_clause(stage)`.
  DB-free twin: `enums/stage.py:186` `domain_completed(status_map, stage)`.
  `FAILURE_IS_TERMINAL` is real code at `enums/stage.py:87` (Phase 81 created it; the original
  80-CONTEXT cited it while it existed in no `.py`).

  The `fingerprint` asymmetry is intentional and must be encoded, not smoothed away: a failed
  fingerprint with a surviving ledger row IS genuinely re-drivable; a failed analyze is NOT.
  (Rejected hand-rolling per-stage done-set SELECTs in `reenqueue.py` against the new marker
  columns — it duplicates the terminal semantics and lets the analyze/fingerprint asymmetry drift.)

### Phase sequencing — read-cutover follows writer-cutover (D-02, D-03 — both RESOLVED)

The governing principle this discussion established, twice:

> **A reader can only safely cut over to a derived source once that source has a LIVE WRITER
> keeping it current past the one-time `032` backfill.**

- **D-02 [RESOLVED — Phase 81 shipped `191a8c79`]:** Phase 81 (Per-Stage Failure Persistence) became
  an upstream dependency of Phase 80, because `domain_completed(analyze)`/`(metadata)` read failure
  markers that had no live writer. `report_analysis_failed` now dual-writes `analysis.failed_at`
  (FAIL-01) and `report_metadata_failed` now persists a `metadata` row with `failed_at` (FAIL-02).

- **D-03 [RESOLVED — Phase 83 shipped `6855cfe2`]:** Phase 83 (SIDECAR-01) became an upstream
  dependency, because `cloud_job.status='awaiting'` had **no go-forward writer**. Phase 83 added
  `services/backends.py:86` `hold_awaiting_cloud()`, the SINGLE writer, shared by the hold path
  (`routers/pipeline.py:351`) and both spill paths (`routers/agent_push.py:285`,
  `routers/agent_s3.py:212`).

  **Still true after 83:** `CloudJobStatus` has **no `'pushed'` member**
  (`models/cloud_job.py`: uploading, uploaded, failed, submitted, running, succeeded, awaiting).
  Phase 83's D-12 deliberately refused a universal `pushing`/`pushed` predicate because compute and
  kueue collide on status values. See D-07 for how Phase 80 sidesteps this.

### Push-done derivation (D-07)

- **D-07:** `_select_done_push_ids()` derives as:

  ```
  push_done = cloud_job.status == 'succeeded'  OR  domain_completed_clause(Stage.ANALYZE)
  ```

  **Why no backend-kind resolution is needed:** `push_file` ledger rows are created ONLY by
  `ComputeAgentBackend.dispatch` → `_enqueue_push_file` (`services/backends.py:154`). Kueue dispatches
  via `_stage_file_to_s3` and never enqueues `push_file`. So a `push_file` ledger row **implies
  compute**, and 83 D-12's compute/kueue status collision does not apply. On the compute lane
  (83 D-12's table) `SUBMITTED` = still pushing, `SUCCEEDED` = pushed and analyzing.

  This is behavior-identical to today's `state IN (PUSHED, ANALYZED, ANALYSIS_FAILED)`: `SUCCEEDED`
  covers PUSHED, and `domain_completed(analyze)` covers the onward advance to ANALYZED /
  ANALYSIS_FAILED. A file at `SUBMITTED` / `AWAITING` / no-row is NOT push-done and correctly
  re-drives.

  (Rejected collapsing to `domain_completed(analyze)` alone — it loses the landed-but-not-yet-analyzed
  window, so a file that successfully rsynced to compute scratch would re-drive and re-push a large
  file, regressing Phase 50 D-10. Rejected `status IN ('uploaded','submitted','running','succeeded')`
  — `SUBMITTED` means *still pushing* on the compute lane, so an in-flight rsync would be mis-read as
  done and never recover after a crash.)

### The awaiting-candidate clause (D-08, D-09)

- **D-08:** `_get_awaiting_cloud_ids()` must NOT read `cloud_job.status == 'awaiting'` bare. Phase 83
  D-13 keeps `LocalBackend.dispatch`'s `state = LOCAL_ANALYZING` flip but D-05 chose a predicate
  conjunct over row deletion, and D-14 reaps the inert row only at the **analyze-terminal** seams
  (`routers/agent_analysis.py:266`, `:390`). So a file **mid-local-analysis still carries an
  `awaiting` row**. Today's `state == AWAITING_CLOUD` read excludes it; a bare status read would not,
  and recovery would wrongly route a locally-analyzing file to a COMPUTE agent (violating
  CLOUDROUTE-02, the very thing `_get_awaiting_cloud_ids` exists to enforce).

  The correct predicate is the drain's D-05 conjunct:

  ```
  cloud_job.status = 'awaiting'
    AND NOT inflight_clause(Stage.ANALYZE)
    AND NOT domain_completed_clause(Stage.ANALYZE)
  ```

  It is currently **spelled inline in two places** — `get_cloud_staging_candidates` and
  `get_awaiting_cloud_count` (`services/pipeline.py:1116`, `:~1300`). **Extract it into one named
  clause builder** and have all three call sites consume it. 83 D-15's rationale — the card and the
  drain "can NEVER disagree" — now extends to recovery, which *routes* on it. Three inline copies is
  where that guarantee starts to rot.

- **D-09:** The extracted builder lives in **`services/stage_status.py`**, beside the LOCKED builders
  it composes (`inflight_clause:175`, `domain_completed_clause:195`), so the DERIV-04 equivalence
  test has a natural home for it. It requires adding a `CloudJob` import — the module already imports
  seven models.

  This does **not** violate 83 D-12's rejection: that ruled out `pushing_clause()`/`pushed_clause()`
  in `stage_status.py` because they would pull in the `backends.toml` registry, breaking the DB-free
  *and config-free* purity 78 D-04 established. This clause needs only a status literal — **no
  config**. (Rejected `services/backends.py`: it is the delicate end of a managed import cycle
  — module-top `from phaze.tasks.reconcile_cloud_jobs import _reconcile_one`. Rejected
  `services/pipeline.py`: cuts against D-05, which is deliberately reducing `reenqueue.py`'s
  dependence on it.)

### WR-02 — the `in_flight ∧ failed` cell (D-10, D-11)

Phase 81 left WR-02 open and named Phase 80 as its owner
(`.planning/phases/81-.../deferred-items.md`).

- **D-10:** Resolve **at the call site**, using `SchedulingLedger.enqueued_at`
  (`models/scheduling_ledger.py:63`). In `is_domain_completed`, metadata is domain-complete when:

  ```
  done(metadata)  OR  (failed(metadata) AND ledger.enqueued_at <= metadata.failed_at)
  ```

  **Why the cell exists, and why only for metadata.** The two retry paths are asymmetric:
  `retry_analysis_failed` **clears** `analysis.failed_at` before enqueuing (`routers/pipeline.py:956`
  — the Phase-81 CR-01 fix), so analyze has no ambiguous cell. `retry_metadata_failed` deliberately
  **leaves** `metadata.failed_at` set (81 D-11) and then enqueues. So `(ledger row ∧ failed_at)` is
  ambiguous for metadata alone:
  - **orphaned operator retry** (`enqueued_at > failed_at`) → MUST re-drive.
  - **callback-partial-failure** (`enqueued_at < failed_at`: the failure ack wrote the marker but
    crashed before clearing the ledger) → MUST stay terminal.

  The timestamp disambiguates both cleanly. Both twins stay **ledger-agnostic and unchanged**.

- **D-11:** ⚠ **`~inflight_clause(stage)` MUST NEVER be added to `domain_completed_clause`.** This is
  WR-02's own literal suggestion and it is a **trap**: `inflight_clause` is *scheduling-ledger row
  existence* (`stage_status.py:175`), and **every recovery candidate is a ledger row by
  construction**. Adding the disjunct makes `domain_completed` return `False` for *every* candidate,
  disabling the secondary over-enqueue net wholesale — the 44.5K incident class, reintroduced. It
  would be a silent no-op for the drain and the count card (both already `AND ~inflight_clause`), so
  their tests would stay green.

  **Lock it down:** a `reenqueue.py` regression proving both metadata cells resolve correctly, PLUS
  the rejected option and its reasoning recorded in `domain_completed_clause`'s docstring and in the
  equivalence test's SCOPE comment (`tests/integration/test_stage_status_equivalence.py:415-427`).
  Turn an invisible trap into a documented, tested boundary. The `*_inflight` seed exclusion stays.

### Reconcile scope, ownership split & the write swap (D-04, D-12)

- **D-04 [UPHELD, overriding 83 D-00c for this call site]:** `reconcile_cloud_jobs.py`'s **read** side
  is already sidecar-derived (`SELECT cloud_job WHERE status IN (SUBMITTED, RUNNING)` via
  `KueueBackend.reconcile`), with zero `FileRecord.state` reads today. Its only `FileRecord` coupling
  is the at-cap spill-back **write** at `reconcile_cloud_jobs.py:212`.

  **Phase 80 retires that write outright** — `reconcile_cloud_jobs.py` ends the phase with zero
  `FileRecord.state` coupling of any kind. This deliberately diverges from 83 D-00c (which keeps a
  plain dual-write on the two sibling spill paths, `agent_push.py:307` / `agent_s3.py:232`, until
  Phase 90).

  **Verified safe.** At reconcile time a kueue file sits at `state = PUSHED` (`agent_s3.py:128`).
  The `pushed` shadow invariant is `_cloud_job_exists` — *any* `cloud_job` row, any status
  (`shadow_compare.py:68`, loosened per RESEARCH A3/OQ1) — so an `awaiting` row satisfies it and the
  gate stays green. The drain (`get_cloud_staging_candidates`) and the "Awaiting cloud" card
  (`get_awaiting_cloud_count`) both derive from `cloud_job`, so the spilled file is still picked up
  and still counted. `file.updated_at` no longer being bumped is harmless: 83 D-07 already moved the
  lane-entry staleness clock to `cloud_job.updated_at`, which the CAS bumps.
  **Accepted cost:** any remaining *display* read of `FileRecord.state` shows `PUSHED` until Phase 90,
  and reconcile becomes the first cloud-routing writer to stop dual-writing.

  **Phase 83 (SIDECAR-01) was scoped to EXCLUDE `reconcile_cloud_jobs.py`**, and handled the sibling
  writers (`agent_push.py`, `pipeline.py`, `agent_s3.py`) plus the broader sidecar reads.

- **D-12:** The write swap **reuses** `hold_awaiting_cloud` in spill mode — reconcile becomes its
  fourth caller:

  ```python
  await hold_awaiting_cloud(
      session, file,
      attempts=cap,                                         # budget-spent marker; NOT an increment
      expect_status=(CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value),
      clear_cloud_phase=True,                               # WR-01: keep it off the "Running" tile
  )
  ```

  `inadmissible = False` and `staging_bucket = None` stay **inline** — `hold_awaiting_cloud` stamps
  only `status` / `attempts` / optional `cloud_phase`, and 83 kept its parameter set deliberately
  minimal. (Rejected extending the helper with `clear_inadmissible` / `clear_staging_bucket` flags for
  one caller. Rejected a bespoke local UPDATE — it re-creates the hand-copied writer 83 D-01/D-02
  consolidated away.)

  **Implementation hazard:** do NOT pre-mutate `cloud_job.status` on the loaded ORM object before the
  call, or autoflush races the CAS's `WHERE status IN (...)`.

  **Preserve byte-for-byte:** the MKUE-04 clean-before-flip ordering (delete the staged S3 object
  *under the still-held* `pg_advisory_xact_lock(5_000_504)` **before** the commit that flips the file
  into a drain candidate, Pitfall 9); `cloud_job.attempts` is NOT incremented on the at-cap path
  (double-count guard); `delete_job` stays POST-commit (D-04 status-read-vs-GC).

  Phase 80 additionally lands a **regression guard** asserting `reconcile_cloud_jobs.py` performs zero
  `FileRecord.state` reads.

### Pending-import removal — the anti-double-negation cut (D-05, D-06)

- **D-05:** `reenqueue.py` **drops its imports of `get_metadata_pending_files` and
  `get_fingerprint_pending_files`** (`services/pipeline.py:1382`, `:1415`; imported at
  `reenqueue.py:79-81`) entirely, and derives enrich `done` **directly** via `done_clause`. This is
  the phase's core rationale: today `is_domain_completed` treats *"absent from the pending set"* as
  done. Once Phase 82 redefines pending as `NOT done ∧ NOT in_flight`, "absent from pending" silently
  becomes `done ∨ in_flight` — which would wrongly classify a genuinely-orphaned in-flight-ledger
  file as domain-completed and **stop recovering it**. Cutting to a direct `done` derivation *before*
  Phase 82 closes that double-negation. `is_domain_completed`'s metadata/fingerprint branches flip
  from `fid not in pending_set` → `fid in done_set`.

- **D-06:** The derived done-sets are **ledger-scoped**, not full-corpus. Deriving `done` directly
  inverts the set-size characteristic (today the *pending* sets are small and bounded; the *done*
  set is most of a 200K corpus). Recovery only ever asks about files that appear in the ledger, so
  scope every done-set query to the ledger's `file_id`s read earlier in the same run:

  ```python
  rows = await get_ledger_rows(session)
  fids = {_natural_id(r) for r in rows} - {None}

  # done sets scoped to the ledger's files only — O(|ledger|), never O(200K)
  done[analyze]  = SELECT id WHERE id IN fids AND domain_completed_clause(ANALYZE)
  done[metadata] = SELECT id WHERE id IN fids AND domain_completed_clause(METADATA)   # + D-10 gate
  done[fingerprint] = SELECT id WHERE id IN fids AND done_clause(FINGERPRINT)
  ```

  (Rejected full-corpus done-sets — `O(200K)` UUIDs in memory + a full scan per recovery run.
  Rejected per-row correlated `EXISTS` at filter time — N+1 queries.)

### Migration `036` and the numbering drift (D-13, D-14)

- **D-13:** The `analysis.analysis_completed_at` backfill ships **inside Phase 80's PR as migration
  `036`**, and is a **blocking prerequisite** of the cutover — the cutover and the data it depends on
  land atomically, with no window where `reenqueue.py` derives `done(analyze)` against an
  un-backfilled corpus. Mirrors Phase 81's precedent (migration `033` shipped in its own PR alongside
  the writers depending on it).

  Shape (mirroring `032`'s own `files.state`-sourced backfill):

  ```sql
  UPDATE analysis a SET analysis_completed_at = a.updated_at
  FROM files f
  WHERE a.file_id = f.id
    AND f.state = 'analyzed'
    AND a.analysis_completed_at IS NULL
    AND a.failed_at IS NULL;          -- REQUIRED: see the constraint note below
  ```

  ⚠ **`033`'s constraint is a NAND, not a strict XOR**, despite its name:
  `NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)` (`models/analysis.py:56`).
  Both-NULL is legal. The `failed_at IS NULL` guard is therefore mandatory — without it the
  migration aborts on any analyze-failed row.

- **D-14:** **Planning docs stop hardcoding the destructive migration's number.** Refer to it as
  "the destructive migration (number assigned at plan time)" in `ROADMAP.md:21`, `:36`, `:535`,
  `:542`, `REQUIREMENTS.md:98` (MIG-04), and
  `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md`. Correct the current **collision** in passing:
  `034` presently names BOTH Phase 83's shipped `034_backfill_cloud_awaiting.py` (recorded at
  `ROADMAP.md:416`) and Phase 90's planned destructive migration.

  This is the third renumber (81 D-08 `033→034`; 83 D-04 predicted `034→035` but never applied it;
  Phase 84 then took `035`). Each was caused by a downstream phase claiming the next free number.
  De-numbering ends the churn permanently. Dated/historical records keep their literal numbers per
  81 D-08. Note `just docs-drift` does **not** check migration numbers, so nothing catches this class
  of drift automatically.

### Carried forward from prior phases (locked — do not re-litigate)

- **Phase 78 D-01 (written decision record, `services/stage_status.py` module docstring):** the
  **authoritative** `in_flight` source is `scheduling_ledger`. `saq_jobs` is **corroborating-only**
  and NEVER flips the `in_flight` boolean; it is read-only, detail-only, SAVEPOINT-isolated, and
  degrades to a safe default. The naked `saq_jobs ∪ ledger` union was **rejected**.
- **Phase 78:** `done_clause` / `failed_clause` / `inflight_clause` / `stage_status_case` are the
  reuse targets. Do not reinvent predicates; the DERIV-04 equivalence test locks SQL⇔Python.
- **Phase 83 D-00c:** writers keep dual-writing `FileRecord.state`; only *reliance* on it is replaced.
  The `state` write dies in Phase 90. **Phase 80's D-04 is a deliberate, scoped exception.**
- **Phase 83 D-00d:** the shadow gate's contract is **implication, not equality** (79 D-04).
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
- The name of the extracted awaiting-candidate clause builder (D-08/D-09).
- Whether `036` and the D-14 doc de-numbering land in the same commit or adjacent commits.
- Whether `analysis.updated_at` or `created_at` is the better backfill source (D-13) — verify
  `AnalysisResult` carries `updated_at` via `TimestampMixin` before committing to it.
- Exact test-bucket placement for the new regression tests (must pass via
  `just test-bucket <bucket>` **in isolation**, per the CI bucket-isolation constraint).

### Folded Todos

- **`.planning/todos/pending/analysis-completed-at-backfill.md`** (severity: major; found by Phase 84
  UAT, test 8) — **folded as a BLOCKING PREREQUISITE.** Production has **1050** files at
  `state='analyzed'`; only **49** carry `analysis_completed_at`. **1001 have it NULL.** Nothing in
  migrations `032`–`035` populates it.

  **Why it is a Phase 80 blocker, not just a shadow-gate issue.** The todo frames this as
  "`just shadow-compare` exits 1 on first deploy". For Phase 80 it is a **safety regression**: today
  `_select_done_analyze_ids` reads `state IN (ANALYZED, ANALYSIS_FAILED)` and correctly marks all
  1050 as done. After the cutover, `done_clause(ANALYZE)` requires `analysis_completed_at IS NOT NULL`
  (DERIV-03, `stage_status.py:123`), so those 1001 files stop being domain-completed — and any that
  still hold a `process_file` ledger row get **re-enqueued for re-analysis** (4h jobs). That is the
  44.5K over-enqueue incident class, introduced *by* the cutover. It also propagates to `push_done`
  via D-07's `domain_completed(analyze)` disjunct.

  **Critically, the todo's other two options do not fix this.** Soft-allowlisting `analyzed` (option
  2) or accepting a non-zero `hard_fail_total` (option 3) repair the *gate* while leaving
  `done_clause(ANALYZE)` returning `False` for all 1001 rows. Only the backfill (option 1) closes the
  recovery hazard. Resolved by **D-13**.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase requirements & roadmap
- `.planning/ROADMAP.md` §"Phase 80: Recovery / Re-enqueue Cutover" — goal, 3 success criteria.
  `Depends on: Phase 78, Phase 79, Phase 81, Phase 83` — all complete. **Carries the D-14 doc debt.**
- `.planning/REQUIREMENTS.md` — READ-03 (full text, line 48); also READ-01/READ-02 (Phase 82, the
  pending/counts cutover this phase deliberately precedes) and MIG-04 (line 98, carries the D-14
  stale number).
- `.planning/todos/pending/analysis-completed-at-backfill.md` — **the folded blocking prerequisite.**
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` —
  §2.2 (`in_flight` + the D-01 open question, resolved in Phase 78),
  §2.3 (per-stage failure markers),
  §3 (the `eligible` predicate + the `FAILURE_IS_TERMINAL` table + the ⚠️ **load-bearing**
  analyze-terminal warning citing `reenqueue.py:179-186` and the 44.5K over-enqueue incident),
  §6.2 (two-step migration + shadow-compare gate + the quiesce requirement),
  §7 (call-site inventory — names `reenqueue.py`'s five readers explicitly),
  §8 (constraints: uv only, ruff/mypy strict, 90% coverage, per-bucket test isolation, `:5433`
  test DB, migrations never reference `saq_jobs`),
  §9 (non-goals — **"Not fixing PROV-01 … though `reenqueue.py` is heavily touched here, so
  re-check the overlap during planning"**).

### Upstream phases (the derived model this phase consumes)
- `.planning/phases/81-per-stage-failure-persistence-retry-paths/81-CONTEXT.md` — D-05 (dual-write),
  D-11 (metadata retry leaves `failed_at` set — the root of D-10), D-17 (`domain_completed`).
- `.planning/phases/81-per-stage-failure-persistence-retry-paths/deferred-items.md` — **WR-02**
  (owned by this phase, resolved by D-10/D-11) and WR-01.
- `.planning/phases/83-cloud-routing-sidecar-cutover/83-CONTEXT.md` — D-00c (dual-write contract),
  D-01/D-02 (the `hold_awaiting_cloud` writer), D-05/D-06 (drain conjunct + `with_for_update`),
  D-12 (**no universal pushing/pushed predicate**), D-13 (keep the `LOCAL_ANALYZING` flip),
  D-14 (the analyze-terminal reap), D-15 (`get_awaiting_cloud_count` re-anchor).
- `.planning/phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` — the
  predicate-module decisions; the two-module split; the DERIV-04 SQL⇔Python equivalence test.
- `.planning/phases/79-shadow-compare-gate-live-corpus/79-CONTEXT.md` — the standing gate this phase
  must keep green (SC-3); D-04 implication-not-equality; D-06's soft allowlist.
- `alembic/versions/032_add_derived_status_schema.py` — the additive schema being read.
- `alembic/versions/033_add_analysis_completed_xor_failed.py` — the NAND constraint (D-13).
- `alembic/versions/034_backfill_cloud_awaiting.py` — Phase 83's corpus repair (the `034` collision).
- `alembic/versions/035_reconcile_dedup_resolution.py` — Phase 84. **Next free number is `036`.**

### Existing code — the predicates to REUSE (never reinvent)
- `src/phaze/services/stage_status.py` — `done_clause` / `failed_clause` / `inflight_clause:175` /
  `stage_status_case` / `domain_completed_clause:195`. **Read the D-01 DECISION RECORD in the module
  docstring.** New home of the D-08/D-09 awaiting-candidate builder.
- `src/phaze/enums/stage.py` — `Stage`/`Status`, `ELIGIBILITY_DAG`, `FAILURE_IS_TERMINAL:87`,
  `resolve_status()`, `eligible()`, `domain_completed:186`.
- `src/phaze/services/backends.py:86` — `hold_awaiting_cloud()`, the SINGLE go-forward `awaiting`
  writer (hold mode vs. spill-mode CAS). `IN_FLIGHT`, `resolve_backends`, `KueueBackend.reconcile`.
  `_enqueue_push_file:154` — proves `push_file` is compute-only (D-07).
- `src/phaze/services/shadow_compare.py` — `_cloud_job_exists:68`, `_cloud_awaiting:82`, the
  `awaiting_cloud` / `pushing` / `pushed` HARD invariants (`:131`, `:133`, `:134`). Must stay green.

### Existing code — the cutover targets
- `src/phaze/tasks/reenqueue.py` — **the primary target.** Module docstring carries THREE reframes
  that must be honored: the Phase-42 durability reframe, the **Phase-45 ledger reframe** (the
  operator spec for the over-enqueue incident), and the per-stage domain-completed predicate
  contract. Cutover sites: `_build_done_sets` (:136), `_select_done_analyze_ids` (:177, state read at
  :187), `_select_done_push_ids` (:190, state read at :197), `_get_awaiting_cloud_ids` (:200, state
  read at :209), `is_domain_completed` (:242). `_in_flight_cloud_job_ids` (:212) needs **no** change.
  Pending-fn imports to drop: `:79`, `:81`.
- `src/phaze/tasks/reconcile_cloud_jobs.py` — reads already sidecar-derived; the single write to
  retire is at **:212**, inside `_handle_no_callback_terminal`. Preserve the MKUE-04
  clean-before-flip ordering documented at :174-219.
- `src/phaze/services/scheduling_ledger.py` — `get_ledger_rows`, `insert_ledger_if_absent`.
- `src/phaze/models/scheduling_ledger.py:63` — `enqueued_at`, the D-10 disambiguator.
- `src/phaze/services/pipeline.py` — `count_inflight_jobs`, `get_live_job_keys` stay;
  `get_metadata_pending_files:1382` / `get_fingerprint_pending_files:1415` imports are **removed**
  (D-05). `get_awaiting_cloud_count:1116` + `get_cloud_staging_candidates` are the two existing
  inline spellings of the D-08 conjunct.
- `src/phaze/routers/pipeline.py:956` — `retry_analysis_failed` clears `failed_at` (why analyze has
  no D-10 cell). `:974` — `retry_metadata_failed`, which does not (why metadata does).
- `src/phaze/routers/agent_push.py:285,307` / `src/phaze/routers/agent_s3.py:212,232` — the two
  sibling spill paths; the pattern D-04 deliberately diverges from.
- `src/phaze/routers/agent_analysis.py:266,390` — the D-14 analyze-terminal reap seams.
- `src/phaze/models/cloud_job.py` — `CloudJobStatus` (note: **still no `'pushed'` member**),
  the status CHECK constraint, `ix_cloud_job_awaiting`.
- `src/phaze/models/analysis.py:56` — the NAND check constraint (D-13).

### Test harness conventions
- `tests/buckets.json` + `tests/shared/test_partition_guard.py` — one bucket per file; new tests must
  pass via `just test-bucket <bucket>` **in isolation**, not merely in the full suite.
- `tests/test_recovery.py` — the existing totality test asserting
  `_DOMAIN_COMPLETED_STAGES` XOR live-keys-only against `_KEY_BUILDERS` (T-45-17). The SC-2
  never-scheduled-`discovered`-file regression test belongs alongside it.
- `tests/integration/test_stage_status_equivalence.py:415-427` — the SCOPE comment + `*_inflight`
  seed exclusion that D-11 amends.
- `tests/test_task_split.py` — enforces the control-only import boundary; `reenqueue.py` must never
  be importable from the agent worker.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`services/stage_status.py` + `enums/stage.py` (Phases 78 + 81)** — the entire derivation layer,
  now including both `domain_completed` twins. This is the primary reuse and the reason Phase 80 is a
  thin cutover rather than new logic.
- **`services/backends.py:86` `hold_awaiting_cloud()` (Phase 83)** — the single go-forward `awaiting`
  writer, with a rowcount-guarded spill-mode CAS. D-12 makes reconcile its fourth caller.
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
- **CAS-then-gated-write**: `agent_push.py:285-307` — the sidecar CAS runs first, and the `FileRecord`
  write (plus ledger clear) is gated behind its rowcount. D-12 adopts the CAS half; D-04 drops the
  `FileRecord` half.
- **Fingerprint "done" spelling** `status IN ('success','completed')` (Phase-59 WR-02 / PR #189) —
  already encoded in `done_clause(fingerprint)`; renders as `= ANY (ARRAY[...])` matching
  `ix_fprint_success`.
- **SAVEPOINT-isolated, degrade-to-safe-default** reads for anything touching `saq_jobs`
  (`services/pipeline.py` `_safe_count` idiom). Alembic **never** references `saq_jobs`.
- **Never a raw random-key enqueue** — every replay goes through `_replay_row` with `key=row.key`,
  so the deterministic-key dedup collapses a still-live item to a skipped no-op (the Phase-32
  doubling backstop).
- **Stored job policy replay** — `row.timeout` / `row.retries` are replayed when present so a
  recovered long `process_file` keeps its 7200s/retries=2 bound (the recover-button timeout-loss
  bug). Untouched by this phase, but do not regress it.

### Integration Points
- `recover_orphaned_work` is called by **both** the controller startup hook and the manual "Recover"
  button (`force=True`), by design so the automatic and manual paths cannot drift. Any change
  to the done-set semantics changes **both** at once.
- The `force=True` path bypasses **only** the no-op DETECT gate — never the per-item deterministic-key
  dedup. Preserve this exactly.
- `reconcile_cloud_jobs` dispatches per-backend (`for b in resolve_backends(cfg)`), and the per-row
  advisory lock is acquired at the top of each `KueueBackend.reconcile` unit. The :212 write swap
  happens *inside* that held lock.
- `services/backends.py` does a module-top `from phaze.tasks.reconcile_cloud_jobs import _reconcile_one`,
  so `reconcile_cloud_jobs` imports `resolve_backends` **function-locally** to break the cycle. Do not
  hoist that import. This is also why D-09 rejects `backends.py` as the builder's home.

</code_context>

<specifics>
## Specific Ideas

### Findings surfaced by the 2026-07-10 refresh (not present in the original context)

1. **`reconcile_cloud_jobs.py`'s at-cap spill violates a HARD shadow invariant on `main` today.**
   It writes `state = AWAITING_CLOUD` while stamping `cloud_job.status = FAILED`, but
   `awaiting_cloud` (`shadow_compare.py:131`, `soft=False`) asserts
   `state == AWAITING_CLOUD ⇒ cloud_job(status='awaiting')`. Phase 83 could not fix it because
   D-04 scoped this file to Phase 80. **D-04 + D-12 fix it as a side effect** — worth an explicit
   assertion in the phase's regression suite rather than leaving it implicit.

2. **The `034` number names two migrations in the planning docs.** Resolved by D-14.

3. **`033`'s constraint is a NAND, not the XOR its name implies.** Resolved by D-13's `failed_at IS
   NULL` guard.

4. **WR-02's own suggested fix would disable the over-enqueue net.** Resolved (and permanently
   documented) by D-11.

### The governing principle this discussion established

> **A read-cutover phase must follow the writer-cutover phase that keeps its derived source live.**

The `032` migration backfilled the derived sources **once**. Any reader that cuts over before its
source has a live writer reads data frozen at backfill time. This bit Phase 80 twice (analyze/metadata
failure markers → Phase 81; cloud-routing sidecar → Phase 83), and the folded todo is a **third
instance of the same class**: `analysis_completed_at` was never backfilled at all.

**Worth checking during Phase 82's discussion:** the same inversion may affect READ-01/READ-02
(pending sets + `get_pipeline_stats` counts), which likewise derive from `failed` markers and the
cloud sidecar. Not acted on here — flagged only.

### Non-negotiables carried into planning

- The SC-2 regression test — **a never-scheduled `discovered` file is not recovered** — is the
  headline guard. It belongs next to the existing `_DOMAIN_COMPLETED_STAGES` totality test in
  `tests/test_recovery.py`.
- A companion regression asserting **a failed analyze is never produced by any automatic recovery
  path** (the `FAILURE_IS_TERMINAL[analyze]` encoding, ELIG-03's twin at the recovery layer).
- The D-10 regression covering **both** metadata cells (orphaned retry re-drives; partial-failure
  callback stays terminal) — non-vacuous, proven by reverting the fix and watching it go red.
- `reenqueue.py`'s three module-docstring reframes (Phase-42 durability, Phase-45 ledger, the
  domain-completed contract) must be **updated in place**, not deleted — they are the institutional
  memory for two production incidents.

</specifics>

<deferred>
## Deferred Ideas

- **PROV-01 — N-compute-aware orphan recovery.** Design §9 explicitly non-goals this, while noting
  *"`reenqueue.py` is heavily touched here, so re-check the overlap during planning."* Phase 80
  touches `recover_orphaned_work`'s done-set derivation, **not** its compute-agent selection
  (`select_active_agent(session, kind="compute")` stays single-active-compute). Deferred to
  v2 per the 2026.7.2 close-out. Re-check, do not fix.
- **Clearing `metadata.failed_at` at retry-enqueue** (to mirror `retry_analysis_failed`). This was
  the considered alternative to D-10: it would eliminate the ambiguous `in_flight ∧ failed` cell at
  its source rather than disambiguating it. **Rejected here** because it reverses Phase 81's shipped
  D-11 and would drop a retried-but-not-yet-succeeded file out of the failed-metadata UI count during
  the retry window. Revisit if the enqueued_at gate proves fragile.
- **Un-excluding the `*_inflight` equivalence seeds** by asserting the twins deliberately *disagree*
  on those cells. Rejected as reading oddly inside a drift-lock test; D-11's docstring + regression
  is the chosen lock.
- **Phase 82 read-before-write inversion check** — whether READ-01/READ-02 hit the same D-02/D-03
  hazard. Belongs to Phase 82's discussion.
- **Soft-allowlisting `analyzed` in the shadow gate**, or accepting a non-zero `hard_fail_total`
  (options 2 and 3 of the folded todo). Neither fixes Phase 80's over-enqueue hazard, so both are
  out of scope here; they belong to Phase 79/90's gate design if the backfill ever proves incomplete.
- **`report_uploaded`'s now-redundant `FileRecord.state == PUSHING` guard** (`agent_s3.py:128`) —
  83 left the symmetry question to research. Not Phase 80's file.
- **Phase 79's deferred live-corpus shadow-compare run (SC-3)** — must be recorded before the
  destructive migration. Migration `036` (D-13) is a precondition for that run going green.
- **Dropping the now-unused `FileState` import from `reconcile_cloud_jobs.py:45`** once D-04 lands —
  a natural consequence, noted so it is not mistaken for scope creep.

None else — discussion stayed within phase scope.

</deferred>

---

*Phase: 80-recovery-re-enqueue-cutover*
*Context gathered: 2026-07-08 · refreshed 2026-07-10 against `main` @ `09cefc6d`*
