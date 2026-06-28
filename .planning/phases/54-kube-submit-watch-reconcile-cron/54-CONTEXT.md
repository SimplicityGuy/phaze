# Phase 54: Kube submit / watch + reconcile cron - Context

**Gathered:** 2026-06-27
**Status:** Ready for planning

<domain>
## Phase Boundary

Build the **Kube submit + reconcile** control-plane choreography for the v6.0
Kubernetes Burst leg: the control plane submits one **suspended** per-file Kueue
`batch/v1` Job (via **kr8s**), a **fast submit task** that returns in seconds without
blocking a worker, and a **periodic reconcile cron** that owns Workload lifecycle,
cleanup, and bounded re-drive — with the out-of-band `/api/internal/agent/*` callback
as the **only** authoritative result channel.

Locked by KSUBMIT-01..06 (see REQUIREMENTS.md):
- submit a single **suspended** `batch/v1` Job (`kueue.x-k8s.io/queue-name=<LocalQueue>`,
  `restartPolicy: Never`, `parallelism: 1`, cpu/memory **requests only**), **idempotently**
  via a deterministic name keyed to `file_id` (KSUBMIT-01)
- the **submit task returns within seconds** and never blocks a worker; the **reconcile
  cron** owns Workload status, cleanup, and re-drive (KSUBMIT-02)
- the result is authoritative **only** via the out-of-band callback reconciled by
  `file_id`; a dropped/expired kube watch never loses or duplicates a result (KSUBMIT-03)
- reconcile distinguishes healthy **`Pending`** (queued behind quota — waits indefinitely)
  from **`Inadmissible`** (misconfigured LocalQueue/ClusterQueue — surfaced to the
  operator) and detects success / failure / eviction (KSUBMIT-04)
- on failure/eviction the file is **re-driven** through the K8s staging window up to a
  bounded **max-attempts cap**, then marked `ANALYSIS_FAILED` — **no cross-target
  fallback** in v6.0; Job `backoffLimit`/Kueue requeue are neutralized so the control
  plane solely owns retry (KSUBMIT-05)
- finished Jobs are cleaned up **without a TTL-vs-read race**; **no `process_file:<id>`
  ledger row is seeded for K8s files** so `recover_orphaned_work` never re-enqueues them
  onto an agent queue (the CLOUDROUTE-02 hazard) (KSUBMIT-06)

This is the **highest-risk phase** but fully **testable against a fake kube API** (no live
cluster). The S3 staging leg it builds on is Phase 53; the routing/`stage_cloud_window`
K8s branch that *triggers* submission is **Phase 55**; deploy/RBAC/runbook is **Phase 56**.

</domain>

<decisions>
## Implementation Decisions

