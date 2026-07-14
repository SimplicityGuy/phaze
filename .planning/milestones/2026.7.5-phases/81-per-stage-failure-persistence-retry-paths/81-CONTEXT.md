# Phase 81: Per-Stage Failure Persistence & Retry Paths - Context

**Gathered:** 2026-07-08
**Status:** Ready for planning

<domain>
## Phase Boundary

Make all three enrich stages persist a **durable failure marker** and gain a **retry path** —
closing the latent bug where a failed metadata extraction records *nothing* and becomes
invisible-and-permanently-ineligible.

**In scope:**
- `analyze` failure writes `analysis.failed_at` + `error_message` (FAIL-01, go-forward writer).
- `metadata` failure writes a `metadata` row with `failed_at` set and payload columns NULL
  (FAIL-02) — `report_metadata_failed` currently persists nothing.
- An operator retry path for terminally-failed metadata (FAIL-03).
- `fingerprint` failure keeps persisting via `fingerprint_results.status='failed'` and stays
  auto-retryable (FAIL-04) — **reused, not re-invented**.
- The `FAILURE_IS_TERMINAL` / `ELIGIBLE_AFTER_FAILURE` tables + `domain_completed()` and its SQL
  twin, which Phase 80 consumes.
- Migration `033` (CHECK constraint + mixed-row cleanup). **This phase is NOT writer-only.**

**Out of scope:** reader cutover (Phases 80/82/83/84), the destructive `FileState` drop (Phase 90),
per-engine fingerprint `done` semantics (Phase 82 — see Deferred Ideas).

**Requirements:** FAIL-01, FAIL-02, FAIL-03, FAIL-04.

</domain>

<decisions>
## Implementation Decisions

### Upstream contract (carried forward — do not re-litigate)

- **D-01:** The metadata failure marker's shape is already constrained by upstream phases:
  a metadata failure **inserts a `metadata` row with `failed_at` set and payload columns NULL**,
  and `done(metadata)` = `EXISTS metadata WHERE file_id = … AND failed_at IS NULL`. That row must
  read as `failed`, **never** `done`. Phase 78 already encodes this at
  `src/phaze/services/stage_status.py:101` (`done_clause`) and `:130` (`failed_clause`); Phase 81
  is the **writer** that has to honor it. (Sources: 77-CONTEXT D-02, 78-CONTEXT D-03,
  80-CONTEXT D-01.)

- **D-02:** **Phase 81 is upstream of Phase 80** — this is why it was reordered ahead of it.
  Recovery derives `failed(analyze)` and `failed(metadata)` from these markers, but today the
  analyze path writes `state = ANALYSIS_FAILED` and no `failed_at`
  (`src/phaze/routers/agent_analysis.py:329`), and the metadata path writes nothing at all
  (`src/phaze/routers/agent_metadata.py:99`). **FAIL-01 and FAIL-02 are what unblock Phase 80.**
  FAIL-03 (metadata retry) and FAIL-04 (fingerprint reuse) are **not** on that critical path.
  (Source: 80-CONTEXT D-02.)

- **D-03:** **FAIL-01's backfill is already shipped.** Migration `032`
  (`alembic/versions/032_add_derived_status_schema.py:74-80`) UPSERTs `analysis.failed_at` for
  every `state='analysis_failed'` file. Phase 81 writes the **go-forward writer only** — it must
  not re-backfill. Metadata deliberately has no backfill (77-CONTEXT D-03: no historical source).

- **D-04:** The Phase 79 shadow-compare gate **must stay green** after this phase's writer
  changes. Every decision below is chosen so that no file's *derived* status changes as a result
  of Phase 81.

### Analyze failure marker (FAIL-01)

- **D-05:** `report_analysis_failed` **dual-writes**: it stamps `analysis.failed_at` +
  `error_message` **and** keeps writing `state = FileState.ANALYSIS_FAILED`, in the same
  transaction. FAIL-01's "replacing the `ANALYSIS_FAILED` enum value" means *reliance* is
  replaced, not the write. Three live readers still consume `files.state` until Phases 80/82 cut
  over: `tasks/reenqueue.py::_select_done_analyze_ids`, `get_analysis_failed_files` (which feeds
  `retry_analysis_failed`), and `get_pipeline_stats`. The `state` write is removed by Phase 90.
  (Rejected: stop writing `state` now — it empties the red bucket, breaks the existing analyze
  retry, and re-opens the 44.5K over-enqueue class before Phase 80 lands.)

