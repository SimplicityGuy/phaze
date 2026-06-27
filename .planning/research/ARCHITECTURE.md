# Architecture Research

**Domain:** Integrating a Kueue/K8s burst-analysis target into phaze's existing control-plane → cloud-window → result-reconciliation design (v6.0)
**Researched:** 2026-06-26
**Confidence:** HIGH (every integration seam read directly from `src/phaze`: `enqueue_router.py`, `scheduling_ledger.py`, `release_awaiting_cloud.py` (`stage_cloud_window`), `routers/pipeline.py` (duration router), `routers/agent_analysis.py` + `routers/agent_push.py` (callbacks), `tasks/reenqueue.py` (recovery), `models/file.py` (state machine), `config.py` (settings + `_FILE`))

> Scope: this file answers ONLY "how do the v6.0 K8s features bolt onto what already
> exists." The Kueue Job→Workload lifecycle (FEATURES.md) and the kr8s/aioboto3/presigned-URL
> stack (STACK.md) are treated as settled inputs and not re-derived here. The thesis: **v6.0 is
> a third branch at exactly ONE existing seam (the `stage_cloud_window` staging step), reusing
> the AWAITING_CLOUD→window→out-of-band-callback spine wholesale.** The execution unit changes
> from "persistent compute agent draining a SAQ queue" to "ephemeral Kueue Job," but the
> control-plane choreography is the same shape as v5.0's rsync push pipeline.

---

## The v5.0 spine v6.0 must reuse (verified in code)

This is the existing cloud-burst data flow, traced through the real modules. v6.0 mirrors it.

```
 routers/pipeline.py :: _route_discovered_by_duration   (the duration ROUTING SEAM)
   long file (duration >= cloud_route_threshold_sec, cloud_burst_enabled)
        │  set FileState.AWAITING_CLOUD  (HELD; enqueues nothing — committed)
        ▼
 tasks/release_awaiting_cloud.py :: stage_cloud_window   (controller cron */5, THE single staging entry)
   advisory-xact-lock → window = COUNT(PUSHING+PUSHED) → slots = cloud_max_in_flight - window
   GATE 1 compute agent online?  GATE 2 fileserver agent online?
   per slot:  AWAITING_CLOUD → PUSHING ; enqueue push_file on the FILESERVER per-agent queue
        ▼
 tasks/push.py :: push_file   (FILESERVER agent — owns the media mount)
   rsync-over-SSH/Tailscale to compute scratch ; sha256-verify ; POST .../push/{file_id}/pushed
        ▼
 routers/agent_push.py :: report_pushed   (control callback)
   guarded PUSHING → PUSHED ; clear_ledger_entry("push_file:<id>") ; enqueue process_file on COMPUTE queue
        ▼
 tasks (compute agent) :: process_file   (essentia analysis on the persistent A1 host)
   PUT .../analysis/{file_id}
        ▼
 routers/agent_analysis.py :: put_analysis   (control callback — THE result channel)
   idempotent ON CONFLICT(file_id) upsert ; FileRecord → ANALYZED ; clear_ledger_entry("process_file:<id>")
```

Load-bearing properties v6.0 inherits unchanged:

- **The duration router is target-agnostic.** `_route_discovered_by_duration` only decides "long → AWAITING_CLOUD vs short → local fileserver." It names no cloud target. v6.0 touches it **zero**.
- **`stage_cloud_window` is the single, unbypassable entry to any cloud pipeline** and the only place that introduces new in-flight work, bounded by `cloud_max_in_flight` via the `PUSHING+PUSHED` window count under a pg advisory lock.
- **The result arrives out-of-band** at `put_analysis` (reconciled by `file_id`, idempotent). The lifecycle signal (rsync done / Job finished) is decoupled from the result. A dropped signal never loses a result.
- **`PUSHED` is "push-done" to recovery.** `tasks/reenqueue.py::_select_done_push_ids` treats `PUSHED/ANALYZED/ANALYSIS_FAILED` as done — so a file parked in `PUSHED` is NOT re-driven by `recover_orphaned_work`.

---

## Q1 — Where the submit→watch loop lives

