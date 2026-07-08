# Requirements: Phaze — 2026.7.5 Parallel Enrich DAG

**Defined:** 2026-07-08
**Core Value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata in Postgres — human-in-the-loop approval so nothing moves without review.
**Design contract:** `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` (approved; do not re-litigate the core model)
**Research:** `.planning/research/{STACK,FEATURES,ARCHITECTURE,PITFALLS,SUMMARY}.md`

**Milestone goal:** Make the enrich pipeline truly per-file parallel by deleting the linear `FileState` enum and deriving per-file, per-stage status from the output tables that already exist.

> **Framing.** "User" here is the single operator driving the pipeline console, plus the system-correctness guarantees the operator depends on. Requirements are written to be observable and testable — via a query result, a UI state, a passing regression test, or a shadow-compare invariant.

---

## v1 Requirements

### Derived Status Layer (DERIV)

- [ ] **DERIV-01**: A single predicate module is the one source of truth for every stage's `done` and `failed` predicate, expressed as reusable `ColumnElement[bool]` builders that compose into both SQL `.where(...)` and a Python per-row resolver — no stage predicate is written twice.
- [ ] **DERIV-02**: A pure function `stage_status(file, stage) -> {not_started | in_flight | done | failed}` returns the derived status for any file/stage, with precedence `in_flight ≻ done ≻ failed ≻ not_started`.
- [ ] **DERIV-03**: `done` is derived from the stage's output row using the *correct* completion predicate per stage: `metadata` row present (and not failure-only); `fingerprint_results.status IN ('success','completed')` (any engine); `analysis.analysis_completed_at IS NOT NULL` (not bare row existence); `tracklists`/`proposals`/`execution_log` presence for downstream stages.
- [ ] **DERIV-04**: A parametrized equivalence test proves the SQL-derived status and the Python-derived status agree for every stage across a representative fixture matrix (locks the two definitions against drift).
- [ ] **DERIV-05**: `stage_status` correctly aggregates multi-row output tables — a file with one `success` and one `failed` fingerprint engine derives `done`, not `failed`.

### Eligibility (ELIG)

- [ ] **ELIG-01**: The three enrich stages (metadata, fingerprint, analyze) are eligible iff `NOT done AND NOT in_flight`, each **independent of every other stage** — every `discovered` file is simultaneously eligible for all three, workable in any order.
- [ ] **ELIG-02**: Downstream eligibility is a pure predicate over `stage_status`: tracklist = fingerprint-done & not-tracklisted; propose = metadata-done AND analyze-done; review = a proposal exists; apply = an approved proposal exists.
- [ ] **ELIG-03**: A failed **analyze** is terminal — it is NOT auto-eligible and never re-enqueued by any automatic path (retry is manual-only), with a regression test asserting a failed analyze is absent from the analyze pending set (guards against the 44.5K-job over-enqueue class).
- [ ] **ELIG-04**: A failed **fingerprint** remains eligible (auto-retry preserved), consistent with today's D-16 behavior.

### In-Flight Detection (INFLIGHT)

- [ ] **INFLIGHT-01**: `in_flight(file, stage)` is true when an active/queued unit of work exists for that `(file, stage-function)`, and it is a first-class input to both eligibility and the DAG busy pills.
- [ ] **INFLIGHT-02**: Every read of the SAQ `saq_jobs` table is static SQL wrapped in a `begin_nested()` SAVEPOINT and degrades to a safe default on any error — the 5s `/pipeline/stats` poll never 500s; Alembic never references `saq_jobs`.
- [ ] **INFLIGHT-03**: *(Open decision D-01 — resolve during Phase 78 planning with a written decision record.)* `in_flight`'s authoritative source is chosen between `scheduling_ledger` alone (Architecture's position) and `saq_jobs ∪ scheduling_ledger` (design/Stack position); whichever is chosen, a crashed-mid-run or callback-lost file is not falsely re-enqueued as `not_started`.

### Per-Stage Failure (FAIL)

