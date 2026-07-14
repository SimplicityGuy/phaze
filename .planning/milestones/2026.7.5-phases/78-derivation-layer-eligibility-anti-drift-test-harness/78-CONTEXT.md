# Phase 78: Derivation Layer, Eligibility & Anti-Drift Test Harness - Context

**Gathered:** 2026-07-08
**Status:** Ready for planning

<domain>
## Phase Boundary

Ship the single source-of-truth predicate module — `enums/stage.py` (DB-free, agent-safe) +
`services/stage_status.py` — so every caller can derive per-file, per-stage
`{not_started | in_flight | done | failed}` and eligibility from the output tables (`metadata`,
`fingerprint_results`, `analysis`, `tracklists`, `proposals`, `execution_log`) + the durable
`scheduling_ledger` (with `saq_jobs` as a corroborating signal), with the SQL and Python
definitions locked together against drift by a parametrized equivalence test.

**Purely additive.** No reader or writer cuts over to the new module in this phase — the module
and its test harness ship alongside the existing linear `FileState` logic. Cutover is later
phases (79 shadow-compare, then reader/writer swaps).

**Requirements:** DERIV-01..05, ELIG-01..04, INFLIGHT-01..03.
</domain>

<decisions>
## Implementation Decisions

### in_flight source (INFLIGHT-03 / the roadmap-flagged OPEN DECISION — RESOLVED)
- **D-01:** `in_flight(file, stage)`'s authoritative source is the **`scheduling_ledger`** — a ledger
  row for the `(file, stage-function)` key means in_flight. `saq_jobs` is a **corroborating signal
  only**, not authoritative. Rationale: the ledger is written at the same single `before_enqueue`
  chokepoint that creates the `saq_jobs` row (so `ledger ⊇ saq_jobs` keys in the normal path), it is
  **durable** (survives a broker truncate/restore and outlives a crashed job's lost `saq_jobs` row),
  and it decouples the hot 5s `/pipeline/stats` poll from live-broker coupling. This satisfies the
  safety property — a crashed-mid-run / callback-lost file keeps its ledger row and therefore reads
  `in_flight`, never falsely `not_started` (guards the 44.5K over-enqueue class). Chosen over
  Architecture's strict **ledger-alone** (loses the corroboration hook) and design/Stack's
  **`saq_jobs ∪ scheduling_ledger` union** (makes the live broker load-bearing on the hot path and
  enlarges the false-positive-stuck set). **This is the required written decision record for D-01.**

### in_flight degrade behavior (INFLIGHT-02)
- **D-02:** The **`in_flight` boolean = a ledger row exists** — full stop. This is also the
  **degrade-safe default**: the `saq_jobs` read is static SQL wrapped in a `begin_nested()` SAVEPOINT
  and is used **only** to enrich observability / the DAG busy pills with the queued-vs-active detail;
  it **never flips the boolean**. On ANY `saq_jobs` error, drop the detail and keep `in_flight` from
  the ledger. Consequence: `/pipeline/stats` never 500s on a broker read hiccup, and Alembic never
  references `saq_jobs` (Phase 77 banner/guard carried forward).

### done(metadata) predicate (DERIV-03)
- **D-03:** `done(metadata)` = **a `metadata` row is present AND `failed_at IS NULL`.** This honors
  the Phase 77 D-02 handoff (a metadata failure inserts a row with `failed_at` set; a failure-only
  row therefore derives NOT-done → failed). Additive-safe today: Phase 77 skipped the metadata
  backfill, so every existing `metadata` row has `failed_at = NULL` and behavior is unchanged now,
  correct after writers cut over. (Bare row-presence was rejected — it would let a failure row read
  `done`, defeating the whole failure-marker design.)

### Predicate module boundary & drift-lock (DERIV-01 / DERIV-04)
- **D-04:** Two-module split with the equivalence test as the real lock:
  - **`enums/stage.py`** (DB-free, agent-safe — no SQLAlchemy model imports): the `Stage` / `Status`
    enums, the **eligibility DAG topology** (which stage gates on which upstream), and the **pure-Python
    per-row resolver** that computes `{not_started | in_flight | done | failed}` from plain scalars
    (so a compute/file-server agent can derive status without a DB round-trip).
  - **`services/stage_status.py`**: the SQLAlchemy **`ColumnElement[bool]` builders** that compose into
    `.where(...)` for set-based queries.
  - **`DERIV-04` parametrized equivalence test** asserts SQL-derived status == Python-derived status
    for every stage across the full fixture matrix — this is the drift-lock. Author-once via shared
    comparison expressions where the SQL/Python idioms coincide; the test is authoritative where they
    diverge (`IS NOT NULL`, `IN (...)`).

### Locked by ROADMAP success criteria (not re-discussed — carried into planning as-is)
- Precedence **`in_flight ≻ done ≻ failed ≻ not_started`** (DERIV-02).
- Per-stage `done`: `fingerprint_results.status IN ('success','completed')` any engine;
  `analysis.analysis_completed_at IS NOT NULL` (not bare row existence); `tracklists`/`proposals`/
  `execution_log` presence for downstream stages (DERIV-03).
- **DERIV-05** multi-row aggregation: one `success` + one `failed` fingerprint engine derives `done`.
- **ELIG-01** the three enrich stages (metadata, fingerprint, analyze) have **no upstream** — every
  `discovered` file is simultaneously eligible for all three, in any order; `eligible = NOT done AND
  NOT in_flight`.
- **ELIG-02** downstream eligibility is pure over `stage_status`: tracklist = fingerprint-done &
  not-tracklisted; propose = metadata-done AND analyze-done; review = a proposal exists; apply = an
  approved proposal exists.
- **ELIG-03** a **failed analyze is terminal** — never auto-eligible / auto-re-enqueued (retry is
  manual-only); regression test asserts it is absent from the analyze pending/eligible set.
- **ELIG-04** a **failed fingerprint stays eligible** (auto-retry preserved, consistent with D-16).

### Claude's Discretion
- Exact fixture-matrix shape for the DERIV-04 equivalence test, the internal signature of the shared
  predicate builders, and the precise SAVEPOINT/degrade helper are left to research + planning.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase requirements & roadmap
- `.planning/ROADMAP.md` §"Phase 78: Derivation Layer, Eligibility & Anti-Drift Test Harness" — goal, 5 success criteria, the D-01 OPEN-DECISION note.
- `.planning/REQUIREMENTS.md` — DERIV-01..05, ELIG-01..04, INFLIGHT-01..03 (full text).
- `.planning/MILESTONES.md` — parallel-enrich DAG milestone framing and prior-phase accomplishments.

### Upstream phase (additive schema this phase reads)
- `.planning/phases/77-additive-schema-rescan-wipe-fix-migration-032/77-CONTEXT.md` — D-01 (failed_at/error_message markers), D-02 (reader phase tightens `done(metadata)` to `failed_at IS NULL`), D-03 (metadata NOT backfilled), D-04 (`cloud_job.AWAITING`), D-05 (LOCAL_ANALYZING = in_flight(analyze), no stored row), D-07 (dedup_resolution).
- `alembic/versions/032_add_derived_status_schema.py` — the additive columns/table/CHECK/indexes this layer derives over.

### Existing code (source-of-truth for in_flight & recovery)
- `src/phaze/models/scheduling_ledger.py` — the durable "was scheduled" ledger; module docstring explains the 44.5K over-enqueue incident and `orphaned = ledger − live saq_jobs − domain-completed`.
- `src/phaze/tasks/reenqueue.py` — `recover_orphaned_work`; already reads ledger ∩/− saq_jobs; the reconcile that D-01 must stay consistent with.
- `src/phaze/services/scheduling_ledger.py` — `get_ledger_rows`, `insert_ledger_if_absent`.
- `src/phaze/models/analysis.py`, `src/phaze/models/metadata.py`, `src/phaze/models/fingerprint.py`, `src/phaze/models/cloud_job.py`, `src/phaze/models/dedup_resolution.py` — the output tables the predicates read.
- `src/phaze/enums/` — where `stage.py` lands (currently `execution.py`, `__init__.py`).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `scheduling_ledger` model + `services/scheduling_ledger.py` accessors — the authoritative in_flight backbone; reuse, don't reinvent.
- `begin_nested()` SAVEPOINT pattern already used in `tasks/reenqueue.py`, `services/review.py`, `services/pipeline.py` — the established degrade-safe idiom for the corroborating `saq_jobs` read (INFLIGHT-02).
- Phase 77 partial indexes (`ix_analysis_completed`, `ix_analysis_failed`, `ix_metadata_failed`, `ix_cloud_job_awaiting`, `ix_fprint_success`) — the derivation queries' index support already exists.

### Established Patterns
- `recover_orphaned_work` reconcile (`orphaned = ledger − live saq_jobs − domain-completed`) is the precedent that D-01 (ledger authoritative, saq_jobs corroborating) mirrors.
- Fingerprint "done" already uses `status IN ('success','completed')` (the Phase-59 WR-02 fix, PR #189) — reuse the same spelling; the `= ANY (ARRAY[...])` index form from Phase 77.

### Integration Points
- `enums/stage.py` must be importable by agents (compute / file-server) with NO SQLAlchemy/DB dependency.
- `services/stage_status.py` `.where()` builders feed the eligibility/pending SELECTs and (later phases) the DAG busy pills and reader cutover.

</code_context>

<specifics>
## Specific Ideas

- The whole phase is additive scaffolding + an anti-drift test harness; correctness is proven by the DERIV-04 equivalence test and the ELIG-03 terminal-failed-analyze regression test (the explicit guard against the 44.5K-job over-enqueue class).

</specifics>

<deferred>
## Deferred Ideas

- Reader/writer cutover to the derived status (DAG busy pills reading `in_flight`, pending-set queries using `eligible()`) — later milestone phases (79 shadow-compare gate first, then cutover). Not in Phase 78.
- Tightening any metadata *writer* to set `failed_at` on failure — writer-side change, later phase; Phase 78 only reads the column.

None else — discussion stayed within phase scope.

</deferred>

---

*Phase: 78-derivation-layer-eligibility-anti-drift-test-harness*
*Context gathered: 2026-07-08*
