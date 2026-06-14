# Phase 42: Recovery-Only Pipeline Automation - Context

**Gathered:** 2026-06-14
**Status:** Ready for planning
**Source:** Inline operator discussion + 42-RESEARCH.md (HIGH confidence)

<domain>
## Phase Boundary

Enforce the theme's core principle: **the ONLY automatic enqueue is a restart/queue-loss recovery pass** that restores in-flight work; there is NO steady-state auto-advance. The finale of Phases 39-42.

Key reframe (from research): Phase 36 migrated the SAQ broker Redis→Postgres (`saq_jobs` table, `PostgresQueue`). Queued/active jobs now **survive a controller restart** — SAQ re-dequeues them itself. So the old `reenqueue_discovered` premise ("Redis is empty after a reboot") is obsolete, and a genuine "queue-loss" is now a rare, *detectable* asymmetry: `saq_jobs` has zero queued/active rows while the domain DB still shows pending work.

**In scope:** remove the steady-state auto-advance cron; add a gated, all-stages recovery pass (startup + manual button); idempotent via existing deterministic keys; tests.
**Out of scope:** the Phases 39-41 manual trigger nodes (keep working); the `trigger_scan` file_id-only dead-letter fix (separate follow-up).
</domain>

<decisions>
## Implementation Decisions

### D1 — Remove the steady-state auto-advance (LOCKED)
- DELETE the `CronJob(reenqueue_discovered, cron="*/5 * * * *")` entry (`controller.py:185`). Per research it is the ONLY steady-state auto-enqueuer in Phaze.
- KEEP `reap_stalled_scans` (every-minute stall reaper — recovery/liveness, not auto-advance) and KEEP `refresh_tracklists` (monthly maintenance re-scrape of existing tracklist data — operator chose to keep it; it is NOT pipeline auto-advance).

### D2 — Recovery design: HYBRID, gated, all-stages (LOCKED)
- Replace `reenqueue_discovered` with a single `recover_orphaned_work(ctx)` that reconciles **ALL stages** (metadata, analyze, fingerprint, proposals, tracklist), re-enqueuing each stage's pending set.
- **Gate (queue-loss detector):** run the reconcile ONLY when `saq_jobs` has **zero queued+active rows** AND the **DB shows pending work**. On a normal/durable restart (jobs survived) this is a no-op — recovery fires only after a genuine wipe. Run the gate+reconcile once on controller **startup** (replacing the current unconditional boot-time `reenqueue_discovered(ctx)` call at `controller.py:114`).
- **Manual "Recover" button** on the DAG that POSTs to an endpoint invoking the SAME `recover_orphaned_work` producer (so the automatic and manual paths can never drift). The button is the safety net for cold-boot (no agent yet) and operator-initiated recovery. Placement: a global pipeline action (e.g. near Discovery / pipeline header), not a per-stage node — planner decides exact placement, keep it visually distinct from the per-stage triggers.

### D3 — All stages on loss (LOCKED)
- After a detected loss, restore the FULL eligible set across every stage — bring the pipeline back to where it was. Reuse the EXACT pending-set queries the Phase 39-41 manual DAG triggers use (and `get_stage_progress` done/total), so manual and recovery paths share one definition of "pending" and cannot drift.

### D4 — Idempotency / safety (LOCKED — research-confirmed)
- All 8 routable functions are deterministically keyed at the single `before_enqueue` chokepoint (`deterministic_key.py:74-83`), so a reconcile re-enqueue dedups against any surviving live job — no doubling (ref the Phase 32 queue-doubling incident). CARE POINT: `generate_proposals` uses a set-hash/batch key — recovery MUST reuse the identical batch query so its key matches; verify during planning.
- Recovery must NEVER abort controller boot (existing rule, `controller.py:111` — broad try/except, log, continue).

### D5 — Cold-boot / no-agent (Claude's Discretion — research Open Q1)
- On cold boot the agent stages (analyze/fingerprint/scan) may have no active agent yet, so those re-enqueues skip (NoActiveAgentError) — the manual Recover button is the documented safety net to re-run once an agent is online. Controller-side stages (proposals, tracklist search/scrape/match) reconcile regardless.

### Manual-only principle (LOCKED, theme-wide)
- After this phase, steady-state produces ZERO automatic enqueues. The only auto-enqueue is the gated recovery pass.
</decisions>

<specifics>
## Specific Ideas

- Current automation to change: `src/phaze/tasks/controller.py` startup hook (`~107-118`, the boot-time `reenqueue_discovered(ctx)` call) and `settings["cron_jobs"]` (`~176-186`, remove the 5-min entry; keep refresh_tracklists + reap_stalled_scans). `reenqueue_discovered` is also in `settings["functions"]` (line 173) — replace with the new recover function (keep it a registered task so SAQ can run it).
- New producer `recover_orphaned_work(ctx)` lives in (or replaces) `src/phaze/tasks/reenqueue.py`. Reuse `select_active_agent` / `AgentTaskRouter` (already in ctx) for agent-stage routing and `enqueue_router.resolve_queue_for_task` for controller stages.
- Pending-set queries to reuse (one source of truth): the Phase 39-41 service helpers in `src/phaze/services/pipeline.py` (search/scan/scrape/match pending + busy) and the metadata/analyze/fingerprint eligible-file queries used by the `/pipeline/extract-metadata|analyze|fingerprint` endpoints (`routers/pipeline.py`), plus `get_stage_progress`.
- Queue-loss detector: count `saq_jobs` rows with status in ('queued','active') (the same `saq_jobs` scans used by `get_search_busy_count` etc.) — zero across all stages = "queue empty"; combine with "DB has pending work" from the pending-set queries. Degrade-safe.
- Manual Recover endpoint: mirror the Phase 39-41 bulk-trigger endpoints (`routers/pipeline.py`, HTMX partial, background task) → calls `recover_orphaned_work`.

</specifics>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

- `.planning/milestones/v4.0-phases/42-recovery-only-pipeline-automation-gate-reenqueue-discovered-/42-RESEARCH.md` — full mechanics, per-stage needs-recovery queries, durability reframing, idempotency confirmation, options analysis (recommended D).
- `src/phaze/tasks/controller.py` — startup hook + cron_jobs + functions list (the edit sites).
- `src/phaze/tasks/reenqueue.py` — `reenqueue_discovered` (the function being generalized/replaced).
- `src/phaze/services/pipeline.py` — `get_stage_progress`, the Phase 39-41 pending/busy helpers (the pending-set source of truth).
- `src/phaze/services/enqueue_router.py`, `src/phaze/services/analysis_enqueue.py` — routing + keyed enqueue helpers.
- `src/phaze/tasks/_shared/deterministic_key.py` — the keys that make recovery idempotent (note generate_proposals batch key).
- `src/phaze/routers/pipeline.py` — bulk-trigger endpoints to mirror for the manual Recover button; the metadata/analyze/fingerprint eligible-file queries.
- `src/phaze/templates/pipeline/partials/dag_canvas.html` — where the Recover button lands (global pipeline action).
- Tests: existing reenqueue/startup tests (grep `reenqueue_discovered`), `tests/integration` stage_env fixtures, `tests/test_routers/test_pipeline.py`, `tests/test_services/test_pipeline.py`.

</canonical_refs>

<deferred>
## Deferred Ideas

- `trigger_scan` file_id-only dead-letter fix → separate follow-up PR (found in Phase 40).
</deferred>

---

*Phase: 42-recovery-only-pipeline-automation*
*Context gathered: 2026-06-14 via inline operator discussion + 42-RESEARCH.md*
