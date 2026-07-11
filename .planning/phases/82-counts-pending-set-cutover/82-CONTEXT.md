# Phase 82: Counts & Pending-Set Cutover - Context

**Gathered:** 2026-07-10
**Status:** Ready for planning

<domain>
## Phase Boundary

Rewrite the **three enrich pending sets** and the **pipeline stats/DAG counts** off `FileRecord.state`
and onto the derived `stage_status` layer — so metadata/fingerprint/analyze each surface every
not-done, not-in-flight file **independent of the others** and the cross-stage deadlock dissolves — then
**measure** the 5s `/pipeline/stats` poll at 200K-file scale. This is the milestone's thesis made
executable (READ-01, READ-02, PERF-02).

**This is a pure READER cutover.** Unlike siblings 83/84 (each of which discovered a *missing
go-forward writer* and had to repair the corpus), Phase 82 adds no writer and owns no data-repair
migration — every building block already exists on `main`. Its risk is entirely in getting the derived
read queries right and proving they stay fast at scale.

**In scope:**
- **`get_metadata_pending_files`** (`services/pipeline.py:1370`) — currently returns *all* music/video
  files (stateless idempotent set); narrow to `eligible_clause(METADATA) ∧ ~dedup_resolved_clause()`.
- **`get_fingerprint_pending_files`** (`services/pipeline.py:1403`) — currently `state ==
  METADATA_EXTRACTED` (the upstream gate that *is* the deadlock) UNION failed-retry; cut to
  `eligible_clause(FINGERPRINT) ∧ ~dedup_resolved_clause()`.
- **The analyze pending set** — `get_discovered_files_with_duration` (`services/pipeline.py:1098`),
  currently `state == DISCOVERED`; cut to `eligible_clause(ANALYZE) ∧ ~dedup_resolved_clause()` while
  **keeping the `LEFT JOIN FileMetadata.duration`** the cloud duration-router reads.
- **A new `eligible_clause(stage)` SQL builder** in `services/stage_status.py` (the SQL twin of the
  Python `eligible()`), drift-locked against `eligible()` by extending Phase-78's DERIV-04 equivalence
  harness.
- **`get_stage_progress`** (`services/pipeline.py:302`) — extend the three enrich nodes from
  `{done, total}` to the four-bucket `{not_started, in_flight, done, failed, total}`.
- **`get_pipeline_stats`** (`services/pipeline.py:61`) — remove the linear `GROUP BY FileRecord.state`
  entirely; derive all counts from output tables.
- **The PERF-02 measurement** — a local synthetic-seed 200K benchmark + EXPLAIN ANALYZE, recorded in
  VERIFICATION.

**Out of scope:**
- Any new **writer** or **data-repair migration** — the analyze-corpus repair (`036`) already shipped in
  **Phase 80** (see D-02). No new Alembic revision in this phase unless PERF-02 proves DENORM-01 needed.
- **`proposals.status` authority / `_TERMINAL_FILE_STATES`** — **Phase 86** (SIDECAR-03). Phase 82 may
  *count* proposal/execute nodes from output-table row existence (as `get_stage_progress` already does),
  which is orthogonal to 86's authority cutover.
- **`services/dedup.py` + `get_fingerprint_progress`** — **Phase 84** (already merged); this phase only
  *consumes* the `dedup_resolved_clause()` that phase landed.
- **Cloud routing / `cloud_job` sidecar** — **Phase 83** (already merged); this phase only *reads*
  `inflight_clause(ANALYZE)`, which now spans the sidecar states.
- **The `files.state` column drop + `FileState` enum deletion + remaining `.state=` writers** —
  **Phase 90**. Dual-write stays (READ-01 forbids `state` *reads*, not writes).

</domain>

<decisions>
## Implementation Decisions

### Upstream contract (carried forward — do not re-litigate)

- **D-00a: Writers dual-write.** `FileRecord.state` keeps being stamped; only *reliance* on it (reads)
  is replaced. The `state` write dies in Phase 90. (78-CONTEXT via 81 D-05 / 83 D-00c / 84 D-00a.)
