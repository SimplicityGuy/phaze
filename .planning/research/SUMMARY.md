# Project Research Summary

**Project:** phaze — 2026.7.5 Parallel Enrich DAG
**Domain:** Internal refactor of a live, distributed batch pipeline — retire a stored linear status enum in favor of per-file, per-stage status *derived* from output tables + `saq_jobs` + new per-stage failure markers, so the three enrich stages (metadata / fingerprint / analyze) become genuinely parallel instead of accidentally serialized.
**Researched:** 2026-07-08
**Confidence:** HIGH overall, with one explicit open disagreement (D-01, below) and one explicitly unmeasured risk (5s poll latency at 200K scale) flagged for planning.

This document supersedes the prior SUMMARY.md (2026.7.1 Multi-Cloud Backends). Nothing here concerns `backends.toml`, cloud in-flight counting, or REG-*/MKUE-* — that milestone is shipped and archived.

---

## Executive Summary

phaze's `FileRecord.state` (a 17-member `StrEnum`) is a scalar trying to represent a fact that is structurally a *set*: whether metadata, fingerprint, and analyze — three independent, unordered stages — are each done. A scalar can't hold three simultaneous truths, so writers clobber each other and readers gate on whichever value won last. The approved design (`PARALLEL-ENRICH-DAG-DESIGN.md`) replaces the stored enum with a pure function `stage_status(file, stage) -> {not_started | in_flight | done | failed}`, computed from existing output tables, `saq_jobs`, and two new failure-marker columns. All four researchers converge on the same core implementation shape and independently surface the same set of sharp edges — this summary exists to make those edges visible before planning starts.

The single most consequential technical decision is a **single-source-of-truth predicate module**: plain module-level functions returning `ColumnElement[bool]` (not `column_property`, not `hybrid_property`, not `query_expression`), composed into both SQL `case()`/`exists()` and a pure Python precedence resolver, locked together by a SQL-vs-Python equivalence test. Stack, Architecture, and Pitfalls research independently arrived at this same shape from different angles (SQLAlchemy idiom fit, existing service-module conventions, and anti-drift discipline, respectively) — treat it as settled. Equally settled: the anti-join must be written as `NOT EXISTS`, never `LEFT JOIN ... WHERE <predicate> IS NULL` (silently wrong on the three 1:N/partial-row enrich tables) and never `NOT IN` (a measured >170s planner cliff). The database is **PostgreSQL 18.4**, not 16 as the milestone brief assumed — verified live; the PG16 anti-join planner improvement is present and active in 18, but every EXPLAIN idiom and BUFFERS-default behavior cited in planning should be sourced against 18, not 16.