### Do NOT pin a worker on `job.wait()`

The submit→watch must NOT be one SAQ task that blocks on `await job.wait(timeout=hours)`. That recreates the exact failure class v4.0.10/Phase 43 fixed (a worker slot held for a multi-hour essentia run). It is also unnecessary: **the result does not come from the watch** — it comes out-of-band at `put_analysis`. The Kueue watch is purely lifecycle (admission visibility + cleanup + eviction detection).

### Recommended split: a fast submit task + a periodic reconcile cron

Mirror v5.0's `report_pushed → process_file` handoff, but both halves run control-side:

| Piece | Where | What it does | Returns |
|-------|-------|--------------|---------|
| `submit_k8s_job` | NEW controller task on the **`controller`** queue | presign GET (aioboto3) → build & submit the **suspended** labeled Job (kr8s) → record `job_name/uid` + S3 key on the FileRecord → flip `PUSHING → PUSHED` | fast (seconds), never waits for analysis |
| `reconcile_k8s_jobs` | NEW controller **cron** (`*/2 * * * *`) | for every K8s-in-flight file (`PUSHED` + has job ref), read Job/Workload status via kr8s; on terminal/finished → delete Job + delete S3 object; on evicted/timed-out → re-route; on pending/running → leave | idempotent sweep |

This is the watch-as-fast-path-but-poll-as-safety-net pattern FEATURES.md mandates, realized with the existing cron machinery. `submit_k8s_job` returns in seconds; the hours of analysis happen in the cluster pod; the cron re-reads status on a cheap schedule. No worker is ever pinned.

### Coexistence with `stage_cloud_window` and the ledger

- **`stage_cloud_window` stays the single staging entry — it just branches by target.** Today it always enqueues `push_file`. v6.0 adds: *if* `cloud_target == "k8s"`, enqueue **`upload_file_s3`** on the fileserver queue instead (the S3 analogue of `push_file`; see Q3). The window math (`COUNT(PUSHING+PUSHED)`, `cloud_max_in_flight`, advisory lock, FIFO `SKIP LOCKED`) is **reused verbatim** — it is target-agnostic backpressure.
- **GATE 1 changes meaning per target.** For A1, GATE 1 is "a compute agent is online" (`select_active_agent(kind="compute")`). For K8s there is no persistent compute agent, so GATE 1 becomes "K8s target is configured/reachable" (LocalQueue name set, kube endpoint present). GATE 2 (fileserver agent online — the byte mover) is unchanged: the fileserver still owns the media and performs the upload.
- **Reuse the scheduling ledger by STATE, not by a synthetic SAQ row.** The Phase-45 ledger exists so backfill/recovery only re-drive *previously-scheduled* work. K8s gets that property **for free** because a K8s-in-flight file sits in `PUSHED`, which the backfill predicates (`ANALYSIS_FAILED`, `AWAITING_CLOUD`) already exclude and which `recover_orphaned_work` already treats as push-done. **Do not seed a `process_file:<id>` ledger row for a K8s file** — there is no SAQ `process_file` job, and a seeded row would make `recover_orphaned_work` wrongly re-enqueue `process_file` onto an agent queue when it finds no live SAQ job. The K8s re-drive owner is `reconcile_k8s_jobs`, keyed on FileRecord state. `put_analysis`'s existing `clear_ledger_entry("process_file:<id>")` is then a harmless no-op for K8s files (DELETE matches nothing) — **`put_analysis` needs no change.**
- **Ledger DOES cover the upload leg.** `upload_file_s3` is an agent task, so add it to `AGENT_TASKS`; its `before_enqueue` hook seeds `upload_file_s3:<id>` (parallel to `push_file:<id>`), and its `uploaded` callback clears it — giving the upload step the same recover-only semantics rsync push has today.

> Net: only previously-scheduled long files reach K8s, because the only path in is `AWAITING_CLOUD → stage_cloud_window`, which is fed only by the duration router + the ledger-scoped backfill — both untouched.

---

## Q2 — Slotting the active-target selector into the routing seam

### One new setting, consulted at exactly one place

