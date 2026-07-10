# Phase 84: Dedup & Fingerprint-Progress Cutover - Context

**Gathered:** 2026-07-09
**Status:** Ready for planning

<domain>
## Phase Boundary

Cut `services/dedup.py`'s nine `state != DUPLICATE_RESOLVED` exclusion filters and
`services/fingerprint.py`'s `get_fingerprint_progress` off `FileRecord.state` and onto the
`dedup_resolution` marker and the fingerprint output tables — **and add the go-forward
`dedup_resolution` writer that does not exist today** (see D-01, the discovery that reshapes this
phase), repairing the corpus in both directions before the readers flip.

**Requirements:** READ-04, SIDECAR-02.

**In scope:**
- The **go-forward `dedup_resolution` writer** in `resolve_group` (`services/dedup.py:251`) —
  the marker table has had no writer since migration `032`'s one-shot backfill created it.
- **`undo_resolve`** (`services/dedup.py:274`) becomes a marker `DELETE` that CAS-gates the
  `FileRecord.state` restore.
- The **nine dedup read sites** (`dedup.py:78,90,128,141,188,209,221,235,260`) cut over to a
  marker-existence predicate.
- **`get_fingerprint_progress`** (`services/fingerprint.py:256`) — all three keys redefined over
  `done_clause` / `failed_clause` / the dedup predicate. Its `completed` count currently reads
  `state == FINGERPRINTED`, whose sole writer is `retry_analysis_failed` (`routers/pipeline.py:954`),
  so it counts approximately nothing. This phase fixes that.
- **Migration `035`** — a data-only, bidirectional reconcile of `dedup_resolution` against
  `files.state`, landing before the reader flip.
- A **new file-level predicate** in `services/stage_status.py`.
- A **mutation-tested divergence guard** proving the readers key on the marker, not on `state`.

**Out of scope:**
- The three enrich pending sets and `get_pipeline_stats` — **Phase 82** (READ-01, READ-02).
  `services/pipeline.py:1436`'s `state != FINGERPRINTED` read belongs to 82, not here.
- `services/proposal.py:39` `_TERMINAL_FILE_STATES` — **Phase 86** (SIDECAR-03). This is why we do
  **not** stop stamping `FileRecord.state` on resolve (see D-05).
- `services/scan_deletion.py` — left as-is by explicit decision (D-08).
- The destructive `files.state` column drop and the `FileState` enum deletion — **Phase 90**.
- Per-engine fingerprint failure visibility in the operator console — **Phase 87** (UI-02).

**Sequencing:** ROADMAP declares `Depends on: Phase 82`, but branch `SimplicityGuy/phase-82` does not
exist and the file sets are disjoint (82 owns `services/pipeline.py`; 84 owns `services/dedup.py` +
`services/fingerprint.py`). 84 needs 77 (the `dedup_resolution` table + `ix_fprint_success`), 78 (the
predicate module), and 79 (the shadow gate). **Planning must confirm the base branch** — main appears
sufficient; 82 does not appear to be a real prerequisite.

</domain>

<decisions>
## Implementation Decisions

### Upstream contract (carried forward — do not re-litigate)

- **D-00a:** **Writers dual-write.** `FileRecord.state` keeps being stamped; only *reliance* on it is
  replaced. The `state` write dies in Phase 90. READ-04 forbids `FileRecord.state` **reads**, not
  writes. (81-CONTEXT D-05, restated as 83-CONTEXT D-00c.)
- **D-00b:** The Phase-79 shadow gate asserts **implication, not equality**, and
  `state = DUPLICATE_RESOLVED ⇒ dedup marker exists` is **hard** (`services/shadow_compare.py:135`,
  `soft=False`). Nothing asserts the converse.
- **D-00c:** `done(fingerprint)` = EXISTS a `fingerprint_results` row with
  `status IN ('success','completed')` for **any** engine; `failed(fingerprint)` = no engine succeeded
  AND ≥1 failed; fingerprint failure is **non-terminal** (auto-retries, ELIG-04). Already built at
  `services/stage_status.py:89,120`. DERIV-05 settles aggregation: one success wins.