- [ ] **FAIL-01**: `analyze` failures persist a durable failure marker (replacing the `ANALYSIS_FAILED` enum value) with an error reason, backfilled from existing `ANALYSIS_FAILED` rows.
- [ ] **FAIL-02**: `metadata` failures persist a durable failure marker — `report_metadata_failed` records the failure instead of nothing (closes the latent bug where terminally-failed metadata is invisible everywhere).
- [ ] **FAIL-03**: A terminally-failed metadata file has a retry path — the operator can re-run it, so a metadata failure is never a permanent dead-end that blocks the file from ever reaching `propose`. *(closes gap G-01, CRITICAL)*
- [ ] **FAIL-04**: `fingerprint` failure continues to persist via `fingerprint_results.status='failed'` (reused, not re-invented) and stays auto-retryable.

### Reader Rework (READ)

- [ ] **READ-01**: The three enrich pending sets are derived from `stage_status` (not from `FileRecord.state`), so metadata/fingerprint/analyze each surface every not-done, not-in-flight file independent of the others — the current cross-stage deadlock is gone (a file can complete all three, in any order).
- [ ] **READ-02**: `get_pipeline_stats` reports per-stage counts from output tables (the linear `GROUP BY FileRecord.state` is removed), and the DAG shows four-bucket per-stage counts (not_started / in_flight / done / failed) including a visible failed count per enrich stage.
- [ ] **READ-03**: Recovery/re-enqueue (`reenqueue.py`, `reconcile_cloud_jobs.py`) derive their done/in-flight sets from `stage_status`/sidecars with no `FileRecord.state` read, preserving the scheduling-ledger recovery contract and the "only previously-scheduled work recovers" guarantee.
- [ ] **READ-04**: Dedup (`services/dedup.py`) and `get_fingerprint_progress` derive from the dedup marker / output tables rather than `FileRecord.state`.
- [ ] **READ-05**: The dead `state == EXECUTED` gates are revived against the real apply-outcome source — tag writing, review, tags/cue/tracklists guards fire for actually-applied files (fixes the permanently-dead tag-writer path).

### Sidecar Migration (SIDECAR)

- [ ] **SIDECAR-01**: Cloud-routing status (`AWAITING_CLOUD`/`PUSHING`/`PUSHED`/`LOCAL_ANALYZING`) is represented via the `cloud_job` sidecar (and/or derived `in_flight(analyze)`), with the CAS-guard behavior of `/pushed`, `/mismatch`, and `/upload-failed` preserved or strengthened (closes the missing-CAS-guard bug at `agent_s3.py:195`).
- [ ] **SIDECAR-02**: Dedup resolution (`DUPLICATE_RESOLVED`) is represented via a durable dedup marker, with resolve/undo preserved and backfilled from existing rows.
- [ ] **SIDECAR-03**: Review decisions (approve/reject) and apply outcomes are read from `proposals.status` + `execution_log` — `FileRecord.state` is no longer a redundant, drift-prone mirror of proposal state (fixes the `store_proposals` MOVED-regression bug).

### Operator UI (UI)

- [ ] **UI-01**: The file-row "State" display is derived from `stage_status` per stage (a per-file stage matrix), replacing the raw-enum-string column; the list is paginated and never renders a query that scans the whole 200K corpus per poll.
- [ ] **UI-02**: The operator can see failed files per enrich stage and trigger a retry from the console (fingerprint/metadata retry + the existing manual analyze retry). *(failure visibility + retry affordance)*
- [ ] **UI-03**: For any file not in a stage's pending set, the operator can see **why it is not eligible** — an eligibility trace over the pure `eligible()` conjuncts (done? in-flight? upstream unmet? terminally failed?). *(closes gap G-04; the diagnostic whose absence hid the current deadlock)*
- [ ] **UI-04**: The operator can force a stage to done / skip a stage for a specific file, so the `failed` bucket for genuinely-unprocessable files can converge rather than accumulate permanently. *(gap G-03)*
- [ ] **UI-05**: An orphaned/stuck-work count is surfaced (files with an in-flight marker but no progress), derived for free from the chosen `in_flight` source.

### Performance (PERF)