- **D-00b: `in_flight` authority = `scheduling_ledger`** (78 D-01). `saq_jobs` is a corroborating signal
  only, read inside a `begin_nested()` SAVEPOINT, never flips the boolean, degrade-safe. Guards the
  44.5K over-enqueue class: a crashed/callback-lost file keeps its ledger row → reads `in_flight`, never
  falsely `not_started`.
- **D-00c ⚠ CORRECTED (2026-07-10, post-research A1): `inflight_clause(ANALYZE)` is LEDGER-ONLY — it does
  NOT read `cloud_job`.** `services/stage_status.py:176-193` derives in-flight solely from a
  `scheduling_ledger` row on the `"<analyze-fn>:<file_id>"` key; and `tasks/reconcile_cloud_jobs.py:122-123`
  states the cloud path **"writes NO scheduling-ledger row … never a `process_file` ledger seed."** So a
  cloud-dispatched file (`AWAITING_CLOUD`/`PUSHING`/`PUSHED`) is excluded from the derived analyze pending
  set **only if** its original local analyze ledger row is still present (not cleared on cloud hand-off).
  **This is the phase's sharpest correctness risk (double-dispatch: local + cloud).** The plan MUST (a)
  trace the ledger clear-on-handoff timing across the Phase-83 dispatch path, and (b) EITHER prove the
  ledger row survives until cloud analysis reports terminal, OR add an explicit active-`cloud_job`
  exclusion conjunct to the analyze pending set (e.g. `~exists(cloud_job WHERE status IN
  ('awaiting','pushing','pushed'))`). A regression test asserting a `PUSHING`/`PUSHED` file is ABSENT from
  the analyze pending set is mandatory (mirrors the Phase-83 drain-re-pick hazard). `LOCAL_ANALYZING` is
  `in_flight(analyze)` via the ledger (local job running) — that limb is fine.
- **D-00d: Per-stage failure policy is fully encoded** in `enums/stage.py`: `ELIGIBLE_AFTER_FAILURE =
  {ANALYZE: False, METADATA: True, FINGERPRINT: True}`. So a failed **analyze** is terminal (excluded
  from the analyze pending set, ELIG-03 — the manual-retry-only guard against the 44.5K incident); a
  failed **metadata/fingerprint** stays eligible for auto-retry (ELIG-04). `eligible_clause` must mirror
  this table, not inline it.
- **D-00e: The shadow-compare gate (Phase 79) must stay green** across the cutover — asserts implication
  not equality (`soft` allowlist). The pending-set/stats flip must introduce no new hard divergence.

### Pending-set derivation (D-01, D-03 — LOCKED)

- **D-01: Add `eligible_clause(stage)` to `services/stage_status.py`; drift-lock it against the Python
  `eligible()` via Phase-78's DERIV-04 equivalence harness.** All three enrich pending sets compose
  `eligible_clause(stage) ∧ ~dedup_resolved_clause() ∧ FileRecord.file_type.in_(MUSIC_VIDEO_TYPES)`.
  Single source of truth; the parametrized equivalence test proves SQL == Python for every stage across
  the fixture matrix. This is exactly the anti-drift shape Phase 78 built the harness for.

  (Rejected: hand-composing `~done_clause ∧ ~inflight_clause (∧ ~failed_clause for analyze)` inline in
  `pipeline.py` — the eligibility logic then lives in three places with no equivalence lock against
  `eligible()`, the drift class the milestone exists to prevent.)

- **D-03: Apply `~dedup_resolved_clause()` in all three enrich pending sets; keep `eligible()` /
  `eligible_clause()` dedup-agnostic.** This consummates **Phase 84's flagged hand-off**: once pending =
  `NOT done ∧ NOT in_flight`, a dedup-resolved duplicate with no fingerprint/analysis row becomes
  eligible and would be enqueued (today excluded only *incidentally* because `state = duplicate_resolved`
  ≠ `METADATA_EXTRACTED`). The dedup exclusion is composed at the `pipeline.py` query level, NOT baked
  into the eligibility primitives — dedup is a **file-level** predicate, not a `Stage`, and Phase 84
  deliberately kept `dedup_resolved_clause()` out of the `Stage` ladders and the DERIV-04 harness.

  (Rejected: baking a dedup arg/notion into `eligible()`/`eligible_clause()` — pollutes the pure-Python
  `eligible()` (which takes only a `status_map`) and the equivalence harness.)