- **D-00d:** The marker carries **no `previous_state` column** — "a transition artifact, unnecessary
  under the derive-don't-store principle" (77 D-07, `models/dedup_resolution.py`). Any state to
  restore on undo must come from elsewhere.
- **D-00e:** `services/fingerprint.py` is imported by the **agent worker**, which must not import
  `phaze.database` / `phaze.models`. Its DB imports are function-local by design
  (`fingerprint.py:263-267`, Phase 26 Plan 10/11). Any predicate it consumes must be imported
  **inside** the function.

### The missing dedup writer (D-01 — a discovery, not a choice)

- **D-01:** ⚠ **There is no go-forward writer of `dedup_resolution`.** `resolve_group`
  (`services/dedup.py:266-268`) stamps `f.state = FileState.DUPLICATE_RESOLVED` and nothing else;
  `services/dedup.py` never imports `DedupResolution`. The only code that ever *inserted* a marker is
  migration `032`'s `_BACKFILL_DEDUP` (`alembic/versions/032_add_derived_status_schema.py:84`); the
  only code that deletes one is `services/scan_deletion.py:108`.

  **Consequence:** every group resolved since `032` landed carries `state = duplicate_resolved` with
  **no marker**, violating the *hard* invariant at `services/shadow_compare.py:135` (`soft=False`).
  Symmetrically, `undo_resolve` restores `state` while leaving any backfilled marker orphaned, and an
  orphaned marker will — once the reader flips to `NOT EXISTS(marker)` — hide its file from the dedup
  UI **permanently and unreachably**.

  **This phase is not a pure reader cutover.** It must add the writer, fix undo, and reconcile the
  corpus in both directions. Exactly the shape of 83's D-01.

### Marker writer & corpus repair (D-02 … D-04 — LOCKED)

- **D-02:** **Bulk insert + `ON CONFLICT (file_id) DO NOTHING`.** One
  `postgresql.insert(DedupResolution)` for all non-canonical files in the group, inside
  `resolve_group`'s caller-owned transaction, **never committing** (the `backends.py` dispatch
  discipline). Idempotent against an HTMX double-submit; matches `032`/`034`'s backfill idiom.
  `resolve_group` already loads the ORM objects for the `previous_state` capture, so the ids are free.

  (Rejected: `session.add(DedupResolution(...))` per file — a concurrent double-submit raises
  `IntegrityError` on `uq_dedup_resolution_file_id` and poisons the transaction rather than no-opping.)

  `resolve_group` is already the single funnel — `bulk_resolve` (`routers/duplicates.py:214`) calls it
  per group — so there is no "three hand-written copies" problem that 83 had to solve.

- **D-03:** **The writer populates `canonical_file_id` with the operator's actual pick.**
  `resolve_group` already receives `canonical_id` from the comparison table. Go-forward rows become
  strictly better than `032`'s backfilled ones, which guess `ORDER BY c.id LIMIT 1` and are documented
  best-effort (RESEARCH Pitfall 4). Unblocks the "duplicate of X" UI the column exists for. Zero cost.

  **Backfilled NULLs are left alone** — the original human keeper is genuinely unrecoverable for
  pre-`032` resolutions, and re-deriving would re-guess, not recover.

- **D-04:** **Migration `035` — a sync, data-only, bidirectional reconcile.** Mirrors 83's
  `alembic/versions/034_backfill_cloud_awaiting.py` precedent exactly: `op.execute` of static,
  parameter-free SQL, **no DDL**, so `alembic revision --autogenerate` stays empty (assert via
  `compare_metadata`, as `tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py`
  does).

  Two statements:
  1. `032`'s `_BACKFILL_DEDUP` re-run **verbatim** (`INSERT … SELECT … ON CONFLICT (file_id) DO NOTHING`)
     — inserts the missing markers.
  2. A `DELETE` of orphaned markers (`marker exists AND files.state <> 'duplicate_resolved'`).

  **Both directions, so `marker ≡ state` exactly at the cutover instant.** Rationale: pre-cutover,
  `state` is still the authority, so reconcile the derived representation *to* it. The failure mode is
  safe — a wrongly-deleted marker merely makes its file reappear in the dedup UI for re-review, while
  a wrongly-kept one hides it forever with no operator path to fix it.

  **Ordering is load-bearing: `035` must land before the dedup reader flips** to
  `NOT EXISTS(marker)`, or resolved files reappear and orphan-hidden files vanish.

  Note the ROADMAP calls Phase 90's destructive migration `034`; that number is taken. See Deferred.

