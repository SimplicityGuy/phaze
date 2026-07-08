<!-- GSD:milestone-context -->

# Milestone Context: Parallel Enrich DAG

**Proposed version:** `2026.7.5` (CalVer `YYYY.M.REVISION`; current `pyproject` version is `2026.7.4`)
**Proposed name:** Parallel Enrich DAG (Retire Linear FileState)
**Status:** core design **already brainstormed and APPROVED** — do not re-litigate it.

> **Full design contract:** `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md`
> Read it before defining requirements. It carries the verified problem statement, the exact
> per-stage `done`/`in_flight`/`failed` predicates with real column names, the eligibility table,
> the sidecar mapping, the two-step migration + shadow-compare gate, the verified ~20-writer /
> ~40-reader call-site inventory, and five open decisions (D-01…D-05) for planning to resolve.

---

## Goal

Make the enrich pipeline **truly per-file parallel** by deleting the linear `FileState` enum and
deriving per-file, per-stage status from the output tables that already exist.

## Why now (verified against `main` @ `ce0c6434`)

**No file on current `main` can complete all three enrich stages.** Analyze's pending set is
`state == DISCOVERED` (`services/pipeline.py:1106`); fingerprint's is `state == METADATA_EXTRACTED`
(`services/pipeline.py:1359`); the metadata callback advances `DISCOVERED → METADATA_EXTRACTED`
(`routers/agent_metadata.py:89`); the fingerprint callback writes no state. Run metadata first and
analyze is stranded forever; run analyze first and fingerprint is stranded forever.

PR #221 didn't create this — it moved it. Before #221 nothing wrote `METADATA_EXTRACTED`, so
fingerprint's set was permanently empty. Each surgical patch trades one stranded stage for another.
The corpus takes **months**; the DAG must be per-file progressive, never globally stage-gated.

## Target features

1. **Derived status.** A pure function `stage_status(file, stage) -> {not_started | in_flight | done | failed}`
   with **no stored status enum**. `done` = the stage's output row exists; `in_flight` = an
   active/queued `saq_jobs` row for that `(file, function)`; `failed` = a per-stage failure marker.
2. **Truly independent enrich.** `eligible = NOT done AND NOT in_flight`, independent of every other
   stage. All `discovered` files light up in all three enrich tabs, in any order.
   (Exception, load-bearing: a failed *analyze* is terminal — manual retry only. Re-enabling
   auto-retry re-creates the 44.5K-job over-enqueue incident.)
3. **Per-stage failure markers.** Add one for `analyze` (replacing `ANALYSIS_FAILED`) and one for
   `metadata` — `report_metadata_failed` currently persists **nothing**, a latent bug fixed here.
   `fingerprint_results.status='failed'` already exists and is reused.
4. **Delete `FileState` entirely** (comprehensive scope, explicitly chosen over surgical). Its
   non-completion jobs move to sidecars: cloud routing → `cloud_job`; `DUPLICATE_RESOLVED` → a dedup
   marker; approve/reject → `proposals.status`; apply outcome → `execution_log`.
5. **Rework every reader/writer** — the three enrich pending sets, the file-row "State" display,
   `recovery`/`reenqueue`, `reconcile_cloud_jobs`, and `get_pipeline_stats` (linear `GROUP BY state`
   → output-table counts). ~20 writers, ~40 readers, 23 source files, ~50 test files.
6. **Partial indexes** so the `NOT EXISTS` pending anti-joins stay fast at 200K-file corpus scale.
   The bitmap stays **derived** — no new status column. Denormalize later only if a poll measures
   slow (YAGNI).
7. **Migration + backfill + verification.** Two-step: additive `032` (markers + sidecars + backfill
   + indexes) → **shadow-compare gate** asserting per-file invariants across the live corpus →
   destructive `033` (drop `ix_files_state`, drop `files.state`, delete the enum). Completion states
   derive for free; only `ANALYSIS_FAILED`, `DUPLICATE_RESOLVED`, and the cloud states need backfill.

## Bonus: latent bugs this deletion fixes

Independent justification for comprehensive over surgical scope — all are consequences of the enum:

- **Tag writing is permanently dead.** `services/tag_writer.py:185` raises unless `state == EXECUTED`;
  nothing in `src/` ever writes `EXECUTED` (apply writes `MOVED`/`UNCHANGED`). Same dead gate in
  `review.py`, `tags.py`, `cue.py`, `tracklists.py`.
- **Rescan wipes pipeline progress** — `ingestion.py:114` `ON CONFLICT DO UPDATE SET state = excluded.state`
  resets any file to `DISCOVERED`.
- **Metadata failures are invisible** in every UI surface and progress count.
- **`get_stage_progress` over-counts analyze done** — counts bare `analysis` row existence, but a
  partial row is upserted at analysis *start*; `analysis_completed_at IS NOT NULL` is the sound predicate.
- **`store_proposals` can regress a `MOVED` file**; **`report_upload_failed` lacks a CAS guard**.

## Key context and constraints

- **Fresh branch** `SimplicityGuy/true-parallel`, based on `main` @ `ce0c6434` (includes PR #221).
- Python **3.14**, **uv only**; ruff (line 150) + mypy strict clean; **90% coverage**.
- **Per-bucket test isolation** — tests must pass via `just test-bucket <bucket>` *in isolation*
  (`tests/buckets.json`). DB tests need `TEST_DATABASE_URL` / `PHAZE_QUEUE_URL` on the **`:5433`**
  ephemeral DB (`conftest.py` defaults to `:5432`).
- **PR per phase**, worktree per phase, **never push to `main`**, never `--no-verify`.
- Migrations are **sync**, next number is **`032`**, mirrored `downgrade()`, integration test each,
  and must **never reference `saq_jobs`** (SAQ-owned, not Alembic-managed).
- `saq_jobs` reads: static SQL + `begin_nested()` SAVEPOINT + degrade-to-safe-default (the
  `/pipeline/stats` poll runs every 5s and must never 500).
- Live data migration against an existing corpus — drain cloud-push lanes before the destructive step.
- **No new dependencies.**

## Non-goals

- No denormalized stage-bitmap column (YAGNI — derive first, measure, only then consider).
- No change to routing *policy* (duration threshold, backend rank/cap) — only to where routing
  *state* is stored.
- `PROV-01` (N-compute-aware orphan recovery, deferred from 2026.7.2) stays deferred, though
  `reenqueue.py` is heavily touched here — re-check the overlap during planning.

## Suggested phase shape (roadmapper input, not a mandate)

Ordered so every step is independently shippable and the destructive change lands last:

1. **Derivation layer** — `stage_status()` + eligibility predicates + partial indexes. Purely
   additive; nothing reads it yet. Resolve D-01/D-02.
2. **Failure markers** — analyze + metadata markers, `report_metadata_failed` made real, migration `032`.
3. **Sidecars** — cloud routing → `cloud_job` (resolve D-03), dedup marker, approve/reject →
   `proposals.status`. Backfill.
4. **Readers** — the three enrich pending sets (this is where the deadlock actually dies),
   `get_pipeline_stats` → output-table counts, `reenqueue`/`recovery`, `reconcile_cloud_jobs`,
   dedup's 9 `!= DUPLICATE_RESOLVED` sites, `get_fingerprint_progress`, the dead `EXECUTED` gates.
5. **UI** — file-row "State" display derived from `stage_status()`; `notYetEnriched`; the three
   enrich workspace templates.
6. **Shadow-compare gate + destructive migration `033`** — prove derivation on the live corpus, then
   drop `ix_files_state`, drop `files.state`, delete `FileState`.
