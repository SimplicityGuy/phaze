# Phase 49: Duration routing & backfill - Research

**Researched:** 2026-06-25
**Domain:** Control-plane SAQ enqueue routing (internal plumbing extension)
**Confidence:** HIGH (all findings grounded in current source; two CONTEXT.md premises diverge from shipped code — flagged below)

## Summary

This is an internal-plumbing extension, not greenfield. Every decision in 49-CONTEXT.md was checked against the current code. The reuse assets named in CONTEXT (`select_active_agent`, `resolve_queue_for_task`, `insert_ledger_if_absent`, the `_safe_count` count-card pattern, the deterministic-key chokepoint, `Agent.kind`) all exist and behave as described. **Two CONTEXT premises are stale and must be reconciled before planning the held-file release path** (see "Critical Divergences"):

1. **D-03 assumes a `CronJob(*/5)` reenqueue path still exists.** Phase 42 (PR #132) **deleted** that cron. `recover_orphaned_work` now fires only on detected queue-loss (`count_inflight_jobs == 0`) or the manual `force=True` Recover button. There is no steady-state */5 sweep to "extend."
2. **D-03 assumes held files can ride the existing ledger-driven recovery.** They cannot: a file transitioned to `AWAITING_CLOUD` is **never enqueued** (D-02), so it has **no `before_enqueue` hook fire → no scheduling-ledger row**. The ledger-replay recovery (`orphaned = ledger − live − domain-completed`) structurally cannot see it. Release must be a **state-driven scan** (`SELECT … WHERE state = AWAITING_CLOUD`), not a ledger replay.

**Primary recommendation:** Add a kind filter to `select_active_agent`; fork `trigger_analysis`/`_enqueue_analysis_jobs` into a per-file duration router that pre-selects a fileserver queue and (optionally) a compute queue once, then routes each file by `metadata.duration`; hold ≥threshold files with no online compute agent in a new `FileState.AWAITING_CLOUD` (state write needs an explicit `session.commit()` — the trigger endpoints are currently read-only); add a dedicated **state-driven** `release_awaiting_cloud` producer plus a count card and a backfill button. Resolve the release-trigger mechanism (new narrow cron vs. event-driven vs. manual-only) with the operator before locking the plan.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Held state = new `FileState.AWAITING_CLOUD`. `FileRecord.state` is `String(30)`, `FileState` is a code-only `StrEnum` (ANALYSIS_FAILED precedent) → **no migration**.
- **D-02:** A ≥threshold file with no online compute agent transitions to `AWAITING_CLOUD` instead of enqueuing locally. **Never** routed to a fileserver agent.
- **D-03:** Release of held files is automatic via the existing controller reenqueue path (CONTEXT states: runs on startup + `CronJob(*/5)`). Extend that path to scan `AWAITING_CLOUD` and route to a compute queue once a compute agent is online — ledger-scoped, deterministic-key dedup. ~5 min release latency acceptable. **[SEE CRITICAL DIVERGENCE — the */5 cron no longer exists and held files have no ledger row.]**
- **D-04:** `AWAITING_CLOUD` is NOT terminal/done; it stays eligible for re-drive. `process_file` "done" stays `{ANALYZED, ANALYSIS_FAILED}` — confirm `AWAITING_CLOUD` treated as pending.
- **D-05:** Surface held files as an "Awaiting cloud" count card on the pipeline dashboard, reusing `_safe_count` + count-card pattern. Click-through deferred.
- **D-06:** `metadata.duration` can be null at routing time. Null/unknown duration routes **local** (treated as short).
- **D-07:** `cloud_route_threshold_sec: int` default **5400** (90 min), alias `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC`, following `straggler_threshold_sec` convention. (Claude's discretion.)
- **D-08:** Backfill trigger = "Backfill to cloud" button on the pipeline dashboard, next to Recover/trigger controls, returns a count-confirmed response partial.
- **D-09:** Backfill selects `state == ANALYSIS_FAILED AND duration >= cloud_route_threshold_sec`, resets to `DISCOVERED`, seeds the ledger via `insert_ledger_if_absent`, routes each through the same new duration-aware router.
- **D-10:** Over-enqueue class closed by (a) explicit `ANALYSIS_FAILED ∧ duration≥threshold` filter (not a whole-backlog sweep) and (b) deterministic-key dedup at the `before_enqueue` chokepoint.
- **D-11:** Per-file routing replaces the all-files-to-one-queue enqueue in `trigger_analysis`/`_enqueue_analysis_jobs`.
- **D-12:** "Run analysis" response reports split counts (e.g. "Enqueued 50 local, 12 cloud, 5 awaiting cloud").
- **D-13:** Extend `select_active_agent` with a kind filter: long→most-recently-seen `kind='compute'`, short→most-recently-seen `kind='fileserver'`. Keep the existing deterministic "most-recently-seen, non-revoked" rule, scoped by kind.

### Claude's Discretion
- D-07 threshold knob naming/default (convention match to `straggler_threshold_sec`).
- Exact wiring of kind-filtered selection (new param vs. sibling helper) and where the per-file routing loop lives, provided the D-13 kind boundary holds.
- Backfill response-partial copy and count formatting.

### Deferred Ideas (OUT OF SCOPE)
- Cost/throughput-aware routing beyond a fixed duration threshold — CLOUDROUTE-05.
- Round-robin / least-loaded dispatch among multiple compute agents.
- Click-through drill-down list for the "Awaiting cloud" count card — count-only.
- Backfill dry-run/preview count before enqueue.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CLOUDROUTE-01 | Files with `metadata.duration ≥ threshold` route to an available compute agent's queue instead of local | Per-file fork in `trigger_analysis`/`_enqueue_analysis_jobs` (pipeline.py:225-285); kind-filtered `select_active_agent` (enqueue_router.py:93); duration join (FileMetadata.duration, metadata.py:25, relationship is `lazy="noload"` so must be fetched explicitly) |
| CLOUDROUTE-02 | No compute agent online → ≥threshold files held in "awaiting cloud", never silently analyzed locally | New `FileState.AWAITING_CLOUD` (file.py:20-47); state write + explicit `session.commit()` (get_session does NOT auto-commit, database.py:34); `get_awaiting_cloud_count` count card (mirror straggler_failed_card.html) |
| CLOUDROUTE-03 | Sub-threshold files analyze locally with unchanged behavior | Short/null-duration path reuses existing `enqueue_process_file` to the fileserver agent queue (analysis_enqueue.py:43) — same key/payload/policy |
| CLOUDROUTE-04 | Backfill the 144 `analysis_failed` long files via the Phase 45 ledger, no over-enqueue | `state==ANALYSIS_FAILED ∧ duration≥threshold` query + reset to DISCOVERED + `insert_ledger_if_absent` (scheduling_ledger.py:95) + deterministic-key dedup (deterministic_key.py:99-103); count via `_safe_count` (pipeline.py:272) |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Duration-based routing decision | API / Control plane | — | Control-side has the ORM + `metadata.duration`; agents are media-only, Postgres-free (agent worker boundary, test_task_split.py). The agent's own `_probe_duration_sec` is unavailable here (D-06). |
| Queue selection by agent kind | API / Control plane | — | `select_active_agent` reads the `agents` table (control DB) |
| Held-state persistence (`AWAITING_CLOUD`) | Database / Control | — | `FileRecord.state` column; written by the control-side trigger endpoint |
| Held-file release | Controller worker (SAQ) | API (manual) | Release scans state + selects compute agent; runs control-side where both DB and `task_router` exist |
| Backfill trigger | API (HTMX) → Controller enqueue | — | Operator button → control-side query/reset/enqueue |
| Count card / split-count UI | Frontend Server (Jinja/HTMX) | — | Server-rendered partials, OOB-swapped on the 5s poll (existing pattern) |

## Standard Stack

No new external packages. This phase is pure first-party Python against the existing stack (FastAPI, SQLAlchemy async, SAQ Postgres backend, Jinja2/HTMX). All work is additive edits to existing modules.

- **Config:** add `cloud_route_threshold_sec` to `ControlSettings` (config.py, alongside `straggler_threshold_sec` at L350).
- **State:** add `AWAITING_CLOUD` member to `FileState` (file.py:20-47).
- **No Alembic migration** — verified: `FileState` is `enum.StrEnum`, `FileRecord.state` is `mapped_column(String(30))` (file.py:62), `ANALYSIS_FAILED` (file.py:39) is the exact code-only precedent. Longest current value `"metadata_extracted"` = 18 chars; `"awaiting_cloud"` = 14 chars — well under 30. **D-01 CONFIRMED accurate.**

## Package Legitimacy Audit

Not applicable — no external packages are installed or recommended in this phase. All changes are first-party edits within `src/phaze/`.

## Architecture Patterns

### System Architecture Diagram

```
                         POST /pipeline/analyze  (HTMX, operator-triggered)
                                      │
                                      ▼
                      get_discovered_files_with_duration(session)   ◄── NEW helper
                      (FileRecord ⟕ FileMetadata.duration; lazy="noload" → explicit outerjoin)
                                      │
                  ┌───────────────────┼────────────────────────────┐
                  │ pre-select queues ONCE (kind-filtered):         │
                  │   fileserver_q = task_router.queue_for(fs.id)   │  select_active_agent(kind="fileserver")
                  │   compute_q    = task_router.queue_for(cmp.id)  │  select_active_agent(kind="compute")  (may be None)
                  └───────────────────┬────────────────────────────┘
                                      │  per file:
              duration is None or < threshold ──► fileserver_q  ──► enqueue_process_file (key process_file:<id>)  [LOCAL]
              duration ≥ threshold AND compute online ──► compute_q ──► enqueue_process_file                       [CLOUD]
              duration ≥ threshold AND no compute ──► UPDATE state = AWAITING_CLOUD (+ commit)                     [HELD]
                                      │
                                      ▼
                  trigger response partial: "Enqueued N local, M cloud, K awaiting cloud" (D-12)

   ── separately ──
   Held-file release  (STATE-driven, NOT ledger replay):
       SELECT FileRecord WHERE state = AWAITING_CLOUD
       IF compute agent online: enqueue_process_file → compute_q  (deterministic key dedups; ledger row written by before_enqueue hook)
       (trigger mechanism TBD — see Critical Divergence #1)

   Backfill (operator button):
       SELECT FileRecord JOIN metadata WHERE state=ANALYSIS_FAILED AND duration >= threshold
       UPDATE state = DISCOVERED (+ commit)
       insert_ledger_if_absent(...) per file
       route through the SAME per-file duration router (compute_q if online, else AWAITING_CLOUD)
```

The file-to-implementation mapping is in the table under "Integration Points" below.

### Pattern 1: Kind-filtered active-agent selection (D-13)
**What:** `select_active_agent` currently takes only `session` and returns the single most-recently-seen non-revoked agent of ANY kind.

```python
# Source: src/phaze/services/enqueue_router.py:93
async def select_active_agent(session: AsyncSession) -> Agent:
    stmt = (
        select(Agent)
        .where(Agent.revoked_at.is_(None), Agent.last_seen_at.is_not(None))
        .order_by(Agent.last_seen_at.desc())
        .limit(1)
    )
    ...
```

**Recommended change (minimal, discretion D-13):** add an optional `kind: str | None = None` param; when set, add `Agent.kind == kind` to the `.where(...)`. Keeps the existing deterministic rule, scopes by kind. Existing callers (`resolve_queue_for_task` enqueue_router.py:146, `recover_orphaned_work` reenqueue.py:299) pass no kind → unchanged behavior. The short-file path calls `select_active_agent(session, kind="fileserver")`; the compute path calls `select_active_agent(session, kind="compute")` inside a `try/except NoActiveAgentError` (None → held).

**Why a param, not a rewrite:** `resolve_queue_for_task` is the single chokepoint that guarantees "never the consumer-less default queue" (enqueue_router.py:120-151). The per-file fork must keep obtaining queues from `task_router.queue_for(agent.id)` / `controller_queue` so that invariant holds. Pre-selecting the two kind-scoped agents once and looping is simpler than threading kind through `resolve_queue_for_task` per file.

### Pattern 2: Per-file routing fork (D-11)
**What:** Today `trigger_analysis` (pipeline.py:254) resolves ONE queue for `process_file` and background-enqueues ALL DISCOVERED files to it:

```python
# Source: src/phaze/routers/pipeline.py:265-284 (current — all-to-one-queue)
files = await get_files_by_state(session, FileState.DISCOVERED)
routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session)
agent_id = cast("str", routed.agent_id)
task = asyncio.create_task(_enqueue_analysis_jobs(routed.queue, files, agent_id, settings.models_path))
```

**Fork:** replace with a per-file loop that picks the queue (or holds) per `duration`. Reuse `enqueue_process_file` (analysis_enqueue.py:43) verbatim for both local and cloud enqueues — it already owns the deterministic key, full payload, and `timeout=7200/retries=2` policy. The only new logic is the duration comparison + agent-kind selection + the AWAITING_CLOUD state write.

**Anti-pattern to avoid:** Do NOT call `resolve_queue_for_task("process_file", …)` per file — it re-runs `select_active_agent` (a DB query) on every file and is kind-blind. Pre-select the (≤2) kind-scoped queues once before the loop.

### Pattern 3: Duration is not auto-loaded — fetch it explicitly (D-06)
`FileRecord.file_metadata` is `relationship(..., lazy="noload")` (file.py:71) — accessing it yields `None`, never a lazy query. `get_files_by_state` (pipeline.py:725) selects only `FileRecord`. So the router needs duration via an explicit LEFT OUTER JOIN, e.g. a new `get_discovered_files_with_duration(session) -> list[tuple[FileRecord, float | None]]`:

```python
# Recommended new helper (services/pipeline.py)
stmt = (
    select(FileRecord, FileMetadata.duration)
    .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
    .where(FileRecord.state == FileState.DISCOVERED)
)
```

**Load-bearing detail:** the enqueue runs in a **background task** (pipeline.py:280) after the request session may have closed. The current code is safe because `id/original_path/file_type` are already loaded scalars. Duration must likewise be captured into the in-memory list **before** backgrounding — returning `(FileRecord, duration)` tuples does this. Do not defer duration access into the background coroutine.

### Pattern 4: Count card (D-05) — mirror `straggler_failed_card.html`
The exact precedent for "Awaiting cloud": `get_analysis_failed_count` (pipeline.py:761) is a one-line `_safe_count` over `state == ANALYSIS_FAILED`. Clone it as `get_awaiting_cloud_count` over `state == AWAITING_CLOUD`. Wire into `dashboard()` (pipeline.py:362-386) AND `pipeline_stats_partial()` (pipeline.py:411-430) contexts, and add the card to a partial that is included inline on first load and OOB-swapped on the 5s poll (straggler_failed_card.html:16-18 is the OOB contract — same `id` on both renders, `hx-swap-oob="true"` when `oob` is truthy).

### Pattern 5: Backfill button (D-08) — mirror the Recover button + a count partial
The global "Recover orphaned work" button (dag_canvas.html:269-278) is the exact UI precedent: `hx-post` to an endpoint, `hx-target` a response slot, `hx-indicator` a spinner, returns a small fragment (recover_response.html). Backfill = a sibling button posting to a new `/pipeline/backfill-cloud` endpoint returning a count-confirmed partial (model on trigger_response.html / recover_response.html).

### Anti-Patterns to Avoid
- **Routing a compute task to the default queue.** Always obtain the destination from `task_router.queue_for(agent.id)` (per-agent) — never construct an unnamed queue. (Phase-30 incident; enqueue_router.py module docstring.)
- **Adding `AWAITING_CLOUD` to `_DOMAIN_COMPLETED_STAGES` or the analyze done-set.** It must stay pending (D-04). The analyze done-set is `{ANALYZED, ANALYSIS_FAILED}` only (reenqueue.py:167).
- **Forgetting `await session.commit()` on state writes.** `get_session` does NOT commit (database.py:34); the existing trigger endpoints never wrote state so never needed it. AWAITING_CLOUD transition and backfill reset are the first state writes in these endpoints.
- **Treating held-file release as a ledger replay.** Held files have no ledger row (never enqueued). Release is a state scan. (Critical Divergence #2.)

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Deterministic job key / dedup | A new key scheme for cloud jobs | `enqueue_process_file` → `process_file:<file_id>` (analysis_enqueue.py:43; deterministic_key.py:74-83) | Same key whether local or cloud → a double-click / re-trigger / release-then-backfill collapses to a no-op (D-10) |
| Ledger seeding | Manual INSERT | `insert_ledger_if_absent` (scheduling_ledger.py:95) — ON CONFLICT DO NOTHING | Idempotent; the Plan-04 backfill primitive, exactly the D-09 need |
| Active-agent selection | A new "pick an online agent" query | `select_active_agent` + kind param (enqueue_router.py:93) | Deterministic, non-revoked, recently-seen rule already shared by routing + recovery |
| Degrade-safe count | A raw `session.execute(count)` | `_safe_count(session, stmt, node=…)` (pipeline.py:272) | Rolls back aborted txns so the 5s poll never 500s |
| "Is a compute agent online?" gate | New liveness logic | `count_active_agents` precedent (pipeline.py:700) — clone with `Agent.kind == "compute"` filter | Same SAVEPOINT-isolated, fail-safe-to-0 discipline |

**Key insight:** Nearly every primitive Phase 49 needs already exists from Phases 30/32/35/42/44/45. The phase is composition, not invention — the risk is in the two stale CONTEXT premises, not in missing infrastructure.

## Runtime State Inventory

This is a refactor/extension touching routing + a new state. Runtime-state audit:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | The 144 `analysis_failed` long files are live FileRecords in Postgres (`state='analysis_failed'`). Backfill RESETS them to `DISCOVERED` (D-09) and seeds ledger rows. New `AWAITING_CLOUD` files will be written going forward. | Data migration is operator-triggered (the backfill button), NOT an Alembic step. No automatic data migration. |
| Live service config | `cloud_route_threshold_sec` is a new env knob (`PHAZE_CLOUD_ROUTE_THRESHOLD_SEC`); default 5400 means a fresh deploy works with no env change. The compute agent itself (its `agents` row, `kind='compute'`) is provisioned out-of-band per Phase 48 — not created here. | None in this phase (Phase 51 owns deploy/config wiring). Document the new knob in README/config docs per project memory. |
| OS-registered state | None — no OS-level registrations involved. | None — verified by scope (control-plane code + DB only). |
| Secrets/env vars | None new beyond the non-secret `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC` knob. | None. |
| Build artifacts | None — no package rename, no entry-point change, no compiled artifact. | None — verified: edits are within existing modules. |

**The canonical question — after every file is updated, what runtime systems still hold old state?** The 144 ANALYSIS_FAILED files persist in Postgres until the operator clicks Backfill; that is the intended human-in-the-loop step (D-08), not a regression. No cached/registered string state.

## Common Pitfalls

### Pitfall 1: The */5 reenqueue cron D-03 relies on no longer exists (CRITICAL)
**What goes wrong:** Planning "extend the existing `CronJob(*/5)` reenqueue to release AWAITING_CLOUD" produces a plan against code that was deleted.
**Why it happens:** 49-CONTEXT D-03 predates Phase 42. Phase 42 (ROADMAP L396-407, PR #132) replaced the unconditional */5 `reenqueue_discovered` cron with restart/queue-loss-only recovery. Controller confirms: `recover_orphaned_work` is registered as a `functions` entry but **not** as a `CronJob` (controller.py:204-224), with an explicit comment "NO periodic auto-advance. DO NOT re-add a `recover_orphaned_work` CronJob here." It runs once on startup (controller.py:151) and via the manual Recover button (`force=True`, pipeline.py:944).
**How to avoid:** Treat the release-trigger mechanism as an open design decision (see Open Questions). The cleanest options that respect the Phase-42 "automation only in recovery" principle: (a) a **new, narrowly-scoped** `CronJob` that ONLY transitions `AWAITING_CLOUD → compute queue` when a compute agent is online (not a general pipeline auto-advance); (b) event-driven release on compute-agent heartbeat/registration; (c) manual-only release via a button. D-03's "~5 min latency acceptable" language implies the operator expects (a). Confirm before locking.
**Warning signs:** A plan task that says "edit the reenqueue cron" — there is no reenqueue cron.

### Pitfall 2: Held files have no ledger row, so ledger-replay recovery can't release them (CRITICAL)
**What goes wrong:** Planning release as part of `recover_orphaned_work`'s `ledger − live − domain-completed` replay silently no-ops — there are no rows to replay.
**Why it happens:** The scheduling-ledger WRITE happens only at the `before_enqueue` chokepoint (deterministic_key.py:117-147). A file held in `AWAITING_CLOUD` was **never enqueued** (D-02 — held instead of enqueued locally), so no hook fired, so no ledger row exists.
**How to avoid:** Release MUST be a state-driven scan: `SELECT FileRecord WHERE state = AWAITING_CLOUD`, then for each, IF a compute agent is online, `enqueue_process_file(compute_q, file, …)`. The enqueue itself writes the ledger row (via the hook) and is dedup-safe via the `process_file:<file_id>` key. This is "ledger-scoped + deterministic-key dedup, same as recovery" in spirit (D-03's intent) but mechanically a new producer, not a `recover_orphaned_work` extension.
**Warning signs:** A unit test for release that seeds ledger rows for held files — held files don't have them.

### Pitfall 3: State write without commit
**What goes wrong:** The `AWAITING_CLOUD` transition or backfill `DISCOVERED` reset appears to work in a test but the row is never persisted.
**Why it happens:** `get_session` yields a non-committing session (database.py:34, `expire_on_commit=False` but no commit-on-exit). Every existing pipeline trigger endpoint is read-then-enqueue with no DB write, so none commit.
**How to avoid:** Explicit `await session.commit()` after the state UPDATE(s) in the analyze and backfill endpoints. Do the state write in the request (a bounded UPDATE), not the background enqueue task.

### Pitfall 4: Compute agent winning short-file selection
**What goes wrong:** Without the kind filter, the most-recently-seen agent could be the compute agent, and short files would route to a queue whose worker has no media access (compute agents are media-less per Phase 48 / CLOUDAGENT-02).
**Why it happens:** `select_active_agent` is currently kind-blind (enqueue_router.py:93-117).
**How to avoid:** D-13's kind filter — short→`fileserver`, long→`compute`. This is the load-bearing reason D-13 exists.

### Pitfall 5: Counting backfill targets without the duration join
**What goes wrong:** Reusing `get_analysis_failed_count` (pipeline.py:761) for the backfill count over-counts — it counts ALL `ANALYSIS_FAILED`, not just `duration ≥ threshold`. The 144 figure is specifically the long subset (49-CONTEXT specifics: "precisely `ANALYSIS_FAILED ∧ duration ≥ threshold` — not all `ANALYSIS_FAILED`").
**How to avoid:** A new count helper that JOINs `metadata` and filters `duration >= cloud_route_threshold_sec`. Mirror `_safe_count` discipline.

## Code Examples

### Confirmed: analyze done-set is `{ANALYZED, ANALYSIS_FAILED}` — AWAITING_CLOUD stays pending (D-04)
```python
# Source: src/phaze/tasks/reenqueue.py:165-167
def _select_done_analyze_ids() -> Any:
    """Build the SELECT for file ids whose analyze stage is terminal (ANALYZED / ANALYSIS_FAILED)."""
    return select(FileRecord.id).where(FileRecord.state.in_([FileState.ANALYZED, FileState.ANALYSIS_FAILED]))
```
`_DOMAIN_COMPLETED_STAGES` (reenqueue.py:107-113) = `{process_file, extract_file_metadata, fingerprint_file}`. **No edit needed** to keep AWAITING_CLOUD pending — simply do not add it to the done-set. D-04 is satisfied by omission. (Worth a regression test asserting an AWAITING_CLOUD file is NOT treated as analyze-done.)

### Confirmed: reusable single-file producer (local AND cloud)
```python
# Source: src/phaze/services/analysis_enqueue.py:43-101 (signature)
async def enqueue_process_file(
    queue: Any, file: FileRecord, agent_id: str, models_path: str,
    *, fine_cap: int | None = None, coarse_cap: int | None = None,
) -> Any:
    # builds full ProcessFilePayload, sets key=process_file:<file_id>, timeout=7200, retries=2
```
Pass the fileserver queue + fileserver agent_id for local; the compute queue + compute agent_id for cloud. Identical key → cross-path dedup.

### Confirmed: idempotent ledger seed for backfill (D-09)
```python
# Source: src/phaze/services/scheduling_ledger.py:95-114
async def insert_ledger_if_absent(session, *, key, function, kwargs, timeout=None, retries=None) -> None:
    # INSERT ... ON CONFLICT (key) DO NOTHING
```
Note: the backfill can seed the ledger explicitly, but since `enqueue_process_file` already writes the ledger via the `before_enqueue` hook for any control-side queue, the explicit `insert_ledger_if_absent` is primarily needed for the **AWAITING_CLOUD** backfill path (held, not enqueued) so the future release has a record — OR rely on the release-time enqueue to write it. Clarify in planning whether D-09's explicit seed is redundant for the enqueued branch.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `CronJob(*/5)` `reenqueue_discovered` auto-advances analyze | Recovery only on detected queue-loss + manual Recover button | Phase 42 (PR #132) | D-03's "extend the */5 cron" premise is stale — no such cron |
| Complement-of-done sweep recovery (`get_files_by_state(DISCOVERED)` etc.) | Ledger-driven `ledger − live − domain-completed` | Phase 45 | Held files (no ledger row) need a separate state-scan, not ledger replay |
| Redis SAQ broker | Postgres SAQ broker (`saq_jobs`) | Phase 36 | Queued/active jobs are durable; `count_inflight_jobs`/`get_live_job_keys` read `saq_jobs` |
| All DISCOVERED → one agent queue | (this phase) per-file duration routing | Phase 49 | The fork being built |

**Deprecated/outdated in CONTEXT:**
- D-03's reference to `CronJob(*/5)` — deleted in Phase 42.
- D-03's "extend that [ledger-driven recovery] path" — won't see held files (no ledger row).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The 144 ANALYSIS_FAILED long files all carry a non-null `metadata.duration ≥ 5400` (D-06 rationale + D-09 count) | Pitfall 5 / backfill | If some long failures have null duration, the backfill count < 144 and those files stay stuck. Verify with a live `SELECT count(*) FROM files f JOIN metadata m ON m.file_id=f.id WHERE f.state='analysis_failed' AND m.duration >= 5400` before trusting the "144" figure. Cannot verify in this static research session (no DB access). |
| A2 | Releasing a held file by enqueuing to compute, while leaving its state at `AWAITING_CLOUD` until ANALYZED, is acceptable UX (count card still shows it as awaiting until analyzed) | Architecture / release | If the operator expects the card to drop immediately on release, the release path must also move state (e.g. → DISCOVERED). Design decision for the planner. |
| A3 | `seed_active_agent` test helper relies on `Agent.kind` server_default `'fileserver'`; a compute-agent fixture needs `kind="compute"` set explicitly | Validation Architecture | Tests for compute routing will mis-seed if the helper isn't extended. Low risk — easily fixed in Wave 0. |

## Open Questions (ALL RESOLVED 2026-06-25)

1. **What triggers held-file release? (was BLOCKING)** — **RESOLVED → CONTEXT D-03 (RECONCILED).** Operator chose a NEW dedicated `CronJob(release_awaiting_cloud, "*/5 * * * *")` scoped only to AWAITING_CLOUD→compute, gated on a compute agent online, state-driven scan (NOT ledger replay, NOT a resurrection of the deleted */5 reenqueue cron). Implemented in Plan 49-04.

2. **Does the released file leave AWAITING_CLOUD immediately, or only on ANALYZED? (A2)** — **RESOLVED → CONTEXT D-03a.** On release, enqueue to compute AND reset state to `DISCOVERED` (symmetry with D-09 backfill reset). Implemented in Plan 49-04.

3. **Is the explicit `insert_ledger_if_absent` in D-09 redundant for the enqueued branch?** — **RESOLVED → CONTEXT D-09 + Plan 49-03.** Keep the explicit seed ONLY for files backfill routes to AWAITING_CLOUD (held, not enqueued); the enqueued branch's `before_enqueue` hook already writes the ledger. No double-write.

4. **The 144 count — confirm live (A1).** — **RESOLVED → 49-VALIDATION.md "Manual-Only Verifications."** Backfill plan verifies the count against the live DB (`SELECT count(*) … state='analysis_failed' AND duration >= 5400`) and surfaces it in the button label.

## Environment Availability

No external tools/services introduced. All dependencies (FastAPI, SQLAlchemy async, SAQ Postgres, Jinja2/HTMX, Postgres, Redis-cache) are already provisioned and used by the running app. The compute agent + arm64 image are Phase 47/48 deliverables (already shipped) and are provisioned out-of-band; this phase only routes to an `agents` row with `kind='compute'`. **Step 2.6: no new external dependencies.**

## Validation Architecture

Test framework verified from `CLAUDE.md` + repo: `uv run pytest` (pytest + pytest-asyncio), real Postgres `session` fixture (tests/conftest.py), 85% coverage min, ruff/mypy strict. Run a single test: `uv run pytest tests/path::test_name`.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (real PG session fixture) |
| Config | `pyproject.toml` (`[tool.pytest...]`), `tests/conftest.py` |
| Quick run | `uv run pytest tests/test_services/test_enqueue_router.py tests/test_routers/test_pipeline.py -x` |
| Full suite | `uv run pytest --cov --cov-report=term-missing` |

### Observable signals per Success Criterion
| Success Criterion (ROADMAP) | Observable signal | Test type | Target test file |
|---|---|---|---|
| 1. ≥threshold → compute queue | A ≥5400s file enqueues `process_file` onto `phaze-agent-<compute-id>` (captured queue name), NOT the fileserver queue | router/integration | `tests/test_routers/test_pipeline.py` (new cases) using `FakeQueue`/`FakeTaskRouter` capture list (_queue_fakes.py) |
| 2. Sub-threshold/null → local unchanged | A <5400s or null-duration file enqueues `process_file` onto `phaze-agent-<fileserver-id>` with the same key/payload/policy as today | router/service | `tests/test_routers/test_pipeline.py`; `tests/test_services/test_analysis_enqueue.py` |
| 3. No compute online → held, never local | A ≥threshold file with only a fileserver agent online ends in `state=AWAITING_CLOUD`, NO `process_file` enqueue captured; count card shows it; split-count reports `awaiting` | router + service + template | `tests/test_routers/test_pipeline.py`; `get_awaiting_cloud_count` in `tests/test_services/test_pipeline.py` |
| 4. Ledger-scoped backfill of 144, no over-enqueue | Backfill enqueues exactly the `ANALYSIS_FAILED ∧ duration≥threshold` set; a double-click dedups to no-op (deterministic key); never-failed/short files untouched | router + service | `tests/test_routers/test_pipeline.py`; `tests/test_services/test_pipeline.py`; `tests/test_tasks/test_recovery.py` for the domain-completed/AWAITING_CLOUD-pending assertion |
| D-13 kind filter | `select_active_agent(session, kind="compute")` returns only the compute agent; `kind="fileserver"` excludes it; no-match raises `NoActiveAgentError` | unit | `tests/test_services/test_enqueue_router.py` |
| D-04 pending | An `AWAITING_CLOUD` file is NOT in the analyze done-set / not treated domain-completed | unit | `tests/test_tasks/test_recovery.py` |

### Sampling Rate
- **Per task commit:** the quick-run pair above.
- **Per wave merge:** full suite + coverage.
- **Phase gate:** full suite green before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] Extend `seed_active_agent` (tests/_queue_fakes.py:331) with a `kind` param (default `"fileserver"`) so a `kind="compute"` agent can be seeded. (A3)
- [ ] Confirm `FakeTaskRouter`/`DedupFakeTaskRouter` capture the per-agent queue name so a test can assert compute-vs-fileserver destination (they do — _queue_fakes.py captures `(queue_name, task, kwargs)`).
- [ ] New test fixtures: a FileRecord + FileMetadata.duration pair (≥threshold and <threshold and null), reusing the real PG session.
- [ ] No framework install needed — existing infra covers all of it.

## Security Domain

`security_enforcement` not configured for this repo; this phase is internal control-plane plumbing with no new external input surface. Relevant controls, all satisfied by existing patterns:

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | yes (minor) | `cloud_route_threshold_sec` is a pydantic `int Field` with `gt/lt` bounds (mirror `straggler_threshold_sec` config.py:350-356); the backfill/analyze endpoints take NO operator free-text — file ids are server UUIDs |
| V5 Injection | yes | All new queries are ORM / bound params; the `duration >= threshold` compare is a bound int, never interpolated SQL (project T-44-05 discipline) |
| V6 Cryptography | no | none |

| Threat | STRIDE | Mitigation |
|--------|--------|------------|
| Held files silently analyzed locally and time out | Denial of Service (self-inflicted) | The load-bearing invariant (CLOUDROUTE-02): ≥threshold + no compute → `AWAITING_CLOUD`, never the fileserver queue (D-02) |
| Backfill double-click detonates the queue | DoS | Explicit `ANALYSIS_FAILED ∧ duration≥threshold` filter (not a backlog sweep) + `process_file:<id>` deterministic-key dedup (D-10) — the Phase-32/45 incident class is already closed |

## Sources

### Primary (HIGH confidence — current source, this session)
- `src/phaze/services/enqueue_router.py:93-151` — `select_active_agent`, `resolve_queue_for_task`, task sets, `NoActiveAgentError`, `RoutedQueue`
- `src/phaze/services/agent_task_router.py:76-110` — `queue_for(agent_id)`
- `src/phaze/routers/pipeline.py:225-285, 434-461, 944-972` — `trigger_analysis`, `_enqueue_analysis_jobs`, `trigger_analysis_ui`, Recover button endpoint
- `src/phaze/services/pipeline.py:272-289, 700-775, 725-737` — `_safe_count`, `count_active_agents`, `get_analysis_failed_count`, `get_files_by_state`
- `src/phaze/services/analysis_enqueue.py:43-101` — `enqueue_process_file` (key/payload/policy)
- `src/phaze/models/file.py:20-77` — `FileState` StrEnum, `FileRecord.state String(30)`, `file_metadata` lazy="noload"
- `src/phaze/models/metadata.py:25` — `FileMetadata.duration Float nullable`
- `src/phaze/models/agent.py:28, 39-42` — `Agent.kind String(16)` + CHECK in('fileserver','compute')
- `src/phaze/tasks/reenqueue.py:1-60, 107-205, 238-311` — recovery model, `_DOMAIN_COMPLETED_STAGES`, `is_domain_completed`, `recover_orphaned_work` (no */5 cron)
- `src/phaze/tasks/controller.py:204-224` — `functions`/`CronJob` registration ("DO NOT re-add a recover cron")
- `src/phaze/services/scheduling_ledger.py:95-114` — `insert_ledger_if_absent`, `routing_for_function`
- `src/phaze/tasks/_shared/deterministic_key.py:74-152` — `_KEY_BUILDERS`, `apply_deterministic_key` ledger WRITE hook
- `src/phaze/config.py:350-356` — `straggler_threshold_sec` convention template
- `src/phaze/database.py:31-37` — `get_session` (no auto-commit)
- `src/phaze/templates/pipeline/partials/straggler_failed_card.html`, `trigger_response.html`, `dag_canvas.html:269-278` — card / response / button precedents
- `tests/_queue_fakes.py:217-348` — `DedupFakeQueue/Router`, `seed_active_agent` (no kind param), `stub_app_state`
- `tests/test_services/test_enqueue_router.py`, `tests/test_tasks/test_recovery.py` — existing test patterns
- `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md` (Phase 42 §, Phase 49 §) — scope + the Phase-42 cron removal

### Secondary
- Project memory entries (Phase 42 recovery-only automation; Phase 45 ledger; Phase 30 default-queue misrouting; Recover over-enqueue incident) — corroborate the two divergences.

## Metadata

**Confidence breakdown:**
- Standard stack / no-migration / reuse assets: HIGH — every claim cites current source read this session.
- Per-file routing fork + kind filter mechanics: HIGH — signatures and call sites confirmed.
- Held-file release path: HIGH on the *problem* (the two divergences are confirmed in code), MEDIUM on the *recommended solution* (a new narrow cron) — needs operator confirmation.
- The "144" backfill count: MEDIUM — depends on live DB data (A1) not verifiable in a static session.

**Research date:** 2026-06-25
**Valid until:** ~2026-07-25 (stable internal codebase; revalidate if Phases 50/51 land first)