### Undo semantics under dual-write (D-05 … D-08 — LOCKED)

- **D-05:** **Undo = `DELETE` the marker + restore `previous_state`.** Undo's *dedup semantics* become
  a plain `DELETE` (SC#1); the `FileRecord.state` restore is dual-write bookkeeping that vanishes in
  Phase 90 along with the JSON payload. The client payload shape `[{id, previous_state}]` is
  **unchanged** — it round-trips through the browser (`routers/duplicates.py:162` → template hidden
  field → `:176`), so no template churn.

  **A bare `DELETE` is not an option today:** `resolve_group` still stamps
  `state = DUPLICATE_RESOLVED` (D-00a), so deleting only the marker leaves `state` without a marker —
  precisely the hard invariant at `shadow_compare.py:135`, turning this phase's own SC#3 gate red.

  (Rejected: *stop stamping `state` on resolve*, which would make SC#1 literally true. It front-runs
  Phase 86 — `services/proposal.py:39` `_TERMINAL_FILE_STATES` would stop excluding newly-resolved
  duplicates, so phaze starts generating proposals for them — and drifts `services/pipeline.py:57`
  `PIPELINE_STAGES` display counts. It breaks readers this phase does not own, and contradicts D-00a.)

  (Rejected: *re-derive* a linear state from `stage_status` on undo. Needs a `linearize` helper that
  exists only to be deleted in Phase 90, and it silently rewrites states the operator never chose.)

- **D-06:** **The CAS anchor is the marker.** One
  `DELETE FROM dedup_resolution WHERE file_id IN (…) RETURNING file_id`; the `FileRecord.state`
  restore is applied **only** to the ids actually returned.

  The `file_states` payload lives in the browser, so a stale tab can replay an undo against files that
  were since re-resolved or advanced. Under the CAS, a replay finds no marker, returns zero rows, and
  does nothing. This is the direct analogue of 83 D-09 (`cloud_job.status` as the single CAS domain,
  the `FileRecord` dual-write gated behind `rowcount`), and it survives Phase 90 — the restore limb
  simply drops away.

  (Rejected: the current unconditional per-file `update()` loop at `dedup.py:280` plus a `DELETE`. A
  stale replay silently rewrites `state` on files that moved on — the same class of bug as the missing
  `/upload-failed` guard that Phase 83 existed to close.)

- **D-07:** **`ON CONFLICT DO NOTHING` stays `DO NOTHING`** (not `DO UPDATE SET canonical_file_id`).
  Post-cutover the group selection filters on `NOT EXISTS(marker)`, so a marker-bearing file is never
  in the insert set; the conflict can only fire on a genuine concurrent double-submit, where
  first-writer-wins is the correct idempotent outcome.

- **D-08:** **`services/scan_deletion.py` is left as-is — deleting a canonical file un-resolves its
  duplicates.** `scan_deletion.py:108` deletes markers matching **either** FK
  (`file_id IN batch | canonical_file_id IN batch`), so deleting the scan batch holding a *keeper*
  deletes its duplicates' resolution markers. `032`'s backfill already populates `canonical_file_id`,
  so this behavior exists today — but D-03 makes **every** go-forward resolution exposed to it.

  Accepted deliberately: the keeper is gone, so "keep this one, drop those" no longer holds, and the
  duplicates should reappear for re-review. Safe failure mode, zero new code.
  **Requirement:** note this in `models/dedup_resolution.py`'s docstring so it is not rediscovered as
  a bug.

  (Rejected: splitting `scan_deletion.py:108` into a `DELETE` for `file_id` matches and an
  `UPDATE SET canonical_file_id = NULL` for canonical-only matches. It preserves the resolution,
  degrading to a bare marker — which the model docstring already calls the marker's primary job — but
  it touches a file outside this phase's named scope.)

### `get_fingerprint_progress` (D-09 … D-12 — LOCKED)

**Context:** the endpoint `GET /api/v1/fingerprint/progress` (`routers/pipeline.py:1334`) has **no UI
consumer**. It is referenced only by `justfile:500` (a curl recipe), `docs/api.md:35`, and one
mock-based test. Its `completed` key reads `state == FINGERPRINTED`, whose sole writer is
`retry_analysis_failed` (`routers/pipeline.py:954`) — the same class of bug as the `get_stage_progress`
one fixed in PR #189, still live in a second function.

- **D-09:** **Keep the 3-key contract (`total` / `completed` / `failed`); redefine each body over the
  derived predicates.** Zero API break (`docs/api.md:35`, `justfile:500` keep working), zero overlap
  with Phase 82's four-bucket per-stage counts, and READ-04 is satisfied literally. `completed` stops
  being ~0 for the first time.

  (Rejected: reshaping to `not_started/in_flight/done/failed` via `stage_status_case` — it duplicates
  READ-02's deliverable in a second place and drags `scheduling_ledger` in-flight detection, with its
  SAVEPOINT handling, into this phase. Rejected: deleting the endpoint — it retires a documented public
  API surface and reads READ-04's "derive" as "delete", which the requirement does not say.)

- **D-10:** **`total` = `file_type IN MUSIC_VIDEO_TYPES` AND `NOT EXISTS(dedup marker)`.**
  `MUSIC_VIDEO_TYPES` (`services/pipeline.py:46`) is already the set the enrich stages are enqueued
  for. Under ELIG-01 fingerprint is independent of metadata, so the old
  `state IN {METADATA_EXTRACTED, …}` proxy for eligibility is meaningless.

  Excluding dedup-resolved files ties both halves of this phase to one clause and keeps
  `completed/total` reachable — a resolved duplicate is never going to be worked, and a non-audio file
  can never be fingerprinted.

- **D-11:** **`completed` and `failed` both become FILE counts.**
  `completed` = `count(files)` where `done_clause(Stage.FINGERPRINT)` (rides `ix_fprint_success`, the
  partial index `032` created at `:156` for exactly this). `failed` = `count(files)` where
  `failed_clause(Stage.FINGERPRINT)` (`stage_status.py:131`).

  Today `failed` is a **row** count over `fingerprint_results` (`fingerprint.py:292`) while the other
  two are file counts: a file failing both engines is counted twice, and a file with one success and
  one failure is counted as failed even though DERIV-05 says it is `done`. **`failed` will visibly
  drop and `completed` will visibly jump.** That is the fix, not a regression — say so in the SUMMARY.

- **D-12:** **No per-engine breakdown.** SC#2's "per-engine coverage predicate" names what
  `done_clause(FINGERPRINT)` already is — EXISTS a row for this file with
  `status IN ('success','completed')` for *any* engine. Per-engine visibility (`FingerprintResult.engine`
  exists, `models/fingerprint.py:21`, so a `GROUP BY` would be cheap) is operator-console work and
  belongs to Phase 87's UI-02.

### Predicate home & anti-drift guard (D-13 … D-16 — LOCKED)

- **D-13:** **The dedup-resolved predicate lives in `services/stage_status.py`** — the module Phase 78
  established as the single-source predicate module for everything derived from output tables. Both
  `services/dedup.py` (module-level import) and `services/fingerprint.py` (**function-local** import,
  per D-00e) consume it.

  **Constraint:** dedup is not a `Stage`. Name it for what it is — a **file-level** predicate — and
  keep it **out of** the `Stage` dispatch ladders (`done_clause` / `failed_clause` /
  `inflight_clause` / `domain_completed_clause` / `stage_status_case`), all of which raise on unknown
  stages and are drift-locked by the Phase-78 equivalence test.

  (Rejected: a private helper in `dedup.py` — `fingerprint.py` would then lazily import the UI-facing
  dedup service to build its denominator, which is backwards. Rejected: a new `services/dedup_status.py`
  — a whole module for one clause, fragmenting the singular answer 78 deliberately established.)

- **D-14:** **Guard = a mutation-proof divergence test (load-bearing) + a source scan (insurance).**

  **The divergence test is the only shape with teeth.** On a *consistent* corpus (`marker ≡ state`) no
  test can distinguish "reads the marker" from "reads `state`" — both return identical rows, so the
  guard is green against the un-cut-over code. The test MUST construct a deliberately **inconsistent**
  corpus:
  - a file with a marker but `state = 'analyzed'` → must be **excluded** (marker wins);
  - a file with `state = 'duplicate_resolved'` but no marker → must be **included**.

  Revert the predicate and both assertions invert. Cover every dedup reader
  (`find_duplicate_groups`, `find_duplicate_groups_with_metadata`, `count_duplicate_groups`,
  `get_duplicate_stats`, `resolve_group`'s selection) and `get_fingerprint_progress`'s denominator.

  **The source scan** asserts `FileState.DUPLICATE_RESOLVED` no longer appears in `services/dedup.py`
  or `services/fingerprint.py` — it catches a `state` read reintroduced at a *new* site the behavioral
  test does not exercise.

  **Both guards MUST be mutation-tested before the phase closes** (break the source, watch RED,
  restore). The source scan in particular must survive the failure mode that made 83's grep toothless:
  SQLAlchemy splits the call across lines, so a line-oriented `grep` for a single-line spelling passes
  against buggy source. Check for false positives on the `.where()` readers too.

- **D-15:** **Replace `tests/fingerprint/services/test_fingerprint.py:295` with a real-DB integration
  test.** The existing test stubs three `session.execute` calls with a `side_effect` list and asserts
  the dict it fed in — it stays green through **any** rewrite of the predicates, including a wrong one.

  The replacement asserts real counts against a real corpus containing: music + video + a non-audio
  file, a dedup-resolved duplicate, a file with one engine success and one engine failure, and a file
  with all engines failed. That pins D-10's denominator, D-11's units change, and DERIV-05's
  aggregation in one test.

  **Acceptance:** a test must exist that goes RED if `completed` is reverted to
  `state == FileState.FINGERPRINTED`.

- **D-16:** **SC#3 is proven two ways.**
  1. A **committed integration test** asserting zero *hard* divergences from `services/shadow_compare.py`
     after `resolve → undo → re-resolve` on a synthetic corpus, so the new writer/undo paths can never
     introduce one. This gates every future PR.
  2. A **live-corpus `shadow_compare` run after `035`, before merge.** The CI test cannot see the real
     post-`032` resolved-without-marker rows; only the live run proves the repair covered them. Phase 79
     built the gate as re-runnable but **deferred the live run** (79 D-02) — which is exactly why D-01
     went unnoticed.

### Claude's Discretion

- **`035`'s `downgrade()`.** `034` chose a documented-lossy `DELETE` (it cannot distinguish repaired
  rows from live go-forward writes; `016`/`032`/`033` precedent). Planning may follow that precedent or
  make `035`'s downgrade a no-op — but must document the choice in the migration docstring.
- **Statement shape for the `state` restore in `undo_resolve`** once gated on `RETURNING file_id` —
  N per-file `UPDATE`s vs one `UPDATE … FROM (VALUES …)`. `bulk_undo` can carry a full page of groups.
  **Constraint:** only ids returned by the `DELETE` may be written.
- **Plan/PR decomposition.** The milestone's hard requirement is small blast-radius per phase, one
  shippable PR per seam. Natural seams: (a) migration `035` + its migration test; (b) the writer +
  undo + the nine dedup readers + the divergence guard; (c) `get_fingerprint_progress` + its
  integration test. **Constraint:** `035` must land before (b).
- **Base branch.** Confirm whether Phase 82 is a real prerequisite; the file sets appear disjoint.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone design contract
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` §4, §6.1, §6.2 — what replaces `FileState`;
  the `FINGERPRINTED` documented divergence and why it must not be "fixed"; the two-step migration and
  the shadow-compare gate between the steps. §7 names `services/dedup.py` (9 sites) and
  `get_fingerprint_progress` as this phase's cutover surface.
- `.planning/REQUIREMENTS.md` — READ-04, SIDECAR-02, DERIV-03, DERIV-05, ELIG-01, ELIG-04, MIG-01, MIG-02.
- `.planning/ROADMAP.md` — Phase 84 goal + 3 success criteria; the milestone phase graph.

### Upstream phase contracts (locked decisions — do not re-litigate)
- `.planning/phases/77-additive-schema-rescan-wipe-fix-migration-032/77-CONTEXT.md` — D-07 (the dedup
  marker's shape: marker-existence = resolved, undo = DELETE, no `previous_state` column; best-effort
  `canonical_file_id`).
- `.planning/phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` — the
  single-source predicate module; the SQL⇔Python equivalence harness.
- `.planning/phases/79-shadow-compare-gate-live-corpus/79-CONTEXT.md` — D-02 (the live-corpus run was
  deferred), D-04 (implication, not equality), D-06 (the soft allowlist).
- `.planning/phases/81-per-stage-failure-persistence-retry-paths/81-CONTEXT.md` — D-05 (writers
  dual-write; only *reliance* on `state` is replaced).
- `.planning/phases/83-cloud-routing-sidecar-cutover/83-CONTEXT.md` — the template for this phase:
  D-01 (a missing go-forward sidecar writer), D-04 (corpus repair), D-09/D-10 (the sidecar as the
  single CAS domain, `FileRecord` dual-write gated behind `rowcount`).
- `.planning/phases/83-cloud-routing-sidecar-cutover/83-02-SUMMARY.md` — the data-only repair-migration
  pattern, verbatim.

### Source of truth in code
- `alembic/versions/032_add_derived_status_schema.py:84` (`_BACKFILL_DEDUP`), `:129-141` (the
  `dedup_resolution` DDL), `:156` (`ix_fprint_success`).
- `alembic/versions/034_backfill_cloud_awaiting.py` — the migration `035` template.
- `src/phaze/models/dedup_resolution.py` — the marker's contract (D-08's docstring note lands here).
- `src/phaze/services/stage_status.py:89,120,131` — `done_clause` / `failed_clause`; D-13's new
  predicate lands here.
- `src/phaze/enums/stage.py` — `FAILURE_IS_TERMINAL`, `ELIGIBLE_AFTER_FAILURE`, `resolve_status`.
- `src/phaze/services/shadow_compare.py:85-87,135` — `_dedup_exists`, the hard `duplicate_resolved`
  invariant.
- `src/phaze/services/dedup.py` — the nine read sites + `resolve_group` / `undo_resolve`.
- `src/phaze/services/fingerprint.py:256-295` — `get_fingerprint_progress`; note the function-local DB
  imports and why (D-00e).
- `src/phaze/routers/duplicates.py:151,162,176,214,242` — the resolve/undo/bulk endpoints and the
  browser-held `file_states` payload.
- `src/phaze/services/scan_deletion.py:102-108` — the dual-FK marker delete (D-08).
- `src/phaze/services/pipeline.py:46` — `MUSIC_VIDEO_TYPES` (D-10's denominator).
- `tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py` — the migration-test
  template (idempotent backfill + empty autogenerate diff via `compare_metadata`).
- `tests/integration/test_shadow_compare.py:157` — how a `DedupResolution` row is constructed in tests.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`alembic/versions/034_backfill_cloud_awaiting.py`** — Phase 83's data-only repair migration. `035`
  is the same shape: sync `upgrade()`, single static parameter-free `op.execute`, no DDL, empty
  autogenerate diff. Its test is the test template.
- **`_BACKFILL_DEDUP`** (`032:84`) — already written as `INSERT … SELECT … ON CONFLICT (file_id) DO
  NOTHING`. `035` re-runs it verbatim; nothing new to author for the insert half.
- **`ix_fprint_success`** (`032:156`) — a partial index on `fingerprint_results(file_id) WHERE status
  = ANY(ARRAY['success','completed'])`, created precisely to back `done_clause(FINGERPRINT)`. D-11's
  `completed` count rides it.
- **`done_clause` / `failed_clause`** (`stage_status.py:89,120`) — already implement DERIV-05's
  aggregation (any-engine success wins; failed = no success AND ≥1 failure). D-11 consumes them
  unchanged.
- **`uq_dedup_resolution_file_id`** — the unique FK's implicit index serves the marker-EXISTS lookup;
  no new index needed.

### Established Patterns
- **Sidecar as the single CAS domain** (83 D-09): CAS on the sidecar, gate the `FileRecord` dual-write
  behind `rowcount`/`RETURNING`, no-op cleanly on zero rows. D-06 applies it to `dedup_resolution`.
- **Correlated `exists(...)` predicates only** — never an outer-join-null or negated-membership
  anti-pattern (`stage_status.py:92-94`). D-13's predicate follows.
- **Caller-owned transactions in `services/`** — build and flush, never commit (the `backends.py`
  dispatch discipline). D-02 follows; `routers/duplicates.py` relies on `get_session` to commit.
- **Function-local DB imports in `services/fingerprint.py`** — the agent-worker import boundary
  (Phase 26 Plan 10/11). D-00e / D-13.

### Integration Points
- `resolve_group` is the **single funnel** for all resolution: `bulk_resolve`
  (`routers/duplicates.py:214`) calls it per group. One writer, no copies.
- `undo_resolve` is the single funnel for undo: both `undo_resolve_endpoint` (`:177`) and `bulk_undo`
  (`:242`) call it with a browser-held `[{id, previous_state}]` payload.
- `get_fingerprint_progress` has exactly one caller: `routers/pipeline.py:1339`, serving
  `GET /api/v1/fingerprint/progress`. No template consumes it.
- `services/shadow_compare.py:135` is the gate this phase must keep green; `035` is what makes it
  green for the first time on the live corpus.

</code_context>

<specifics>
## Specific Ideas

- The operator's framing of SC#1: **"undo becomes a plain DELETE"** describes the *dedup semantics*,
  not the function body. The `state` restore is dual-write bookkeeping with a known death date
  (Phase 90), not a violation of the criterion.
- **Standing rule, applied here:** a green guard test proves nothing. Every guard shipped by this phase
  gets mutation-tested — break the source, watch it go RED, restore. Phase 83 shipped two toothless
  guards (a `grep` that passed against buggy source because SQLAlchemy splits the call across lines;
  an AST scan blind to `.values(**splat)`). Mutate every syntactic form, then check for false positives
  on the `.where()` readers.
- **Number changes are the fix, not a regression.** `completed` will jump (it currently counts a state
  almost nothing writes); `failed` will drop (per-engine double-counts collapse, partial failures
  reclassify as `done`). Both belong in the SUMMARY so they are not read as breakage.

</specifics>

<deferred>
## Deferred Ideas

- **`eligible()` has no dedup notion.** Once Phase 82's pending sets become `NOT done ∧ NOT in_flight`
  (ELIG-01), a dedup-resolved duplicate with no fingerprint row becomes *eligible* and gets enqueued.
  Today it is excluded only incidentally, because `state = duplicate_resolved` is not
  `METADATA_EXTRACTED`. **The file-level dedup predicate this phase adds to `stage_status.py` (D-13) is
  the same one Phase 82's pending sets will need.** → flag for Phase 82 (READ-01).
- **ROADMAP calls Phase 90's destructive migration `034`**, but Phase 83 took that revision for its
  repair backfill and this phase takes `035`. Phase 90's is `036`+. → roadmap hygiene.
- **`find_duplicate_groups`' `dup_hashes` subquery applies `LIMIT`/`OFFSET` with no `ORDER BY`**
  (`services/dedup.py:81`, `:131`, and the same shape at `:207`), so duplicate-group pagination is
  nondeterministic across pages. Pre-existing; untouched by the cutover. → its own quick task.
- **`get_pushing_count` / `get_pushed_count`** remain an unowned gap (carried forward from
  83-CONTEXT's deferred ideas).
- **Preserving a resolution when its canonical file is deleted** — splitting `scan_deletion.py:108`
  into a `DELETE` for `file_id` matches and an `UPDATE SET canonical_file_id = NULL` for canonical-only
  matches. Rejected here as out of scope (D-08); revisit if operators complain about duplicates
  reappearing after a batch delete.
- **Per-engine fingerprint coverage** (e.g. "audfprint has been down for a week") — `GROUP BY
  fingerprint_results.engine` would be cheap, and nothing surfaces this today. → Phase 87, UI-02.

</deferred>

---

*Phase: 84-Dedup & Fingerprint-Progress Cutover*
*Context gathered: 2026-07-09*