### Watch model & reconcile loop (KSUBMIT-02, KSUBMIT-03)
- **D-01: Cron-only poll — NO live kube watch stream.** The reconcile cron lists/re-reads
  in-flight Workloads/Jobs each tick and reconciles; the out-of-band `/api/internal/agent/*`
  callback IS the real-time completion signal, and the cron is the safety net for
  eviction / Inadmissible / failure / cleanup. No long-lived watch task, no
  reconnect/expired-stream handling. Matches the KSUBMIT-02/03 framing ("reconcile cron
  owns status", "a dropped or expired kube watch never loses a result") and the FEATURES.md
  anti-pattern "treat a long-lived watch as the only completion signal". For an analysis
  that runs multi-hour, sub-minute responsiveness buys nothing. Testable against a fake
  kube **list** API.
- **D-02: The `cloud_job` rows are the durable in-flight registry the cron iterates.**
  Because KSUBMIT-06 forbids a ledger seed, the cron finds work to reconcile by reading
  `cloud_job` rows in a submitted/in-flight state (the `kueue_workload` Job name is
  recorded there at submit time) and reconciles each against the kube API. The DB sidecar
  — not a kube watch, not `recover_orphaned_work` — is the source of in-flight truth;
  survives restarts; never touches the agent-queue ledger.
- **D-03: Reconcile cadence = every 5 minutes** (`*/5 * * * *`), matching the existing
  `stage_cloud_window` cron family on the controller. Plenty responsive for hour-scale
  work + long Kueue quota waits; trivial kube-API/DB load; comfortable margin for the
  TTL-vs-read ordering (D-04). (Not a config knob in this phase — fixed cron, consistent
  with `reap_stalled_scans` / `stage_cloud_window`.)

### Job & object cleanup (KSUBMIT-06, ties to Phase 53 D-02)
- **D-04: phaze explicitly deletes the Job after reconcile records the terminal outcome.**
  A generous `ttlSecondsAfterFinished` is set as a backstop, but the control plane owns
  lifecycle: deletion happens only *after* the DB reflects the result, so the status read
  can never lose to GC. The TTL only covers the "phaze never reconciled" orphan case. This
  is FEATURES.md's "single most important ordering decision", resolved toward explicit
  delete. Deterministic; testable against a fake kube delete.
- **D-05: Reconcile deletes the staged S3 object on no-callback terminal outcomes**
  (eviction, lost pod, Job `Failed` before POSTing). It calls the existing Phase 53
  `delete_staged_object(file_id)` capability (53-CONTEXT D-02 explicitly reserved this for
  Phase 54's reconcile) before/at Job deletion — closing the leak the instant phaze knows
  the object is dead, rather than waiting on the bucket lifecycle TTL. Reuses the Phase 53
  delete; no new delete logic. (The success path still deletes inline in the result
  callback per Phase 53 D-02.)

### Inadmissible surfacing (KSUBMIT-04)
- **D-06: Inadmissible → pipeline-UI alert + WARNING log.** When reconcile finds a
  Workload stuck `Inadmissible` (misconfigured LocalQueue/ClusterQueue), surface it on the
  pipeline DAG dashboard (the existing Phase 34 queue-depth / Phase 44 analyze-observability
  surface), driven off a flag on the `cloud_job` row, plus a WARNING log line. Healthy
  `Pending` (quota wait) stays silent. The operator sees "K8s Jobs not admitting — check
  LocalQueue config" without tailing logs.
- **D-07: Inadmissible holds indefinitely and does NOT consume the re-drive cap.** It's an
  operator-config fault, not a transient analysis failure — re-submitting won't help (same
  broken queue). Reconcile alerts and leaves the file held (Job stays suspended/queued)
  without counting attempts or marking `ANALYSIS_FAILED`. Once the operator fixes the
  LocalQueue, Kueue admits the existing Job and it proceeds. Mirrors the "Pending waits
  indefinitely" stance, but loud instead of silent.

### Re-drive cap & `cloud_job` state model (KSUBMIT-05, KSUBMIT-06)
- **D-08: New `cloud_submit_max_attempts` ControlSettings knob, default ~3**, mirroring the
  existing `push_max_attempts` pattern (`config.py:421`), resolved via the same env/`_FILE`
  machinery. Attempt count tracked on the `cloud_job` row. A re-drive = re-stage through
  the K8s window (submit a fresh Job). Distinct knob from `push_max_attempts` so the
  kube-submit retry budget and the rsync-push retry budget tune independently. After the
  cap → `ANALYSIS_FAILED` (no cross-target fallback, KSUBMIT-05).
- **D-09: Extend `CloudJobStatus` + add `kueue_workload` and `attempts` columns in Phase
  54's own migration.** Add new string-backed `CloudJobStatus` members for the
  submit/reconcile lifecycle (e.g. submitted/running/succeeded/failed — planner finalizes
  the exact members) — only the CHECK-constraint membership list changes, no enum-type
  migration (per the model's documented design). Add `kueue_workload` (the Kueue Job name,
  already reserved for Phase 54 by 53-CONTEXT D-03) and an `attempts` integer.
  **`cloud_phase` is left untouched for Phase 55** (routing/orchestration concern). Each
  migration stays scoped to its phase.

### Claude's Discretion (planner/researcher decide, grounded in FEATURES.md)
- Exact suspended-Job spec details: `backoffLimit` value (0 vs 1) and the precise mechanism
  to neutralize Kueue requeue, the `ttlSecondsAfterFinished` value (must outlast the 5-min
  reconcile per D-04), resource `requests` sizing (reuse Phase 52's measured peak-RSS).
- Submit-task shape & idempotency guard ("Job already exists" handling), deterministic Job
  name scheme keyed to `file_id`, and where it routes through `enqueue_router` (built here,
  wired into `stage_cloud_window` only in Phase 55).
- kr8s client construction / kubeconfig & namespace surface on `ControlSettings` (`_FILE`
  secrets; RBAC/least-privilege is Phase 56's, but the config field shapes land where this
  phase needs them).
- How reconcile maps kube conditions → outcomes: Job `status.succeeded`/`Complete`,
  `status.failed`/`Failed`, Workload `Evicted`/`WorkloadInactive`, `QuotaReserved=False`
  reason `Pending` vs `Inadmissible` (FEATURES.md §"Status signals" table).
- The fake-kube test harness shape (respx against the kr8s HTTP surface vs a fake client).
- Exact new `CloudJobStatus` member names and the precise pipeline-UI alert element.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase scope & requirements
- `.planning/ROADMAP.md` — Phase 54 line + the v6.0 Kubernetes Burst Analysis intro (the
  image → S3 leg → submit/reconcile → routing seam → deploy spine). Note the dependencies:
  Phase 53 produces the presigned GET URL baked into the Job spec; Phase 52 is the Job
  image (execution unit); Phase 55 wires the routing trigger; Phase 56 owns deploy/RBAC.
- `.planning/REQUIREMENTS.md` §"Kube submission, watch & reconcile (KSUBMIT)" — KSUBMIT-01..06
  are the locked requirements; KSUBMIT-07/08/09 are explicitly **out of scope** for v6.0
  (runaway `maximumExecutionTimeSeconds` guard, cross-target re-route, multi-file/elastic
  Jobs).
- `.planning/research/FEATURES.md` — the v6.0 Kubernetes Burst research. **Most important
  ref for this phase.** Contains: the status-signals table (success/failure/eviction,
  `Pending` vs `Inadmissible`), the TTL-vs-read ordering hazard ("single most important
  ordering decision" — drove D-04), the "watch as fast path + periodic reconcile" vs
  cron-only framing (drove D-01), anti-patterns (no log-scraping, no watch-as-sole-signal,
  no Kueue requeue for app retry), and the Context7 `/kubernetes-sigs/kueue` source notes.

### Prior-phase context (the legs this phase joins)
- `.planning/phases/53-s3-object-staging-leg/53-CONTEXT.md` — D-02 (inline object delete on
  result callback; reconcile invokes the same delete for evicted Jobs → drove D-05), D-03
  (`cloud_job` is staging-only now; `kueue_workload` reserved for Phase 54, `cloud_phase`
  for Phase 55 → drove D-09).
- `.planning/phases/52-job-runner-image-one-shot-entrypoint/52-CONTEXT.md` — the Job image
  (execution unit), the one-shot entrypoint, exit-code contract, and `request_download_url`
  (the just-in-time presigned-GET call made post-admission at pod startup).

### Models, tasks & config to build on / mirror
- `src/phaze/models/cloud_job.py` — `CloudJob` + `CloudJobStatus` (string-backed StrEnum,
  CHECK-constraint membership gate). D-09 extends this in Phase 54's own migration.
- `src/phaze/models/` + Alembic migrations dir — follow the existing model/migration
  conventions; the v5.0 `scheduling_ledger.py` per-`file_id` sidecar is the precedent.
- `src/phaze/tasks/controller.py` (`cron_jobs` block, ~line 214-235) — where the reconcile
  CronJob is registered alongside `stage_cloud_window` (`*/5`), `reap_stalled_scans`,
  `refresh_tracklists`. **Read the Phase 42/50 comments**: do NOT re-add a general
  auto-advance / `recover_orphaned_work` cron; the reconcile cron is narrow (in-flight
  K8s reconcile only), like `stage_cloud_window`.
- `src/phaze/tasks/reenqueue.py` — `recover_orphaned_work` + the CLOUDROUTE-02 hazard
  (lines ~196, 328-352): why K8s files must NOT seed a `process_file:<id>` ledger row
  (KSUBMIT-06). The reconcile loop owns K8s re-drive; the ledger/recovery path must never
  touch these files.
- `src/phaze/routers/agent_analysis.py` — the `/api/internal/agent/*` result callback
  (authoritative result channel, KSUBMIT-03; Phase 53 D-02 inline S3 delete lives here).
- `src/phaze/config.py` — `ControlSettings`, `_FILE`-secret machinery
  (`SECRET_FILE_FIELDS`, `_resolve_secret_files`), and `push_max_attempts` (line 421) /
  `cloud_max_in_flight` (line 411) as the precedent for D-08's `cloud_submit_max_attempts`
  and the kr8s/kubeconfig config fields.
- `src/phaze/services/enqueue_router.py` — `resolve_queue_for_task`; the single enqueue
  seam the fast submit task must route through (Phase 30 invariant; AST-guarded in Phase 55).

### Pipeline-UI surface (for D-06)
- The Phase 34 queue-depth / Phase 44 analyze-observability pipeline DAG dashboard
  (routers/templates under `src/phaze/routers/pipeline.py` + the pipeline templates) — where
  the Inadmissible alert surfaces.

### External docs (verified same-day per milestone note — no research-phase)
- Context7 `/kubernetes-sigs/kueue` — suspended Job + `queue-name` label (KEP-973),
  `QuotaReserved` reasons `Pending`/`Inadmissible`, `Evicted`/`WorkloadInactive`
  (KEP-349/1282), Finished-condition reconcile (KEP-369). kr8s async API for submit/list/delete.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `CloudJob` / `CloudJobStatus` (Phase 53) — the per-`file_id` sidecar; extend the enum +
  add `kueue_workload`/`attempts` columns (D-09) for the kube lifecycle.
- `delete_staged_object(file_id)`-style S3 delete (Phase 53 D-02) — reused by reconcile on
  no-callback terminal outcomes (D-05); no new delete logic.
- `stage_cloud_window` CronJob registration pattern (controller.py) — the narrow,
  recovery-scoped cron precedent the reconcile cron mirrors (every `*/5`).
- `push_max_attempts` config + the v5.0 re-drive loop shape (tasks/push.py,
  routers/agent_push.py) — precedent for the bounded re-drive + attempts tracking (D-08).
- `enqueue_router.resolve_queue_for_task` — the single enqueue seam for the fast submit task.
- `config.py` `_FILE`-secret machinery — auto-resolves S3/kube secret `_FILE` siblings.

### Established Patterns
- **Callback authority + reconcile-by-`file_id` (v4.0/v5.0):** the result lands only via
  `/api/internal/agent/*`; all kube state is keyed to `file_id`. KSUBMIT-03.
- **No ledger seed for cloud files (CLOUDROUTE-02):** K8s files must never enter the
  `process_file:<id>` ledger that `recover_orphaned_work` replays — that would re-enqueue
  them onto a local agent queue. KSUBMIT-06. The `cloud_job` row is the in-flight registry
  instead (D-02).
- **Narrow recovery-only crons (Phase 42/50):** the controller deliberately has NO general
  auto-advance cron; the reconcile cron is scoped to in-flight K8s reconcile only — do NOT
  re-add a broad re-enqueue cron.
- **Control plane owns retry, not the infra (FEATURES.md anti-pattern):** Job
  `backoffLimit`/Kueue requeue neutralized; phaze owns the bounded re-drive (KSUBMIT-05).
- **`_FILE`-convention secrets (v4.0.1):** kube/S3 creds reach the control plane via
  file-based secrets, never request bodies, never logged.
- **DIST-01 no-media-mount boundary:** the control plane orchestrates kube + presigns; it
  never reads file bytes (preserved by Phase 53's leg; reconcile only touches the kube API
  and the S3 delete capability, not bytes).

### Integration Points
- New reconcile CronJob registered in `controller.py` `cron_jobs` (every `*/5`).
- New fast submit task (routes through `enqueue_router`; built here, wired into
  `stage_cloud_window` in Phase 55).
- New kr8s client/config surface on `ControlSettings` (kubeconfig, namespace, LocalQueue
  name; `_FILE` secrets).
- Reconcile → existing `agent_analysis` callback path (success) + Phase 53 S3 delete
  (no-callback terminal outcomes, D-05).
- New `cloud_job` columns + Phase 54 Alembic migration (D-09).
- Inadmissible alert flag on `cloud_job` → pipeline DAG dashboard (D-06).

</code_context>

<specifics>
## Specific Ideas

- The "single most important ordering decision" (FEATURES.md): the Job status read must
  never lose to GC. Resolved by having phaze delete the Job only *after* recording the
  outcome, with TTL as a backstop for the unreconciled-orphan case (drove D-04).
- Distinguish *loud* faults from *silent* waits: `Inadmissible` (operator misconfig) is
  loud (alert + hold, no cap consumption); `Pending` (quota) is silent and waits forever
  (drove D-06/D-07).
- The `cloud_job` row — not a kube watch, not the recovery ledger — is the durable
  source of in-flight truth the cron iterates (drove D-02, ties to KSUBMIT-06).

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope. Adjacent work explicitly owned by other
phases (already on the roadmap, not deferred): the `stage_cloud_window` K8s branch +
`cloud_target` selector + `enqueue_router` additions + AST guard (Phase 55); the
`cloud_phase` column on `cloud_job` (Phase 55); deploy, least-privilege RBAC, Kueue admin
runbook, `_FILE` secrets wiring, master toggle (Phase 56). Out-of-scope per REQUIREMENTS.md:
`maximumExecutionTimeSeconds` runaway guard (KSUBMIT-07), cross-target re-route on eviction
(KSUBMIT-08), multi-file/elastic Jobs (KSUBMIT-09).

</deferred>

---

*Phase: 54-kube-submit-watch-reconcile-cron*
*Context gathered: 2026-06-27*
