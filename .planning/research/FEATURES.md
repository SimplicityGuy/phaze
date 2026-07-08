# Feature Research

**Domain:** Per-item, per-stage workflow status in a long-running (months-scale) batch/DAG pipeline
**Milestone:** 2026.7.5 Parallel Enrich DAG (retire linear `FileState`)
**Researched:** 2026-07-08
**Confidence:** HIGH on Airflow / Prefect / Temporal semantics (official docs). MEDIUM on Dagster internals (docs + maintainer discussions + source-derived summaries).

> **Scope note for the requirements author.** The core model in `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` is APPROVED and is not re-litigated here. This document answers: *given that model, what must the operator be able to see and do?* Every item below is implementable on FastAPI + Jinja2 + HTMX + Alpine + Postgres + SAQ — **zero new runtime dependencies**. The **Design-Contract Gap Register** (§below) names the features the approved design does not cover; each needs an explicit scope-in or defer decision.

---

## 0. Executive answer to the six questions

**Q1 — Do mature tools store, derive, or event-log per-item state?**
All four store *something*. **None of them derive per-item status on the read path at scale.**

| Tool | Source of truth | Read-path representation | Verdict for phaze |
|---|---|---|---|
| **Airflow** | Stored enum. One `task_instance` row per `(dag_run, task_id, map_index)` with a `state` column | Same row. The Grid view reads the enum directly | Denormalized *by design* |
| **Dagster** | **Event log** (append-only: `ASSET_MATERIALIZATION`, `ASSET_MATERIALIZATION_PLANNED`, `ASSET_FAILED_TO_MATERIALIZE`) | **Derived, then cached.** `AssetRecord`/`AssetEntry` caches `last_materialization_record`; partitioned assets get an `AssetStatusCacheValue` holding *serialized partition subsets* (materialized / failed / in-progress) precisely so the UI does not re-derive from the event log ([storage internals](https://deepwiki.com/dagster-io/dagster/5-storage-and-persistence), [issue #14988](https://github.com/dagster-io/dagster/issues/14988)) | Derive is the truth; **cache the aggregate** |
| **Prefect** | Stored `State` object with `type` + `name`; transitions gated by orchestration rules | `flow_run`/`task_run` carry a denormalized `state_type` | Stored, with a human/machine split |
| **Temporal** | **Pure event history** (append-only, replayed) | **Never queried for listing.** A separate **Visibility store** is the denormalized projection powering list/filter/UI ([Visibility docs](https://docs.temporal.io/visibility)) | Derive is the truth; **separate read model** |

**So: phaze's DERIVED choice is validated — but only for the *source of truth*, and only if the *read path* is aggregate-first.**
The two event-sourced tools (Dagster, Temporal) both refused to serve UI reads from the log. Dagster caches per-partition bitmaps; Temporal maintains an entirely separate store. The design's §5 "derive, don't denormalize (yet)" is **correct about the per-file bitmap column** and **under-specified about the aggregate read path** (see A4 and G-09).

**Q2 — Table stakes vs differentiators.** See the Feature Landscape. Short version: the operator must see `not_started / in_flight / done / failed` **counts per stage that sum to the total**, and must be able to *clear a failure* (Airflow's `clear`), *force a re-run of a done item* (Airflow's `clear` on a success), and *see what is stuck* (Temporal's pending-activity `attempt` + `lastFailure`). The design contract delivers the model for all three and the UI/affordances for **none** of them.

**Q3 — "Explain eligibility" is table stakes at the API/CLI layer and a differentiator in the UI.** Airflow ships a first-class explainer: `airflow tasks failed-deps`, whose originating PR is titled *"Task Dependency Engine + Blocked Task Instance Explainer"* ([apache/airflow#1729](https://github.com/apache/airflow/pull/1729/files)), described as returning *"the unmet dependencies for a task instance from the perspective of the scheduler … why a task instance doesn't get scheduled"* ([CLI ref](https://airflow.apache.org/docs/apache-airflow/stable/cli-and-env-variables-ref.html)). Dagster does the same for automation: evaluation history shows *"why an asset was or wasn't materialized"*, with a `skip_message` rendered in the UI ([Declarative Automation](https://docs.dagster.io/guides/automate/declarative-automation)). Airflow additionally *materializes cheap reasons as states* — `upstream_failed`, `skipped`, and `none` = *"The Task has not yet been queued for execution (its dependencies are not yet met)"* ([Tasks](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/tasks.html)) — and reserves the explainer for the expensive ones.
For phaze this is unusually cheap: the design already makes `eligible(f, stage)` a **pure conjunction of four predicates**. Evaluating them for one file and rendering which conjunct is false *is* the explainer. It is also the exact tool that would have surfaced the current deadlock (design §1.1) in seconds instead of a release cycle.

**Q4 — Auto-retryable vs terminal has a settled vocabulary. Adopt it; don't invent one.**

| Tool | Retryable-failed | Terminal-failed | Mechanism |
|---|---|---|---|
| Airflow | `up_for_retry` — *"The task failed, but has retry attempts left and will be rescheduled"* | `failed` | `retries` count; `retries=0` ⇒ straight to `failed` ([Tasks](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/tasks.html)) |
| Prefect | **`AwaitingRetry`** (name), type `SCHEDULED`, **non-terminal** | `Failed` / `TimedOut`, type `FAILED`, **terminal** | *"State types drive orchestration logic, whereas state names provide visual bookkeeping"* ([States](https://docs.prefect.io/v3/concepts/states)) |
| Temporal | pending activity carrying `attempt: N` + `lastFailure` | `nonRetryableErrorTypes`, `ApplicationError(non_retryable=True)`, or `maximumAttempts: 1` | *"When an error is marked non-retryable in code, it overrides policy settings — it will never retry regardless of configuration"* ([Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies)) |

**Prescription for phaze:** `FAILURE_IS_TERMINAL[stage]` is exactly Temporal's `nonRetryableErrorTypes` and is the right shape. Render it with Prefect's name/type split:
- `fingerprint` failed → display **"Awaiting retry"** (non-terminal; the pending set reclaims it)
- `analyze` / `metadata` failed → display **"Failed — manual retry"** (terminal; needs an operator action)

The load-bearing lesson from Temporal is that non-retryability must be **encoded at the source predicate**, not enforced only inside one pending-set query. `FAILURE_IS_TERMINAL[analyze] = true` must be asserted at the shared eligibility function that `reenqueue.py` also calls — the 44.5K over-enqueue incident was a *second* query forgetting the rule.

**Q5 — Anti-features.** See §Anti-Features: raw internal state strings in the UI (Prefect's name/type doctrine exists to prevent this), conflating "no output row" with "not started" (Airflow deliberately materializes a `none` row; Temporal never infers), retry storms without jitter, and corpus-scanning polls.

**Q6 — Scale.** Dagster is the named cautionary tale: it *recommends limiting each asset to ≤100,000 partitions (conservatively ≤25,000)* or UI load time degrades, and the specific slow part is **"reporting the materialization status of each partition"** ([partitioning docs](https://docs.dagster.io/guides/build/partitions-and-backfills/partitioning-assets), [#21581](https://github.com/dagster-io/dagster/discussions/21581), [#19802](https://github.com/dagster-io/dagster/discussions/19802), [#10330](https://github.com/dagster-io/dagster/issues/10330)). **phaze has 200K files × 3 enrich stages = 600K cells — 6–24× past Dagster's conservative ceiling for a single asset.** A per-file bitmap must never be computed for the whole corpus on the 5s `/pipeline/stats` poll. Aggregate counts + paginated drill-down is the only shape that survives.

---

## Feature Landscape

Complexity is rated against phaze's actual surface. The **Deps** column names the *existing* phaze feature each item leans on.

### Table Stakes (the milestone reads as broken without these)

| # | Feature | Why Expected | Complexity | Deps on existing phaze | Notes |
|---|---|---|---|---|---|
| **TS-1** | **Four-bucket per-stage counts** — `not_started / in_flight / done / failed`, per enrich stage | Every named tool exposes exactly this: Airflow's Grid legend, Dagster's partition health bar (`Materialized / Failed / Missing / In progress`), Prefect's run-state counts. Verified: `get_stage_progress` (`services/pipeline.py:299`) returns only `{done, total}`; `get_stage_busy_counts` (`:466`) returns only busy. There is **no** `failed` and **no** `not_started` anywhere | **LOW** | DAG rail poll context (`routers/pipeline.py:189-240`); `get_stage_progress` | Extend the existing per-node dict to `{done, total, in_flight, failed, not_started}`. `not_started = total − done − in_flight − failed` is a **free self-consistency check** — if it goes negative the precedence rule (`in_flight ≻ done ≻ failed ≻ not_started`) is broken. Rides the single existing 5s poll; no new loop. |
| **TS-2** | **Failure visible for all three enrich stages** — a count and an openable list | The milestone's own §4.1(4) calls invisible metadata failures a latent bug. Adding a `failed_at` column no operator can read *repeats that bug in a new table*. Airflow surfaces `failed` in the Grid; Dagster added `ASSET_FAILED_TO_MATERIALIZE` for exactly this | **LOW** | `straggler_failed_card.html` + `get_analysis_failed_count` (`pipeline.py:1068`) are the template — generalize to 3 stages | Analyze already has a count card + list. Metadata and fingerprint have neither. |
| **TS-3** | **Manual retry for every terminal-failed stage** (`analyze` **and** `metadata`) | Airflow's `clear`: *"Clearing a task instance … resets the state to None, prompting a re-run."* A terminal state with no operator escape hatch is a permanent leak | **LOW** | `retry_analysis_failed` (`routers/pipeline.py:883`) is the exact precedent; stage triggers | **SEE G-01 — the milestone's sharpest new hazard.** Under the new model retry = *clear the failure marker* (null `failed_at`) → file drops to `not_started` → eligible. Strictly cleaner than today's `ANALYSIS_FAILED → FINGERPRINTED` roll-back hack (`routers/pipeline.py:935`). |
| **TS-4** | **In-flight labels distinguish `queued` / `active` / `parked (stage paused)`** | Prefect ships a dedicated non-terminal state name for precisely this: **`AwaitingConcurrencySlot`** (type `SCHEDULED`). Airflow separates `queued` from `running`. Design §2.2 concedes parked jobs (`scheduled = SENTINEL`) read as `in_flight` — so a paused stage will show every parked file as "running" for weeks | **LOW** | `stage_control.SENTINEL` (`tasks/_shared/stage_control.py:65`); `dag["<stage>Paused"]` is **already in the poll context** (`routers/pipeline.py:189`) | Presentation only. Do **not** change `in_flight` semantics — the design is right that parked = in-flight for eligibility. Split only the *rendered label*. Prefect's name-vs-type doctrine applied verbatim. |
| **TS-5** | **Per-file stage matrix** — one row per file, one cell per stage, replacing the raw-enum "State" column | Airflow's Grid view is *the* canonical artifact of this domain. The design mandates deleting the enum that the current column renders (`metadata_workspace.html:43,50`) and never says what replaces it | **MEDIUM** | Per-file record slide-in (v7.0 Phase 61, RECORD-01); existing paginated file tables | **Paginated only** — never a 200K-row bitmap (A4). Cells are `stage_status()` for the visible page in one query. |
| **TS-6** | **Failure reason text on the per-file record** | Temporal's `describe` surfaces `lastFailure` on pending activities ([Activity Operations](https://docs.temporal.io/activity-operations)); Airflow surfaces the log + `try_number` | **LOW** | D-02's proposed `error_message` column | The design adds the column and names no reader. |
| **TS-7** | **Last-activity timestamp per stage on the per-file record** | Months-long grind — "when did this file last move?" is the first question asked. Airflow TIs carry `start_date`/`end_date`/`duration` | **LOW** | Data already exists: `metadata.created_at`, `analysis.analysis_completed_at`, `fingerprint_results.updated_at` | Zero new columns. |
| **TS-8** | **Trigger buttons state their blast radius before firing** — "Enqueue N eligible" | Airflow's clear / mark-success flows show a confirmation enumerating the affected task instances before mutating. D-04 silently changes the metadata ALL trigger from "everything, dedup'd" to "eligible only" — the operator must see the new number | **LOW** | Stage ALL triggers (v7.0 WORK-02); the number *is* TS-1's eligible count | Also the cheapest structural guard against a repeat of the 44.5K incident class: a bulk action that names its count before running. |
| **TS-9** | **Bulk "retry all failed \<stage\>" — ledger-scoped and count-confirmed** | Airflow: `airflow tasks clear --task-regex … --start-date … --end-date …` — always scoped, never global | **MEDIUM** | `retry_analysis_failed` (bulk, analyze-only today); scheduling ledger (v5.0 ledger-scoped backfill precedent) | Must be scoped exactly like the v5.0 ledger-scoped backfill. Unscoped bulk retry over 200K files at 4h/analyze *is* the incident. |
| **TS-10** | **Corrected `done` predicates** — analyze on `analysis_completed_at IS NOT NULL`; fingerprint on any-engine-success | Progress bars that lie are worse than absent ones. §4.1(7) documents the analyze over-count | **LOW** | `get_stage_progress` (already the reader) | Already in the design contract; listed because it is the *visible* half of the fix. |

### Differentiators (real value; each has a named precedent)

| # | Feature | Value Proposition | Complexity | Deps on existing phaze | Notes |
|---|---|---|---|---|---|
| **D-1** | **"Why is this file not eligible?" — a per-file eligibility trace** | Airflow's `tasks failed-deps` / *"Blocked Task Instance Explainer"* ([PR #1729](https://github.com/apache/airflow/pull/1729/files)); Dagster's `skip_message` in evaluation history. **This is the feature whose absence hid the current deadlock (design §1.1) for an entire release cycle.** | **LOW–MEDIUM** | `eligible(f, stage)` is already a pure 4-conjunct predicate in the design | Render the conjuncts as a per-stage checklist: `✓ not done · ✗ in-flight (job queued 3d ago) · ✓ not terminal-failed · ✓ upstream met`. **Single-file scoped — no corpus scan.** Highest value-to-cost ratio in this document. Ship the API form first (`GET /files/{id}/eligibility`); the UI panel is additive. |
| **D-2** | **Stage-level eligibility explainer** — *"0 eligible: 190K done · 8K in-flight · 2K terminal-failed"* | The aggregate form of D-1. Answers "why is nothing happening?" — the most common operator question in a months-long grind | **LOW** | Identical to TS-1's four buckets; free once TS-1 lands | Renders as each stage workspace's empty state. Had phaze shown this, "no file can complete all three enrich stages" would have been visible on the dashboard. |
| **D-3** | **Orphaned / stuck-work surface** — files with a `scheduling_ledger` row but **no** `saq_jobs` row | This class is currently *invisible*, and `recover_orphaned_work` silently "fixes" it — once by enqueueing 44,500 jobs. Temporal surfaces pending-activity `attempt` + `lastFailure` for exactly this. Making the recovery cron's input set observable **before** it runs is the durable safety rail | **LOW** | Scheduling ledger; recovery cron (`tasks/reenqueue.py`); D-01 resolved as the union | The union *creates* this signal for free: `in_flight_by_ledger ∧ ¬in_flight_by_saq` = orphaned. Show count + age. Gate the recovery cron's manual trigger behind the same count. |
| **D-4** | **Force re-run of a `done` stage (per file)** | Airflow's `clear` works on `success` task instances — re-running a done unit is first-class, not an edge case | **MEDIUM** | Per-file record; `enqueue_router` | **SEE G-02.** §4.1(3) correctly kills rescan-clobbers-progress — which removes phaze's *only* (accidental) re-analyze mechanism. Needs `DELETE analysis WHERE file_id=…` (mind the `analysis_windows` FK) + enqueue. A 4-hour re-analyze deserves an explicit confirm. |
| **D-5** | **Skip / mark-done a stage (per file)** | Airflow: `Mark Success` + the `skipped` state. Dagster: `report_asset_materialization` for externally-satisfied assets | **MEDIUM** | Failure markers; propose's upstream gate | **SEE G-03.** A permanently-corrupt file with terminal `metadata` failure can *never* satisfy propose's `done(metadata) ∧ done(analyze)` upstream. Without skip it sits in the "not done" bucket for the life of the corpus. |
| **D-6** | **Filter the file table by stage-status tuple** — e.g. `analyze=failed ∧ metadata=done` | Airflow: `/dags/{id}/tasks?state=failed`. Dagster: partition filter by status | **MEDIUM** | Unified search (v3.0 Phase 18); ⌘K palette (v7.0 RECORD-02); the same partial indexes the pending anti-joins need | Rides indexes the milestone must build anyway. Cheap once TS-5 lands. |
| **D-7** | **Retry / attempt count per file-stage** | Airflow keeps `try_number` across clears; Temporal keeps `attempt` in mutable state | **MEDIUM** | Existing `push_attempt` ledger column (2026.7.2 HARD-02) is the in-tree precedent | D-02's single-row failure marker is destroyed on retry, losing "this file has failed analyze 4 times." One `failure_count` integer on the same row buys most of the value for nearly nothing, and prevents a human-driven retry loop on a file that will never succeed. |
| **D-8** | **Cached aggregate counts (a rebuildable read model) — measurement-gated** | **The named precedent for phaze's exact problem.** Dagster's `AssetStatusCacheValue` caches serialized materialized/failed/in-progress partition subsets; Temporal's Visibility store is a separate denormalized projection. Both keep the log/table as truth and treat the projection as disposable | **MEDIUM** | Controller cron; the existing degrade-to-safe-default idiom (`pipeline.py:466-497`) | **Does not violate §5's YAGNI.** §5 forbids a *per-file bitmap column*; this is an *aggregate* cache (≈12 integers), refreshed by the controller worker, safe to be seconds-stale, never authoritative. Zero new deps (a `stage_counts` table, or a Redis key). Ship only behind a measurement, exactly as §5 demands. |
| **D-9** | **A permanent per-stage sum invariant** (`done + in_flight + failed + not_started == total`) | The design's shadow-compare gate dies with `files.state` at migration `033`. This is its successor: an always-on assertion that the precedence rule is honored | **LOW** | TS-1 | Log a warning (never 500 the poll) on violation; surface on an ops page. |

### Anti-Features (tempting; each has a named tool that forbids or regrets it)

| # | Anti-Feature | Why Requested | Why Problematic | Alternative |
|---|---|---|---|---|
| **A1** | **Render the internal status enum verbatim in the UI** (which is what `metadata_workspace.html:43,50` does today with the raw `FileState` string) | Zero effort; the value is right there | Prefect explicitly separates the two: *"State types drive orchestration logic, whereas state names provide visual bookkeeping."* Their `Retrying` (type `RUNNING`) and `AwaitingRetry` (type `SCHEDULED`) exist *only* because the machine value is the wrong thing to show a human. `in_flight` alone cannot tell an operator whether a file is running, queued behind 40K others, or **parked because the operator paused the stage last Tuesday** | Four machine values (`stage_status`), a larger set of display names. Minimum split: `running` / `queued` / `parked (paused)` / `done` / `failed — awaiting retry` / `failed — manual retry` / `not started`. The design already knows about the parked case (§2.2) and does not act on it. |
| **A2** | **Treat "no output row" as "not started"** — unqualified (design §2.4) | It's the elegant derivation | Airflow deliberately materializes a `task_instance` row whose state is `none` = *"the Task has not yet been queued for execution (its dependencies are not yet met)"* — an *absent* row means the task **does not exist** (`removed`), not that it hasn't run. Temporal never infers; it replays. **In phaze the conflation is concrete:** worker dies mid-`process_file` → no `saq_jobs` row, no `analysis` row → the file reads `not_started`, is eligible, and the next trigger re-enqueues it. That is precisely the crash window the `scheduling_ledger` was introduced to close | **Resolve D-01 as the union:** `in_flight = saq_jobs(queued|active) ∪ scheduling_ledger`. Then `ledger ∧ ¬saq_jobs` is not a hole — it is D-3's orphan signal, an *asset*. Choosing `saq_jobs` alone reopens the hole *and* deletes the signal. |
| **A3** | **Auto-retry the terminal stages** ("failed just means try again") | `FAILURE_IS_TERMINAL` looks like an arbitrary asymmetry between fingerprint and analyze | This is the 44,500-job over-enqueue incident; `tasks/reenqueue.py:179-186` already carries the in-tree warning. Temporal's stance: a non-retryable error *"overrides policy settings — it will never retry regardless of configuration."* Airflow's `retries=0` goes straight to `failed`, skipping `up_for_retry` entirely | Encode non-retryability **at the shared predicate**, not as a UI convention or a per-query `WHERE` clause, and regression-test that a failed analyze is absent from the analyze pending set. Retryable failures (fingerprint) additionally need backoff **+ jitter** — Airflow ships `retry_exponential_backoff` + `max_retry_delay`; Prefect ships `retry_jitter_factor`; Temporal defaults `backoffCoefficient: 2.0`. Without jitter, N simultaneous fingerprint failures re-enter the pending set in lockstep on the next poll. |
| **A4** | **A whole-corpus per-file stage bitmap on the 5s poll** (or a 200K-row matrix page) | "The DAG should show every file's status" | **Dagster's documented ceiling is ≤100,000 partitions per asset — conservatively ≤25,000 — and the named bottleneck is "reporting the materialization status of each partition"** ([partitioning docs](https://docs.dagster.io/guides/build/partitions-and-backfills/partitioning-assets), [#19802](https://github.com/dagster-io/dagster/discussions/19802), [#10330](https://github.com/dagster-io/dagster/issues/10330)). phaze is at 200K × 3 = 600K cells. Airflow bounds its Grid to a window of recent DAG runs for the same reason | Aggregate-first: four indexed `COUNT(*)` per stage on the poll (TS-1); per-file cells only in paginated drill-down (TS-5) and the single-file record (D-1). Keep the existing `visibilitychange` poll shed (v7.0 Phase 58). If it still measures slow → D-8's aggregate cache, never a per-file column. |
| **A5** | **A denormalized per-file `stage_bitmap` column as the authority** | Fast reads | Design §5 already forbids it. Field reinforcement: Dagster and Temporal both denormalize, but **never the source of truth** — Dagster's `AssetStatusCacheValue` is invalidated and rebuilt, and has a documented history of going wrong across code-location reloads and partition-definition changes ([#13280](https://github.com/dagster-io/dagster/issues/13280)); Temporal's Visibility store is explicitly *not* the Event History. A second writable authority for a fact the output tables already own is the linear-enum bug wearing a new hat | If you denormalize, denormalize the **aggregate**, mark it disposable, and rebuild it from the tables (D-8). |
| **A6** | **A global "retry everything failed" / "mark all done" button** | One click clears the backlog | Airflow gates `clear` and `Mark Success` behind a confirmation enumerating affected task instances and scopes them by task regex + date range. An unscoped retry across 200K files at 4h/analyze is the 44.5K incident restated | Scoped + counted + ledger-bounded (TS-9); show the count first (TS-8). Never offer a corpus-wide mark-success on `analyze` — it would admit files with no analysis data into `propose`. |
| **A7** | **A new generic `stage_failure` / "workflow status" table as the derivation source** | Feels tidy and symmetric | Adds a second write path, a new FK, and breaks the ≤1-row-per-file invariant that makes the partial indexes trivial. Design D-02 already recommends against it | Nullable `failed_at` + `error_message` (+ D-7's `failure_count`) on the existing per-stage output tables. Tighten `done(metadata)` to `EXISTS metadata WHERE file_id=… AND failed_at IS NULL`, exactly as D-02 says. |
| **A8** | **Real-time push (WebSocket/SSE) for stage status** | "It's a live DAG" | Zero-new-deps constraint; and the existing single 5s `/pipeline/stats` poll with visibility shedding is already the right shape for a months-long batch job where nothing meaningful changes inside a 5s window | Keep the one poll. Fan out from it, as v7.0 Phase 58 established. |
| **A9** | **Silently changing the metadata ALL trigger's semantics (D-04)** | It's strictly better (no more 200K enqueue-and-dedup churn per click) | An operator trained on "the metadata button re-enqueues everything" now has a button that no-ops on a corpus that still looks unfinished. Airflow's answer to this class is the confirmation page | Ship D-04 **with** TS-8 (the eligible count on the button) and a one-line runbook note. Rework `is_domain_completed`'s metadata branch in the same change (design §3 already flags this). |

---

## Feature Dependencies

```
TS-1 (four-bucket counts)
    ├──requires──> failure markers (design §2.3)
    ├──requires──> in_flight predicate (design §2.2 + D-01)
    └──enables───> D-2 (stage eligibility explainer)
                   D-9 (sum invariant)
                   TS-8 (blast-radius count on triggers)
                   D-8 (aggregate cache — only if TS-1 measures slow)

TS-2 (failure visible)
    ├──requires──> failure markers
    └──requires──> TS-1

TS-3 (manual retry per terminal stage)          <-- CRITICAL, see G-01
    ├──requires──> failure markers
    └──requires──> TS-2   (you cannot retry what you cannot see)
                   └──enables──> TS-9 (bulk retry)
                                 └──requires──> scheduling ledger

TS-4 (queued/running/parked labels)
    └──requires──> stage_control.SENTINEL + dag["<stage>Paused"]   [both already exist]

TS-5 (per-file stage matrix)
    ├──requires──> stage_status() as a per-FILE function (not only a set query)
    ├──requires──> partial indexes (design §5)
    └──enables───> D-1 (eligibility trace)
                   D-4 (force re-run)
                   D-5 (skip / mark done)
                   D-6 (filter by status tuple)

D-1 (eligibility trace)
    └──requires──> eligible() exposed as a per-file, per-CONJUNCT evaluation

D-3 (orphan surface)
    ├──requires──> D-01 resolved as saq_jobs ∪ scheduling_ledger
    └──enhances──> recovery cron (makes its input set observable pre-run)

D-4 (force re-run)  ──conflicts──> §4.1(3) "rescan cannot clobber progress"
    (the fix removes the only existing re-run path; D-4 restores it deliberately)

D-5 (skip)  ──required-by──> propose eligibility for permanently-corrupt files
    (propose upstream = done(metadata) ∧ done(analyze); a terminal metadata
     failure is neither done nor ever-eligible → propose is unreachable forever)

A3 (no auto-retry for analyze)  ──constrains──> reenqueue.py rewrite
    (must be structurally prevented at the shared predicate, not conventionally avoided)

A4 (no corpus bitmap)  ──conflicts──> a naive TS-5 rendered for all 200K files
```

### Dependency Notes

- **TS-3 requires TS-2:** the design adds `failed_at` to `metadata` with `FAILURE_IS_TERMINAL[metadata]=true`. Today a metadata failure is invisible *but the file stays eligible* (the pending set is literally every music/video file, `pipeline.py:1330`). After the change it becomes **visible-nowhere and eligible-never**. Shipping the marker without the reader and the retry makes the milestone a *regression* for metadata.
- **D-1 requires `eligible()` to be per-file, not only set-returning:** if `eligible` exists only as a `NOT EXISTS` anti-join inside `get_*_pending_files`, the explainer cannot be built. Define the predicate once in a form both a single-file evaluation and a set query can consume. This is a *design-layer* requirement with a UI payoff — cheap now, expensive to retrofit.
- **D-8 must be gated on measurement:** §5 explicitly requires "a measurement in the phase's verification doc." Honor it. Dagster built their cache because they measured; do the same, and pre-authorize the *shape* so a slow measurement doesn't reopen the §5 YAGNI debate mid-phase.
- **A3 constrains the `reenqueue.py` rewrite:** the milestone heavily touches `reenqueue.py` (design §7) — the file where the 44.5K incident lived. `FAILURE_IS_TERMINAL[analyze]` must be enforced at the shared eligibility predicate `reenqueue` calls, not duplicated as a separate clause a future edit can drop.
- **TS-5 conflicts with A4 unless paginated:** the matrix is table stakes; the *corpus-wide* matrix is an anti-feature. Same feature, different scope.

---

## Design-Contract Gap Register

Features the **approved design does not cover**. Each needs an explicit scope-in or defer decision. Ordered by severity.

| ID | Gap | Severity | Evidence | Recommendation |
|---|---|---|---|---|
| **G-01** | **A terminally-failed `metadata` has no retry affordance and no reader.** §2.3 adds the marker; §3 sets `FAILURE_IS_TERMINAL[metadata]=true`; §7 lists no UI and no endpoint. `retry_analysis_failed` covers analyze only | **CRITICAL — creates a new permanent-stranding class** | Design §2.3, §3 table; `routers/pipeline.py:883` | **Scope in** (TS-2 + TS-3). Retry = null the failure marker (Airflow `clear`). LOW complexity: same endpoint shape as `retry_analysis_failed`, minus the state roll-back hack. |
| **G-02** | **No way to re-run a `done` stage.** §4.1(3) celebrates that "a rescan physically cannot clobber progress" — correct, but rescan-clobber was the *only* re-analyze path that existed | **HIGH — capability removed, not replaced** | Design §4.1(3); `services/ingestion.py:114` | **Scope in** as D-4 (per-file, confirm-gated). Mind the `analysis_windows` FK on delete. |
| **G-03** | **No skip / mark-done.** A permanently-corrupt file with a terminal `metadata` failure can never satisfy propose's `done(metadata) ∧ done(analyze)` upstream — it sits in "not done" for the life of the corpus and permanently distorts every count | **HIGH** | Design §3 upstream table | **Scope in** as D-5, **or explicitly defer** with a written consequence (a permanent, growing residue in TS-1's `failed` bucket that never converges). Airflow's `Mark Success` is the precedent. |
| **G-04** | **No "why is this file not eligible?" trace**, despite `eligible()` being a pure 4-conjunct predicate. The milestone exists *because* nobody could see why analyze was stranded | **HIGH value / LOW cost** | Design §3; [airflow#1729](https://github.com/apache/airflow/pull/1729/files) | **Scope in** as D-1 (API form at minimum). Best value-to-cost item in this document. |
| **G-05** | **Paused-stage files will render as "running" indefinitely.** §2.2 documents that parked jobs (`scheduled = SENTINEL`) count as `in_flight` and says "preserve it" — correct for eligibility, misleading as a label | **MEDIUM — the UI lies for weeks at a time** | Design §2.2; `stage_control.py:65`; `dag["<stage>Paused"]` already in the poll context | **Scope in** as TS-4. Presentation-only, LOW. |
| **G-06** | **`get_stage_progress` gains no `failed` / `in_flight` / `not_started` buckets.** §7 says `get_pipeline_stats` "collapses into `get_stage_progress`" — but that function returns only `{done, total}` (verified at `services/pipeline.py:299`), which structurally cannot express the failure the milestone is adding | **MEDIUM** | `services/pipeline.py:299` | **Scope in** as TS-1. The four buckets summing to `total` is also the post-`033` successor to the shadow-compare gate (D-9). |
| **G-07** | **No `failure_count` / attempt history.** D-02's single-row marker is destroyed on each retry | **LOW–MEDIUM** | Design D-02; Airflow `try_number`; Temporal `attempt` | One integer column on the same row (D-7). Add the column with the marker (free); surface later. |
| **G-08** | **No jitter/backoff requirement for the auto-retryable stage.** `FAILURE_IS_TERMINAL[fingerprint]=false` means N simultaneous failures re-enter the pending set together | **LOW–MEDIUM** | Design §3 table; Airflow `retry_exponential_backoff`+`max_retry_delay`; Temporal `backoffCoefficient: 2.0` | Verify SAQ's retry backoff covers the in-job path. If the *pending-set re-eligibility* is the loop (not SAQ retry), add a `failed_at + interval` cooldown conjunct to fingerprint's eligibility. |
| **G-09** | **§5 addresses index cost for the pending anti-joins but not the read cost of the four status counts on the 5s poll over 200K rows.** Dagster's documented ceiling for exactly this operation is 25K–100K items | **MEDIUM** | Design §5; [Dagster partitioning docs](https://docs.dagster.io/guides/build/partitions-and-backfills/partitioning-assets) | Add a measurement requirement for the *counts*, not only the pending queries. Pre-authorize D-8 (aggregate cache, **not** a per-file column) as the sanctioned remediation. |
| **G-10** | **No orphan surface.** D-01's recommended union creates the `ledger ∧ ¬saq_jobs` signal for free, and the design never names it | **MEDIUM (safety)** | Design D-01; the 44.5K incident | **Scope in** as D-3, at minimum as a count. It is the observability the recovery cron has never had. |
| **G-11** | **Trigger buttons don't state their new blast radius** after D-04's semantic change | **LOW** | Design §3 "Behavior change to call out" | TS-8. Same number as TS-1's eligible count — no new query. |

---

## MVP Definition

### Launch With (this milestone)

- [ ] **TS-1** four-bucket per-stage counts on the existing 5s poll — *the milestone's model is unobservable without it; also the sum-check*
- [ ] **TS-2** failure counts + lists for metadata and fingerprint — *otherwise the new markers repeat the exact bug they fix (§4.1.4)*
- [ ] **TS-3** manual retry (clear-marker) for terminal-failed `analyze` **and** `metadata` — *G-01; prevents a new permanent-stranding class*
- [ ] **TS-4** `queued` / `running` / `parked` display split — *G-05; LOW cost; prevents a months-long lie*
- [ ] **TS-5** per-file stage matrix replacing the raw-enum "State" column — *forced: the design deletes the column's data source*
- [ ] **TS-6** failure reason on the per-file record — *D-02 adds `error_message`; give it a reader*
- [ ] **TS-8** eligible-count on the ALL triggers — *D-04's semantic change made visible; cheapest incident guard*
- [ ] **TS-10** corrected `done` predicates (analyze completion, fingerprint any-engine-success)
- [ ] **D-1** per-file eligibility trace, **API form at minimum** — *G-04; the diagnostic that would have caught §1.1*
- [ ] **D-3** orphaned-work count (`ledger ∧ ¬saq_jobs`) — *G-10; free once D-01 resolves as the union*

### Add After Validation (next release)

- [ ] **TS-7** last-activity timestamps per stage — *data exists; add when the per-file record is next touched*
- [ ] **TS-9** bulk retry-all-failed per stage, ledger-scoped + count-confirmed — *only after single-file retry has run against the live corpus*
- [ ] **D-2** stage-level eligibility explainer as the workspace empty state — *free after TS-1*
- [ ] **D-4** force re-run of a done stage — *G-02; needs the `analysis_windows` cascade worked out*
- [ ] **D-9** permanent sum invariant — *the post-`033` successor to the shadow-compare gate*
- [ ] **D-7** `failure_count` — *add the column with the failure marker (free); surface later*

### Future Consideration / Explicit Defer

- [ ] **D-5** skip / mark-done — *G-03. Defer only with a written acknowledgement that terminally-failed files accumulate permanently and never reach `propose`.*
- [ ] **D-6** filter by stage-status tuple — *rides indexes built this milestone; purely additive UI*
- [ ] **D-8** cached aggregate counts — *gated on a slow measurement, per §5. Pre-approved shape: aggregate cache (Dagster's `AssetStatusCacheValue` pattern), never a per-file bitmap column (§5 / A5).*
- [ ] **D-1 UI panel** (the API form ships in MVP) — *slide-in on the per-file record*

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---|---|---|---|
| TS-1 four-bucket counts | HIGH | LOW | **P1** |
| TS-2 failure visibility (3 stages) | HIGH | LOW | **P1** |
| TS-3 manual retry for terminal stages | HIGH | LOW | **P1** |
| TS-10 corrected `done` predicates | HIGH | LOW | **P1** |
| D-1 eligibility trace (API) | HIGH | LOW–MED | **P1** |
| TS-5 per-file stage matrix (paginated) | HIGH | MEDIUM | **P1** (forced: the enum column dies) |
| TS-4 queued/running/parked labels | MEDIUM | LOW | **P1** |
| TS-8 eligible-count on triggers | MEDIUM | LOW | **P1** |
| D-3 orphaned-work count | MEDIUM (HIGH safety) | LOW | **P1** |
| TS-6 failure reason text | MEDIUM | LOW | **P1** |
| TS-7 last-activity timestamps | MEDIUM | LOW | P2 |
| TS-9 bulk retry (ledger-scoped) | MEDIUM | MEDIUM | P2 |
| D-2 stage eligibility explainer | MEDIUM | LOW | P2 |
| D-9 sum invariant | MEDIUM (safety) | LOW | P2 |
| D-7 failure_count | LOW–MED | LOW | P2 (column now, UI later) |
| D-4 force re-run done stage | MEDIUM | MEDIUM | P2 |
| D-5 skip / mark-done | MEDIUM | MEDIUM | P3 (decide explicitly) |
| D-6 status-tuple filter | LOW–MED | MEDIUM | P3 |
| D-8 aggregate cache | LOW (until slow) | MEDIUM | P3 (measurement-gated) |

---

## Competitor Feature Analysis

| Capability | Airflow | Dagster | Prefect | Temporal | phaze's plan |
|---|---|---|---|---|---|
| **Per-item state source** | Stored enum on `task_instance` | Event log (`ASSET_MATERIALIZATION`, `..._PLANNED`, `ASSET_FAILED_TO_MATERIALIZE`) | Stored `State` (type + name) + orchestration rules | Event history (replayed) | **Derived from output tables** ✓ closest to Dagster/Temporal |
| **UI read path** | The enum itself | `AssetStatusCacheValue` (cached partition subsets) | Denormalized `state_type` on the run row | **Visibility store** (separate denormalized projection) | Direct queries + partial indexes; **no cache** — accept, but pre-authorize D-8 (G-09) |
| **"Not started"** | Explicit `none` state on a real row: *"dependencies are not yet met"* | "Missing" (no materialization event) | `Scheduled` / `Pending` | (n/a) | Absence of output row **∧** absence of ledger row (D-01 union). Without the union this conflates crashed-mid-job with never-started (A2). |
| **Auto-retryable failed** | `up_for_retry` | `RetryPolicy(backoff, jitter)` | `AwaitingRetry` (SCHEDULED, non-terminal) | pending activity, `attempt: N` | `fingerprint`: `FAILURE_IS_TERMINAL=false` ✓ — add jitter/cooldown (G-08) |
| **Terminal failed** | `failed` (via `retries=0`) | `ASSET_FAILED_TO_MATERIALIZE` | `Failed` (FAILED, terminal) | `nonRetryableErrorTypes` / `ApplicationError(non_retryable=True)` — *overrides policy* | `analyze`/`metadata`: `FAILURE_IS_TERMINAL=true` ✓ — enforce at the shared predicate, not per-query (A3) |
| **Blocked / no-slot** | `queued` vs `running` | in-progress subset | **`AwaitingConcurrencySlot`** | task-queue backlog | Parked (`scheduled=SENTINEL`) reads `in_flight` — **relabel** (TS-4 / G-05) |
| **Retry one item** | `clear` (state → None) | Re-materialize partition | `Retrying` | Reset / retry activity | **Null the failure marker** — identical to Airflow `clear`, cleaner than today's roll-back hack |
| **Re-run a *done* item** | `clear` on `success` | Re-materialize | Re-run | Reset workflow | **Missing** (G-02) |
| **Skip / mark done** | `Mark Success`, `skipped` state | `report_asset_materialization` | force-set `Completed` | — | **Missing** (G-03) |
| **Explain "not eligible"** | `airflow tasks failed-deps`; Task Instance Details; `upstream_failed`/`skipped` as materialized reasons | Evaluation history + `skip_message` | orchestration-rule rejection reason | — | **Missing** (G-04) — and nearly free |
| **Bulk retry scoping** | `clear --task-regex --start-date --end-date` + confirmation | Backfill dialog | Deployment run filter | Batch ops with a query | `retry_analysis_failed` (analyze only); ledger-scoped backfill precedent |
| **Scale ceiling** | Bounded Grid window; configurable auto-refresh | **≤100K partitions/asset, conservatively ≤25K** | — | Visibility store + pagination | 200K × 3 = 600K cells → **aggregate-first mandatory** (A4) |
| **Human-in-the-loop** | `awaiting_input` state | — | `Paused` / `Suspended` | Signals | phaze's `review` — the design correctly calls it *availability, not a queue* |

---

## Confidence Assessment

| Claim | Confidence | Basis |
|---|---|---|
| Airflow stores per-TI state as an enum; `none` = "dependencies not yet met"; `up_for_retry` vs `failed`; `clear` resets to None | **HIGH** | [Tasks](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/tasks.html); [DAG Runs](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dag-run.rst) (via Context7 `/apache/airflow`) |
| `airflow tasks failed-deps` exists and explains why a TI isn't scheduled | **HIGH** | [CLI ref](https://airflow.apache.org/docs/apache-airflow/stable/cli-and-env-variables-ref.html); [PR #1729 "Blocked Task Instance Explainer"](https://github.com/apache/airflow/pull/1729/files) |
| Prefect's state name vs type split; `AwaitingRetry` = SCHEDULED, non-terminal; terminal = COMPLETED / FAILED / CRASHED (+ CANCELLED) | **HIGH** | [Prefect States](https://docs.prefect.io/v3/concepts/states) — full state table retrieved |
| Temporal is event-sourced; Visibility is a separate denormalized store for listing; non-retryable-in-code overrides policy | **HIGH** | [Visibility](https://docs.temporal.io/visibility); [Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies) |
| Dagster derives asset status from the event log but caches it (`AssetStatusCacheValue`, `AssetRecord.cached_status`) | **MEDIUM** | Source-derived summaries + [#14988](https://github.com/dagster-io/dagster/issues/14988), [#13280](https://github.com/dagster-io/dagster/issues/13280); not stated in primary docs |
| Dagster recommends ≤100K (conservatively ≤25K) partitions per asset; bottleneck is per-partition status reporting | **MEDIUM–HIGH** | [Partitioning assets](https://docs.dagster.io/guides/build/partitions-and-backfills/partitioning-assets); [#21581](https://github.com/dagster-io/dagster/discussions/21581), [#19802](https://github.com/dagster-io/dagster/discussions/19802), [#10330](https://github.com/dagster-io/dagster/issues/10330) |
| Dagster's evaluation history explains "why an asset was or wasn't materialized"; `skip_message` renders in the UI | **MEDIUM** | [Declarative Automation](https://docs.dagster.io/guides/automate/declarative-automation); the exact UI surface moved between versions (asset details → sensor page) |
| phaze-side facts: `get_stage_progress` returns `{done,total}`; `SENTINEL=9999999999`; `dag["<stage>Paused"]` in poll context; `retry_analysis_failed` at `routers/pipeline.py:883`; `straggler_failed_card.html` exists | **HIGH** | Read directly from the tree on `SimplicityGuy/true-parallel` |

**Zero-new-deps check:** every feature above is implementable on FastAPI + Jinja2 + HTMX + Alpine + Postgres + SAQ. D-8's cache is a Postgres table or a Redis key (both already in the stack). D-1 is a route + a template partial. No new runtime dependency is proposed.

**Known gaps in this research:** (a) Dagster's `AssetStatusCacheValue` internals come from third-party source summaries and issue threads, not primary docs — the *existence* of a status cache is well-supported, the field-level detail is MEDIUM; (b) Temporal's UI rendering of "next retry time" for pending activities was not confirmed in the docs (the CLI `describe` output showing `attempt` + `lastFailure` was); (c) no source was found for how any of these tools handle a *600K-cell* status matrix — they all decline to, which is itself the finding.

---

## Sources

- [Airflow — Tasks (task instance states)](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/tasks.html) — HIGH
- [Airflow — DAG Runs / Re-run Tasks (`clear` semantics)](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dag-run.rst) — HIGH (Context7 `/apache/airflow`)
- [Airflow — CLI reference (`tasks failed-deps`)](https://airflow.apache.org/docs/apache-airflow/stable/cli-and-env-variables-ref.html) — HIGH
- [Airflow — FAQ, "Why is task not getting scheduled?"](https://airflow.apache.org/docs/apache-airflow/stable/faq.html) — HIGH
- [apache/airflow#1729 — "Task Dependency Engine + Blocked Task Instance Explainer"](https://github.com/apache/airflow/pull/1729/files) — HIGH
- [apache/airflow#16163 — dependency-explainer log wording](https://github.com/apache/airflow/issues/16163) — MEDIUM
- [Prefect — States (name vs type; terminal table)](https://docs.prefect.io/v3/concepts/states) — HIGH
- [Prefect — Manage states](https://docs.prefect.io/v3/develop/manage-states) — HIGH
- [Temporal — Visibility](https://docs.temporal.io/visibility) — HIGH
- [Temporal — Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies) — HIGH
- [Temporal — Activity Operations (`describe`, pending activities, attempt, lastFailure)](https://docs.temporal.io/activity-operations) — HIGH
- [Temporal — Detecting Activity failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures) — HIGH
- [Dagster — Partitioning assets (partition-count guidance)](https://docs.dagster.io/guides/build/partitions-and-backfills/partitioning-assets) — MEDIUM–HIGH
- [Dagster — Declarative Automation (evaluation history, `skip_message`)](https://docs.dagster.io/guides/automate/declarative-automation) — MEDIUM
- [dagster-io/dagster#14988 — query current partition status](https://github.com/dagster-io/dagster/issues/14988) — MEDIUM
- [dagster-io/dagster#13280 — partition status cache invalidation](https://github.com/dagster-io/dagster/issues/13280) — MEDIUM
- [dagster-io/dagster discussion #21581 — assets with >25,000 partitions](https://github.com/dagster-io/dagster/discussions/21581) — MEDIUM
- [dagster-io/dagster discussion #19802 — UI issues with many partitions](https://github.com/dagster-io/dagster/discussions/19802) — MEDIUM
- [dagster-io/dagster#10330 — slow partitions page](https://github.com/dagster-io/dagster/issues/10330) — MEDIUM
- [Dagster storage & persistence internals (AssetRecord / cached_status / EventLogStorage)](https://deepwiki.com/dagster-io/dagster/5-storage-and-persistence) — MEDIUM (third-party summary)
- Local, verified by direct read: `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md`; `src/phaze/services/pipeline.py:299,466,883,1068`; `src/phaze/tasks/_shared/stage_control.py:51,65`; `src/phaze/routers/pipeline.py:189-240,883`; `src/phaze/templates/pipeline/partials/straggler_failed_card.html` — HIGH

---
*Feature research for: per-item, per-stage status in a months-long batch DAG pipeline*
*Researched: 2026-07-08*