Add `cloud_target` to `ControlSettings`. The cleanest model keeps the existing master toggle as-is and adds a target enum consulted only when bursting:

```python
# ControlSettings (config.py) — NEW
cloud_target: Literal["a1", "k8s"] = Field(default="a1",
    validation_alias=AliasChoices("PHAZE_CLOUD_TARGET", "cloud_target"),
    description="Active cloud burst target when cloud_burst_enabled. 'a1' = v5.0 rsync→compute agent; 'k8s' = Kueue Job. 'local' of the three-way == cloud_burst_enabled=False.")
```

The milestone's "local / A1 / K8s" three-way maps to: `cloud_burst_enabled=False` ⇒ local (no bursting, the existing dormant path); `cloud_burst_enabled=True` + `cloud_target` ⇒ A1 or K8s. (A redundant `cloud_target="local"` member is avoidable; if the roadmap prefers a literal three-way enum, make it `Literal["local","a1","k8s"]` and treat `local` identically to the toggle being off.)

**The selector is read in ONE branch — inside `stage_cloud_window`** — choosing which staging task to enqueue (`push_file` vs `upload_file_s3 → submit_k8s_job`). The duration router (`_route_discovered_by_duration`), the AWAITING_CLOUD hold, the window count, and the backfill are **all untouched**. This is what "doesn't re-architect v5.0's routing seam" means concretely: the seam splits one level *below* where the long/short decision is made.

### FileRecord states: REUSE `PUSHING`/`PUSHED`, do not add `SUBMITTED_K8S`

Reuse the existing pair as **generic "staged-or-in-flight" window states**, reinterpreted per target:

| State | A1 meaning (v5.0) | K8s meaning (v6.0) |
|-------|-------------------|--------------------|
| `AWAITING_CLOUD` | held, awaiting a slot | held, awaiting a slot (identical) |
| `PUSHING` | rsync in progress to compute scratch | uploading bytes to S3 **and** submitting the Job |
| `PUSHED` | landed on compute scratch, within analysis | Job submitted (queued/admitted/running) — within analysis |
| `ANALYZED` | result POSTed | result POSTed (identical) |

Why reuse rather than add `SUBMITTED_K8S`:

- `get_cloud_window_count` = `COUNT(PUSHING+PUSHED)`, `cloud_max_in_flight`, the staging candidate query, the D-09 dashboard cards, and `recover_orphaned_work`'s push-done set **all key on this exact pair**. Reuse means every one of those keeps working with zero edits. A new state would require touching all of them.
- The state column is `String(30)`, so adding a state is "code-only, no migration" — but the *cost* is the downstream fan-out, not the migration. Reuse avoids the fan-out.

**Kueue admission phase (Pending-behind-quota vs Admitted vs Running) is observability, not a core state.** Surface it as a nullable `cloud_phase` string the reconcile cron writes from the Workload `status.conditions` (`QuotaReserved`/`Admitted`/`Finished`), feeding the P2 dashboard cards. It rides alongside `PUSHED`; it does not fork the state machine. This keeps the MVP state-machine delta at **zero new states** while still letting the UI say "5 long files queued behind cluster quota."

### Schema delta

One Alembic migration adding K8s bookkeeping. Two viable shapes:

- **Columns on `files`** (simplest): `cloud_job_name`, `cloud_job_uid`, `cloud_object_key`, `cloud_phase`, `cloud_submitted_at`. Nullable; only K8s-in-flight rows populate them.
- **Sidecar `cloud_job` table** (cleaner separation, FK to `files.id`): preferable if you want the `files` table to stay lean and to keep a per-attempt history. Given ~200K files and only `≤cloud_max_in_flight` ever in flight, either is cheap; **recommend the sidecar** to avoid widening the hot `files` row and to mirror the existing audit/sidecar style (TagWriteLog, ExecutionLog).

---

## Q3 — Object-storage staging flow

### CRITICAL integration constraint: the control plane cannot read media bytes (DIST-01)

