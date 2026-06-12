# Phase 35: Pipeline Determinism, Idempotency & Per-Job-Type Observability - Context

**Gathered:** 2026-06-11
**Status:** Ready for planning
**Source:** Brainstorming session (operator decisions 2026-06-11) + codebase scout + stage-dependency research (`35-STAGE-DEPENDENCIES.md`)

<domain>
## Phase Boundary

**In scope (5+1 work items from ROADMAP.md Phase 35):**
1. **Deterministic SAQ keys for ALL job types**, enforced CENTRALLY in the enqueue layer so every task is keyed by construction as `<task>:<natural_id>` and no call site can drift. Generalizes the Phase 32 `process_file` pattern (`analysis_enqueue.py:32` → `process_file:<file_id>`) to the 9 remaining enqueue sites that currently use random UUID keys.
2. **Audit + ensure ALL task DB writes upsert** (`ON CONFLICT DO UPDATE`) so re-runs never duplicate rows. Already idempotent: `agent_analysis`, `agent_metadata`, `agent_fingerprint`, `agent_files`, `agent_tracklists`, `execution_log` (ON CONFLICT DO NOTHING on `id`). Fill the gap: `generate_proposals` (raw INSERTs today), and audit `tag_write_log`.
3. **Remove auto metadata-extraction** from discovery/scan (2 auto-enqueue paths: `routers/agent_files.py:143` and `services/ingestion.py:190`). Make `extract_file_metadata` MANUAL-only — operator triggers it from the dashboard (button already exists from Phase 34).
4. **Metadata stage** in the pipeline view, between Discovered and Fingerprinted.
5. **Per-job-type progress** on the dashboard, backed by MAINTAINED per-function counters, rendered as a **DAG view** (sketch 001 Variant B "Graph canvas").
6. **Stage ordering & parallelization model** — drive orchestration fan-out + the per-job-type UI tiers from `35-STAGE-DEPENDENCIES.md`.

**Out of scope:** Changing how essentia/fingerprint/tracklist tasks compute their results; reworking the SAQ broker or queue topology; the SAQ monitoring UI (Phase 33); reboot re-enqueue resilience (Phase 32); adding new pipeline stages/capabilities beyond the existing task set.

## Why this phase exists (2026-06-11 queue-doubling incident)

Random-uuid `process_file` jobs from the pre-Phase-32 "Run Analysis" path could not dedup against the new deterministic-key re-enqueue, doubling the live queue to ~22,830 jobs over 11,428 files (cleaned via purge + cron-rebuild). Phase 32 fixed only `process_file`. Phase 35 generalizes determinism + idempotency to the whole pipeline so no future re-enqueue or re-run can duplicate queue items or DB rows, and gives the operator per-stage visibility that the Phase-34 aggregate card could not.
</domain>

<decisions>
## Implementation Decisions