The main risk is not technical performance (that's solvable with partial indexes, `IS NOT NULL`-shaped predicates, and a measured poll budget) — it is **silent regression at the concurrency and completeness boundary**. The enum was quietly doing three jobs: a status people read, a mutual-exclusion invariant, and a compare-and-swap concurrency token defending against at-least-once, out-of-order agent callbacks. Deleting it deletes 23 CAS guards unless each is explicitly replaced. Two stages (`metadata`, and to a lesser extent `fingerprint`) are about to acquire failure visibility for the first time — if the design ships the failure *marker* without a failure *reader and retry affordance*, terminally-failed metadata files become invisible-and-permanently-ineligible, which is a worse regression than the deadlock this milestone fixes. And there is one genuine, unresolved disagreement between two of the four researchers on where `in_flight` should read from (D-01, below) that must be decided explicitly during planning, not defaulted silently in code.

---

## Key Findings

### Recommended Stack

Zero new dependencies. This is entirely about using the existing stack's capabilities correctly: PostgreSQL 18.4 (not 16 — corrected, load-bearing for planner-behavior citations), SQLAlchemy 2.0.51, Alembic 1.18.4, SAQ 0.26.4 on Postgres.

**Core findings:**
- **`NOT EXISTS` (`~exists().where(...)`) is the only correct anti-join form.** Measured on a live 200K-file PG18.4 corpus: `NOT EXISTS` and a *correctly-written* `LEFT JOIN ... ON pred ... WHERE pk IS NULL` tie in speed (both compile to `Hash Anti Join`), but `LEFT JOIN` with the predicate in `WHERE` instead of `ON` silently under-counted the 1:N `fingerprint_results` set by 1,507 rows. `NOT IN (subquery)` is disqualified outright — cost estimate 418M, degraded to `Materialize` + linear rescan, canceled after ~3 minutes; also NULL-unsafe. Three of this milestone's stages are 1:N or partial-row, so this is not a style preference.
- **Predicate module, not `column_property`/`hybrid_property`/`query_expression`.** Plain functions `-> ColumnElement[bool]` compose cleanly into `.where()`, `case()`, and cross-table `saq_jobs`/`scheduling_ledger` predicates that don't belong to any one mapped entity. Verified strict-mypy and ruff clean against the real models.
- **Partial indexes: sparse failure markers pay off (144 kB–912 kB, planner uses them); dense "done" predicates (60–95% of rows) are ignored by the whole-corpus count but earn keep on single-file/paginated reads.** `INCLUDE`/covering indexes are unnecessary — a plain `(file_id) WHERE marker` already gives `Heap Fetches: 0`.
- **`GROUP BY (CASE WHEN EXISTS ...)`** — tempting as a drop-in for the retiring `GROUP BY state` — measured **~30x slower** (792ms, forced seq scan + JIT) than per-stage anti-join counts. Do not use it.
- **`EXISTS` in a `SELECT` projection list over an un-materialized page** plans as a whole-table hashed subplan (1875 buffers for 50 rows); wrapping the page in a `MATERIALIZED` CTE first drops it to 0.9ms. This governs how the per-file stage matrix must be built.
- **Two-step migration (`032` additive → shadow-compare gate → `033` destructive)** is the mandated shape; Stack details the concrete DDL, the bound-param backfill discipline, and — critically — that `033.downgrade()` cannot be byte-faithful (derivation is strictly more informative than the scalar it replaces) and must be documented as reversible-with-lossiness, not pretended lossless.

### Expected Features

Researched against Airflow, Dagster, Prefect, and Temporal — the four production systems that solve this exact problem (per-item status in a long-running batch/DAG). **The core finding: all four store or derive status, but none of them serve UI reads by deriving over the whole corpus.** Dagster caches per-partition bitmaps; Temporal maintains a wholly separate Visibility store. phaze's derive-don't-denormalize choice is validated for the *source of truth* — but only if the *read path* stays aggregate-first. Dagster's documented ceiling (≤100K partitions/asset, conservatively ≤25K, with "reporting per-partition status" named as the bottleneck) matters directly: phaze is at 200K files × 3 stages = 600K cells, 6–24x past that ceiling for a single asset.

**Must have (table stakes) — the model is unobservable/regressive without these:**
- Four-bucket per-stage counts (`not_started/in_flight/done/failed`) that sum to total — a free self-consistency check on the precedence rule.
- Failure visible for **all three** enrich stages, not just analyze (a bare marker without a reader repeats the exact latent bug it fixes).
- Manual retry for **both** terminal-failed stages (analyze and metadata) — the design adds `FAILURE_IS_TERMINAL[metadata]=true` with no retry endpoint named anywhere in the design. This is the single sharpest new hazard in the approved design (see Gap Register below).
- Queued/running/parked display split — a paused stage's files render as "running" indefinitely otherwise (the design documents this and doesn't act on it).
- Per-file stage matrix (paginated, never corpus-wide) replacing the raw-enum "State" column the design deletes the data source for.
- Corrected `done` predicates (analyze completion timestamp, fingerprint any-engine-success) — the design already commits to this; it's the visible half of latent bug 7.
- Eligible-count shown before a bulk trigger fires — cheapest guard against a repeat of the 44.5K-job incident class.

**High-value, nearly-free differentiator:**
- **A per-file "why is this file not eligible?" trace.** `eligible(f, stage)` is already a pure 4-conjunct predicate in the design; rendering which conjunct is false *is* the explainer, and it is the exact tool that would have surfaced the current deadlock in seconds instead of a release cycle. Airflow ships this natively (`tasks failed-deps`, "Blocked Task Instance Explainer"); Dagster's evaluation history does the equivalent. API form is nearly free — ship it in this milestone.