`docker-compose.yml` mounts no media on the application server — "the app-server has no way to read or write music/video file content (DIST-01)," CI-enforced. **Therefore the control plane physically cannot upload the file bytes to S3.** STACK.md's "aioboto3 on the control plane" is correct *for the S3 client/credentials/presigning/delete*, but the **byte transfer must originate on the file-server agent**, exactly as the v5.0 rsync push did. Resolve the apparent conflict this way (and lock it as a decision):

| Actor | Holds S3 creds? | Touches bytes? | Mechanism |
|-------|-----------------|----------------|-----------|
| **Control plane** | YES (aioboto3, `_FILE` secrets) | NO | `generate_presigned_url("put_object")`, `("get_object")`, `delete_object` |
| **File-server agent** | NO | YES (owns media mount) | `httpx` **PUT** bytes to the presigned PUT URL — credential-free, mirrors `push_file` |
| **Job pod** | NO | YES (downloads) | `httpx` **GET** bytes from the presigned GET URL — credential-free (STACK.md) |

This extends STACK.md's "no S3 SDK in the pod" to "no S3 SDK on the agent either" — both move bytes with the `httpx` they already have, against short-lived presigned URLs. aioboto3 lives **only** on the control plane. DIST-01 is preserved (only the agent and the ephemeral pod ever see media bytes; the control plane only ever sees presigned URLs).

### Who uploads, and when

**The file-server agent uploads, at staging time** (when `stage_cloud_window` allocates a slot — "stay one ahead"), **not at route time.** Route time only sets `AWAITING_CLOUD`. This is byte-for-byte the v5.0 timing, with `upload_file_s3` substituted for `push_file`:

```
stage_cloud_window (k8s target)
  → AWAITING_CLOUD → PUSHING ; enqueue upload_file_s3 on fileserver queue
upload_file_s3 (agent)
  → control issues a presigned PUT URL (new internal endpoint, or carried in the task payload)
  → httpx PUT bytes ; (optional) re-read + report sha256
  → POST .../s3/{file_id}/uploaded
report_s3_uploaded (control callback — the report_pushed analogue)
  → presign GET ; enqueue submit_k8s_job on the controller queue   (PUSHING stays; flip to PUSHED at submit)
submit_k8s_job (controller)
  → submit suspended labeled Job referencing the presigned GET + callback secret ; PUSHING → PUSHED
```