- [x] **PERF-01**: Partial indexes sized to the exact `done`/`failed` predicates keep the `NOT EXISTS` pending anti-joins and the per-stage counts fast at 200K-file scale; each index is mirrored into the ORM `__table_args__` so `autogenerate` stays in sync.
- [ ] **PERF-02**: The `/pipeline/stats` poll latency at 200K-file corpus scale is measured and recorded in the phase VERIFICATION; no denormalized status column is added unless that measurement shows the derived query is too slow (YAGNI is the default).

### Legacy Sentinel Retirement (LEGACY)

*Folded in from the #222 post-deploy backlog. Data-model-migration twin of this milestone; part (a) directly removes two `FileState` writers. See memory `project_legacy_sentinel_retirement`.*

- [ ] **LEGACY-01**: The orphaned legacy scan path is deleted — `POST /api/v1/scan` (`routers/scan.py`), `run_scan`, and `discover_and_hash_files` (`services/ingestion.py`) are removed, so no new `files`/`scan_batches` row is ever attributed to `legacy-application-server` (and two `FileState`-writing upsert sites disappear from the migration surface). The FK ownership model (`agent_id` = owning fileserver) is preserved.
- [ ] **LEGACY-02**: A data-migration reattributes all historical `legacy-application-server`-owned `files` and `scan_batches` to a designated real `kind='fileserver'` agent (e.g. nox), with a backfill-verification check.
- [ ] **LEGACY-03**: After reattribution, the `agent_id` column `default=` is dropped and the `legacy-application-server` sentinel `Agent` row is deleted (the `ondelete=RESTRICT` FK is satisfiable only because LEGACY-02 reattributed first).

### Priority UI Control (PRIO)

*Folded in from the #222 post-deploy backlog. Backend is live end-to-end; only the v7.0-deleted UI control is missing.*

- [ ] **PRIO-01**: The operator can change a per-stage job priority from the shell (a stepper wired to the existing `POST /pipeline/stages/{stage}/priority` endpoint — ▲ raises priority / lowers the number), re-connecting the orphaned setter; pause/resume controls are surfaced too if that endpoint is likewise orphaned.

### Lane / Agent Drill-In (DRILL)

*Folded in from the #222 post-deploy backlog. The agent-activity view consumes the new `stage_status`.*

- [ ] **DRILL-01**: Clicking a backend-lane card opens a lane-detail view (new `GET /pipeline/lanes/{backend_id}`) showing that lane's queues / in-flight / waiting / quota / recent completions.
- [ ] **DRILL-02**: Clicking an agent row opens an agent-detail view (new `GET /admin/agents/{agent_id}/_activity`) showing owned files grouped by derived `stage_status`, recent scan batches, per-lane queue depths, and liveness.
- [ ] **DRILL-03**: The drill-in survives the 5s poll swap (selection carried via URL param / rendered outside the polled `outerHTML` region so it is not clobbered) and is keyboard-accessible (`role=button`, Enter/Space, focus ring).

### Migration & Verification (MIG)

- [x] **MIG-01**: Migration `032` is additive-only — it creates the failure markers, the dedup marker, and the cloud sidecar representation, adds the partial indexes, and backfills them from `FileRecord.state`, **without touching `files.state`**.
- [ ] **MIG-02**: A committed, re-runnable shadow-compare check asserts per-file *implication* invariants (e.g. `state=ANALYZED ⇒ analysis_completed_at IS NOT NULL`; `state=DUPLICATE_RESOLVED ⇒ dedup marker`) across the live corpus, with `FINGERPRINTED` documented as the one expected divergence; it must pass before any reader cutover and before the destructive migration.
- [x] **MIG-03**: Rescanning a file no longer resets pipeline progress — with `FileRecord.state` gone, the `ON CONFLICT DO UPDATE SET state = excluded.state` progress-wipe is structurally impossible (fixes the rescan-wipe bug).
- [ ] **MIG-04**: Migration `033` is destructive and lands last — after the shadow-compare passes on the live corpus and cloud-push lanes are drained/quiesced, it drops `ix_files_state`, drops `files.state`, and deletes the `FileState` enum; its `downgrade()` documents the enum reconstruction from derived sources (and its lossiness).

