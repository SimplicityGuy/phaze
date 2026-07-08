# Phase 77: Additive Schema & Rescan-Wipe Fix (migration `032`) - Context

**Gathered:** 2026-07-07
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 77 is the **additive-only** first phase of the Parallel Enrich DAG milestone. It lands
migration `032`, which creates the schema objects the later derivation phases will read, and
ships the independently-verifiable rescan-wipe fix.

**In scope:**
- Migration `032` (additive): create the analyze/metadata **failure markers**, the **dedup
  marker** table, and the **cloud-routing sidecar** representation; **backfill** them from
  `files.state`; add **partial indexes** sized to the exact predicates, mirrored into the ORM
  `__table_args__` so `alembic revision --autogenerate` produces an empty diff (MIG-01, PERF-01).
- The **rescan-wipe fix** (MIG-03): remove the `ON CONFLICT DO UPDATE SET state = excluded.state`
  progress-wipe from both upsert sites.
- Per-migration integration test for `032` (upgrade path).

**Explicitly OUT of scope (later phases of this milestone):**
- The reader/derivation cutover — the `stage_status()` pure function and all `NOT EXISTS`
  pending-query rewrites (READ-*). Phase 77 only builds the backing schema; nothing reads it yet.
- The corpus-wide shadow-compare gate (MIG-02).
- The destructive migration `033` that drops `ix_files_state`, drops `files.state`, and deletes
  the `FileState` enum (MIG-04).

**Hard invariants for this phase:**
- `files.state` is **byte-unchanged** — `032` never writes it.
- The migration **never references `saq_jobs`** (SAQ-owned; every migration since `020` carries
  this banner).

</domain>

<decisions>
## Implementation Decisions

### Failure Markers (design D-02)
- **D-01:** The analyze and metadata `failed` markers are **nullable `failed_at` + `error_message`
  columns added to the existing 1:1 output tables** (`analysis`, `metadata`) — NOT a generic
  `stage_failure` table. Rationale: no new FK, preserves the ≤1-row-per-file invariant, keeps
  failure co-located with the stage's other facts, and makes the partial index trivial.
- **D-02:** Because a metadata failure inserts a `metadata` row with `failed_at` set and the
  payload columns NULL, the later `done(metadata)` predicate must tighten to
  **`EXISTS metadata WHERE file_id = … AND failed_at IS NULL`**. (This phase only creates the
  column; the tightened predicate lands in the derivation phase — call it out in the migration
  docstring so the reader phase honors it.)
- **D-03:** Backfill asymmetry — **analyze backfills, metadata does not.** `032` sets
  `analysis.failed_at` (e.g. `= updated_at`, else `now()`) for every file with
  `state = ANALYSIS_FAILED`, with a placeholder `error_message` such as
  `'backfilled from ANALYSIS_FAILED'`. `metadata.failed_at` gets **no** backfill — there is
  genuinely no historical source (`report_metadata_failed` persisted nothing); the marker only
  records go-forward. Document this in the migration docstring + phase VERIFICATION.

### Cloud-Routing Sidecar (design D-03)
- **D-04:** `AWAITING_CLOUD` is represented by **adding `AWAITING = "awaiting"` to the
  `CloudJobStatus` StrEnum and the `ck_cloud_job_status_enum` CHECK membership list**, not a new
  `analyze_route` table. An awaiting file carries a `cloud_job` row with `status='awaiting'`,
  `s3_key` NULL (already nullable), `upload_id` NULL. Reuses the existing `uq_cloud_job_file_id`
  sidecar; no new table. Backfill: `state = AWAITING_CLOUD` → insert (or promote) a `cloud_job`
  row to `status='awaiting'`.
- **D-05:** `LOCAL_ANALYZING` gets **no sidecar row** — it is exactly `in_flight(analyze)` and is
  derived in a later phase. A dead local job correctly re-derives as `not_started` and re-enqueues
  (intended recovery behavior). This upholds the milestone's derive-don't-store principle.