### Pipeline UI — DAG replaces the Phase-34 layout fully (D-01)
- **D-01:** The new DAG view (sketch 001 Variant B, "Graph canvas") becomes the **single** pipeline UI. The four Phase-34 action buttons (`stage_cards.html`) and the aggregate `processing_card.html` are **removed**; trigger controls fold into the DAG nodes. Accepted trade-off: largest blast radius on freshly-shipped Phase 34 code — but the operator wants one coherent graph, not graph + legacy cards. (Rejected: "DAG primary, keep cards below" and "augment only".)
- Each DAG node = a stage with: live count, per-stage progress bar, and a trigger button gated by upstream deps **and** agent-busy (button disabled when an upstream stage hasn't produced inputs or the agent worker is busy). Edges drawn from node anchor points (NOT hand-placed coordinates as in the throwaway sketch).
- Node/edge topology comes from `35-STAGE-DEPENDENCIES.md`: Discovery → {Extract Metadata ∥ Fingerprint ∥ Analyze ∥ tracklist-branch} parallel; Proposals joins on **Analyze + Metadata only** (NOT fingerprint, NOT tracklist); Approve → Execute terminal; tracklist sub-chain (search/scan_live_set → scrape → discogs) sequential side-branch.

### Progress data — fully maintained per-function counters, reconciled from DB on read (D-02, D-03)
- **D-02:** Each node's progress is derived from **fully maintained per-function counters** (Redis), per locked decision (B). Track enqueued + completed per function. This is the pure expression of decision B (not DB-state-only, not SAQ-stats-only).
- **D-03 (resolves the SAQ hook constraint):** Counters are **reconciled against authoritative DB stage counts on every dashboard read** (and/or periodically). The "done" count self-heals from DB-truth, so a purge, worker restart, or a *missed completion increment* can never leave the UI permanently wrong. This is the key design pin: **SAQ 0.26.x exposes only `register_before_enqueue` — there is no public after-process hook**, so the completion-side increment may be best-effort (a worker hook, a task-side `INCR`, or omitted entirely) precisely BECAUSE the DB reconcile is the backstop. Researcher/planner choose the completion mechanism; the reconcile-on-read is mandatory regardless.
- This deliberately reconciles locked decision (B) ("maintained counters") with the Phase-34 operator choice that progress denominators survive worker restarts. Counters are a fast cache; DB is truth.

### generate_proposals idempotency — upsert per file, protect approvals (D-04)
- **D-04:** One active proposal per file. Re-running "Generate Proposals" for a file overwrites a **PENDING** proposal in place (`ON CONFLICT DO UPDATE` keyed on `file_id`), but **never touches APPROVED / EXECUTED proposals** (skip non-pending). Idempotent re-runs AND human-approved decisions are protected. (Rejected: blanket overwrite-all, which could discard an approval; rejected: `(file_id, batch_index)` keying, which still duplicates a file across batches.)
- Planner must confirm the proposals-table schema can support a `file_id`-scoped conflict target (unique constraint / partial index on pending), and that the status guard is enforced in the upsert (e.g. `WHERE status = 'pending'` or equivalent), not just in app code.

### Deterministic-key enforcement is centralized (D-05)
- **D-05 (locked operator decision A):** Keys are enforced **centrally in the enqueue layer**, not per-call-site, so no future call site can drift back to random UUIDs. The natural-id per task and the exact seam (a SAQ `before_enqueue` hook that sets `job.key`, vs. a key-builder threaded through `enqueue_router` / `agent_task_router`) are **research/planner discretion** — see Claude's Discretion. The existing `apply_project_job_defaults` before_enqueue hook (`tasks/_shared/queue_defaults.py:62`) is the proven hook seam to consider extending.

### Metadata extraction is operator-triggered only (D-06)
- **D-06:** Remove BOTH auto-enqueue paths (`routers/agent_files.py:143` on file insert, `services/ingestion.py:190` on legacy scan completion). `extract_file_metadata` runs only when the operator triggers it (Phase-34 "Extract Metadata" backfill button, now a DAG node trigger per D-01). Reverses the Phase-34 D-09/D-20/21/22 auto-extract behavior. Soft-dependency note from research: `search_tracklist` falls back to filename parsing when metadata is absent, so manual-only metadata does not hard-break the tracklist branch.

### Claude's Discretion
- **Natural-id choice per task** for the deterministic key: e.g. `extract_file_metadata:<file_id>`, `fingerprint_file:<file_id>`, `scan_live_set:<file_id>`, `search_tracklist:<file_id>`, `scrape_and_store_tracklist:<tracklist_id>`, `match_tracklist_to_discogs:<tracklist_id>`, and the batch task `generate_proposals` (single-file keying per D-04, or a batch-hash — researcher to recommend, keeping D-04's per-file idempotency intact).
- **Where central key enforcement lives** — `before_enqueue` hook setting `job.key` vs. a key-builder in the router seam (D-05). Verify a before_enqueue hook can actually set/override the key in SAQ 0.26.x; if not, fall back to a router-level key builder that every site already flows through.
- **Completion-increment mechanism** for the maintained counters (worker after_process hook if one exists, task-side `INCR`, or none + rely on DB reconcile) — see D-03.
- Exact Tailwind classes / SVG markup / Alpine wiring for the DAG canvas (match existing dark-mode-aware partials; reuse the `$store.pipeline` + OOB-swap + 5s `/pipeline/stats` poll pattern from Phase 34).
- `tag_write_log` idempotency: audit and add upsert only if a gap is found (execution_log is already idempotent).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase 35 research + design artifacts
- `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-STAGE-DEPENDENCIES.md` — stage DAG + per-stage data-dependency evidence (file:line). Drives node/edge topology and parallelization tiers.
- `.planning/sketches/001-pipeline-dag-view/README.md` — sketch findings; winner = Variant B ("Graph canvas"). Design question, dependency model, what-to-look-for.
- `.planning/sketches/001-pipeline-dag-view/index.html` + `variant-b-graph-canvas.png` — the chosen DAG layout reference.

### Prior-phase locked context to honor
- `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-CONTEXT.md` — Phase-34 decisions Phase 35 BUILDS ON and partially REVERSES: `$store.pipeline` OOB-swap pattern, `/pipeline/stats` 5s poll, `get_queue_activity`, DB-derived progress denominator rationale, and the D-09/D-20/21/22 auto-extract behavior being reversed by D-06.

### Deterministic key + enqueue seam (Phase 30/32 patterns to generalize)
- `src/phaze/services/analysis_enqueue.py:32` — `process_file_job_key()` + `enqueue_process_file()`; THE deterministic-key pattern to replicate (`process_file:<file_id>`, `key=`, shared by both producers).
- `src/phaze/services/enqueue_router.py` — CONTROLLER_TASKS vs AGENT_TASKS routing; non-revoked-agent predicate; the unroutable-task ValueError guard (v4.0.6). Candidate central key seam.
- `src/phaze/services/agent_task_router.py:99,148` — `enqueue_for_agent()` (`queue.enqueue(task_name, **dumped)` — no `key=` today); per-agent queue construction registers the before_enqueue hook.
- `src/phaze/tasks/_shared/queue_defaults.py:62` — `apply_project_job_defaults` before_enqueue hook (timeout/retries/ttl). The proven hook seam to extend for central key enforcement (verify it can set `job.key`).
- `src/phaze/main.py:103` — `register_before_enqueue` wiring on controller queue; mirrored in `tasks/controller.py:139`, `tasks/agent_worker.py:185`.

### Enqueue sites to convert (9 random-key sites)
- `src/phaze/routers/pipeline.py` — `extract_file_metadata` (`:305`), `fingerprint_file` (`:377`), `generate_proposals` (`:70`).
- `src/phaze/services/tracklists.py` — `scan_live_set` (`:232`), `search_tracklist` (`:458`), `scrape_and_store_tracklist` (`:384`), `match_tracklist_to_discogs` (`:658`).
- `src/phaze/routers/agent_files.py:143` + `src/phaze/services/ingestion.py:190` — `extract_file_metadata` auto-enqueue paths to REMOVE (D-06).

### Idempotency audit targets
- `src/phaze/services/proposal.py:249` — `store_proposals()` raw INSERTs (`session.add` + commit); convert to upsert per D-04.
- `src/phaze/routers/agent_execution.py:77` — `execution_log` already idempotent (`on_conflict_do_nothing(index_elements=["id"])`) — reference for the pattern; verify only.
- `src/phaze/routers/agent_metadata.py:61`, `agent_fingerprint.py:40` — existing `on_conflict_do_update` examples to mirror.

### Dashboard / progress UI + counters
- `src/phaze/templates/pipeline/partials/stage_cards.html` — 4 action buttons to fold into DAG nodes (D-01).
- `src/phaze/templates/pipeline/partials/processing_card.html` — aggregate card to REMOVE (D-01).
- `src/phaze/templates/pipeline/dashboard.html` — page shell where the DAG canvas slots in.
- `src/phaze/services/pipeline.py:34,47,107` — `get_pipeline_stats()` (DB stage counts for reconcile), `get_queue_activity()`, `queue_progress_percent()`.
- `src/phaze/routers/pipeline.py` — `dashboard()` (full-page seed) + `pipeline_stats_partial()` (`/pipeline/stats` poll) contexts need the per-function counter data.
- `src/phaze/services/proposal.py:221` — `check_rate_limit()` Redis `INCR`+`EXPIRE` precedent for maintained counters.

### Tests + fakes
- `tests/_queue_fakes.py` — `FakeQueue`/`DedupFakeQueue` (models SAQ dedup no-op on duplicate `key`), `FakeTaskRouter`/`DedupFakeTaskRouter`, `install_fake_queues`/`wire_fakes`, `seed_active_agent`. Use `DedupFakeQueue` to assert deterministic-key dedup; extend for counter-hook capture.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `process_file_job_key()` / `enqueue_process_file()` (`analysis_enqueue.py`) — exact template for per-task keyed enqueue helpers.
- `apply_project_job_defaults` before_enqueue hook — the registration seam to extend for central key enforcement (D-05).
- `$store.pipeline` + OOB-swap + 5s `/pipeline/stats` poll (Phase 34) — reuse for live DAG node counts; no new poll loop / no SSE.
- `check_rate_limit` Redis `INCR`/`EXPIRE` (`proposal.py:221`) — pattern for maintained per-function counters.
- `DedupFakeQueue` (`tests/_queue_fakes.py`) — already models duplicate-key dedup; ready for determinism tests.

### Established Patterns
- Idempotent upserts via `pg_insert(...).on_conflict_do_update/nothing` are the house style (metadata, fingerprint, execution_log) — `generate_proposals` is the lone outlier.
- All enqueues already route through `enqueue_router` / `agent_task_router` (Phase 30) — a real central choke point exists for D-05.
- Dashboard reads DB stage counts AND live SAQ queue depth (Phase 34) — DB reconcile (D-03) extends existing `get_pipeline_stats`.

### Integration Points
- Central key seam: `enqueue_router`/`agent_task_router` or the before_enqueue hook — every enqueue already passes through here.
- Counter increment: enqueue-side via before_enqueue hook; completion-side best-effort; reconcile-on-read in `get_pipeline_stats`/the new counters service.
- DAG canvas swaps into `dashboard.html` and is fed by the extended `/pipeline/stats` context.

</code_context>

<specifics>
## Specific Ideas

- DAG = sketch 001 Variant B exactly (graph canvas, curved edges from node anchors, compact node chips with live count + per-stage progress bar + gated trigger button). Live-ish states the sketch demonstrated: Discovery done · Metadata done · Analyze N/total (active) · Fingerprint disabled (agent busy) · Proposals disabled (waiting on Analyze) · Execute gated.
- Disabled-button reasons must read clearly on the node ("Waiting on Analyze", "Agent busy", "Needs tracklist") — carried from the sketch's what-to-look-for list.
- Counters self-heal: dashboard must never show a permanently-wrong "done" after a purge/restart (direct lesson from the 2026-06-11 doubling incident).
- Ships as a subsequent v4.0.x → GHCR publish → homelab redeploy.

</specifics>

<deferred>
## Deferred Ideas

- Per-stage trigger of the tracklist sub-chain as individual DAG node buttons beyond the existing endpoints — only surface triggers for stages that already have endpoints; net-new endpoints are out of scope unless a scoped item requires them.
- Animated edge/flow effects on the DAG canvas — visual polish, not required for the functional graph.
- None of the above changes phase scope; captured so they aren't lost.

### Reviewed Todos (not folded)
None — no pending todos matched this phase.

</deferred>

---

*Phase: 35-pipeline-determinism-idempotency-per-job-type-observability*
*Context gathered: 2026-06-11*
