# Phase 55: Routing, state & ledger integration (the live seam) - Context

**Gathered:** 2026-06-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Wire Kubernetes (Kueue) in as the third cloud analysis target, selected by a single
`cloud_target` config value, into the existing v5.0 duration-router / `stage_cloud_window` /
scheduling-ledger seam — as **one new branch**, not a parallel pipeline. This is the ONLY
phase that edits the live v5.0 seam, kept last among code phases to minimize the
partially-integrated window.

In scope (KROUTE-01..05, plus KROUTE-06 pulled in — see D-04):
- `cloud_target` selector + routing of ≥threshold long files to the K8s path.
- Reuse of the duration router + AWAITING_CLOUD hold + advisory-locked `stage_cloud_window`
  in-flight window (`cloud_max_in_flight`) — long files only, never a whole-backlog sweep.
- Reuse of `PUSHING`/`PUSHED` FileRecord states for K8s in-flight files; new `cloud_phase`
  admission column on `cloud_job` (Alembic migration) — FileRecord state machine unchanged.
- Static AST guard test: every K8s enqueue site routes through `enqueue_router`.
- Ledger-scoped backfill of timed-out (`analysis_failed`) long files to K8s.

Out of scope (own phases): the live Kueue cluster / RBAC / runbook / `_FILE` secrets /
master-toggle docs polish are Phase 56 (KDEPLOY) — EXCEPT the portion of the fail-fast
validator that D-02 necessarily pulls forward (see below).
</domain>

<decisions>
## Implementation Decisions

### K8s branch point in the staging window (KROUTE-02, KROUTE-03)
- **D-01: K8s reuses the in-flight window as ONE branch at TWO coordinated fork points, keyed on `cloud_target`.**
  (a) In `stage_cloud_window` (`tasks/release_awaiting_cloud.py`): `a1` enqueues `push_file`
  (file-server rsync) as today; `k8s` enqueues the Phase 53 S3-staging path
  (`cloud_staging.stage_file_to_s3` → `s3_upload`) instead. Both flip `AWAITING_CLOUD → PUSHING`
  and reuse the existing window math (advisory lock, FIFO `FOR UPDATE SKIP LOCKED`,
  `window = COUNT(PUSHING|PUSHED)`, `slots = cloud_max_in_flight - window`).
  (b) In the post-staging callback: the `a1` `report_pushed` path enqueues `process_file` on the
  COMPUTE queue (as today); the `k8s` path enqueues `submit_cloud_job` (controller queue, Phase 54)
  once bytes are in S3 — the pod fetches via the Phase 53 presigned GET, so **K8s skips the
  file-server rsync entirely.** Reuses `PUSHING`/`PUSHED` (no new FileRecord state, KROUTE-03).
  Rejected: making `submit_cloud_job` do its own S3 staging (couples staging into submit, breaks
  the Phase 53/54 seam boundaries).

### `cloud_target` selector + master toggle (KROUTE-01)
- **D-02: HARD REPLACE — `cloud_burst_enabled` is REMOVED; `cloud_target: Literal["local","a1","k8s"]`
  (default `"local"`, `"local"` == cloud off) is the single source of truth.** No back-compat alias.
  This intentionally **amends KROUTE-01's "under the existing `cloud_burst_enabled` master toggle"
  wording** (flag for milestone audit). Consequences the planner MUST handle IN THIS PHASE:
  - Rewrite the existing `cloud_burst_enabled`-coupled model validators in `config.py` (the
    `compute_scratch_dir` fail-fast at ~608/631) to key off `cloud_target` (`a1` requires
    `compute_*`).
  - Add the `cloud_target == "k8s"` fail-fast validator (requires `kube_api_url`/`kube_namespace`/
    `kube_local_queue`) — this **pulls the K8s portion of KDEPLOY-02 forward** from Phase 56.
  - Migrate every operator-facing config surface in this phase: `.env.example`, `.env.example.agent`,
    `docker-compose*.yml`, the Phase 51 homelab runbook, and `docs/` — or cloud silently goes
    local on redeploy.

### Backfill trigger & ledger scoping (KROUTE-05)
- **D-03: Backfill is an operator-initiated "Backfill to K8s" action on the pipeline dashboard,
  ledger-scoped.** Re-drives ONLY files that are `analysis_failed` AND `duration ≥ cloud_route_threshold_sec`
  AND carry a prior scheduling-ledger row (previously-scheduled work only — mirrors v5.0 exactly).
  Bounded and explicit; NOT a cron sweep (avoids the v4.0.6 / v5.0 whole-backlog over-enqueue
  incidents). Routes through `enqueue_router`, enforced by the KROUTE-04 AST guard.

### `cloud_phase` column + admission cards (KROUTE-03, KROUTE-06)
- **D-04: `cloud_phase` is a small enum on `cloud_job`** (`queued_behind_quota` / `admitted` /
  `running` / `finished`) added via a new Alembic migration. The **Phase 54 reconcile cron
  (`reconcile_cloud_jobs._reconcile_one`) is extended to write `cloud_phase`** alongside `status`
  as it maps Job/Workload conditions; `submit_cloud_job` seeds the initial value on the SUBMITTED
  row. **KROUTE-06 dashboard admission-state cards ship in this phase** — a thin read over
  `cloud_phase`, mirroring the Phase 54 `get_inadmissible_count` + `inadmissible_card.html` pattern.

### Claude's Discretion
- Exact enum storage form for `cloud_phase` (CHECK-constrained varchar vs StrEnum) — follow the
  `CloudJobStatus` precedent from Phase 54.