**Anti-features (each has a named tool that forbids or regrets it):**
- Rendering the raw internal status string in the UI (Prefect explicitly separates machine `type` from display `name` for exactly this reason).
- Treating "no output row" as unqualified "not started" — Airflow materializes an explicit `none` row for exactly the reason phaze needs it: a worker dying mid-job must not read identically to "never scheduled."
- Auto-retrying terminal failures — this is literally the 44.5K-job incident; non-retryability must be encoded at the shared predicate, not a per-query convention.
- A whole-corpus per-file bitmap on the 5s poll (Dagster's named ceiling, 6-24x exceeded here).
- A global unscoped "retry everything failed" button.

**Defer explicitly (with written consequence, not silent omission):** skip/mark-done for permanently-corrupt files (without it, a terminal `metadata` failure can never satisfy `propose`'s upstream and accumulates permanently); force-re-run of a done stage; bulk ledger-scoped retry; an aggregate read-model cache (only if a measurement shows the direct-query poll is slow — Dagster's `AssetStatusCacheValue` is the sanctioned precedent for *that* shape specifically, never a per-file column).

### Architecture Approach

The derivation layer is a **new leaf service module** (`src/phaze/services/stage_status.py`) plus a **new DB-free enum module** (`src/phaze/enums/stage.py`, mirroring the existing `enums/execution.py` precedent), built on a `StageSpec` registry per stage carrying `done_exists`/`failed_exists` predicate factories, the SAQ function-name key prefix, `failure_is_terminal`, and `upstream`. Every consumer — pending-set SQL, aggregate-count SQL, per-file UI status, and recovery's `is_domain_completed` — composes the *same* predicate objects; nothing re-derives.

**Major components:**
1. **`enums/stage.py`** (DB-free, agent-safe) — `Stage` and `StageStatus` StrEnums. Must stay importable from the Postgres-free agent worker boundary.
2. **`services/stage_status.py`** (control-only leaf) — the `StageSpec` registry, predicate factories, the pure `resolve_status()`/`eligible()` precedence resolver, and the SQL builders (`pending_stmt`, `done_count_stmt`, `domain_completed_stmt`, `bulk_stage_status`).
3. **`in_flight` is never joined into SQL.** It is a separate, already-existing, SAVEPOINT-wrapped, degrade-safe `saq_jobs` key-set read (`get_live_job_keys`), subtracted in Python. Joining it would drag an Alembic-invisible table into the ORM anti-join and — critically — **invert the degrade direction from safe (over-inclusive) to unsafe (under-inclusive, silently no-ops a trigger button)**.
4. **Two new sidecar tables**: `analyze_route(file_id PK, route)` for the `AWAITING_CLOUD`/`LOCAL_ANALYZING` routing decision (recommended over widening `cloud_job.status`, which is the substrate for five independent cap/recovery/admission predicates), and `dedup_resolution(file_id PK, canonical_file_id)`.

### Critical Pitfalls

1. **`in_flight = saq_jobs ∪ scheduling_ledger` (the design's tentative D-01) creates a permanent, silent stall — see the Open Decision below; do not adopt the union as-written.**
2. **The enum is a concurrency primitive, not just a status.** 23 `WHERE state == X` CAS guards defend against at-least-once, out-of-order agent callbacks (e.g. `UPDATE files SET state=PUSHED WHERE id=? AND state=PUSHING`). Deleting the column deletes the guards unless each is explicitly replaced (mostly by moving the CAS onto `cloud_job.status`, which already has a uniqueness constraint). This is the largest gap in the approved design's own call-site inventory, which lists these sites as "writers" — inviting deletion, not replacement.
3. **"Row exists ⇒ done" has a second-order form nobody enumerated.** The known first-order trap (an `analysis` row is upserted partial at analysis *start*) has a sibling: the moment `metadata` gains a `failed_at` marker, a failure *inserts* a metadata row with NULL payload columns — every other consumer of "has a metadata row" (the duration router, the cloud-backfill candidate query, proposal convergence) must tighten simultaneously or a metadata failure silently routes a possibly-4-hour file to local analysis via a NULL duration.
4. **`fingerprint_results` is 1:N per engine, and `done ≻ failed` precedence silently kills the auto-retry the design claims to preserve.** Today a failed engine retries because `FINGERPRINTED` (the only competing state) is written by almost nothing. Under naive `done = any engine succeeded` + `eligible = ¬done`, a file with one succeeded engine and one failed engine becomes permanently ineligible — the failed engine never retries. Must be modeled as per-engine coverage, not a collapsed scalar.
5. **Anti-join partial indexes will not be used by the whole-corpus count they were sized for** (a hash anti-join over two seq scans is cheaper at 200K rows — this is fine, expected, and the design's own §6 flags it) but **parameterized predicates can never match a partial index at all** (PG docs, verified), and phaze reaches Postgres through PgBouncer in *session mode* with long-lived prepared statements — a footgun invisible in CI (fresh connection, one execution, always a custom plan) that only manifests in prod after ~5 executions on a pinned connection. The fix is structural: shape `done` predicates as `IS NOT NULL` timestamp discriminators (un-parameterizable, always index-eligible), not `status IN (...)` binds.
6. **Live-migration ordering: a two-step migration only protects you if the intermediate deploy dual-writes, not merely dual-presents the column.** `files.state` is `NOT NULL` with no server default; if writers stop before the column drops, ingestion breaks. If the column stays but readers flip while writers *also* stop, `state` freezes stale and a rollback resumes reading garbage — "more destructive than the failure it's rolling back from." A flag-guarded dual-write (`_LEGACY_STATE_WRITES_ENABLED`) is the only ordering safe in both directions.

---

## Cross-Cutting Findings (all four researchers, independently)

**1. Database version correction.** The live container is **PostgreSQL 18.4**, not 16 as the milestone brief and design assumed (`docker-compose.yml`, `justfile`, live `SELECT version()`). The PG16 RIGHT/OUTER anti-join planner improvement the design's framing attributes to PG16 is present and active in 18 — so the anti-join symmetry claims hold — but EXPLAIN idioms (BUFFERS is on by default with ANALYZE in 18) and any future planner-behavior citation should be sourced against 18.4.

**2. `NOT EXISTS` is the correct anti-join form, unconditionally.** Measured: `LEFT JOIN ... WHERE <predicate> IS NULL` (predicate in WHERE instead of ON) silently under-counted the 1:N `fingerprint_results` set by 1,507 rows out of 51,507 — no error, wrong answer. `NOT IN` is a >170s planner cliff (418M cost estimate, `work_mem` spill, NULL-unsafe). Three of the milestone's enrich stages are 1:N or partial-row, making this an active footgun, not a style question.

**3. Single-source-of-truth predicate module.** All three technical researchers (Stack, Architecture, Pitfalls) converged independently on the same shape: module-level functions returning `ColumnElement[bool]`, composed into both the SQL `case()`/pending-set/count queries and a pure Python precedence resolver, with the precedence order encoded once as an ordered list both sides consume, and locked together by a SQL-vs-Python equivalence test over a fixture matrix (Pitfalls recommends a full 32-case table-driven oracle over the five underlying booleans). Treat this as the settled architecture, not an open question.

**4. Open disagreement on D-01 (`in_flight` source) — NOT resolved, needs an explicit planning decision.**

- **The design's tentative recommendation (also Stack's read of it):** `in_flight = saq_jobs(queued|active) ∪ scheduling_ledger`, on the rationale that `saq_jobs` rows vanish on completion but the ledger survives worker crashes, closing the crash window recovery already trusts across restarts.
- **Architecture's position: REJECT the union.** Grounded in five specific code citations: (a) the broker is Postgres — `saq_jobs(queued|active)` already survives worker/controller death since the Phase 36 Redis→Postgres migration (`reenqueue.py:11-18` explicitly documents this and warns against "restoring" logic that assumes otherwise); (b) nothing enqueues on a steady-state poll, so the "re-enqueued by every poll" hazard the union is meant to prevent doesn't exist here; (c) the ledger clear is callback-driven and **not present at all on the agent worker** (no `ledger_sessionmaker` — Postgres-free by construction), so a hard-killed agent process leaves a ledger row that survives *until an operator manually clicks Recover* — under the union, that file reads `in_flight` forever, is permanently ineligible, and renders "running" in every UI surface forever — architecture calls this failure mode "mechanical," not theoretical; (d) `in_flight ≻ done` precedence makes it worse — a stale ledger row can mask a genuinely completed stage; (e) the union creates a two-owner disagreement with `recover_orphaned_work`'s own orphan predicate (`ledger − live − domain_completed`), which was written specifically to guard against double-ownership incidents (SCHED-05).
- **Pitfalls independently reaches the same critical-severity verdict** (its Pitfall #1), citing the exact same in-tree docstring (`_backfill_candidates_stmt`, `pipeline.py:1287`) admitting orphaned ledger rows are *normal*, and proposes a middle path if the union is kept anyway: bound it with an `enqueued_at`-based staleness grace window, and change the *display* precedence to `done ≻ in_flight` (while keeping `in_flight`'s veto only inside `eligible()`, where it's redundant with `done` anyway).
- **This is a genuine three-way split** (design leans union; Architecture says saq_jobs-alone with a separate `scheduled_not_terminal` diagnostic; Pitfalls says union-with-grace-bound-and-flipped-display-precedence) and must be decided explicitly during planning — do not let it default silently in whichever phase touches it first. Architecture's position is the most structurally argued (falsifies each clause of the design's own rationale against specific line numbers) and Pitfalls independently corroborates the severity, which is meaningful convergence toward rejecting the naked union — but the final call belongs to planning, not this summary.

**5. Feature gaps the approved design omits — candidate requirements, not yet scoped in.**
- **G-01 (CRITICAL):** the design adds a `metadata` failure marker with `FAILURE_IS_TERMINAL[metadata]=true` and names **no retry endpoint and no reader anywhere in §7**. Under the new model, a terminally-failed metadata extraction becomes invisible-nowhere-but-eligible-never — it can never satisfy `propose`'s `done(metadata) ∧ done(analyze)` upstream and sits stranded for the corpus's lifetime. `retry_analysis_failed` is the exact precedent to generalize; low complexity, same endpoint shape.
- **G-04 (high value / low cost):** no "why is this file not eligible?" trace, despite `eligible()` already being a pure 4-conjunct predicate — and this is the exact instrument that would have caught the current deadlock in seconds instead of a release cycle. Best value-to-cost item across all four documents; ship the API form in this milestone.
- Also flagged (lower severity, explicit scope-in/defer decisions needed): no way to re-run a done stage (the design's own bug-3 fix removes rescan as the only accidental re-analyze path and replaces it with nothing); no skip/mark-done for permanently-corrupt files (permanent residue in the `failed` bucket otherwise); paused-stage files rendering as "running" for weeks; no orphaned-work surface (the D-01 union, if adopted, creates this signal for free and the design never names it); no jitter/backoff on the auto-retryable fingerprint stage.

**6. Load-bearing constraints — non-negotiable across all four documents:**
- A failed **analyze** must stay terminal — retry is manual-only via the existing button. `tasks/reenqueue.py:179-186` carries an explicit in-tree warning against re-introducing auto-retry here; this is the exact mechanism of the 44.5K-job over-enqueue incident. `FAILURE_IS_TERMINAL[analyze]=true` must be enforced at the *shared* eligibility predicate every caller (including any future `reenqueue.py` rewrite) consumes — not duplicated as a per-query clause a future edit can silently drop.
- The `/pipeline/stats` 5s poll must never 500. Every new `saq_jobs`-touching read needs the existing SAVEPOINT + degrade-to-safe-default idiom — and post-milestone, "safe default" must mean *fail closed on eligibility, fail open (render `—`) on display*, because these probes now drive enqueue decisions, not just cosmetics. Degrade-to-zero on a decision probe silently recreates the incident's causal shape in a new location.
- **Never reference `saq_jobs` from Alembic.** Every migration since `020` carries this banner; `032`/`033` must too.
- Two-step migration: `032` additive (schema + backfill + dual-write) → a **standing, re-runnable shadow-compare gate** (run once post-backfill, again before the destructive step — not a one-shot pre-drop script) → `033` destructive, with cloud-push lanes quiesced (`--profile drain`) before `033` to avoid snapshotting `PUSHING`/`uploading` rows mid-flight.
- **Zero new dependencies** across all four research documents' recommendations.

---

## Implications for Roadmap

Based on the architecture research's recommended build order (§12.2, itself a correction of the design's own §11 sequence) and the pitfalls' phase vocabulary, the milestone decomposes into roughly this sequence. Treat phase boundaries as a starting point for `/gsd:plan-phase`, not a final cut.

### Phase 1: Additive schema + rescan-wipe fix (migration 032)
**Rationale:** Schema must exist before predicates can reference it — the derivation layer cannot precede the failure-marker columns. This phase is purely additive, ships with no reader-visible behavior change, and is safely rollback-able at every step.
**Delivers:** `analysis.analysis_failed_at`/`error_message`, `metadata.failed_at`/`error_message`, `analyze_route` table, `dedup_resolution` table, `cloud_job.status` += `'pushed'`, 5 partial indexes (`IS NOT NULL`-shaped, never `status IN (...)`-shaped, per the parameterized-predicate footgun), mutual-exclusion CHECK constraints, idempotent backfill from `files.state`, `ANALYZE` after backfill. Also: the two-line deletion of `"state": excluded.state` from the rescan upserts (bug 3) — independently shippable, no dependency on anything else.
**Avoids:** Pitfall 7 (parameterized predicates can never match a partial index — shape predicates as `IS NOT NULL` from the start), Pitfall 8's server-default gap (`files.state` has no server default; a later writer omission would break ingestion mid-rollout unless one is added here).

### Phase 2: Derivation layer + anti-drift test harness (concurrent DERIV + TEST)
**Rationale:** Pitfalls' strongest process finding: TEST must run *concurrently with* DERIV, not after — the oracle and fact-writing fixture builder are prerequisites for trusting every subsequent reader cutover, not a cleanup step.
**Delivers:** `enums/stage.py` (DB-free), `services/stage_status.py` (`StageSpec` registry, predicate factories, `resolve_status()`/`eligible()`), the totality/anti-drift test suite, a table-driven oracle asserting SQL and Python agree across the full boolean combination space, and a fact-writing fixture builder (never one that calls the derivation layer itself — that would make tests tautological).
**Research flag:** **D-01 must be resolved here, explicitly, before this phase's predicates are written** — this is where the disagreement lands in code. Do not let whichever engineer picks up this phase default to the design's tentative union without a documented decision.

### Phase 3: Shadow-compare gate (run for real, on the live corpus)
**Rationale:** The gate's entire value is comparing two live representations — it must run immediately after the Phase 1 backfill (does the historical corpus derive correctly?) and remain re-runnable, not a one-shot script executed once at the end.
**Delivers:** A committed, runnable implication-checker (assert implication, not equality — derivation is deliberately more informative than the scalar) with a closed, declared divergence list (`FINGERPRINTED` is the one documented, expected divergence; anything else is a hard fail).
**Must pass before any reader cutover proceeds.**

### Phase 4: Recovery cutover (reenqueue.py) — first reader, deliberately
**Rationale:** `is_domain_completed`'s metadata/fingerprint branches are today defined as "absent from the pending set" — a definition that only stays correct if recovery flips *before* the pending sets do (flipping pending sets first makes recovery's correctness silently depend on the new `in_flight` term through a double negation).
**Delivers:** `domain_completed_stmt` sourced from the Phase 2 registry, replacing `_select_done_analyze_ids`/`_select_done_push_ids`/`_build_done_sets`. **The `FAILURE_IS_TERMINAL[analyze]` regression test ships in this same PR, test-first** — this is the 44.5K-job incident's exact seam.

### Phase 5: Counts + pending sets cutover — where the deadlock actually dissolves
**Rationale:** Everything before this phase is scaffolding; this is the milestone's thesis made executable.
**Delivers:** The three enrich pending sets rewritten with zero upstream (every discovered file eligible for all three stages, in any order); `get_stage_progress`/`get_pipeline_stats` collapsed into per-stage anti-join counts; **TS-1** (four-bucket counts), **TS-10** (corrected done predicates), **TS-2** (failure visibility for metadata + fingerprint, not just analyze — closes G-01), **TS-3** (manual retry for both terminal stages), **D-1** (per-file eligibility trace, API form).
**Avoids:** Pitfall 1 (whichever D-01 resolution Phase 2 landed on gets its regression test here), Pitfall 4 (metadata's `done` predicate must tighten to `failed_at IS NULL` the same day the failure marker's reader ships, or the second-order "row exists but is a failure" trap reopens in a new table), Pitfall 5 (fingerprint must be per-engine coverage, not a collapsed scalar, or the D-16 auto-retry silently breaks).

### Phase 6: Cloud routing sidecars (D4 — one atomic plan)
**Rationale:** The drain-candidate query, the three `Backend.dispatch` route flips, and the CAS-guard collapse onto `cloud_job` are one consistency domain — landing them separately creates a double-dispatch or re-pick window (Architecture §12.3 R3).
**Delivers:** `analyze_route` wired into `LocalBackend`/`ComputeAgentBackend`/`KueueBackend` dispatch; the `PUSHED` status + `NOT_TERMINAL = IN_FLIGHT ∪ {PUSHED}` set (keeping `IN_FLIGHT` itself unchanged, preserving the compute cap's existing semantics exactly); the two-row CAS guards on `/pushed`/`/mismatch`/`report_upload_failed` collapsing to one-row `cloud_job` CASes (fixes latent bug 6 by construction).
**Avoids:** Pitfall 3's `LOCAL_ANALYZING` double-dispatch risk if `in_flight(analyze)` ever degrades to `false` on a `saq_jobs` read error.

### Phase 7: Dedup + fingerprint-progress cutover
**Rationale:** Lower blast radius than Phase 6; can trail it or run in parallel once Phase 5 lands.
**Delivers:** `services/dedup.py`'s 9 exclusion filters → `dedup_resolution` marker (undo becomes a plain DELETE, `previous_state` payload leaves the router contract); `get_fingerprint_progress` rewritten off the per-engine coverage predicate from Phase 5.

### Phase 8: Presentation cutover (UI)
**Rationale:** Presentation-only, but the template layer silently hides breakage today (Jinja `Undefined` renders blank, nothing 500s) — this needs `StrictUndefined` and a grep guard, not just a template edit.
**Delivers:** Per-file stage matrix (**TS-5**, paginated only — never corpus-wide, per Dagster's documented ≤25K-100K/asset ceiling against phaze's 600K cells), status chips replacing the raw enum column, **TS-4** (queued/running/parked display split), **TS-6**/**TS-7** (failure reason + last-activity timestamps on the per-file record), **TS-8** (eligible-count shown before bulk triggers fire), **D-3** (orphaned-work count, if Phase 2's D-01 resolution creates that signal).

### Phase 9: EXECUTED-gate revival — own phase, own PR, live UAT
**Rationale:** This is the one **behavior-reviving** change in the milestone, not a refactor. `tag_writer.py`, `review.py`, `tags.py`, `cue.py`, `tracklists.py` (14 sites total) all gate on `state == EXECUTED`, a value nothing in `src/` has ever written — so these gates have been permanently dead. Replacing them with the derived `apply`-done predicate turns tag/CUE writing on for the first time, across 200K files, on a filesystem-mutating surface.
**Delivers:** The `applied(f)` predicate (joining `execution_log` through `proposals`, since `execution_log` has no `file_id`) wired into all 14 sites, with an explicit test asserting the behavior change rather than discovering it via diff review.
**Must not be bundled with any other phase.**

### Phase 10: Proposals cutover
**Delivers:** `_TERMINAL_FILE_STATES` and the file-state cascade in `proposal.py`/`proposal_queries.py`/`agent_proposals.py` deleted (bug 5, the `MOVED`/`UNCHANGED` omission, evaporates rather than needing a fix); `proposals.status` becomes the sole authority.

### Phase 11: Destructive migration (033) + writer removal
**Rationale:** Gated, last, and highest-risk after the migration itself — writer removal must be the *last* step, same phase as the column drop, never earlier (Architecture's R1: "readers before writers, always").
**Delivers:** In one transaction: archive `files.state` to `files_state_archive` (the only rollback net — the enum is unreconstructable afterward), a delta backfill for anything that changed since `032`, `DROP INDEX ix_files_state`, `DROP COLUMN files.state`, deletion of `FileState` and the 20 `.state=` writer statements, with `lock_timeout` set so the `ACCESS EXCLUSIVE` lock aborts-and-retries rather than queuing behind the 5s poll and taking the whole site down.
**Gate:** shadow-compare (Phase 3) green on the live corpus + cloud-push lanes drained (`--profile drain`) + the 4h-analyze quiesce handled (in-flight callbacks must be schema-compatible across the drop, via the flag-guarded dual-write discipline established in Phase 1).

### Phase Ordering Rationale

- **Schema before predicates before gate before readers before writers** is not a preference — Architecture's R1/R2 derive it directly from in-tree code (`reenqueue.py:266`'s definition of domain-completion structurally depends on this ordering) and Pitfalls' Pitfall 8 shows the reverse ordering is actively catastrophic on rollback, not merely suboptimal.
- **Recovery flips before pending sets** (Phase 4 before Phase 5) specifically to avoid a hidden double-negation dependency that would otherwise make recovery's correctness an accident of ordering.
- **Cloud routing (Phase 6) is deliberately one atomic phase**, not spread across the milestone, because its three pieces (candidate query, dispatch flips, CAS collapse) are one consistency domain with a documented double-dispatch window if split.
- **The EXECUTED-gate revival (Phase 9) is isolated on purpose** — it's the only phase in this milestone that changes filesystem behavior rather than refactoring status representation, and mixing it into a "just deleting an enum" phase would hide a live-UAT-worthy change inside a refactor PR.
- **The destructive migration is last and its own phase** because it is irreversible in practice (the archive table is the only net) and gated on every prior phase's shadow-compare having stayed green.

### Research Flags

Needs deeper research during planning (`--research-phase`):
- **Phase 2 (derivation layer):** D-01 must be resolved here with an explicit decision record, not inherited silently — this is a genuine three-way disagreement across the source documents (see Cross-Cutting Finding #4), and Architecture's rejection of the union is the most code-grounded argument but is not unanimous.
- **Phase 6 (cloud routing sidecars) and Phase 11 (destructive migration):** flagged by Architecture as needing "phase-level research" independently — Phase 6 for the `PUSHED`/drain-re-pick hazard (Architecture rates its own confidence here MEDIUM-HIGH, inferred rather than directly tested, and recommends a live/integration test before committing the drain-candidate query); Phase 11 for a full rehearsal against a `pg_dump` restore of the real corpus (and note: `saq_jobs` is SAQ-owned and absent from a plain schema dump — a rehearsal without it will make the shadow-compare look cleaner than reality).
- **Phase 5 (the 5s poll under the new query shape):** 200K-scale poll latency is explicitly **unmeasured** (Architecture rates this LOW confidence) despite being the design's own Risks-table requirement to measure and record. This phase's plan should include the measurement as a first-class deliverable, with the folded-single-round-trip shape (six anti-joins measured at ~263ms separately vs ~140ms folded) as the concrete technique if the naive approach is slow.

Phases with standard, well-documented patterns (research-phase likely unnecessary):
- **Phase 1 (additive schema):** direct precedent in-tree (migrations 012/014/018/019/031) for partial indexes, sync migrations, mirrored downgrades.
- **Phase 3 (shadow-compare):** a bounded, mechanical implication-check script; no novel technology.
- **Phase 8 (UI):** presentation-only, existing template conventions (`straggler_failed_card.html`, `_diff_row.html`'s `|tojson` precedent) directly reusable.
- **Phase 10 (proposals cutover):** a deletion, not new logic — `proposals.status` is already authoritative today.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Every planner claim measured live against the project's actual PG18.4 container with a 200K-row synthetic corpus mirroring the real schema; SQLAlchemy/Alembic APIs verified via Context7 against installed versions. |
| Features | HIGH for Airflow/Prefect/Temporal (official docs, Context7-verified); MEDIUM for Dagster internals (docs + maintainer discussions/source-derived summaries, not primary API docs for the caching internals specifically). |
| Architecture | HIGH for module placement, predicate shape, `saq_jobs` decoupling, and the D-01 rejection (every claim traced to a file:line in the actual tree); MEDIUM-HIGH for the `analyze_route`/`PUSHED` drain-re-pick hazard (inferred from code, not yet integration-tested); explicitly LOW for 200K-scale poll latency (unmeasured, flagged as a required measurement). |
| Pitfalls | HIGH for DB/planner/SAQ findings (empirically reproduced against the project's own PG18.4 + installed SAQ source) and codebase claims (read from `main` @ `ce0c6434`); the union-vs-alone severity assessment (Pitfall 1) independently corroborates Architecture's D-01 rejection from a different angle (a documented in-tree docstring about orphaned ledger rows being "normal"), which is meaningful convergence. |

**Overall confidence:** HIGH, with two explicit gaps carried forward to planning rather than papered over.

### Gaps to Address

- **D-01 (`in_flight` source) is unresolved across the source documents** — the design leans union, Architecture rejects it outright, Pitfalls proposes a bounded-union middle path. Resolve explicitly in Phase 2's plan, with a written decision record, before any predicate depending on it is coded.
- **200K-scale poll latency is unmeasured.** The design's own Risks table requires a measurement recorded in VERIFICATION; treat this as a blocking deliverable of Phase 5, not an optional nice-to-have.
- **The `AWAITING_CLOUD`/`PUSHED`-drain-candidacy interaction (Architecture's D-03 follow-on) is inferred, not integration-tested.** Architecture explicitly flags this as "the sharpest new-regression risk in the milestone" and recommends a live/integration test before committing the drain-candidate query in Phase 6.
- **Feature gaps G-01 through G-11 are candidate requirements the approved design doesn't scope in or explicitly defer.** Requirements definition should force an explicit scope-in/defer decision on each (especially G-01, which is CRITICAL severity — a new permanent-stranding class for metadata failures — and G-04, which is the highest value-to-cost item across all four documents). Do not let these fall through silently between design approval and requirements.
- **The vacuous-test-suite risk (Pitfalls #11) is a process gap, not a technical one.** ~50 test files / 201 call sites construct `FileRecord(state=...)` as their entire test setup; the project has already shipped one vacuous-pass regression once (Phase 75 WR-01). Requirements/planning should budget for a mutation-harness deliverable (`just mutate-derivation`) on the derivation module specifically, not assume coverage percentage alone catches this class.

## Sources

### Primary (HIGH confidence)
- Live measurement — PostgreSQL 18.4 (`phaze-test-db`, `postgres:18-alpine`), 200K-file synthetic corpus mirroring the real schema; all EXPLAIN ANALYZE plans, row-count equivalence, and index-usage numbers in STACK.md and PITFALLS.md.
- Direct source read of `SimplicityGuy/true-parallel` @ `ce0c6434` — every architectural and pitfall claim traced to file:line (`services/pipeline.py`, `tasks/reenqueue.py`, `tasks/_shared/deterministic_key.py`, `routers/agent_{metadata,fingerprint,analysis,push,s3}.py`, `models/*`, `alembic/versions/*`, templates).
- Installed SAQ source (`saq/queue/postgres.py::_enqueue`, `saq/queue/base.py::enqueue`, `saq/job.py::Status`) — the dedup-guard and status-enum findings underlying Pitfall 9.
- [PostgreSQL 16 release notes](https://www.postgresql.org/about/news/postgresql-16-released-2715/) + [pganalyze: PG16 anti-joins](https://pganalyze.com/blog/5mins-postgres-16-faster-query-plans) — RIGHT/OUTER anti-join planner improvement.
- [PostgreSQL 18 release notes](https://www.postgresql.org/docs/current/release-18.html) — EXPLAIN BUFFERS default-on.
- [PostgreSQL — Partial Indexes](https://www.postgresql.org/docs/current/indexes-partial.html) — parameterized predicates cannot match a partial index (verified, load-bearing).
- Context7 `/websites/sqlalchemy_en_20_orm` & `_core` — `case()`, `exists()`, `hybrid_property`, `column_property`, `query_expression` semantics.
- [Airflow — Tasks / CLI reference / apache/airflow#1729](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/tasks.html) — task-instance states, the Blocked Task Instance Explainer precedent.
- [Prefect — States](https://docs.prefect.io/v3/concepts/states) — name/type split doctrine.
- [Temporal — Visibility](https://docs.temporal.io/visibility) / [Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies) — separate read-model precedent, non-retryable-overrides-policy.

### Secondary (MEDIUM confidence)
- [Dagster — Partitioning assets](https://docs.dagster.io/guides/build/partitions-and-backfills/partitioning-assets) + issue threads (#21581, #19802, #10330, #14988, #13280) — the ≤25K-100K partition ceiling and `AssetStatusCacheValue` internals (well-supported at the existence level; field-level detail is third-party-sourced).

### Project incidents cited and generalized (in-tree, HIGH confidence as historical fact)
- 2026-06-18 ~44,500-job over-enqueue incident (`tasks/reenqueue.py` module docstring).
- PR #189 — `fingerprint.done` always 0 (allowlist-vs-enum-you-don't-own class of bug).
- Phase 75 WR-01 — vacuous-pass regression test.
- PgBouncer session-pool exhaustion (`database.py:26-42`, PR #221).
- Phase 73 CR-01 (blocker) — missing CAS guard on `/mismatch`.

---
*Research completed: 2026-07-08*
*Ready for roadmap: yes — with D-01 flagged for an explicit planning decision and 200K-scale poll latency flagged as a required Phase 5 measurement.*