### The analyze-corpus over-enqueue guard (D-02 — LOCKED, verify-only)

- **D-02: Phase 82 owns NO backfill migration — Phase 80's `036_backfill_analysis_completed_at.py`
  already repaired the corpus.** `036` (shipped in PR #229, on `main` at `bd551bff`, READ-03/D-13)
  stamps `analysis.analysis_completed_at = analysis.updated_at` for every `state='analyzed'` file still
  holding NULL (NAND-guarded against the `033` CHECK, idempotent, no-op downgrade), precisely so
  `done_clause(ANALYZE)` (which tests `analysis_completed_at IS NOT NULL`, `stage_status.py:123`) does
  not judge those ~1001 rows NOT-done and re-enqueue them for 4-hour re-analysis.

  **Phase 82's job is to *verify*, not re-repair.** VERIFICATION must assert the deploy target is at
  Alembic `≥036` **and** `COUNT(files WHERE state='analyzed' AND analysis_completed_at IS NULL AND
  failed_at IS NULL) = 0` (a live/shadow gate, mirroring 84 D-16's live-corpus run) **before the analyze
  pending-set flip is trusted**. The in-memory unit tests cannot see the pre-`036` production rows; only
  the live check proves the guard covered them.

  ⚠ **Landmine corrected:** the auto-memory note "*`analyzed` invariant will be RED on first deploy —
  nothing in 032-035 backfills it*" is now **stale** — Phase 80's `036` (outside the 032-035 range it
  cited) *does* backfill it. (Re-running `036` as a fresh Phase-82 revision was rejected: redundant, and
  muddies which phase owns the repair.)

### Stats & DAG four-bucket (D-04, D-05 — LOCKED)

- **D-04: Extend `get_stage_progress` (not a new function).** It already derives every DAG node from the
  output tables (`{done, total}`, no `FileRecord.state` read, each read wrapped in `_safe_count`). Extend
  the **three enrich nodes** to return `{not_started, in_flight, done, failed, total}` — the four-bucket
  counts summing to `total`, with a visible `failed` per enrich stage (READ-02). Compose from the LOCKED
  `stage_status_case(stage)` (the 4-way `in_flight ≻ done ≻ failed ≻ not_started` CASE) — e.g. a single
  `GROUP BY stage_status_case(stage)` per enrich stage — reusing the existing degrade discipline.
  Downstream nodes keep `{done, total}`.

  (Rejected: a separate `get_enrich_stage_buckets()` — a second derived-counting path to keep consistent
  with the first, and two poll queries where one GROUP BY suffices.)

- **D-05: Remove `get_pipeline_stats`'s linear `GROUP BY FileRecord.state` in full; derive all counts
  from output tables now.** SC#2 taken literally: no `state`-keyed GROUP BY survives in the stats path.
  The enrich four-bucket comes from D-04; the Proposals/Approved/Executed tail counts come from
  output-table row existence — which `get_stage_progress` **already** does (proposals table,
  `execution_log`) without reading `proposals.status`. This **front-runs nothing in Phase 86**: 86
  changes proposals-status *authority* (`_TERMINAL_FILE_STATES`), not count *derivation*.
  `notYetEnriched` (`routers/pipeline.py:241`, today `stats["discovered"] − stats["metadata_extracted"]`)
  is re-expressed as `metadata.total − metadata.done`.

  (Rejected: enrich-scoped removal that leaves the tail reading `FileRecord.state` until Phase 86 —
  contradicts SC#2's literal wording and splits the stats cutover across two phases.)

### PERF-02 measurement (D-06, D-07 — LOCKED)

- **D-06: Measure on a LOCAL synthetic-seed ~200K corpus at migration HEAD (`≥036`, so the 032 partial
  indexes exist), via EXPLAIN ANALYZE + full-endpoint timing** — NOT a live lux probe. The three
  pending-set queries + the four-bucket `stage_status_case` GROUP BY are the hot paths; seed realistic
  per-stage output-table coverage, then EXPLAIN ANALYZE each and time the whole `/pipeline/stats`
  endpoint (which fans out many `_safe_count` reads).

  ⚠ **Landmine:** prod/lux is at Alembic `~031` (auto-memory: "*Prod is at Alembic 031 — 032-035
  unreleased*"), so it **lacks 032's partial indexes** (`ix_fprint_success`, `ix_analysis_completed`,
  `ix_metadata_failed`, …) the derived `NOT EXISTS` anti-joins and `stage_status_case` ride — a live
  probe would exercise a pessimistic/invalid plan. A live read-only `COUNT` may be used only as a
  *supplementary* sanity check on the synthetic corpus's stage-coverage distribution.

- **D-07: PASS budget = the full `/pipeline/stats` endpoint completes `< ~1s` at 200K** (ample headroom
  under the 5s poll and concurrent dashboard load). Record the measured number in VERIFICATION
  **regardless** of pass/fail. **DENORM-01** (the denormalized stage-bitmap column) stays deferred —
  YAGNI, derive-first — and is pulled forward **only if** the measurement exceeds budget.

### Claude's Discretion
- **Four-bucket return shape.** Nested `{node: {bucket: int}}` vs a flatter form — planning's call,
  constrained to keep the four-bucket-sums-to-`total` invariant per enrich stage and to keep every read
  `_safe_count`-degrade-safe (never 500 the 5s poll).
- **`stats_bar.html` template churn.** Removing `get_pipeline_stats`'s `FileState`-keyed dict (consumed
  at `routers/pipeline.py:485,629` → `templates/pipeline/partials/stats_bar.html`) forces a template
  key remap; the exact new key set is planning's discretion.
- **The 200K synthetic-seed harness** — shape, coverage distribution, and whether it reuses an existing
  seed/bench fixture — left to research + planning.
- **Manual-trigger endpoint alignment.** Whether the manual metadata/fingerprint trigger endpoints
  (`trigger_metadata_extraction` / `trigger_extraction_ui` / `trigger_fingerprint(_ui)`) should route
  through the same narrowed pending helpers (the Phase-42 "UI and API and recovery must not drift"
  precedent) — planning to confirm, keeping manual/API/recovery aligned.
- **Plan/PR decomposition.** Natural seams: (a) `eligible_clause` + DERIV-04 harness extension; (b) the
  three pending-set cutovers + the divergence/anti-drift guard; (c) the `get_stage_progress` four-bucket
  + `get_pipeline_stats` removal + template remap; (d) the PERF-02 benchmark + VERIFICATION. Small
  blast-radius per PR is the milestone's standing rule.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone design contract & requirements
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` — §4/§6 (what replaces `FileState`; the
  derive-don't-store thesis; the two-step migration + shadow gate); §7 names the three enrich pending
  sets + `get_pipeline_stats` as this phase's cutover surface.
- `.planning/REQUIREMENTS.md` — READ-01, READ-02, PERF-02 (full text); DERIV-01..05, ELIG-01..04,
  INFLIGHT-01..03 (the derivation contract this phase reads); DENORM-01 (the deferred denorm column).
- `.planning/ROADMAP.md` §"Phase 82: Counts & Pending-Set Cutover" — goal + 3 success criteria.

### Upstream phase contracts (locked decisions — do not re-litigate)
- `.planning/phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` — the
  single-source predicate module (`enums/stage.py` + `services/stage_status.py`); the DERIV-04 SQL⇔Python
  equivalence harness `eligible_clause` must extend; ELIG-01..04 + `ELIGIBLE_AFTER_FAILURE` semantics;
  D-01 (ledger-authoritative in_flight); D-02 (SAVEPOINT degrade).
- `.planning/phases/80-recovery-re-enqueue-cutover/80-CONTEXT.md` — READ-03/D-13, the origin of
  migration `036` (the analyze-corpus backfill this phase relies on, D-02).
- `.planning/phases/81-per-stage-failure-persistence-retry-paths/81-CONTEXT.md` — the per-stage failure
  markers (`metadata.failed_at`, analyze failure) the `failed`/`not_started` buckets and terminal-analyze
  exclusion read; D-05 (dual-write).
- `.planning/phases/83-cloud-routing-sidecar-cutover/83-CONTEXT.md` — `in_flight(ANALYZE)` now spans the
  `cloud_job` sidecar; `awaiting_candidate_clause` composition (D-00c here).
- `.planning/phases/84-dedup-fingerprint-progress-cutover/84-CONTEXT.md` — D-13 (`dedup_resolved_clause`
  home & shape) + the **Deferred Ideas** entry that explicitly hands the dedup exclusion to this phase
  (READ-01); the mutation-tested divergence-guard discipline (D-14).

### Source of truth in code
- `src/phaze/enums/stage.py` — `Stage`, `Status`, `ELIGIBILITY_DAG`, `FAILURE_IS_TERMINAL`,
  `ELIGIBLE_AFTER_FAILURE`, `eligible()`, `domain_completed()` (the Python side `eligible_clause` mirrors).
- `src/phaze/services/stage_status.py` — `done_clause`/`failed_clause`/`inflight_clause`/
  `domain_completed_clause`/`stage_status_case`/`dedup_resolved_clause`/`awaiting_candidate_clause`
  (`:91,115,146,176,196,231,265`); **`eligible_clause(stage)` lands here** (D-01).
- `src/phaze/services/pipeline.py:61` (`get_pipeline_stats`, D-05), `:302` (`get_stage_progress`, D-04),
  `:45` (`MUSIC_VIDEO_TYPES`), `:1098` (analyze pending / `get_discovered_files_with_duration`), `:1370`
  (`get_metadata_pending_files`), `:1403` (`get_fingerprint_pending_files`).
- `src/phaze/routers/pipeline.py:240-241,485,629` — the three `get_pipeline_stats` callers +
  `notYetEnriched` (D-05).
- `src/phaze/templates/pipeline/partials/stats_bar.html` — the stats-bar HTMX partial whose keys change.
- `src/phaze/models/analysis.py:38,43,54,56` — `analysis_completed_at`, `failed_at`, `ix_analysis_completed`,
  the `033` NAND CHECK (`036`'s guard rationale, D-02).
- `alembic/versions/036_backfill_analysis_completed_at.py` — the shipped analyze-corpus repair (D-02).
- `alembic/versions/032_add_derived_status_schema.py` — the partial indexes the PERF-02 plan rides (D-06).
- `src/phaze/services/shadow_compare.py` — the Phase-79 gate that must stay green (D-00e).
- Tests: the Phase-78 DERIV-04 equivalence test (`eligible_clause` extends it); Phase 84's mutation-tested
  divergence-guard pattern (the anti-drift model for this phase's pending-set guard).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Every clause builder already exists** in `services/stage_status.py`: `stage_status_case(stage)` (the
  4-way CASE → D-04's four-bucket GROUP BY), `done_clause`/`failed_clause`/`inflight_clause`,
  `domain_completed_clause` (analyze done-or-terminal-failed), and `dedup_resolved_clause` (Phase 84,
  D-03). Phase 82 composes these; it does not author new predicates except `eligible_clause(stage)`.
- **`eligible()`** (`enums/stage.py:215`) — the pure-Python truth `eligible_clause` mirrors; the
  `ELIGIBLE_AFTER_FAILURE` table already encodes the per-stage terminal/auto-retry axis.
- **`get_stage_progress`** (`pipeline.py:302`) — already derives all DAG nodes from output tables with
  `_safe_count` degrade; D-04 extends the three enrich nodes rather than building a new counter.
- **Phase-77/78 partial indexes** (`ix_fprint_success`, `ix_analysis_completed`, `ix_metadata_failed`,
  `ix_cloud_job_awaiting`) — back the `NOT EXISTS` anti-joins; D-06's benchmark seeds at `≥036` so these
  exist.
- **Migration `036`** — the shipped analyze-corpus repair; Phase 82 verifies, does not duplicate (D-02).

### Established Patterns
- **DERIV-04 equivalence harness** (Phase 78) — the drift-lock `eligible_clause` extends: parametrized
  SQL-derived == Python-derived across the fixture matrix.
- **Mutation-tested divergence guard** (Phase 84 D-14, standing rule) — a green guard proves nothing;
  the pending-set guard must construct a corpus where marker/`state` disagree and assert the derived
  reader keys on the output tables, then break-source-watch-RED-restore. Cover all three pending sets.
- **`_safe_count` / `begin_nested()` SAVEPOINT degrade** — every stats read must degrade to 0/None and
  roll back rather than 500 the hot 5s poll (`pipeline.py` precedent).
- **Caller-owned transactions in `services/`** — build/flush, never commit; readers here are pure SELECT.

### Integration Points
- The three pending helpers are consumed by the recovery/re-enqueue path (Phase 80) and the manual
  trigger endpoints — narrowing them changes what those enqueue; confirm manual/API/recovery stay
  aligned (Phase-42 precedent).
- `get_pipeline_stats` has three callers (`routers/pipeline.py:240,485,629`) feeding the DAG-seed
  `notYetEnriched` and the `stats_bar.html` HTMX partial — removing it forces those + the template to
  migrate to the derived source.
- `stage_status_case` feeds both the four-bucket counts (this phase) and any per-file stage matrix
  (Phase 87) — keep it the single CASE definition.

</code_context>

<specifics>
## Specific Ideas

- **Pure reader cutover — the "is there a missing writer?" trap is answered.** 83 and 84 each *looked*
  like reader cutovers and each hid a missing go-forward writer + a corpus-repair migration. Phase 82
  was checked for the same shape: there is none. The one corpus repair it needs (`036`) already shipped
  in Phase 80. Do not invent a writer or a migration.
- **Number changes are the fix, not a regression** — as files light up in all three enrich tabs
  simultaneously (deadlock gone), the per-stage pending counts will jump relative to the old
  serially-gated numbers. Say so in the SUMMARY so it is not read as breakage.
- **The measurement is a deliverable, not a checkbox** (PERF-02): the recorded EXPLAIN ANALYZE numbers
  are what license the YAGNI decision to *not* build DENORM-01. Record them even on a comfortable pass.

</specifics>

<deferred>
## Deferred Ideas

- **DENORM-01 — denormalized stored stage-bitmap column.** Built *only if* PERF-02's measurement proves
  the derived query too slow (D-07). → v2 / same milestone if measurement fails.
- **Per-file stage matrix, per-stage failure retry UI, eligibility trace, priority stepper** — the
  operator-console surface over this phase's derived `stage_status` → **Phase 87** (UI-01..05, PRIO-01).
- **`get_pushing_count` / `get_pushed_count` unowned gap** — carried forward from 83/84 deferred. Not
  this phase.
- **`find_duplicate_groups` nondeterministic pagination** (`dedup.py:81` LIMIT/OFFSET with no ORDER BY) —
  pre-existing, untouched. → its own quick task (carried from 84).

### Reviewed Todos (not folded)
- **`analysis-completed-at-backfill.md`** ("1001 production `analyzed` rows will fail the shadow gate") —
  **reviewed, not folded: effectively resolved upstream by Phase 80's migration `036`** (D-02). Phase 82
  does not own a backfill; it only *verifies* `036` has been applied to the deploy target before trusting
  the analyze pending-set flip. The todo's premise ("nothing in 032-035 backfills it") is stale — `036`
  is outside that range and does the backfill.

</deferred>

---

*Phase: 82-Counts & Pending-Set Cutover*
*Context gathered: 2026-07-10*