- Whether the `cloud_target` routing branch lives in the duration router entry point or inside
  `stage_cloud_window` — planner/researcher to resolve against the actual call graph.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Prior-phase decisions (the seam this phase integrates)
- `.planning/phases/54-kube-submit-watch-reconcile-cron/54-CONTEXT.md` — D-01..D-09; the
  `submit_cloud_job` producer + reconcile-cron contract this phase triggers; D-09 reserved
  `cloud_phase` for THIS phase.
- `.planning/phases/53-s3-object-staging-leg/53-CONTEXT.md` — D-02 (inline S3 object delete on
  terminal), D-03 (`cloud_job` sidecar reservation); the S3 staging path K8s reuses.
- `.planning/REQUIREMENTS.md` — KROUTE-01..06 (note D-02 amends KROUTE-01).
- `.planning/ROADMAP.md` — Phase 55 goal ("one new branch", "the one live-seam edit").

### Live-seam code to integrate with
- `src/phaze/tasks/release_awaiting_cloud.py` — `stage_cloud_window` (window math + advisory lock);
  the D-01 stage-side branch point.
- `src/phaze/routers/agent_push.py` — `report_pushed` callback (`PUSHING→PUSHED` + `process_file`
  enqueue); the D-01 post-staging branch point for `a1`.
- `src/phaze/services/cloud_staging.py` — `stage_file_to_s3` / `s3_upload` trigger (Phase 53); the
  K8s "push".
- `src/phaze/tasks/submit_cloud_job.py` — `submit_cloud_job` + `submit_cloud_job_key` (Phase 54);
  the K8s post-staging enqueue.
- `src/phaze/tasks/reconcile_cloud_jobs.py` — `_reconcile_one` (Phase 54); extend to write `cloud_phase`.
- `src/phaze/services/enqueue_router.py` — `CONTROLLER_TASKS` / `resolve_queue_for_task`; routing surface.
- `src/phaze/config.py` — `cloud_route_threshold_sec`, `cloud_max_in_flight`, the
  `cloud_burst_enabled`-coupled validators to rewrite (D-02).
- `tests/test_routing_seam.py`, `tests/test_task_split.py` — the AST-guard pattern for KROUTE-04.
- `src/phaze/services/pipeline.py` + `src/phaze/templates/pipeline/partials/inadmissible_card.html` —
  the `get_inadmissible_count`/card pattern to mirror for the KROUTE-06 admission cards.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `stage_cloud_window` (advisory-locked, FIFO `SKIP LOCKED`, window/slots math) — the single
  in-flight gate; add the `cloud_target` branch rather than a parallel cron.
- `report_pushed` callback transaction (idempotent `PUSHING→PUSHED` + ledger clear + enqueue) — the
  template for the K8s post-staging enqueue of `submit_cloud_job`.
- Phase 53 `cloud_staging.stage_file_to_s3` + `s3_upload` (presigned multipart) — the K8s byte path.
- Phase 54 `submit_cloud_job` (controller queue, idempotent) + `reconcile_cloud_jobs` (lifecycle).
- v5.0 scheduling ledger — backfill scoping (only previously-scheduled work).
- Phase 54 `get_inadmissible_count` + `inadmissible_card.html` (degrade-safe `_safe_count`, OOB
  poll push) — the model for KROUTE-06 admission-state cards.

### Established Patterns
- All cloud enqueues route through `enqueue_router` (Phase 30 invariant); the KROUTE-04 AST guard
  extends `test_routing_seam.py` / `test_task_split.py`.
- `cloud_job` additive, reversible migrations scoped to `cloud_job` only (never `saq_jobs`) —
  Phase 53/54 precedent (migrations 025/026) for the `cloud_phase` migration.
- CHECK-constrained status enums (`CloudJobStatus`, Phase 54) — precedent for `cloud_phase`.

### Integration Points
- Duration router / `stage_cloud_window`: the `a1`-vs-`k8s` stage-side fork (D-01a).
- Staging callback (`report_pushed` for a1 / S3-upload completion for k8s): the post-staging fork
  to `process_file` vs `submit_cloud_job` (D-01b).
- `config.py` validators: `cloud_burst_enabled` removal + `cloud_target` fail-fast (D-02).
- `reconcile_cloud_jobs._reconcile_one`: `cloud_phase` writes (D-04).
- Pipeline dashboard: KROUTE-06 admission cards (D-04).
</code_context>

<specifics>
## Specific Ideas

- The user wants the **single `cloud_target` setting to be the whole story** — no master-toggle
  layering. A hard, clean replace of `cloud_burst_enabled`, accepting the in-phase config/docs
  migration cost.
- Strong preference for the **operator staying in control** of any backfill (explicit dashboard
  action, ledger-scoped) over any automatic sweep — directly informed by the prior over-enqueue
  incidents.
</specifics>

<deferred>
## Deferred Ideas

- **KROUTE-01 wording amendment:** D-02 (hard-replace the master toggle) diverges from the
  requirement's "under the existing `cloud_burst_enabled` master toggle" text. Surface this at
  `/gsd:audit-milestone` so REQUIREMENTS.md / the milestone intent is reconciled.
- **Phase 56 (KDEPLOY) scope reduction:** the K8s fail-fast validator portion of KDEPLOY-02 is
  absorbed into Phase 55 by D-02. Phase 56 should drop that sub-item and focus on the live cluster
  runbook / RBAC / `_FILE` secrets / transport-agnostic endpoints / Agents-UI ephemeral-identity note.

</deferred>

---

*Phase: 55-routing-state-ledger-integration-the-live-seam*
*Context gathered: 2026-06-28*