(If you prefer fewer hops, `upload_file_s3`'s callback can hand straight to submit; keeping a distinct `report_s3_uploaded` callback maximises symmetry with `report_pushed` and keeps presign-GET on the control side where the creds live.)

### How the presigned URL + callback token reach the pod

Via the Job's pod spec, with a deliberate split by secret lifetime:

| Item | Lifetime | Delivery | Why |
|------|----------|----------|-----|
| Presigned GET URL | short (minutes–hours) | **Job pod `env`** (or a per-Job Secret) | ephemeral; not a durable secret; fine as env |
| `file_id`, callback base URL, queue/Workload names | non-secret | Job pod `env`/`args` | plain config |
| **Compute-agent bearer token** | long-lived | **cluster `Secret` via `secretKeyRef`** (operator pre-creates) | a durable credential — never inline in Job env; reused by every Job |

So `submit_k8s_job` templates a Job whose container env has the presigned GET URL + file_id + callback URL inline, and a `secretKeyRef` to the operator-provisioned compute-agent-token Secret. The pod: `httpx GET` → analyze (existing x86 essentia one-shot) → `httpx PUT /api/internal/agent/analysis/{file_id}` with the bearer token → exit.

### How/when the object is deleted

**After reconcile, by the control plane, belt-and-suspenders with a bucket TTL:**

1. Primary: `reconcile_k8s_jobs`, on seeing the file is `ANALYZED` (result landed) and the Job is `Finished`, calls `delete_object(cloud_object_key)` then deletes the Job. The pod never deletes (it is credential-free and may die).
2. Backstop: a **bucket lifecycle TTL** (operator-configured, e.g. 24h) expires any object the reconcile loop missed (controller down through the whole window). Documented in the runbook; "ephemeral staging only, never a data home" (PROJECT Out-of-Scope).

**The TTL-vs-read race FEATURES.md flags is benign in phaze's design**, because the *result* never comes from the object or the Job — it comes from `put_analysis`. If the Job (and via `ttlSecondsAfterFinished`, its Workload) is GC'd before the reconcile cron reads it, the cron simply finds `PUSHED + ANALYZED + no Job` and treats that as "done — delete the S3 object, clear the job ref." Correctness is decoupled from GC timing. Set a generous `ttlSecondsAfterFinished` as courtesy, but do not depend on it for the read.

---

## Q4 — Job-pod identity, idempotency, orphans, races

### One shared cluster compute-agent identity — NOT per-job tokens

Register **a single `Agent` of `kind="compute"`** representing the whole cluster (mirrors v5.0, where the A1 host is one registered compute agent). Every Job references the same bearer token via the cluster `Secret`. Rationale:

- Per-job tokens would mint + insert + later revoke an `Agent` row (and churn the `ix_agents_token_hash_active` partial index) **per file** — heavy and pointless for a single-user tool.
- Identity is already token-derived on the control side (AUTH-01: `agent_id` from the token hash, never from the body), and the result is reconciled by `file_id`, so a shared identity loses nothing — the Job carries `file_id` in its env/callback path.
- Revocation stays instant and cluster-wide (revoke the one agent → every in-flight Job's callback 403s).

GATE 1 in `stage_cloud_window` for K8s should therefore check "K8s target configured" rather than "a compute agent has heartbeated recently" — the cluster compute agent never heartbeats (it has no long-running worker; the Jobs are ephemeral). The compute `Agent` row exists purely to anchor the token; its liveness columns are not meaningful for K8s. (Worth an explicit note so the v4.0 heartbeat/liveness UI doesn't show the cluster agent as perpetually DEAD — either suppress liveness for a `kind="compute"` cluster identity or document it.)

### Idempotency

| Vector | Guard (mostly already present) |
|--------|-------------------------------|
| Duplicate **result POST** (Job backoff retry, or reconcile races a late pod) | `put_analysis` is `ON CONFLICT(file_id)` idempotent — verified. Second POST is a harmless upsert; the WR-02-style guard advances state only from the expected predecessor. |
| Duplicate **submission** (controller restart mid-stage) | Derive the Job name deterministically from `file_id` (e.g. `phaze-analyze-<short-uuid>`), mirroring the `process_file:<id>` / `push_file:<id>` key pattern. A re-submit hits kube `AlreadyExists` → tolerate as success (don't double-create). |
| Duplicate **upload** | `upload_file_s3` PUT to the deterministic object key `cloud_object_key=<file_id>.<ext>` is idempotent (overwrite); the `upload_file_s3:<id>` SAQ key dedups the enqueue. |
| Double **staging tick** | Existing advisory `pg_advisory_xact_lock` + `PUSHING` flip + deterministic SAQ key — reused unchanged. |

### Orphans, timeouts, evictions

The reconcile cron is the safety net; every branch is keyed on durable FileRecord state, never on a live watch:

| Situation | Detection (reconcile cron) | Action |
|-----------|----------------------------|--------|
| Job **succeeded**, result landed | `PUSHED` + `ANALYZED` (or Job `Complete`) | delete Job + S3 object; clear job ref (state already ANALYZED) |
| Job **succeeded** but result POST lost | Job `Complete`/Workload `Finished` but file still `PUSHED` | re-route for another attempt (back to `AWAITING_CLOUD`) or mark `ANALYSIS_FAILED` for ledger-scoped backfill; clean up S3/Job |
| Job **failed** (`status.failed` past `backoffLimit`) | Job `Failed` / Workload `Finished` non-success | `ANALYSIS_FAILED` (feeds existing backfill) **or** re-route to A1/local per policy; clean up |
| **Evicted/Deactivated** (preemption, `maximumExecutionTimeSeconds`, PodsReady backoff) | Workload `Evicted`, reason `WorkloadInactive` | back to `AWAITING_CLOUD` (re-stage on a later slot) or fall back — reuses the v5.0 routing seam |
| **Queued behind quota** (normal) | Workload `QuotaReserved=False, reason=Pending` | **leave** — not a failure; FEATURES.md P1. Only a long staleness ceiling (a control-side `k8s_inflight_timeout_sec`) converts a truly-stuck submission to a re-route |
| **Stuck `PUSHING`** (controller died mid-submit, no job ref) | `PUSHING` + no `cloud_job_uid` past a short grace | revert to `AWAITING_CLOUD` (frees the slot). `recover_orphaned_work` won't touch it — there is no `push_file` ledger row for K8s — so the cron must own this. |
| **TTL GC'd the Job before read** | `PUSHED` + `ANALYZED` + Job not found | benign — treat as done, delete S3 object, clear ref (see Q3) |

Misconfiguration surfacing (FEATURES.md): a Job whose `queue-name` points at a missing LocalQueue yields `QuotaReserved=False, reason=Inadmissible`. The reconcile cron should distinguish `Inadmissible` (operator error → surface loudly, do not silently retry forever) from `Pending` (normal quota wait).

---

## Q5 — New vs modified components + build order

### NEW components

| Component | Kind | Responsibility |
|-----------|------|----------------|
| `services/object_staging.py` | control service | aioboto3 wrapper: presign PUT, presign GET, `delete_object`; reads S3 creds/endpoint/bucket from `_FILE` settings. Mockable via `moto`/botocore stubber. |
| `services/k8s_client.py` | control service | kr8s wrapper: build the suspended labeled Job manifest, `create()`, find Workload by `kueue.x-k8s.io/job-uid`, read Job+Workload status, delete Job. Workload apiVersion as a config constant (`v1beta2`, fallback `v1beta1`). |
| `tasks/submit_k8s_job.py` | controller task | presign GET → submit Job → record job ref + object key → `PUSHING → PUSHED`. Fast, never waits. |
| `tasks/reconcile_k8s_jobs.py` | controller cron (`*/2`) | poll K8s-in-flight files; cleanup, re-route, evict/stuck handling, `cloud_phase` updates (Q4 table). |
| `tasks/upload_file_s3.py` | **agent** task | `httpx` PUT media bytes to presigned PUT URL (the `push_file` analogue); POST `uploaded` callback. Stays Postgres-free (import-boundary test). |
| `routers/agent_s3.py` | control callback router | `report_s3_uploaded` / `report_s3_mismatch` (the `agent_push.py` analogue) → presign GET → enqueue `submit_k8s_job`. Plus, if presign-on-demand, a small endpoint to mint the PUT URL for the agent task. |
| Job-runner image | Docker/CI | `Dockerfile.k8sjob` `FROM` the published x86 essentia agent base; entrypoint `phaze/cli/k8s_runner.py` (httpx GET → essentia one-shot → httpx PUT `/analysis/{file_id}` → exit). Zero new pip deps (STACK.md). |
| Alembic migration | schema | sidecar `cloud_job` table (or nullable `files` columns): `cloud_job_name/uid`, `cloud_object_key`, `cloud_phase`, `cloud_submitted_at`. No state-enum change (String(30)). |
| Config additions | `config.py` | `cloud_target`; kube endpoint + kubeconfig/SA-token `_FILE`; LocalQueue name; Workload apiVersion; S3 endpoint/bucket/access-key/secret `_FILE`; `k8s_inflight_timeout_sec`. Extend `SECRET_FILE_FIELDS`. |

### MODIFIED components

| Component | Change | Risk |
|-----------|--------|------|
| `tasks/release_awaiting_cloud.py` (`stage_cloud_window`) | branch by `cloud_target`: A1 → `push_file` (today); K8s → `upload_file_s3`. GATE 1 becomes "target reachable" for K8s. Window math/lock/FIFO **unchanged**. | LOW — additive branch at the one seam |
| `services/enqueue_router.py` | add `submit_k8s_job` to `CONTROLLER_TASKS`, `upload_file_s3` to `AGENT_TASKS` (keeps routing + ledger classifier in sync) | LOW — the frozensets are the designed extension point |
| `tasks/controller.py` | register `submit_k8s_job` + `reconcile_k8s_jobs` in `functions`; add the reconcile `CronJob` | LOW |
| `tasks/agent_worker.py` | register `upload_file_s3` | LOW |
| `config.py` | new settings + `_FILE` fields + a "cloud_burst_enabled+k8s requires target config" model-validator (mirrors the existing `_enforce_compute_scratch_dir_when_cloud_enabled`) | LOW |
| `routers/pipeline.py` + templates | P2: admission-state cards from `cloud_phase` | LOW (deferrable) |

### UNCHANGED (deliberately — the proof the seam is right)

- `routers/pipeline.py::_route_discovered_by_duration` — target-agnostic hold.
- `routers/agent_analysis.py::put_analysis` — already idempotent; its `process_file:<id>` ledger-clear is a benign no-op for K8s files.
- `scheduling_ledger.py`, the backfill, `recover_orphaned_work` — K8s integrates by FileRecord **state**, not a synthetic SAQ row (Q1).
- `cloud_burst_enabled`, `cloud_max_in_flight`, `cloud_route_threshold_sec`, the window count, the advisory lock — all reused.

### Build order (phases from 52, dependency-ordered)

```
52  Job-runner image + one-shot entrypoint
      FROM x86 essentia base; httpx GET → analyze → httpx PUT /analysis/{id} → exit.
      No cluster needed: test the analyze→POST loop against a respx mock.   (parallels Phase 47)
        │
53  Object-storage staging leg
      services/object_staging.py (aioboto3 presign/delete) + agent upload_file_s3
      + report_s3_uploaded callback + cloud_object_key.  Test with moto/stubber.
      No cluster needed.                                                    (depends: nothing cluster-side)
        │
54  Kube submit/watch leg
      services/k8s_client.py (kr8s) + submit_k8s_job + reconcile_k8s_jobs cron
      + cloud_job sidecar migration + cloud_phase.  Test against a fake API / recorded responses.
                                                                            (depends: 52 image to run, 53 presigned GET to embed)
        │
55  Routing integration (THE seam)
      cloud_target selector; stage_cloud_window branch; enqueue_router additions;
      controller/agent_worker registration; config + validators; GATE-1 semantics.
                                                                            (depends: 53, 54)
        │
56  Deploy + runbook + docs (+ P2 dashboard cards)
      Kueue admin objects (RF/CQ/LQ) as cluster-admin setup; cluster Secret for the
      compute-agent token; bucket lifecycle TTL; _FILE wiring; master-toggle gating;
      transport-agnostic endpoint config (Tailscale OR WireGuard).
                                                                            (depends: all)
```

Rationale: 52–54 are each independently buildable and unit-testable **without a live cluster or bucket** (respx / moto / fake kube API), keeping the 85% coverage gate reachable. 55 is the only phase that edits the live v5.0 seam, and it does so additively after both legs exist. 56 is pure ops/docs. This mirrors v5.0's own ordering (image → agent → routing → pipeline → deploy: Phases 47–51).

---

## Anti-Patterns (phaze-specific, for v6.0)

### Pinning a worker on `await job.wait()` for the analysis duration
**Wrong:** one SAQ task submits then blocks hours on Job completion. **Why:** recreates the v4.0.10/Phase-43 worker-starvation class; needless since the result is out-of-band. **Instead:** fast `submit_k8s_job` + periodic `reconcile_k8s_jobs` cron.

### Uploading media bytes from the control plane
**Wrong:** `aioboto3.upload_file` on the app server. **Why:** the app server has no media mount (DIST-01, CI-enforced) — it physically can't. **Instead:** control plane presigns (aioboto3); the **file-server agent** PUTs bytes via httpx (credential-free), exactly as it rsync-pushed in v5.0.

### Seeding a `process_file:<id>` ledger row for a K8s file
**Wrong:** mint a ledger row so "recovery knows it's scheduled." **Why:** there is no live SAQ `process_file` job for a K8s file, so `recover_orphaned_work` would re-enqueue it onto an **agent** queue — analyzing a long file locally (the CLOUDROUTE-02 violation v5.0 fought). **Instead:** rely on FileRecord state (`PUSHED`) for recover-scoping; let `reconcile_k8s_jobs` own K8s re-drive.

### Adding `SUBMITTED_K8S` as a core FileState
**Wrong:** new state for "Job submitted." **Why:** forks the window count, dashboard cards, candidate query, and recover push-done set — every consumer of `PUSHING/PUSHED` needs an edit. **Instead:** reuse `PUSHING/PUSHED` as generic in-flight states; carry Kueue admission phase in a non-state `cloud_phase` field.

### Treating the Kueue watch as the result channel
**Wrong:** parse the result from Job logs / wait for `Finished` to read output. **Why:** Kueue carries no payload; watches drop; pod GC races the read. **Instead:** pod POSTs to `/api/internal/agent/analysis/{file_id}`; watch is lifecycle/cleanup only.

### Per-job compute-agent tokens
**Wrong:** mint+revoke an `Agent` row per file. **Why:** churns the agents table + token-hash index for a single-user tool; no security gain (identity is token-derived, result reconciled by file_id). **Instead:** one cluster-wide `kind="compute"` agent token in a k8s Secret.

### phaze creating Kueue admin objects (RF/CQ/LQ)
**Wrong:** manage ClusterQueue/ResourceFlavor from the app for a "self-contained deploy." **Why:** cluster-scoped, needs elevated RBAC, couples phaze to cluster policy (PROJECT scopes them as runbook setup; FEATURES anti-feature). **Instead:** reference a configured LocalQueue name; admin provisions RF/CQ/LQ.

---

## Integration Points

### External services

| Service | Integration pattern | Notes / gotchas |
|---------|---------------------|-----------------|
| Kube API (Kueue cluster) | kr8s (async, httpx) from control plane; kubeconfig/SA token via `_FILE` | submit suspended `batch/v1` Job + `queue-name` label; read Workload dynamically (no bindings). Pin apiVersion constant. Over the operator VPN — wrap in tenacity. |
| S3-compatible bucket | aioboto3 on control (presign + delete only); httpx PUT/GET for bytes (agent + pod) | `endpoint_url=` for non-AWS. No S3 SDK on agent or pod. Bucket lifecycle TTL as cleanup backstop. |
| Job pod → control | reuse `/api/internal/agent/analysis/{file_id}` (bearer token, AUTH-01, idempotent) | unchanged; the existing result channel. |
| Transport (Tailscale/WireGuard) | none — operator-provided reachable endpoints only | no mesh-specific code; just URLs in pydantic-settings. |

### Internal boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| Duration router ↔ staging | FileState `AWAITING_CLOUD` (committed) | unchanged; target-agnostic |
| `stage_cloud_window` ↔ agent | per-agent SAQ queue (`upload_file_s3`) | the one branched seam; window/lock/FIFO reused |
| agent ↔ control | `/api/internal/agent/s3/*` callbacks | new, modeled on `agent_push.py` |
| control ↔ cluster | kr8s submit + `reconcile_k8s_jobs` cron | new; lifecycle only |
| pod ↔ control | `/api/internal/agent/analysis/*` | reused; the authoritative result |

## Sources

- phaze source (HIGH — read directly): `services/enqueue_router.py`, `services/scheduling_ledger.py`, `services/pipeline.py` (cloud helpers), `tasks/release_awaiting_cloud.py`, `tasks/push.py`, `tasks/reenqueue.py`, `tasks/controller.py`, `routers/pipeline.py` (`_route_discovered_by_duration`), `routers/agent_analysis.py`, `routers/agent_push.py`, `models/file.py` (FileState), `config.py` (ControlSettings/AgentSettings, `_FILE`)
- `.planning/PROJECT.md` v6.0 milestone, DIST-01 boundary, CPU-only decision, Out-of-Scope reversals (HIGH)
- `.planning/research/STACK.md` (kr8s/aioboto3/presigned-URL, credential-free pod) and `FEATURES.md` (Kueue Job→Workload lifecycle, admission/eviction signals, TTL-vs-read hazard) — sibling research, treated as settled inputs (HIGH)

---
*Architecture research for: v6.0 Kubernetes Burst Analysis — K8s offload integration with the existing control-plane spine*
*Researched: 2026-06-26*