- **D-06:** For `PUSHING`/`PUSHED`, `032` **ensures a `cloud_job` row exists with the matching
  status** (`uploading`/`uploaded`). Rows created by the live cloud path already exist, so the
  backfill only fills gaps for any legacy rows missing one.

### Dedup Marker (SIDECAR-02)
- **D-07:** The dedup marker is a new table **`dedup_resolution(file_id UNIQUE FK,
  canonical_file_id FK, resolved_at)`** — it records which file the duplicate resolves *to*, not
  merely that it's resolved (enables a future "duplicate of X" UI, robust if the sha256 group
  shifts). Marker-row existence = resolved; undo = **DELETE the row** (the enum's `previous_state`
  was a transition artifact, unnecessary under derivation). Backfill from
  `state = DUPLICATE_RESOLVED`, deriving `canonical_file_id` as the non-resolved member of each
  `sha256_hash` group.

### Rescan-Wipe Fix (MIG-03)
- **D-08:** Ship as the **standalone first task** of the phase (independently verifiable): remove
  `"state": excluded.state` from the `ON CONFLICT DO UPDATE` `set_` dict in **both** upsert sites —
  `services/ingestion.py` (`bulk_upsert_files`, ~line 114) and `routers/agent_files.py` (~line 132).
  New files still INSERT `state = DISCOVERED` via the column default; existing files keep their
  state on rescan. Regression test: upsert a file, advance it to `ANALYZED`, re-upsert the same
  `(agent_id, original_path)`, assert `state` stays `ANALYZED` **and** its `analysis` row survives.

### Migration Mechanics
- **D-09:** `032.downgrade()` is **minimal — the simplest correct DDL reversal only.** The focus
  is the forward upgrade path; do **not** over-invest in downgrade robustness or elaborate
  awaiting-row / corpus-scrubbing cleanup. This decision **explicitly relaxes ROADMAP success
  criterion #4** ("`032.downgrade()` cleanly reverses every additive object"): a best-effort DDL
  downgrade is acceptable. Keep the per-migration integration test focused on the upgrade + backfill
  applying cleanly; downgrade coverage can be minimal.

### Claude's Discretion
- **Backfill batching:** set-based `INSERT … SELECT` / `UPDATE … FROM files WHERE state = …`
  (house style, one statement per object). Chunk only if a measured 200K-row statement proves
  problematic — default is set-based.
- **`error_message` column type:** `Text` (unbounded — essentia tracebacks can be long).
- **Exact partial-index set (PERF-01):** at minimum the design's `ix_analysis_completed`
  (`WHERE analysis_completed_at IS NOT NULL`) and `ix_fprint_success`
  (`WHERE status IN ('success','completed')`), plus `IS NOT NULL`-shaped partial indexes for the
  new `failed_at` markers, the `dedup_resolution` table, and the `awaiting` sidecar lookup — each
  sized to its exact predicate and mirrored into the ORM `__table_args__`. Never `status IN (...)`
  shaped where an `IS NOT NULL` predicate is the real query. Research/planner finalizes the set.
