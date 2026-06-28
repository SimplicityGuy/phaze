# Phase 54: Kube submit / watch + reconcile cron - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-27
**Phase:** 54-kube-submit-watch-reconcile-cron
**Areas discussed:** Watch vs cron-only, Job cleanup ordering, Inadmissible surfacing, Re-drive cap & state

---

## Watch vs cron-only

| Option | Description | Selected |
|--------|-------------|----------|
| Cron-only poll | No long-lived watch; reconcile cron lists in-flight Workloads/Jobs each tick; callback is the real-time signal, cron is the safety net. Simplest; matches KSUBMIT-02/03. | ✓ |
| Watch + reconcile | Live kr8s watch fast-path + periodic reconcile safety net (FEATURES.md line 80). More responsive but adds reconnect/expired-watch handling and test surface. | |

**User's choice:** Cron-only poll
**Notes:** Sub-minute responsiveness buys nothing for hour-scale analysis; the out-of-band callback already delivers results in real time.

### Follow-up — in-flight set discovery

| Option | Description | Selected |
|--------|-------------|----------|
| cloud_job rows | Cron reads cloud_job rows in submitted/in-flight state (kueue_workload Job name recorded at submit) and reconciles each. DB sidecar is the durable in-flight registry; no ledger, no recover_orphaned_work. | ✓ |
| List by kube label | Cron lists Jobs/Workloads by phaze queue-name label and maps back to file_id. Source of truth is the cluster; risks missing just-submitted Jobs. | |

**User's choice:** cloud_job rows
**Notes:** Required because KSUBMIT-06 forbids a ledger seed; the cloud_job sidecar is exactly the in-flight registry.

### Follow-up — reconcile cadence

| Option | Description | Selected |
|--------|-------------|----------|
| Every 5 min | Matches stage_cloud_window (*/5). Plenty responsive for hour-scale work; trivial load; comfortable TTL-vs-read margin. | ✓ |
| Every 1 min | Matches reap_stalled_scans. Faster detection at higher polling frequency; marginal benefit. | |
| Config knob | Add cloud_reconcile_interval setting (default 5 min). | |

**User's choice:** Every 5 min

---

## Job cleanup ordering

| Option | Description | Selected |
|--------|-------------|----------|
| phaze deletes | Generous ttlSecondsAfterFinished as backstop, but phaze explicitly deletes the Job after reconcile records the terminal outcome. Read can never lose to GC. | ✓ |
| Generous TTL only | Rely solely on ttlSecondsAfterFinished > reconcile interval; let k8s GC. Simpler but implicit safety margin. | |

**User's choice:** phaze deletes
**Notes:** FEATURES.md's "single most important ordering decision" — resolved toward explicit delete after the DB reflects the result.

### Follow-up — S3 object cleanup on no-callback terminal outcomes

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, reconcile deletes | On eviction/failure with no callback, reconcile calls the existing Phase 53 delete_staged_object(file_id) before/at Job deletion. Reuses Phase 53 capability. | ✓ |
| Lifecycle TTL backstop only | Leave orphaned S3 objects to the bucket lifecycle TTL. Fewer moving parts but objects linger. | |

**User's choice:** Yes, reconcile deletes
**Notes:** Phase 53 D-02 explicitly reserved this delete for Phase 54's reconcile.

---

## Inadmissible surfacing

| Option | Description | Selected |
|--------|-------------|----------|
| Pipeline-UI alert | Alert on the pipeline DAG dashboard (Phase 34/44 surface) driven off a cloud_job flag, plus WARNING log. Operator sees it without tailing logs. | ✓ |
| Log + agent health | WARNING log + mark compute-agent/cloud health degraded (v5.0 Agents page). | |
| Log only | WARNING log line per reconcile tick. Minimal scope. | |

**User's choice:** Pipeline-UI alert
**Notes:** Healthy Pending (quota) stays silent; only the misconfig fault is surfaced.

### Follow-up — Inadmissible vs the re-drive cap

| Option | Description | Selected |
|--------|-------------|----------|
| Hold, don't consume cap | Operator-config fault; reconcile alerts and holds the file (Job stays suspended/queued) without counting attempts or failing it. Once the queue is fixed, the existing Job proceeds. | ✓ |
| Bounded timeout → fail | Alert, then mark ANALYSIS_FAILED after a grace period. Prevents indefinite stuck files but fails recoverable operator-side faults. | |

**User's choice:** Hold, don't consume cap

---

## Re-drive cap & state

| Option | Description | Selected |
|--------|-------------|----------|
| New knob, default 3 | Add cloud_submit_max_attempts ControlSettings knob (default ~3) mirroring push_max_attempts; attempt count on cloud_job row. Distinct from push retry budget. | ✓ |
| Reuse push_max_attempts | Reuse the existing knob for K8s re-drive too. Simpler but conflates rsync-push and kube-submit retry budgets. | |

**User's choice:** New knob, default 3

### Follow-up — cloud_job state model

| Option | Description | Selected |
|--------|-------------|----------|
| Extend status + attempts col | Add new CloudJobStatus members (string-backed, CHECK-list only) + kueue_workload + attempts columns in Phase 54's own migration; cloud_phase untouched for Phase 55. | ✓ |
| Separate reconcile_state col | Keep CloudJobStatus staging-only; add a distinct submit/reconcile status column + kueue_workload + attempts. Two parallel status columns to keep coherent. | |

**User's choice:** Extend status + attempts col

---

## Claude's Discretion

- Suspended-Job spec details: `backoffLimit` value + Kueue-requeue neutralization mechanism, `ttlSecondsAfterFinished` value, resource requests sizing.
- Submit-task shape & idempotency guard, deterministic Job name scheme keyed to file_id, enqueue_router routing.
- kr8s client construction / kubeconfig & namespace config surface (`_FILE` secrets).
- kube condition → outcome mapping (success/failure/eviction, Pending vs Inadmissible).
- Fake-kube test harness shape (respx vs fake client).
- Exact new CloudJobStatus member names and the precise pipeline-UI alert element.

## Deferred Ideas

None — discussion stayed within phase scope. Adjacent work owned by other phases: stage_cloud_window K8s branch + cloud_target + enqueue_router additions + AST guard + cloud_phase column (Phase 55); deploy/RBAC/runbook/_FILE wiring/master toggle (Phase 56). Out of scope per REQUIREMENTS.md: KSUBMIT-07 (maximumExecutionTimeSeconds guard), KSUBMIT-08 (cross-target re-route), KSUBMIT-09 (multi-file/elastic Jobs).