---

## v2 Requirements

### Deferred

- **PROV-01**: N-compute-aware orphan recovery in `recover_orphaned_work` (carried from 2026.7.2; `reenqueue.py` is heavily touched here — re-check overlap during planning but do not expand scope to it).
- **DENORM-01**: A denormalized stored stage-bitmap column, *only if* PERF-02's measurement proves the derived query too slow. Explicitly not built speculatively.

---

## Out of Scope

| Feature | Reason |
|---------|--------|
| Denormalized stage-bitmap column (built speculatively) | YAGNI — derive first, measure (PERF-02), denormalize only if proven slow (→ v2 DENORM-01) |
| New runtime dependencies | Hard milestone constraint — the existing PostgreSQL 18.4 + SQLAlchemy 2.x stack suffices |
| Changing routing *policy* (duration threshold, backend rank/cap) | Only where routing *state* is stored changes, not how routing decides |
| Auto-retry of failed analyze | Deliberately terminal — auto-retry caused the 44.5K-job over-enqueue incident (ELIG-03) |
| Rendering raw internal status strings in the UI | Anti-feature — every reference tool warns against it; UI shows derived per-stage status only (UI-01) |
| A stats poll that scans the whole corpus | Anti-feature at 200K scale — aggregate-first counts + paginated drill-down only (UI-01, PERF-01) |

---

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| DERIV-01 | Phase 78 | Pending |
| DERIV-02 | Phase 78 | Pending |
| DERIV-03 | Phase 78 | Pending |
| DERIV-04 | Phase 78 | Pending |
| DERIV-05 | Phase 78 | Pending |
| ELIG-01 | Phase 78 | Pending |
| ELIG-02 | Phase 78 | Pending |
| ELIG-03 | Phase 78 | Pending |
| ELIG-04 | Phase 78 | Pending |
| INFLIGHT-01 | Phase 78 | Pending |
| INFLIGHT-02 | Phase 78 | Pending |
| INFLIGHT-03 | Phase 78 | Pending |
| FAIL-01 | Phase 81 | Pending |
| FAIL-02 | Phase 81 | Pending |
| FAIL-03 | Phase 81 | Pending |
| FAIL-04 | Phase 81 | Pending |
| READ-01 | Phase 82 | Pending |
| READ-02 | Phase 82 | Pending |
| READ-03 | Phase 80 | Pending |
| READ-04 | Phase 84 | Pending |
| READ-05 | Phase 85 | Pending |
| SIDECAR-01 | Phase 83 | Pending |
| SIDECAR-02 | Phase 84 | Pending |
| SIDECAR-03 | Phase 86 | Pending |
| UI-01 | Phase 87 | Pending |
| UI-02 | Phase 87 | Pending |
| UI-03 | Phase 87 | Pending |
| UI-04 | Phase 87 | Pending |
| UI-05 | Phase 87 | Pending |
| PERF-01 | Phase 77 | Complete |
| PERF-02 | Phase 82 | Pending |
| MIG-01 | Phase 77 | Complete |
| MIG-02 | Phase 79 | Pending |
| MIG-03 | Phase 77 | Complete |
| MIG-04 | Phase 90 | Pending |
| LEGACY-01 | Phase 89 | Pending |
| LEGACY-02 | Phase 89 | Pending |
| LEGACY-03 | Phase 89 | Pending |
| PRIO-01 | Phase 87 | Pending |
| DRILL-01 | Phase 88 | Pending |
| DRILL-02 | Phase 88 | Pending |
| DRILL-03 | Phase 88 | Pending |

**Coverage:**
- v1 requirements: 42 total *(the initial "41 total" was an off-by-one; the traceability table has always listed 42 distinct IDs)*
- Mapped to phases: 42 (100%)
- Unmapped: 0 ✓ — every v1 requirement maps to exactly one phase, no orphans, no duplicates

---
*Requirements defined: 2026-07-08*
*Last updated: 2026-07-08 — traceability populated by roadmapper (Phases 77–90, milestone 2026.7.5)*