- **D-06:** `analysis_completed_at` and `analysis.failed_at` are **mutually exclusive**, enforced
  by a DB `CHECK (NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL))`.
  `put_analysis` on success clears `failed_at`; `report_analysis_failed` clears
  `analysis_completed_at`. A failed `deepen_analysis` on an already-analyzed file therefore loses
  its `done` marker and becomes re-eligible, keeping its payload columns.
  (Rejected: "guarded failure stamp"; rejected: allow mixed rows and tighten
  `failed_clause(analyze)` — that edits Phase 78's shipped module that Phase 79 baselines against.)

- **D-07:** `analysis.error_message` = the composed string `f"{reason}: {error}"`, truncated to the
  column bound. `AnalysisFailurePayload` already carries a `Literal` `reason` (3 classifications)
  and a bounded free-text `error` (`src/phaze/schemas/agent_analysis.py:114`). No schema change,
  and it matches `032`'s backfill placeholder style (`'backfilled from ANALYSIS_FAILED'`).
  (Rejected: `error` only — loses the triage classification. Rejected: a separate
  `failure_reason` column.)

- **D-08:** **Phase 81 ships migration `033`** (the CHECK from D-06 + the D-09 cleanup), and
  **Phase 90's destructive migration renumbers `033` → `034`**. Alembic numbering is sequential and
  81's migration is genuinely next. Doc churn is accepted and **in scope**: `.planning/ROADMAP.md`
  (lines 21, 25, 36, 281, 485), `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md`, and
  `.planning/REQUIREMENTS.md` (MIG-02 / MIG-04) all refer to the destructive migration as "033".
  Per 77-CONTEXT D-01 precedent, the CHECK must be mirrored into the ORM `__table_args__` so
  `alembic revision --autogenerate` still produces an empty diff.

- **D-09:** **Mixed rows already exist in the live corpus.** `_BACKFILL_ANALYZE_FAILED` in `032`
  does `ON CONFLICT (file_id) DO UPDATE SET failed_at = COALESCE(...)` with **no guard on
  `analysis_completed_at`**, so any file that analyzed successfully and later failed a
  `deepen_analysis` now has both columns set. Migration `033`'s cleanup **clears `failed_at` and
  keeps `analysis_completed_at`** — `done ≻ failed`, matching `_analyze_status`'s existing
  precedence (`src/phaze/enums/stage.py:75-84`), so no file changes derived status and D-04 holds.
  The cleanup must run **before** the CHECK is added or the migration fails.
  (Rejected: clear `completed_at` — flips files DONE→FAILED and trips the shadow gate. Rejected:
  abort on any mixed row.)

### Metadata failure marker & retry (FAIL-02 / FAIL-03)

- **D-10:** `report_metadata_failed` gains an **optional** body
  (`body: MetadataFailurePayload | None = None`, mirroring `AnalysisFailurePayload`'s
  `reason`/`error` shape). New agents send triage detail; **old agent images POST bodyless and
  still receive 200 and still clear the ledger.** Control-plane and agent ship as separate images,
  and a required body would 422 an old agent — which means the terminal-ack never clears
  `extract_file_metadata:<file_id>`, reviving the exact CR-02 unbounded-recovery loop this
  endpoint exists to prevent. When the body is absent, `error_message` falls back to a fixed
  placeholder. (Rejected: required body — version-skew hazard. Rejected: no body — no triage data.)

- **D-11:** The FAIL-03 retry **leaves the failure row in place and simply re-enqueues.**
  `eligible(metadata)` already admits `FAILED` (`src/phaze/enums/stage.py:190` — eligible iff status
  not in `(DONE, IN_FLIGHT)`), so the file is runnable as-is; the ledger row makes it `IN_FLIGHT`,
  and `put_metadata`'s clear-on-success (D-13) wipes `failed_at`. Failure history survives until
  the retry actually succeeds.
  **Explicitly rejected — clearing `failed_at` in place is UNSAFE:** the failure row has payload
  columns NULL, so `done(metadata)` = "row present AND `failed_at IS NULL`" would evaluate TRUE and
  a file with zero metadata would read `DONE` and never be extracted again. (Also rejected:
  deleting the row — destroys the record before the retry has succeeded.)

- **D-12:** FAIL-03 ships as a **bulk operator endpoint**, `POST /pipeline/metadata-failed/retry`,
  returning an HTMX fragment and mirroring `retry_analysis_failed`'s guard ordering
  (`src/phaze/routers/pipeline.py:884-951`): resolve the per-agent queue **once** → catch
  `enqueue_router.NoActiveAgentError` and return **without** enqueuing or mutating state (never
  fall through to the consumer-less default queue, Phase 30) → commit before enqueue → rely on the
  deterministic `extract_file_metadata:<file_id>` key to dedup in-flight files.
  **Simpler than its donor:** metadata has no terminal `FileState`, so there is no bucket to flip
  out of before enqueuing. Needs a new `get_metadata_failed_files` query derived from
  `metadata.failed_at IS NOT NULL`. (Rejected: per-file endpoint — the operator has no
  failed-metadata list to drive it from until Phase 82 adds failed counts to the DAG.)

### Clear-on-success (must-haves, not optional)

- **D-13:** **Both `put_analysis` and `put_metadata` must explicitly clear `failed_at` and
  `error_message` on success.** Both build their upsert `set_` clause from
  `body.model_dump(exclude_unset=True)` (`src/phaze/routers/agent_metadata.py:65-80`,
  `src/phaze/routers/agent_analysis.py:198-210`). `failed_at` is never in the agent's body, so a
  **successful retry after a failure would leave `failed_at` set** — the file then reads `failed`
  forever despite extraction/analysis succeeding, and for analyze it violates D-06's CHECK.
  The clear must be **unconditional**, not driven by `exclude_unset`.
  **`put_metadata`'s empty-body branch is the sharper hazard:** it currently takes
  `on_conflict_do_nothing`, which would never clear the marker at all. That branch must still clear
  `failed_at` on an existing row.

### Terminality encoding (`FAILURE_IS_TERMINAL`)

- **D-14:** `FAILURE_IS_TERMINAL` is referenced by 80-CONTEXT D-01, by ROADMAP Phase-80 success
  criterion 3, and throughout `.planning/research/` — but **it exists in no `.py` file today.**
  Phase 78 encoded analyze's terminality *inline* in `eligible()`'s dispatch. **Phase 81 creates
  it** in the DB-free `src/phaze/enums/stage.py`.

- **D-15:** Encode **two explicit tables**, because `eligible()` and `domain_completed()` are
  different axes and conflating them is a live trap:
  ```
  FAILURE_IS_TERMINAL   = {analyze: True,  metadata: True,  fingerprint: False}  # recovery
  ELIGIBLE_AFTER_FAILURE = {analyze: False, metadata: True,  fingerprint: True}   # eligibility
  ```
  `metadata` is **terminal for recovery** (recovery must not auto-re-drive it) and **eligible for a
  manual trigger** (which is what makes D-11 work). `eligible()`'s inlined ANALYZE carve-out
  disappears into `ELIGIBLE_AFTER_FAILURE`, so the analyze asymmetry stops being a coincidence that
  two readers must independently remember.
  **Explicitly rejected:** making `eligible()` consume `FAILURE_IS_TERMINAL` uniformly — that turns
  `metadata` FAILED ineligible, breaks Phase 78's ELIG-01/ELIG-04 tests, and silently disables the
  FAIL-03 retry.

- **D-16:** The `eligible()` refactor is **semantics-preserving**. Phase 78's ELIG-01..04 tests must
  pass **unchanged**; the refactor only moves the per-stage constants out of the dispatch body.

- **D-17:** Phase 81 ships **the tables + the pure `domain_completed(status_map, stage)` in
  `enums/stage.py` + the `domain_completed_clause()` SQL twin in `services/stage_status.py`**,
  drift-locked now by extending Phase 78's parametrized equivalence test (78-CONTEXT D-04).
  ```
  domain_completed(stage) = done(stage) OR (failed(stage) AND FAILURE_IS_TERMINAL[stage])
  ```
  Phase 80 then only wires recovery, which is what its READ-03 scope actually is. (Rejected:
  shipping the table alone, or the pure helper without the SQL twin — that lands the Python and SQL
  definitions one phase apart, exactly the drift window 78 D-04 closed.)

### Fingerprint (FAIL-04)

- **D-18:** **No new writer.** `fingerprint_results.status='failed'` rows are already persisted by
  `put_fingerprint` from the agent's `IngestResult` (`src/phaze/services/fingerprint.py:103,105`),
  and `report_fingerprint_failed` (`src/phaze/routers/agent_fingerprint.py:60`) deliberately
  persists **nothing** — a synthetic `fingerprint_results(engine='_task', status='failed')` row
  would poison the two aliased per-engine joins at `src/phaze/routers/pipeline.py:939-940` and
  `_trackid_engine_badge` (`:864`). Phase 81's FAIL-04 deliverable is **regression tests +
  docstrings**: assert `report_fingerprint_failed` persists no row and only clears the ledger;
  assert a per-engine `status='failed'` row persists and keeps the file eligible; document the
  asymmetry.

### Claude's Discretion

None — every gray area presented was decided by the operator. Areas not raised (the ordering of
`report_analysis_failed`'s new upsert against its existing `clear_ledger_entry` and
`_delete_staged_object_if_cloud` side effects; whether `get_metadata_failed_files` lives in
`services/` or the router; whether the HTMX fragment reuses `retry_failed_response.html`; whether
the Phase 90 `033→034` rename lands in this PR or its own) are left to research and planning.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone contract
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` §3 — the `eligible` predicate, the
  `FAILURE_IS_TERMINAL` table, and the ⚠ **load-bearing** `FAILURE_IS_TERMINAL[analyze] = true`
  warning (the 44.5K over-enqueue guard).
- `.planning/REQUIREMENTS.md` — FAIL-01..04 (lines 39-42); MIG-02 / MIG-04 reference the
  destructive migration by the number D-08 renumbers.
- `.planning/ROADMAP.md` — Phase 81 entry (line 361); Phase 90 entry (line 485). Lines 21, 25, 36,
  281, 485 name the destructive migration "033" and are edited by D-08.

### Research (read before planning FAIL-04)
- `.planning/research/ARCHITECTURE.md` — "Precision 3" (why `report_fingerprint_failed` should keep
  persisting nothing); the `domain_completed` formula (line 493); D-02 on failure-marker columns
  (line 807).
- `.planning/research/PITFALLS.md` — the `MARK` cluster (line 16); `FAILURE_IS_TERMINAL[fingerprint]
  = false` + the unbounded-retry warning (line 90); the precedence-vs-eligibility contradiction
  (line 188).
- `.planning/research/SUMMARY.md` line 43 — gap G-01: `FAILURE_IS_TERMINAL[metadata] = true` with no
  retry endpoint named anywhere in the design. **FAIL-03 exists to close this.**

### Upstream phase context (locked decisions this phase inherits)
- `.planning/phases/77-additive-schema-rescan-wipe-fix-migration-032/77-CONTEXT.md` — D-01 (marker
  columns, not a generic table), D-02 (the `done(metadata)` handoff), D-03 (backfill asymmetry).
- `.planning/phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` — D-03
  (`done(metadata)`), D-04 (two-module split + equivalence test = the drift lock).
- `.planning/phases/79-shadow-compare-gate-live-corpus/79-CONTEXT.md` — D-01/D-04/D-06 (the standing
  gate that must stay green; the soft allowlist).
- **Note:** `80-CONTEXT.md` lives only on the `SimplicityGuy/phase-80` branch and is NOT in this
  worktree. Its D-01 and D-02 are transcribed above as this phase's D-01 and D-02.

### Code the phase touches
- `src/phaze/enums/stage.py` — `Stage`/`Status`, `ELIGIBILITY_DAG`, `resolve_status()`,
  `eligible()`. DB-free, agent-safe. The D-15/D-17 home.
- `src/phaze/services/stage_status.py` — `done_clause()` / `failed_clause()`. The SQL twin.
- `src/phaze/routers/agent_analysis.py:198-210, 329` — `put_analysis` upsert; the
  `ANALYSIS_FAILED` writer.
- `src/phaze/routers/agent_metadata.py:65-80, 99` — `put_metadata` upsert; `report_metadata_failed`.
- `src/phaze/routers/agent_fingerprint.py:22, 60` — `put_fingerprint`; `report_fingerprint_failed`.
- `src/phaze/routers/pipeline.py:884-951` — `retry_analysis_failed`, the FAIL-03 donor pattern.
- `alembic/versions/032_add_derived_status_schema.py:74-80` — `_BACKFILL_ANALYZE_FAILED`, the
  source of the D-09 mixed rows.
- `src/phaze/services/shadow_compare.py:30-32, 150` — the `FINGERPRINTED` soft allowlist.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`retry_analysis_failed` (`routers/pipeline.py:884`)** — a complete, operator-gated bulk retry:
  resolve-queue-once → `NoActiveAgentError` guard → commit-before-enqueue → deterministic-key
  dedup. FAIL-03's endpoint (D-12) is a direct mirror, minus the state flip.
- **`AnalysisFailurePayload` (`schemas/agent_analysis.py:114`)** — `Literal` reason + bounded
  `error` + `extra='forbid'`. The shape `MetadataFailurePayload` copies (D-10).
- **`pg_insert(...).on_conflict_do_update` idiom** — used identically in `put_analysis`,
  `put_metadata`, `put_fingerprint`. D-13's clear-on-success edits two of the three.
- **Phase 78's parametrized equivalence test** — the drift lock D-17 extends rather than reinvents.

### Established Patterns
- **`enums/stage.py` is DB-free** (78 D-04): no SQLAlchemy model imports, so a Postgres-free agent
  can derive status. D-15's tables must not break this.
- **Ledger-clear in the same transaction as the write** (Phase 45 L-02) — every terminal-ack path
  (`put_*`, `report_*_failed`) clears `<task>:<file_id>` exactly once. New writers must preserve it.
- **Auth: `agent` from the auth dep, never the body; keys reconstructed from the PATH `file_id`
  only** (AUTH-01 / T-45-05). D-10's new optional body must keep `extra='forbid'`.
- **`done ≻ failed` precedence** in `_analyze_status` / `stage_status_case`.

### Integration Points
- `report_analysis_failed` already performs `clear_ledger_entry` +
  `_delete_staged_object_if_cloud` + `session.commit()`. The new `analysis` upsert joins that
  transaction.
- `tasks/metadata_extraction.py:74-80` — the terminal-ack call site. It holds the original
  exception in scope (it logs `exc_info=True` then bare-`raise`s), so D-10's `error` detail is
  available without new plumbing.
- `services/agent_client.py:401` — `report_metadata_failed(file_id)` signature widens to accept the
  optional payload.

</code_context>

<specifics>
## Specific Ideas

- FAIL-04's phrase **"reused, not re-invented"** is load-bearing and was honored literally: Phase 81
  writes no fingerprint writer at all (D-18).
- FAIL-01's **"replacing the `ANALYSIS_FAILED` enum value"** was read as *reliance* replaced, not
  the write removed (D-05). The write dies in Phase 90.
- The operator explicitly wanted `FAILURE_IS_TERMINAL` to be created here rather than inherited as
  an unowned dependency by Phase 80.
- ⚠ `.planning/sketches/MANIFEST.md` exists with no packaged findings skill. If those sketches bear
  on this phase, run `/gsd:sketch --wrap-up` before planning.

</specifics>

<deferred>
## Deferred Ideas

Both belong to **Phase 82 (READ-01 / READ-02)**, which owns the pending sets that make each
reachable. Neither is reachable today, and neither is Phase 81's to fix.

- **The mixed-engine fingerprint retry hole.** `done_clause(fingerprint)` is *"any engine success
  wins"* (DERIV-05, `services/stage_status.py:103`), and `failed_clause(fingerprint)` requires **no**
  engine to have succeeded (`:134-135`). So a file where chromaprint succeeded and panako failed
  reads `DONE` and is never eligible again — panako never retries. **Today the behavior is the
  opposite:** `put_fingerprint` never advances `state` (the sole `FINGERPRINTED` writer is
  `retry_analysis_failed`, `pipeline.py:937`; `shadow_compare.py:30-32` already marks
  `FINGERPRINTED` `soft=True` noting it "need not imply fingerprint success"), so the file stays
  `METADATA_EXTRACTED` and `get_fingerprint_pending_files` (`services/pipeline.py:1344`) re-drives
  it forever. Behavior **inverts** when Phase 82 cuts the pending set over to derivation. This is
  `.planning/research/PITFALLS.md:188`. Fixing it means overturning Phase 78's locked DERIV-05 and
  re-baselining Phase 79's shadow gate — out of scope here.

- **`MAX_FINGERPRINT_ATTEMPTS` bound.** `FAILURE_IS_TERMINAL[fingerprint] = false` means a failed
  fingerprint auto-retries forever. `.planning/research/PITFALLS.md:90` warns a poison file will
  re-enqueue on **every trigger click, forever**, because "derivation removes the accidental gate"
  the linear enum provided. Bounding it needs an `attempts` column and no requirement asks for it.

- **UI surface for failed metadata.** FAIL-03's success criterion names a *backend endpoint* only.
  The failed-count chip and retry button on the DAG belong to Phase 82's READ-02 (four-bucket
  per-stage counts including a visible failed count).

</deferred>

---

*Phase: 81-Per-Stage Failure Persistence & Retry Paths*
*Context gathered: 2026-07-08*
