<!-- GSD:design -->

# Design: Parallel Enrich DAG — Retire the Linear `FileState`

**Status:** APPROVED (core model brainstormed + approved 2026-07-08; captured here without re-litigation)
**Branch:** `SimplicityGuy/true-parallel` (fresh off `main` @ `ce0c6434`, which includes PR #221)
**Scope:** COMPREHENSIVE — delete the `FileState` enum entirely
**Supersedes:** the linear-state pipeline model in place since v1.0

---

## 1. Problem

The DAG label reads **"Enrich · parallel"**. It lies. `metadata`, `fingerprint`, and `analyze`
are serialized by a single linear `FileRecord.state` enum column (`String(30)`, 17 members).

A single scalar cannot represent *"metadata-extracted **and** fingerprinted **and** analyzed"*.
So writers clobber each other and readers gate on whichever value happened to win.

### 1.1 The deadlock on current `main` (verified, post-#221)

Three predicates, read directly from the tree at `ce0c6434`:

| Concern | Predicate | Location |
|---|---|---|
| analyze pending | `FileRecord.state == DISCOVERED` | `src/phaze/services/pipeline.py:1106` |
| fingerprint pending | `FileRecord.state == METADATA_EXTRACTED` | `src/phaze/services/pipeline.py:1359` |
| metadata callback | `DISCOVERED → METADATA_EXTRACTED`, guarded on `state == DISCOVERED` | `src/phaze/routers/agent_metadata.py:89` |
| fingerprint callback | writes **no** state | `src/phaze/routers/agent_fingerprint.py:82` |

`get_discovered_files_with_duration` (`pipeline.py:1095`) is the *sole* pending source for analyze —
for both the API trigger (`routers/pipeline.py:393`) and the UI trigger (`routers/pipeline.py:711`).

Therefore, for any given file:

- **Run metadata first** → state leaves `DISCOVERED` → the analyze trigger can never enqueue it. Analyze is stranded permanently.
- **Run analyze first** → state goes `AWAITING_CLOUD` / `LOCAL_ANALYZING` → `ANALYZED` → the metadata callback's `state == DISCOVERED` guard no-ops → state never reaches `METADATA_EXTRACTED` → fingerprint is stranded permanently.
- **Run metadata → fingerprint → analyze** → fingerprint's callback writes no state, so the file is still `METADATA_EXTRACTED`, not `DISCOVERED` → analyze is *still* stranded.

> **No file on current `main` can complete all three enrich stages.**

PR #221 did not create this bug class; it moved it. Before #221 nothing wrote `METADATA_EXTRACTED`,
so the fingerprint pending set was permanently empty and analyze worked. #221 correctly made the
metadata callback advance state — and thereby closed the analyze door. That is the linear enum
playing whack-a-mole, and it is the sharpest possible argument for this milestone.

The only accidental "unstick" is a rescan: `bulk_upsert_files` does
`ON CONFLICT DO UPDATE SET state = excluded.state` (`src/phaze/services/ingestion.py:114`, mirrored at
`src/phaze/routers/agent_files.py:132`) with `excluded.state = DISCOVERED` — so re-scanning resets an
arbitrarily-advanced file (`ANALYZED`, `APPROVED`, `MOVED`) back to `DISCOVERED`, wiping progress.

### 1.2 Why this must be fixed now, not worked around

The corpus is ~200K files and takes **months** to grind through. The DAG must be **per-file
progressive**, never globally stage-gated: every `discovered` file should light up in all three
enrich tabs simultaneously and be workable in any order. A linear enum structurally cannot express
that. Each surgical patch (like #221) trades one stranded stage for another.

---

## 2. The approved model

Replace `FileState` with a **derived pure function**, with **no stored status enum**:

```
stage_status(file, stage) -> {not_started | in_flight | done | failed}
```

Everything is computed from sources that already exist. `get_stage_progress`
(`services/pipeline.py:299`) already counts `done` from output tables; `get_stage_busy_counts`
(`services/pipeline.py:466`) already derives the DAG busy pills from `saq_jobs`. This milestone
makes those the *authority* rather than a parallel cosmetic view.

### 2.1 `done` — the stage's output row exists

Ground truth per stage, using **real** column names (verified against the models):

| Stage | `done` predicate | Cardinality caveat |
|---|---|---|
| `discovery` | `files` row exists | 1 per file |
| `metadata` | `EXISTS metadata WHERE file_id = f.id` | `uq_metadata_file_id` — exactly ≤1 |
| `fingerprint` | `EXISTS fingerprint_results WHERE file_id = f.id AND status IN ('success','completed')` | **N rows per file** — one per `(file_id, engine)` |
| `analyze` | `EXISTS analysis WHERE file_id = f.id AND analysis_completed_at IS NOT NULL` | ≤1, but a **partial row is upserted at analysis START** |
| `tracklist` | `EXISTS tracklists WHERE file_id = f.id` | N per file; `file_id` is **nullable** |
| `propose` | `EXISTS proposals WHERE file_id = f.id` | N per file; ≤1 with `status='pending'` |
| `apply` | `EXISTS execution_log JOIN proposals ON execution_log.proposal_id = proposals.id WHERE proposals.file_id = f.id` | **`execution_log` has no `file_id`** — must join via `proposal_id` |

Three of these are traps the design must respect and the original one-line summary glossed:

1. **`analysis` row existence is NOT completion.** A coverage-only partial row is upserted when
   analysis *starts* (`routers/agent_analysis.py:294`). `analysis_completed_at IS NOT NULL` is the
   only sound predicate (migration `028`, and the docstring at `models/analysis.py:34-38`, both say so).
   `get_stage_progress:384` currently counts bare row existence — **that is a latent over-count and
   this milestone fixes it.**
2. **`fingerprint_results` is per-engine.** A file may hold `chromaprint=success` and
   `panako=failed` simultaneously. See §2.3 for the precedence rule.
3. **`execution_log` has no `file_id`.** Apply-done joins through `proposals`.

### 2.2 `in_flight` — an active/queued job for that `(file, function)`

`saq_jobs` has no `function` or `file_id` column. Attribution is entirely through the deterministic
key, stamped at the single `before_enqueue` chokepoint (`tasks/_shared/deterministic_key.py:99`):

```
saq_jobs.key = '<function>:<file_id>'   -- for process_file, extract_file_metadata, fingerprint_file, …
```

with `STAGE_TO_FUNCTION = {metadata: extract_file_metadata, analyze: process_file, fingerprint: fingerprint_file}`
(`tasks/_shared/stage_control.py:51`).

So: `in_flight(f, stage) ⇔ EXISTS saq_jobs WHERE status IN ('queued','active') AND key = STAGE_TO_FUNCTION[stage] || ':' || f.id`

**Hard constraints on any `saq_jobs` access** (inherited, non-negotiable):

- `saq_jobs` is **SAQ-owned, not Alembic-managed**. Every migration since `020` carries a banner:
  *"this migration must NEVER reference `saq_jobs`"*. Honor it.
- All reads must be static SQL, wrapped in `session.begin_nested()` (SAVEPOINT), and **degrade to
  a safe default** on any error — the `/pipeline/stats` poll runs every 5s and must never 500.
  Copy the existing idiom at `services/pipeline.py:466-497`.
- A **paused** stage parks its jobs with `status='queued'` and `scheduled = 9999999999`. Parked jobs
  therefore still read as `in_flight`. This is existing, intentional behavior — preserve it.

> **D-01 (open, recommended):** `saq_jobs` rows vanish once complete, but `scheduling_ledger`
> (PK = `'<function>:<natural_id>'`, cleared only on *terminal*) is the durable
> "scheduled and not yet terminal" marker that `tasks/reenqueue.py` already trusts across worker
> restarts. **Recommendation:** `in_flight = saq_jobs(queued|active) ∪ scheduling_ledger`. Using
> `saq_jobs` alone re-opens the crash-window that the ledger was introduced to close
> (a worker dies mid-job → no `saq_jobs` row, no output row → the file reads `not_started` and gets
> re-enqueued by every poll). Confirm during planning.

### 2.3 `failed` — a per-stage failure marker

Today, only `analyze` persists failure, and it does so *in the enum being deleted*
(`state = ANALYSIS_FAILED`, `routers/agent_analysis.py:329`). The other two persist nothing:

- `report_metadata_failed` (`routers/agent_metadata.py:78-105`) records **NOTHING** but a
  `scheduling_ledger` DELETE. Its own docstring admits it. A terminally-failed metadata extraction
  is invisible in every UI surface and every progress count. **This is a latent bug; fix it here.**
- `report_fingerprint_failed` (`routers/agent_fingerprint.py:59-108`) likewise records nothing. Soft
  per-engine failures *do* persist via a different path (`FingerprintResult.status='failed'`), but a
  hard exception before any PUT persists nothing.

**Design:**

| Stage | `failed` marker | Change |
|---|---|---|
| `fingerprint` | `fingerprint_results.status = 'failed'` | **exists already** — reuse |
| `analyze` | new failure marker (replaces `ANALYSIS_FAILED`) | **ADD** |
| `metadata` | new failure marker | **ADD** (closes the latent bug) |

> **D-02 (open, recommended):** Prefer adding nullable `failed_at` + `error_message` columns to the
> *existing* per-stage output tables (`analysis`, `metadata`) over a new generic `stage_failure`
> table. It keeps the ≤1-row-per-file invariant, keeps failure co-located with its stage's other
> facts, needs no new FK, and makes the partial index trivial. `metadata` currently has no row on
> failure — so a failure would insert a metadata row with `failed_at` set and payload columns NULL;
> `done` must therefore be tightened to `EXISTS metadata WHERE file_id = … AND failed_at IS NULL`.
> Alternative (a generic `stage_failure(file_id, stage, failed_at, error)` table) keeps `done` as
> pure row-existence but adds a table and a second write path. Decide during planning; the derivation
> layer must expose the choice behind `stage_status()` either way.

**Precedence** (a file can satisfy several predicates at once — e.g. one engine succeeded and one
failed; or a retry is queued for a previously-failed stage):

```
in_flight  ≻  done  ≻  failed  ≻  not_started
```

`in_flight` wins so an in-progress retry shows as running, not as its stale prior outcome.
`done` beats `failed` so a fingerprint with **any** successful engine reads `done`
(matching today's `get_stage_progress:378` `COUNT(DISTINCT file_id) WHERE status IN ('success','completed')`),
and `failed` means *no engine succeeded*.

### 2.4 `not_started` — none of the above

---

## 3. Eligibility — a pure predicate over `stage_status`

```
eligible(f, stage) ⇔ NOT done(f, stage)
                  ∧ NOT in_flight(f, stage)
                  ∧ NOT (failed(f, stage) ∧ FAILURE_IS_TERMINAL[stage])
                  ∧ upstream(f, stage)
```

| Stage | `upstream` | `FAILURE_IS_TERMINAL` | Rationale |
|---|---|---|---|
| `metadata` | **none** | `true` | Independent of every other stage |
| `fingerprint` | **none** | `false` | Failed engines auto-retry — today's deliberate D-16 behavior, preserved |
| `analyze` | **none** | `true` | See warning below |
| `tracklist` | `done(fingerprint)` ∧ ¬`done(tracklist)` | `true` | |
| `propose` | `done(metadata)` ∧ `done(analyze)` | `true` | Convergence — already how `get_proposal_pending_batches` works |
| `review` | `done(propose)` (a proposal exists) | n/a | Availability, not a queue |
| `apply` | `proposals.status = 'approved'` | `true` | `done(apply)` = `execution_log` row |

**The three enrich stages have NO upstream.** Every `discovered` file lights up in all three tabs
immediately, in any order. This is the whole point of the milestone.

> ⚠️ **`FAILURE_IS_TERMINAL[analyze] = true` is load-bearing.** `tasks/reenqueue.py:179-186` carries
> an explicit warning: *"ANALYSIS_FAILED is DELIBERATELY treated as analyze-DONE … Do NOT add
> ANALYSIS_FAILED to a 'pending' query here; that would re-introduce the auto-loop."* A failed
> 4-hour analysis must never auto-retry — that is the `recover_orphaned_work` over-enqueue incident
> (44.5K jobs). Retry stays **manual only**, via the existing `retry_analysis_failed` button.
> The `failed → eligible` asymmetry between `fingerprint` (cheap, auto-retry) and `analyze`
> (expensive, manual) is intentional and must be encoded, not smoothed away.

**Behavior change to call out:** metadata's pending set is currently *state-agnostic* — literally
every music/video file, forever (`get_metadata_pending_files`, `pipeline.py:1330`), relying on
deterministic-key job dedup to suppress the re-runs. Under the new model it becomes
`NOT done ∧ NOT in_flight`. This is a **strict improvement** (no more enqueue-and-dedup churn over
200K files on every trigger) but it changes the "backfill" semantics of the metadata button, and it
breaks `is_domain_completed`'s metadata branch in `reenqueue.py:266`, which is currently structurally
inert *because* the pending set is everything. Both must be reworked together.

---

## 4. What replaces `FileState` (comprehensive scope)

`FileState`'s 17 members are doing five unrelated jobs. Separate them:

| Members | Job | Replacement |
|---|---|---|
| `DISCOVERED`, `METADATA_EXTRACTED`, `FINGERPRINTED`, `ANALYZED`, `PROPOSAL_GENERATED` | stage completion | **derived** — `stage_status()` over output tables |
| `ANALYSIS_FAILED` | analyze failure | **derived** — analyze failure marker (§2.3) |
| `AWAITING_CLOUD`, `PUSHING`, `PUSHED`, `LOCAL_ANALYZING` | cloud routing / dispatch | **`cloud_job` sidecar** (see D-03) |
| `DUPLICATE_RESOLVED` | dedup resolution | **dedup marker table** |
| `APPROVED`, `REJECTED` | review decision | **`proposals.status`** (already authoritative — file state is a redundant cascade) |
| `EXECUTED`, `FAILED`, `MOVED`, `UNCHANGED` | apply outcome | **`execution_log`** + `proposals.status` |

Then: **drop the `state` column and delete the enum.**

> **D-03 (open):** `cloud_job` already exists with `uq_cloud_job_file_id`, a `status` CHECK
> (`uploading|uploaded|submitted|running|succeeded|failed`) and a `cloud_phase` CHECK
> (`queued_behind_quota|admitted|running|finished`). It covers `PUSHING`/`PUSHED` cleanly. It does
> **not** cover:
> - `AWAITING_CLOUD` — "held, routed to cloud, not yet dispatched"; precedes any `cloud_job` row.
> - `LOCAL_ANALYZING` — `LocalBackend.dispatch` writes **no** `cloud_job` row at all.
>
> **Observation worth exploiting:** `LOCAL_ANALYZING` means exactly "`process_file` is enqueued on
> the local queue" — which *is* the `in_flight(analyze)` signal. It is very likely fully derivable
> and needs no sidecar row. `AWAITING_CLOUD` is genuinely new information (a routing decision) and
> does need one: either a new `status='awaiting'` CHECK value on `cloud_job` (migration touches the
> constraint) or a dedicated `analyze_route` table. Resolve during planning.

### 4.1 Latent bugs this deletion fixes for free

These are all *consequences* of the linear enum and all disappear when it does. They are strong
independent justification for the comprehensive scope over a surgical patch.

1. **Enrich deadlock** (§1.1) — no file can complete all three enrich stages. **Severity: critical.**
2. **Tag writing is permanently dead.** `services/tag_writer.py:185` raises
   `ValueError("Only executed files can have tags written")` unless `state == FileState.EXECUTED`.
   **Nothing in `src/` ever writes `EXECUTED`** — the apply path writes `MOVED`/`UNCHANGED`
   (`routers/agent_proposals.py:115`). So the guard always fires. The same dead `state == EXECUTED`
   gate appears in `services/review.py:109,251`, `routers/tags.py:174,179,336,422`,
   `routers/cue.py:48,89,251`, `routers/tracklists.py:138,600,897`. `FileState.FAILED` has zero
   writers and zero readers.
3. **Rescan wipes pipeline progress.** `ingestion.py:114` / `agent_files.py:132`
   `ON CONFLICT DO UPDATE SET state = excluded.state` resets any file to `DISCOVERED`. With no
   `state` column, a rescan physically cannot clobber progress.
4. **Metadata failures are invisible.** `report_metadata_failed` persists nothing (§2.3).
5. **`store_proposals` can regress a `MOVED` file.** `_TERMINAL_FILE_STATES` (`services/proposal.py:39`)
   guards `{APPROVED, REJECTED, EXECUTED, DUPLICATE_RESOLVED}` but omits `MOVED`/`UNCHANGED`.
6. **`report_upload_failed` has no CAS guard.** `routers/agent_s3.py:195` can clobber an already-
   `ANALYZED` file back to `AWAITING_CLOUD` — the exact stranding its siblings at
   `agent_push.py:126,261` and `agent_s3.py:128` guard against.
7. **`get_stage_progress` over-counts analyze done** — counts bare `analysis` row existence
   (`pipeline.py:384`) rather than `analysis_completed_at IS NOT NULL`, so files count as analyzed
   the moment analysis *starts*.

---

## 5. Performance: derive, don't denormalize (yet)

The DAG bitmap is **derived from output tables. No new status column.** The pending queries become
`NOT EXISTS (…)` anti-joins over `metadata` / `fingerprint_results` / `analysis` / `saq_jobs`.

At 200K files these must stay fast enough for the 5s `/pipeline/stats` poll. Add **partial indexes**
sized to the exact predicates. House style (`alembic/versions/019_add_proposals_pending_unique_index.py:72`)
uses `postgresql_where` and **mirrors the index into the ORM `__table_args__`** so `autogenerate`
stays in sync:

```python
Index("ix_analysis_completed", "file_id", postgresql_where=text("analysis_completed_at IS NOT NULL"))
Index("ix_fprint_success",     "file_id", postgresql_where=text("status IN ('success','completed')"))
```

Precedent already in-tree: `018` (`ix_analysis_window_bpm_fine WHERE tier='fine'`), `019`,
`012` (`WHERE status='live'`), `014` (`WHERE revoked_at IS NULL`).

**Explicitly YAGNI:** do **not** add a denormalized stage-bitmap column. Derive first. Denormalize
only if a measured poll is slow — and only with a measurement in the phase's verification doc.
Dropping `ix_files_state` (`models/file.py:98`) alongside the column is part of the work.

---

## 6. Migration, backfill, and the verification story

Next migration number is **`032`** (head is `031_add_route_control.py`). Migrations here are **sync**
(`def upgrade()`, plain `op.*`; only `env.py` is async), 3-digit zero-padded string revisions, with
mirrored `downgrade()`. **The migration must never reference `saq_jobs`.**

### 6.1 The claim to prove

> Existing output rows should already make derivation "just work."

This is **true for completion states and false for the rest** — precisely the split that dictates
the backfill:

| Legacy `state` | Derivation source | Backfill needed? |
|---|---|---|
| `DISCOVERED` | absence of output rows | no |
| `METADATA_EXTRACTED` | `metadata` row | no |
| `ANALYZED` | `analysis.analysis_completed_at` | no |
| `PROPOSAL_GENERATED` | `proposals` row | no |
| `APPROVED` / `REJECTED` | `proposals.status` | no (already authoritative) |
| `EXECUTED` / `MOVED` / `UNCHANGED` / `FAILED` | `execution_log` + `proposals.status` | no (already authoritative) |
| `FINGERPRINTED` | `fingerprint_results.status` | ⚠️ **see below** |
| `ANALYSIS_FAILED` | *nothing* | **YES** → analyze failure marker |
| `DUPLICATE_RESOLVED` | *nothing* | **YES** → dedup marker |
| `AWAITING_CLOUD` / `PUSHING` / `PUSHED` | partially `cloud_job` | **YES** → cloud sidecar |
| `LOCAL_ANALYZING` | likely `in_flight(analyze)` | probably no (D-03) |

⚠️ **`FINGERPRINTED` does not imply a fingerprint succeeded.** Its *only* writer is
`routers/pipeline.py:935` (`retry_analysis_failed`, rolling a file *back* out of `ANALYSIS_FAILED`).
Such files may have no `fingerprint_results` success row at all. Under derivation they correctly
become `fingerprint: not_started` and get re-fingerprinted. This is intended, cheap, and must be
called out in the migration docstring rather than "fixed."

### 6.2 Verification: a shadow-compare gate, run before the column is dropped

The migration is **two-step by design**, and the gate between the steps is the deliverable:

1. **`032` — additive.** Create the failure markers, the dedup marker, and the cloud sidecar rows.
   Backfill them from `files.state`. **Do not touch `files.state`.** Add the partial indexes.
2. **Shadow-compare (a real, committed, runnable check — not a one-off script).** With both
   representations live, assert per-file invariants across the whole corpus and report divergences:

   ```
   state = ANALYZED           ⇒ analysis.analysis_completed_at IS NOT NULL
   state = METADATA_EXTRACTED ⇒ metadata row exists
   state = ANALYSIS_FAILED    ⇒ analyze failure marker exists
   state = DUPLICATE_RESOLVED ⇒ dedup marker exists
   state IN (PUSHING,PUSHED)  ⇒ cloud_job row exists with the corresponding status
   state = AWAITING_CLOUD     ⇒ cloud sidecar row exists
   ```

   Assert **implication, not equality** — derivation is deliberately *more* informative than the
   scalar (a file can be `metadata`-done *and* `analyze`-done, which no single enum value encodes).
   `FINGERPRINTED` is the one documented, expected divergence (§6.1).

3. **`033` — destructive.** Only after the shadow-compare passes on the live corpus: drop
   `ix_files_state`, drop `files.state`, delete the `FileState` enum.

**Quiesce requirement:** files in `PUSHING`/`uploading` at deploy time are mid-rsync/mid-S3-upload.
The rollout must drain the cloud-push lanes (`--profile drain`) before `033`, or the backfill will
snapshot a moving target. Note this in the release runbook.

---

## 7. Call-site inventory (~20+, verified)

**Writers of `FileRecord.state` — 20 sites** across `services/ingestion.py`, `routers/agent_files.py`,
`routers/agent_analysis.py` (×2), `routers/pipeline.py` (×3), `services/backends.py` (×3),
`routers/agent_push.py` (×2), `routers/agent_s3.py` (×2), `tasks/reconcile_cloud_jobs.py`,
`services/dedup.py` (×2), `services/proposal.py`, `services/proposal_queries.py` (×3),
`routers/agent_proposals.py`.

**Readers** — the load-bearing ones:

- `services/pipeline.py` — `get_pipeline_stats` (the only `GROUP BY state`), `get_discovered_files_with_duration`,
  `get_fingerprint_pending_files`, `get_proposal_pending_batches`, `get_files_by_state`,
  `get_analyze_stage_files` (`_ANALYZE_STAGE_STATES`), `get_analysis_failed_{files,count}`,
  `get_cloud_staging_candidates`, `_backfill_candidates_stmt`, `get_{awaiting_cloud,pushing,pushed}_count`
- `tasks/reenqueue.py` — `_select_done_analyze_ids`, `_select_done_push_ids`, `_get_awaiting_cloud_ids`,
  `_build_done_sets`, `is_domain_completed`
- `services/dedup.py` — 9 sites, all `state != DUPLICATE_RESOLVED`
- `services/fingerprint.py` — `get_fingerprint_progress` (a linear-state progress bar; becomes derived)
- `services/proposal.py:39` — `_TERMINAL_FILE_STATES`
- Dead `EXECUTED` gates — `services/review.py`, `services/tag_writer.py`, `routers/{tags,cue,tracklists}.py`
- **UI** — `templates/pipeline/partials/metadata_workspace.html:43,50` renders the **raw enum string** in a
  "State" column; `analyze_workspace.html:81-86` translates `awaiting_cloud`/`analysis_failed`;
  `templates/proposals/partials/proposal_row.html:46` checks `state == "executed"`;
  `routers/pipeline.py:240` derives `dag["notYetEnriched"]` from `discovered − metadata_extracted`.

`get_pipeline_stats`' linear `GROUP BY state` becomes per-stage output-table counts — i.e. it
collapses into `get_stage_progress`, which already does exactly this and reads zero state.

---

## 8. Constraints

- Python **3.14**, **uv only** (`uv run …`; never bare `pip`/`python`/`pytest`/`mypy`)
- `ruff` clean (line length 150) · `mypy` strict clean · **90% coverage** floor (per-module 90)
- **Per-bucket test isolation:** every new test must pass via `just test-bucket <bucket>` *in isolation*,
  not merely in the full suite. Buckets: `tests/buckets.json` —
  `discovery, metadata, fingerprint, analyze, identify, review, agents, integration, shared`.
  `tests/shared/test_partition_guard.py` enforces one-bucket-per-file.
- **DB tests need `TEST_DATABASE_URL` / `PHAZE_QUEUE_URL` pointed at the `:5433` ephemeral DB** —
  `conftest.py` defaults to `:5432`.
- Pre-commit hooks must pass; **never `--no-verify`**.
- **PR per phase**, worktree per phase, **never push to `main`**.
- Migrations: sync, mirrored `downgrade()`, integration test per migration
  (`tests/integration/test_migrations/`), **never reference `saq_jobs`**.
- This is a **live data-migration** against an existing corpus — additive migration, shadow-compare
  gate, destructive migration, in that order.

## 9. Non-goals

- No denormalized bitmap column (§5, YAGNI).
- No change to routing *policy* (duration threshold, backend rank/cap) — only to where routing
  *state* is stored.
- No new dependencies.
- Not fixing `PROV-01` (N-compute-aware orphan recovery, deferred from 2026.7.2) — though
  `reenqueue.py` is heavily touched here, so re-check the overlap during planning.

## 10. Open decisions for planning

| ID | Decision | Recommendation |
|---|---|---|
| **D-01** | Is `in_flight` = `saq_jobs` alone, or `saq_jobs ∪ scheduling_ledger`? | Union — the ledger is the durable across-restart marker `reenqueue` already trusts |
| **D-02** | Failure markers: columns on existing output tables, or one generic `stage_failure` table? | Columns (`failed_at`, `error_message`) — no new FK, ≤1 row invariant preserved |
| **D-03** | How to represent `AWAITING_CLOUD` and `LOCAL_ANALYZING`? | `LOCAL_ANALYZING` → derive from `in_flight(analyze)`; `AWAITING_CLOUD` → sidecar row (new `cloud_job.status` CHECK value, or `analyze_route` table) |
| **D-04** | Does the metadata "backfill" button keep re-enqueueing done files? | No — `eligible` becomes `NOT done ∧ NOT in_flight`; rework `is_domain_completed`'s metadata branch alongside |
| **D-05** | Fix the 6 latent bugs (§4.1) in this milestone or split them out? | Fix 1/3/4/7 here (they *are* the enum's removal); 2/5/6 are one-line guards that fall out of the same rework |

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Blast radius: 23 source files, ~50 test files reference `FileState` | Phase the work: derivation layer first (additive, no deletions), then readers, then writers, then the drop |
| Anti-join pending queries slow at 200K files | Partial indexes sized to the predicates; measure the `/pipeline/stats` poll and record the number in VERIFICATION |
| Backfill snapshots mid-flight cloud pushes | Drain cloud-push lanes before the destructive migration; two-step migration with a shadow-compare gate between |
| `saq_jobs` coupling | Static SQL + SAVEPOINT + degrade-to-safe-default only; never from Alembic |
| Re-introducing the analyze auto-retry loop (44.5K-job over-enqueue incident) | `FAILURE_IS_TERMINAL[analyze] = true`, with a regression test asserting a failed analyze is **not** in the analyze pending set |
| Silent behavior change in metadata's pending set (D-04) | Explicit requirement + test; call out in the phase summary |