- **Index build lock behavior on the live 200K table:** planner/research decides whether
  `CREATE INDEX CONCURRENTLY` (non-transactional migration) is warranted vs. a plain build; follow
  in-tree house style.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone design (authoritative — read first)
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` — the APPROVED milestone design. Especially:
  §2.3 (failure markers, D-02), §4/D-03 (cloud sidecar + `LOCAL_ANALYZING` derivation),
  §5 (partial-index house style, derive-don't-denormalize), §6.1 (the backfill-needed table),
  §6.2 (two-step migration story), §10 (open decisions D-01..D-05).
- `.planning/REQUIREMENTS.md` — phase requirements **MIG-01, MIG-03, PERF-01** (plus milestone
  context: FAIL-01/02, SIDECAR-01/02, READ-* for the downstream reader phases this schema feeds).
- `.planning/ROADMAP.md` — Phase 77 goal + the four success criteria.

### Code to read before touching (source of truth for shapes/patterns)
- `src/phaze/models/analysis.py` — `AnalysisResult` (1:1, `analysis_completed_at` semantics,
  migration-028 docstring); where `failed_at`/`error_message` are added.
- `src/phaze/models/metadata.py` — `FileMetadata` (1:1); where `failed_at`/`error_message` are added.
- `src/phaze/models/cloud_job.py` — `CloudJobStatus` StrEnum + `ck_cloud_job_status_enum` CHECK
  (add `AWAITING`); `s3_key`/`upload_id` already nullable.
- `src/phaze/models/file.py` — `FileState` enum (`DUPLICATE_RESOLVED`, `AWAITING_CLOUD`, …),
  `ix_files_state`, `sha256_hash` (for dedup canonical derivation). **Not modified this phase.**
- `src/phaze/services/ingestion.py` (`bulk_upsert_files`, ~line 114) — rescan upsert site #1.
- `src/phaze/routers/agent_files.py` (~line 132) — rescan upsert site #2 (mirror of #1).
- `src/phaze/services/dedup.py` — `resolve_group` / `undo_resolve` (dedup semantics + undo);
  `state != DUPLICATE_RESOLVED` exclusion queries the backfill must satisfy.
- `alembic/versions/019_add_proposals_pending_unique_index.py` — partial-index house style
  (`postgresql_where` + ORM `__table_args__` mirror).
- `alembic/versions/028_add_analysis_completed_at.py` — additive-column migration precedent;
  head is `031_add_route_control.py`, so this migration is **`032`**.
- `tests/integration/test_migrations/` — per-migration integration test location + pattern.
- `tests/buckets.json` — per-bucket test isolation; new tests must pass via
  `just test-bucket <bucket>` in isolation.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`CloudJobStatus` string-backed StrEnum + CHECK constraint** — adding `AWAITING` needs only the
  enum member + the `ck_cloud_job_status_enum` membership list; no Postgres enum-type migration.
- **`analysis` / `metadata` are 1:1 with unique `file_id`** — adding nullable columns is clean and
  preserves the ≤1-row invariant the failure-marker design depends on.
- **Partial-index house style (`019`, `028`, `018`, `012`, `014`)** — `postgresql_where=text(...)`
  mirrored into ORM `__table_args__`; the empty-autogenerate-diff acceptance test (SC#2) rides on it.

### Established Patterns
- Migrations are **sync** (`def upgrade()`, plain `op.*`; only `env.py` is async), 3-digit
  zero-padded string revisions, with a `downgrade()` (minimal here per D-09).
- The two rescan upsert sites are near-identical mirrors — the fix must touch **both** or the bug
  survives on one path.
- `bulk_upsert_files` batches via `itertools.batched`; removing `state` from `set_` doesn't change
  batching.

### Integration Points
- The failure-marker columns, `dedup_resolution` table, and `awaiting` cloud-job status are the
  **substrate** the next phase's `stage_status()` derivation reads. Shape them for that consumer:
  `done(metadata)` will need `failed_at IS NULL`; `in_flight`/`awaiting` derivation will union the
  `cloud_job` sidecar. This phase writes them; the reader phase gives them meaning.

</code_context>

<specifics>
## Specific Ideas

- User directive: **"we're focused on forward-looking upgrade paths only"** — minimal downgrades,
  simplest thing (D-09). Do not gold-plate reversal logic.
- Design-doc D-IDs map to this CONTEXT's D-IDs as: design D-02 → D-01/D-02/D-03; design D-03 →
  D-04/D-05/D-06.

</specifics>

<deferred>
## Deferred Ideas

None net-new — the following are milestone scope that belongs to **later phases**, not deferrals:
- `stage_status()` derivation + `NOT EXISTS` pending-query rewrites (READ-*).
- Shadow-compare invariant gate (MIG-02).
- Destructive `033`: drop `files.state`, drop `ix_files_state`, delete `FileState` enum (MIG-04).
- The six latent-bug fixes (design §4.1: dead `EXECUTED` gates, `store_proposals` MOVED regression,
  `report_upload_failed` CAS guard) — they fall out of the reader/writer rework phases, not `032`.

</deferred>

---

*Phase: 77-additive-schema-rescan-wipe-fix-migration-032*
*Context gathered: 2026-07-07*
