# Pitfalls Research

**Domain:** Adding remote Kueue-Job submission + S3 staging to an existing async Python control plane (phaze v6.0 Kubernetes Burst Analysis)
**Researched:** 2026-06-26
**Confidence:** HIGH (Kueue lifecycle + kr8s/aioboto3 verified by sibling FEATURES.md/STACK.md against Context7; S3 presigned-URL credential-expiry constraint verified via AWS docs/re:Post; phaze-specific integration mistakes derived from PROJECT.md decisions + v4.0/v5.0 incident memory)

> Scope: these are mistakes specific to **bolting a remote, ephemeral, quota-scheduled execution
> unit onto phaze's existing async control plane** — not generic Kubernetes advice. The v5.0
> cloud-burst machinery (duration routing, compute-agent callback to `/api/internal/agent/*`,
> reconcile-by-`file_id`, ledger scoping, `cloud_burst_enabled` toggle) already exists and is the
> safety net that several of these pitfalls lean on. Suggested phase numbers assume the v6.0
> roadmap starts at **Phase 52**; the likely phase shape (from FEATURES MVP) is:
> **52** Job-runner image + one-shot entrypoint · **53** S3 staging seam · **54** Kube-API
> submit/watch/reconcile · **55** router/ledger/active-target · **56** deploy/runbook/secrets/docs.

---

## Critical Pitfalls

### Pitfall 1: Treating the kube watch as the result channel (a dropped watch loses a result)

**What goes wrong:**
A developer writes `await job.wait(["condition=Complete"])`, reads the analysis result out of the
Job/pod, and updates `FileRecord` from there. Over an operator VPN (Tailscale or WireGuard), the
watch connection drops after minutes-to-hours, the `resourceVersion` the watch was holding ages out
of etcd's compaction window, and the re-list/re-watch either 410-Gones or silently misses the
terminal transition. The file is stranded as "in progress" forever even though the pod ran perfectly
and the analysis succeeded.

**Why it happens:**
Kube watches *feel* like a reliable real-time stream, so people make them authoritative. But Kueue
carries **no result payload** — it is purely an admission/quota gate (FEATURES.md) — and a kube watch
is a best-effort lifecycle signal, not a durable queue. The VPN makes drops frequent; long-set
analysis (hours) makes `resourceVersion` expiry likely.

**How to avoid:**
Keep phaze's existing invariant absolute: **the pod POSTing to `/api/internal/agent/*`, reconciled by
`file_id`, is the ONLY source of truth for the result.** The kube watch is lifecycle/observability
ONLY — it tells phaze *whether/when* the Job ran, never *what the answer was*. The `FileRecord` must be
able to reach its terminal analyzed state purely from the out-of-band callback even if the watch never
returns a single event. Pair the watch (fast path) with a **periodic reconcile loop** that re-reads
Workload/Job status for in-flight `file_id`s (FEATURES.md "orphan/timeout reconcile"). Use bookmarked
watches (`allowWatchBookmarks`) where kr8s supports it, and on any watch error, fall back to a fresh
list+reconcile rather than resuming a stale `resourceVersion`.

**Warning signs:**
Files sit in `AWAITING_CLOUD`/submitted long after the pod's callback already landed; the callback
log shows a result POST for a `file_id` whose `FileRecord` never advanced; watch handlers throw
`410 Gone` / "resourceVersion too old."

**Phase to address:** Phase 54 (submit/watch/reconcile) — make the callback authoritative and the
reconcile loop mandatory from day one, not a later hardening pass.

---

### Pitfall 2: Holding a SAQ worker slot for hours blocking on `job.wait(...)`

**What goes wrong:**
The submit-and-watch logic runs inside a single SAQ task that calls `await job.wait(...)` with no
timeout (or a multi-hour one). A long-set Job can sit **suspended behind Kueue quota for hours** before
it even starts, then run for hours more. One worker slot is pinned the whole time. With a conservative
long-files workload, a handful of these starve the `controller` queue and stall everything else
(tracklist crons, pipeline triggers).

**Why it happens:**
`await job.wait()` reads like cheap async I/O, so it looks free to hold. But "queued behind quota" is
an indefinite, *normal* state in Kueue (FEATURES.md: tolerate pending-on-quota), not a transient blip —
so the wait genuinely lasts hours. phaze already learned this exact lesson in v5.0 ("stay one ahead"
push pipeline) and the v4.0.10 windowed-analysis timeout incident: don't let one long unit own a worker.

**How to avoid:**
Submit-then-return. The submit task creates the Job and records `submitted` state, then **exits**. A
short, **bounded** `wait(..., timeout=...)` is acceptable to catch fast failures, but on timeout the
task returns and a separate periodic reconcile task re-reads status on its next run (Stack integration
note: "re-poll on the next task run rather than holding a worker slot for hours"). State lives on
`FileRecord` (submitted → admitted → result-reconciled), not in a held coroutine. This also makes the
whole flow restart-safe: a control-plane reboot mid-analysis just reconciles on the next cron tick.

**Warning signs:**
`controller` queue depth climbs while only a few cloud files are in flight; SAQ worker utilization
shows long-lived tasks; unrelated controller work (crons) lags whenever cloud burst is active.

**Phase to address:** Phase 54 — design the submit and the reconcile as separate tasks from the start.

---

### Pitfall 3: `ttlSecondsAfterFinished` deletes the Job/Workload before phaze reads terminal status (TTL-vs-read race)

**What goes wrong:**
The Job is created with a tidy short `ttlSecondsAfterFinished` (say 30–60s) so the cluster stays clean.
The Kubernetes TTL-after-finished controller deletes the finished Job **and its owned Workload** shortly
after completion. phaze's periodic reconcile runs on a longer interval (minutes) — by the time it looks,
the Job and Workload are gone. The reconcile can't distinguish "succeeded and GC'd" from "never
existed / failed," and may wrongly re-route or re-submit the file.

**Why it happens:**
TTL and the reconcile interval are set independently by different people thinking about different goals
(cluster hygiene vs. control-plane polling cost). FEATURES.md flags this as "the single most important
ordering decision in the watch loop."

**How to avoid:**
Make the result callback the durable record (Pitfall 1) so a GC'd Job is *not* catastrophic — but still
prevent the race: set `ttlSecondsAfterFinished` **comfortably longer than the reconcile period** (e.g.
TTL = several × reconcile interval, minutes not seconds), OR have phaze **delete the Job explicitly
after it has recorded the outcome** (delete-on-reconcile) and set a long TTL only as a backstop for
control-plane-down scenarios. Treat "Job not found + no callback received + ledger says it was
scheduled" as "needs re-route," never as success.

**Warning signs:**
Reconcile logs `Job/Workload not found` for files that did complete; occasional duplicate Jobs for the
same `file_id`; flaky success/failure attribution that correlates with cluster load (slower reconcile).

**Phase to address:** Phase 54 — fix TTL/reconcile ordering as an explicit, tested invariant.

---

### Pitfall 4: Presigned GET URL expires before the suspended Job is admitted and fetches

**What goes wrong:**
The control plane uploads the long file to S3, mints a presigned GET URL with a "reasonable" 15-minute
or 1-hour expiry, bakes it into the Job spec, and submits the suspended Job. Kueue holds the Job behind
quota for **3 hours**. When the pod finally runs, the presigned URL is already expired — `httpx` GET
returns 403, the pod fails, and the file looks like an analysis failure when it was really a staging
timing bug.

**Why it happens:**
Two facts collide: (a) Kueue admission is intentionally indefinite under a conservative quota, and (b)
presigned URLs are short-lived by nature. Worse, **SigV4 caps presigned URLs at 7 days (604800s) — but
only if minted from long-lived IAM-user keys; if minted from STS/role temporary credentials the URL
dies when those creds expire (often 1–12h) regardless of the requested expiry** (verified, AWS docs).
A developer who tests with an admitted-immediately Job never sees the failure.

**How to avoid:**
Mint the presigned GET with an expiry that **exceeds the worst-case queue-wait + analysis time** —
hours, with margin — using **long-lived bucket credentials**, not STS/role temp creds (else the URL
silently expires early). Better yet, **stage the file just-in-time on admission**: have the reconcile
loop generate the presigned URL only once the Workload flips `Admitted=True`, so the URL's clock starts
when the pod is about to run, not when the Job is queued. If the spec must carry the URL at submit time,
size the expiry to the operator's quota-wait SLA and surface "presigned URL near expiry" as a re-mint
trigger. Distinguish a 403-on-fetch (staging/expiry) from an analysis error in the pod's exit semantics
so the control plane re-stages rather than abandoning the file.

**Warning signs:**
Pod logs show `403 Forbidden` / `Request has expired` on the GET; failures correlate with high cluster
queue depth (long waits) and never happen when quota is free; STS-based creds make even short waits fail.

**Phase to address:** Phase 53 (S3 staging seam) for the expiry/credential decision; Phase 54 for
just-in-time minting on admission.

---

### Pitfall 5: OOM on long files — MonoLoader decodes the whole set into RAM in a pod with a tight memory limit

**What goes wrong:**
The Job pod requests, say, 2Gi of memory. essentia's `MonoLoader` decodes the **entire** audio file
into a single in-RAM float array before analysis. A multi-hour Coachella set is exactly the workload
v6.0 exists to handle — and exactly the case that blows past 2Gi. The kubelet OOM-kills the pod
(exit 137). Kueue/Job sees a pod failure; with `backoffLimit: 0` the Job fails immediately, and the
file is stranded or bounced to fallback for the wrong reason.

**Why it happens:**
This is a **known phaze issue**: the v4.0.10 windowed-analysis incident was `RhythmExtractor2013`
crashing on long files, and full-file decode is memory-proportional to duration. On a persistent host
(local / OCI A1) there's lots of RAM and swap; an **ephemeral pod with a hard cgroup memory limit has
neither**, so the same file that "worked locally" OOM-kills in the cluster. Resource requests get
copied from a short-track example and never sized for the long-set tail.

**How to avoid:**
Size the Job's `resources.requests`/`limits.memory` from the **measured peak RSS of the longest real
sets**, not a guess — and add headroom (full-file float decode ≈ samplerate × channels × 4 bytes ×
duration, plus model + framework overhead; a 3-hour 44.1k stereo set is multiple GB just for the raw
buffer). Reuse the v4.0.10 windowed/streaming analysis path in the one-shot entrypoint so memory is
bounded by window size, not file length — this is the real fix; large memory limits only delay the
cliff. Set the Kueue ClusterQueue memory quota and the per-Job request consistently so admission
reflects real footprint. Treat OOM (exit 137) distinctly from analysis failure so the control plane can
escalate memory or re-route rather than marking the file permanently failed.

**Warning signs:**
Pods exit 137 / `OOMKilled` on the longest files while short files pass; failures scale with file
duration; node memory pressure / evictions appear under cloud burst load.

**Phase to address:** Phase 52 (one-shot entrypoint — reuse windowed analysis, set realistic
requests); revisit memory quota in Phase 56 (deploy/runbook).

---

### Pitfall 6: Suspended Job never admitted (quota exhausted / `Inadmissible`) looks identical to a hang

**What goes wrong:**
A submitted Job sits forever. The operator stares at "in progress." Two very different causes look
identical: (a) **quota exhausted** — Workload `QuotaReserved=False, reason=Pending`, a *normal* queued
state that resolves when quota frees; (b) **misconfiguration** — Workload `QuotaReserved=False,
reason=Inadmissible` because the `queue-name` points at a LocalQueue/ClusterQueue that doesn't exist (or
flavor mismatch), which will **never** resolve. phaze either times out the legitimate queued file as a
failure, or waits forever on a permanently-broken misconfig.

**Why it happens:**
Both states present as "Job suspended, no pod." Without reading the Workload's *reason*, they're
indistinguishable. Conservative long-file workloads spend real time in `Pending`, so a naive timeout
punishes the healthy case; meanwhile a fat-fingered LocalQueue name in config silently black-holes every
file.

**How to avoid:**
Read the **Workload condition reason**, not just presence/absence of a pod (FEATURES.md Kueue Behavior
Reference). `reason=Pending` → display "queued behind quota," do NOT time out as failure. `reason=
Inadmissible` → surface to the operator immediately as a config error (bad/missing LocalQueue), don't
wait. **Validate the configured LocalQueue exists at startup / first submit** (`kubectl get localqueue`
equivalent via kr8s) and fail fast with a clear message rather than discovering it per-file. Confirm the
served Kueue apiVersion (`v1beta2` vs deprecated `v1beta1`) at deploy time as a config constant.

**Warning signs:**
Every cloud file hangs from the first deploy (→ Inadmissible/misconfig); files hang only under load and
clear later (→ healthy Pending); operator can't tell which from the UI.

**Phase to address:** Phase 54 (read Workload reason; classify Pending vs Inadmissible); Phase 56
(startup LocalQueue validation in the runbook/config surface).

---

### Pitfall 7: Job `backoffLimit` / Kueue requeue fighting the control plane's own retry & fallback

**What goes wrong:**
The Job is created with a generous `backoffLimit` (default is 6) and/or the operator enables Kueue
PodsReady requeue. A failing analysis now retries *inside the cluster* up to 6 times — each attempt
re-fetching the (possibly expired) presigned URL and re-running a multi-hour analysis — while phaze's
own duration-routing fallback (re-route to A1/local) *also* fires. The same expensive file runs many
times across two competing retry systems, multiplying cost and producing duplicate result callbacks.

**Why it happens:**
Two independent retry owners (Kubernetes Job backoff + Kueue requeue + phaze's control-plane fallback)
are easy to leave both "on" because each has sensible defaults. FEATURES.md anti-feature: "Rely on
Kueue/Job requeue + backoff as the retry mechanism" conflates infra retry with app retry.

**How to avoid:**
**The control plane owns retry/fallback by `file_id`, full stop.** Set `backoffLimit: 0` (or 1) and
`restartPolicy: Never` so the pod runs once; a failure surfaces immediately to phaze, which decides
re-submit vs. re-route vs. mark-failed using the v5.0 routing seam. Keep Kueue PodsReady requeue/
preemption off in the runbook (conservative single-CQ, no priority — FEATURES anti-feature). A
`podFailurePolicy` can be added to classify pod-level failures (e.g. don't count an OOM/infra exit
against `backoffLimit`), but the authoritative retry decision stays with the control plane. Make
idempotent submission + idempotent callback (reconcile-by-`file_id` + ledger) absorb any double-fire
that slips through.

**Warning signs:**
The same `file_id` produces multiple result callbacks; cluster shows repeated pod attempts per Job;
cloud-compute cost/wall-clock far exceeds files-analyzed count.

**Phase to address:** Phase 54 (Job spec: backoffLimit 0/1, restartPolicy Never, optional
podFailurePolicy; idempotent submit); Phase 56 (runbook: requeue/preemption off).

---

### Pitfall 8: Orphaned S3 objects when a Job fails, evicts, or never fetches (cleanup leak)

**What goes wrong:**
The staged long file is supposed to be deleted "after analysis." Cleanup is wired only into the **happy
path** (pod POSTs success → control plane deletes the object). When the Job fails, is evicted, OOM-kills,
or the file is re-routed to A1/local instead, nobody deletes the staged object. Over a 200K-file archive
with many long sets, orphaned multi-GB objects accumulate and rack up storage cost on the operator's
bucket — the exact thing PROJECT.md's "ephemeral staging, not a data home" decision is meant to avoid.

**Why it happens:**
Cleanup is modeled as a step in the success flow rather than a guaranteed lifecycle event. Failure and
re-route paths are added later and forget to unstage. The bucket is "someone else's problem"
(operator-provided), so leaks aren't visible to phaze.

**How to avoid:**
Make cleanup **unconditional on terminal outcome**: the reconcile loop deletes the staged object on
success, failure, eviction, AND re-route — driven off the ledger entry, not the callback. Belt-and-
suspenders: set an **S3 lifecycle/TTL rule on the bucket** (operator runbook) so any object older than
N days is auto-expired regardless of phaze — this catches control-plane-crash leaks. Tie the object key
to the `file_id`/ledger so an orphan is always traceable and a sweep can reconcile bucket contents
against in-flight files.

**Warning signs:**
Bucket object count grows monotonically and doesn't drop after analyses complete; storage bill climbs;
objects exist for `file_id`s already in a terminal state.

**Phase to address:** Phase 53 (cleanup as a lifecycle event, not a success step); Phase 56 (bucket
lifecycle rule in the runbook).

---

## Moderate Pitfalls

### Pitfall 9: SigV4 / `endpoint_url` misconfig for a non-AWS S3 backend

**What goes wrong:**
The operator's bucket is MinIO / Ceph RGW / B2 / Wasabi, but the client defaults to AWS endpoints, or
uses path-style vs virtual-host addressing wrong, or signs with the wrong region. Uploads or presigned
URLs 403 / `SignatureDoesNotMatch` against the non-AWS backend.

**Why it happens:**
boto3/aioboto3 assume AWS unless told otherwise; non-AWS backends need `endpoint_url=`, often
`addressing_style="path"`, and a region the backend accepts. The bucket type is operator-provided and
transport-agnostic, so it's unknown at code time.

**How to avoid:**
Drive `endpoint_url`, region, and addressing style from pydantic-settings (no hard-coded AWS). Verify a
round-trip (put → presign → GET → delete) against the operator's actual endpoint in the deploy runbook
before any real file. Keep the pod credential-free: it only ever receives a presigned URL, so a backend
quirk surfaces on the control plane (where it's debuggable), not in an opaque pod.

**Warning signs:** `SignatureDoesNotMatch` / 403 on first upload; presigned URLs work against AWS in a
test but fail against the operator bucket.

**Phase to address:** Phase 53; runbook round-trip check in Phase 56.

---

### Pitfall 10: The result-callback bearer token leaking through the pod spec

**What goes wrong:**
To let the pod authenticate back to `/api/internal/agent/*`, the compute-agent bearer token is passed
as a plain Job container `env` value or, worse, a command-line `arg`. The token is then visible in the
Job/pod manifest to anyone with `get pod`/`get job` RBAC, in `kubectl describe`, in audit logs, and in
the Workload copy — a long-lived credential to phaze's internal API exposed cluster-wide.

**Why it happens:**
Env/arg is the path of least resistance for getting a value into a pod, and the cluster is "trusted."
But the token reaches phaze's internal write API over the VPN — it's not low-value.

**How to avoid:**
Inject the token via a **Kubernetes Secret** mounted as env-from-secret or a file, not an inline env
literal and never an arg (args leak in process listings and manifests). Scope the token to the
compute-agent role (it already is, v5.0). Prefer **short-lived, per-Job tokens** if feasible so a leaked
manifest ages out. Same treatment for the presigned URL (it grants object read) — pass via Secret/env-
from-secret, not arg. Keep kubeconfig/SA-token on the control plane via the `_FILE` convention; never
ship cluster creds into the pod.

**Warning signs:** Token visible in `kubectl get job -o yaml` / `describe pod`; one token reused across
all Jobs indefinitely; secrets appearing in pod args.

**Phase to address:** Phase 54 (pod-spec secret injection); Phase 56 (token rotation/scoping in runbook).

---

### Pitfall 11: RBAC over- or under-scoped for the control plane's kube identity

**What goes wrong:**
The kubeconfig/SA phaze uses is either cluster-admin ("just make it work") — a huge blast radius if the
control plane or VPN is compromised — or too narrow (can create Jobs but can't read Workloads / can't
delete Jobs), so watch/reconcile/cleanup silently fail with 403s that look like hangs.

**Why it happens:**
RBAC is fiddly; people grant broad to unblock, or copy a minimal example that omits the Workload-read or
Job-delete verbs phaze actually needs.

**How to avoid:**
Least-privilege Role in the single target namespace: `create/get/list/watch/delete` on
`batch/jobs`, `get/list/watch` on `kueue.x-k8s.io/workloads`, and `get` on the configured `localqueue`.
No cluster-scoped grants (phaze never touches ResourceFlavor/ClusterQueue — those are admin). Document
the exact RBAC in the runbook and test it with the real SA before go-live.

**Warning signs:** 403s in submit/watch/delete paths; cleanup never runs (missing delete verb);
conversely, the SA can read secrets/other namespaces it has no reason to.

**Phase to address:** Phase 56 (runbook RBAC manifest); exercised in Phase 54.

---

### Pitfall 12: Pod can't reach `/api/internal/agent/*` over the VPN — internal CA not trusted in the pod

**What goes wrong:**
phaze's internal API is HTTPS via a **self-signed internal CA** (v4.0 invariant). The Job pod's `httpx`
callback hits the API over the operator VPN and fails TLS verification because the pod's image doesn't
trust `phaze-ca.crt`. Either the callback fails (file stranded) or someone "fixes" it with
`verify=False` — disabling TLS verification on the internal write API over a VPN.

**Why it happens:**
The CA-distribution step (operator scp's `phaze-ca.crt` to file servers in v4.0) has no equivalent for
ephemeral pods that don't exist yet at provisioning time. The reachable API endpoint over the VPN is
operator-provided, so the URL is configured but the trust anchor is forgotten.

**How to avoid:**
Bake or mount the internal CA cert into the Job image / pod (via ConfigMap or the image's trust store)
and point `httpx` at it (`verify=/path/to/phaze-ca.crt`) — **never `verify=False`**. Make the API
callback URL a config value (operator-provided VPN-reachable endpoint), transport-agnostic. Confirm the
pod→API TLS round-trip in the deploy runbook.

**Warning signs:** Pod callback fails with `SSLCertVerificationError`; a `verify=False` creeps into the
pod's httpx client; callbacks work on LAN but fail from the cluster.

**Phase to address:** Phase 52 (CA into the image) + Phase 54 (callback config); runbook verify in 56.

---

### Pitfall 13: One-shot entrypoint exit-code semantics swallow failures or hide partial work

**What goes wrong:**
The one-shot entrypoint (pull → analyze → POST → exit) catches its own exceptions and `exit 0`
regardless, or POSTs a "success" callback before confirming the analysis actually produced valid output.
The Job shows `Complete`, the control plane marks the file analyzed, but the result is empty/garbage — or
a real failure (download 403, OOM-survived-but-wrong) is masked as success.

**Why it happens:**
Entrypoints often wrap everything in a try/except to "be robust," inverting exit semantics. The Job's
`Complete` vs `Failed` condition is only meaningful if the process exits non-zero on failure.

**How to avoid:**
Make exit codes honest and **distinct**: 0 only after a validated result POST returns 2xx; non-zero on
download failure (distinguish 403/expiry from network), on analysis failure, on callback failure. Don't
POST success before the analysis is validated. Let the process crash/exit-non-zero so the Job goes
`Failed` and the control plane re-routes. Reconcile-by-`file_id` + idempotent callback means a
re-submit after a non-zero exit is safe.

**Warning signs:** Jobs always `Complete` even when files didn't analyze; `FileRecord`s marked analyzed
with empty/implausible BPM/key; no `Failed` Jobs ever despite known bad inputs.

**Phase to address:** Phase 52 (entrypoint exit-code contract).

---

### Pitfall 14: Pod eviction mid-analysis treated as analysis failure (or not handled at all)

**What goes wrong:**
A node drains, a higher-QoS pod preempts, or Kueue's `maximumExecutionTimeSeconds` deactivates a
runaway Workload — the pod dies hours into a long analysis. phaze either marks the file permanently
failed (losing the file from the pipeline) or doesn't notice (stranded), instead of re-routing.

**Why it happens:**
Eviction (`Evicted`, reason `WorkloadInactive`/`Deactivated`) is a distinct Kueue signal from analysis
failure (Job `Failed`) (FEATURES.md), and it's easy to lump all non-success into "failed."

**How to avoid:**
Detect the `Evicted`/deactivated Workload condition and route it back through the v5.0 fallback (re-
submit, or re-route to A1/local) rather than terminal-failing. Set Job pods `restartPolicy: Never` and
let the control plane own re-attempt. Conservative single-CQ no-preemption setup makes this rare, but
detecting it is cheap and prevents stranded long files. A bounded `maximumExecutionTimeSeconds` is a
useful server-side runaway guard (mirrors v4.0.10) — but its eviction must route to fallback, not death.

**Warning signs:** Long files intermittently land in FAILED with no analysis error; failures coincide
with cluster node maintenance / capacity changes; Workloads show `Evicted` but `FileRecord` is terminal.

**Phase to address:** Phase 54 (eviction detection → fallback); FEATURES marks this P2 (add after happy
path proven) — acceptable to defer detection-to-reroute to a v6.x follow-up, but don't terminal-fail
evictions in the meantime.

---

### Pitfall 15: Driving the whole backlog instead of ledger-scoped long files (over-enqueue)

**What goes wrong:**
The K8s router target, when enabled, sweeps every eligible file (or the whole backlog) into cluster
Jobs instead of only the **timed-out long files the ledger says should burst**. phaze has hit this
exact class of bug repeatedly: the v4.0.6 default-queue incident stranded 11,428 jobs; the v5.0 "Recover
orphaned work `force=True`" swept 44.5k jobs. At cluster scale this also instantly exhausts Kueue quota
and floods S3 with staged objects.

**Why it happens:**
A new routing target is wired to "all eligible" rather than reusing v5.0's **scheduling-ledger scope**
(only previously-scheduled long files). The active-target toggle flipping to K8s re-routes more than
intended.

**How to avoid:**
Reuse v5.0's ledger-scoped backfill verbatim — K8s is a third *target* of the same duration-routing +
ledger seam, not a new "analyze everything" path. Only files at/above the duration threshold that timed
out locally and are ledger-tracked are eligible. Add the same AST/guard test phaze uses elsewhere to
prevent an enqueue site from bypassing the router. Cap concurrent in-flight Jobs ("stay one ahead"
analog) so even a correct scope can't flood quota/S3.

**Warning signs:** Sudden spike of submitted Jobs / staged objects far exceeding the long-file count;
Kueue quota instantly saturated; short files appearing as cluster Jobs.

**Phase to address:** Phase 55 (router/ledger scoping) — the guard test is the verification.

---

### Pitfall 16: Active-cloud-target toggle misrouting (local / A1 / K8s)

**What goes wrong:**
The single config setting that selects the active cloud target (local / A1 / K8s) is read
inconsistently across the three cloud entry points (routing seam, staging cron, backfill), so a file is
staged for K8s but routed to A1, or `cloud_burst_enabled` is off yet a K8s submit still fires. phaze's
v5.0 toggle gates three entry points; adding a third *target* multiplies the misroute surface.

**Why it happens:**
A boolean `cloud_burst_enabled` plus a new tri-state target is easy to check in some places and not
others; the staging step and the submit step can disagree about the target.

**How to avoid:**
Resolve the active target **once** through a single helper (analogous to v5.0's
`enqueue_router.resolve_queue_for_task`) that every cloud entry point consults — staging, submission,
and reconcile all key off the same resolved target. The master `cloud_burst_enabled` toggle short-
circuits all three. Test all three entry points honor both the master toggle and the target selector.

**Warning signs:** A file staged to S3 but analyzed on A1 (or vice versa); K8s Jobs created while
`cloud_burst_enabled=false`; staging and submit logs disagree on target.

**Phase to address:** Phase 55 (single target-resolution helper + toggle gating).

---

### Pitfall 17: Transport-specific (Tailscale/WireGuard) assumptions leaking into code

**What goes wrong:**
Code hard-codes a Tailscale hostname/MagicDNS name, a `100.x` CGNAT address, or shells out to
`tailscale` — breaking the PROJECT.md mandate that v6.0 is **transport-agnostic** (Tailscale OR
WireGuard). v5.0's pipeline was deliberately Tailscale-specific; copying that pattern regresses the
generalization.

**Why it happens:**
v5.0 code and runbooks reference Tailscale concretely; reuse drags those assumptions forward. The
operator VPN "just is Tailscale on my box," so it's tempting to special-case it.

**How to avoid:**
phaze consumes only **operator-provided reachable endpoint URLs** (kube API, S3 endpoint, callback URL)
from pydantic-settings — no mesh SDK, no hostname assumptions, no `tailscale`/`wg` shell calls (STACK.md
"What NOT to Use"). The same config works whether the operator runs Tailscale or WireGuard. Grep guard
against `tailscale`/`100.` literals in v6.0 code paths.

**Warning signs:** `tailscale`/`wg`/`100.64.` strings in v6.0 modules; config that only accepts a
MagicDNS name; deploy docs that assume one mesh.

**Phase to address:** Phase 56 (config/docs); enforced as a convention across 52–55.

---

### Pitfall 18: Object-key collisions and non-idempotent multi-attempt staging

**What goes wrong:**
Staged objects are keyed by filename or a non-unique field, so two files (or a re-stage of the same
`file_id` after a failed attempt) collide — one overwrites the other, or a stale object is fetched by
the wrong Job. Large sets also exceed single-PUT limits, needing multipart, which a naive `put_object`
doesn't do.

**Why it happens:**
Filenames in a 200K archive collide (many "set1.mp3"); re-stage-on-retry reuses the same key; multi-GB
uploads silently need multipart.

**How to avoid:**
Key objects by a collision-proof identity tied to the ledger — e.g. `{file_id}/{sha256-or-attempt}` —
so re-stage is idempotent/traceable and two files never collide. Use the SDK's managed `upload_file`/
multipart for large sets (aioboto3 handles multipart transparently above the threshold). The
`file_id`-scoped key also makes orphan cleanup (Pitfall 8) and reconcile trivially correlatable.

**Warning signs:** A Job analyzes the wrong audio (key collision); large-file uploads truncate/fail;
re-staged objects pile up under colliding keys.

**Phase to address:** Phase 53 (key scheme + multipart upload).

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Block a worker on unbounded `job.wait()` | Simplest happy-path code | Worker-slot starvation under real (hours-long) queue waits; not restart-safe | Never — submit-then-reconcile from the start |
| Short presigned-URL expiry (15m/1h) | Tighter security window | 403s whenever Kueue queues the Job for hours | Only with just-in-time minting on admission |
| Presigned URL minted from STS/role temp creds | No long-lived keys | URL silently dies in 1–12h regardless of requested expiry | Only if every Job is admitted well within the cred lifetime (not guaranteed) |
| Cleanup wired only into the success callback | Less code | Orphaned multi-GB objects on failure/re-route → storage cost | Never — cleanup must be terminal-outcome-driven + bucket lifecycle backstop |
| `verify=False` for the pod→API callback | Skips CA distribution to pods | Unauthenticated-TLS on the internal write API over a VPN | Never — mount the internal CA into the image |
| Default `backoffLimit` (6) on the Job | Fewer transient failures bubble up | Multi-hour analysis runs ≤6× and fights phaze's own fallback; cost blowup | Never for this workload — backoffLimit 0/1, control plane owns retry |
| Tailscale hostname hard-coded (copied from v5.0) | Works on the dev box today | Breaks WireGuard operators; regresses transport-agnostic mandate | Never in v6.0 code — endpoints are config |
| Big memory limit instead of windowed analysis | Pod stops OOM-ing today | Cliff just moves to the next longer set; quota waste | Only as interim while wiring windowed analysis |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Kueue Workload | Hard-coding the Workload name (`<job>-<hash>`) | Resolve via Job owner-ref / `kueue.x-k8s.io/job-uid` label selector (FEATURES.md) |
| Kueue apiVersion | Assuming `v1beta1` (deprecated) or `v1beta2` blindly | Make apiVersion a config constant; confirm served version via `/apis/kueue.x-k8s.io` at deploy (STACK.md) |
| Kube watch over VPN | Resuming a stale `resourceVersion` after a drop → 410 Gone, missed events | Bookmarked watch + on-error fall back to list+reconcile; callback is source of truth |
| S3 (non-AWS backend) | Defaulting to AWS endpoints / wrong addressing style | `endpoint_url` + addressing style + region from settings; runbook round-trip test |
| S3 presigned URL | Expiry shorter than queue-wait; minted from temp creds | Long expiry from long-lived keys, or just-in-time mint on admission |
| Internal API callback | Pod doesn't trust the self-signed internal CA | Mount `phaze-ca.crt` into the image; `httpx verify=<ca>`, never `verify=False` |
| Bearer token / presigned URL → pod | Passed as inline env literal or CLI arg (leaks in manifest) | Inject via Kubernetes Secret (env-from-secret/file); never an arg |
| kr8s in a SAQ task | Holding the event loop on a long wait | Bounded `wait(timeout=)`, re-poll next task run; state on `FileRecord` |
| Job retry | Leaving Job backoff + Kueue requeue + phaze fallback all on | `backoffLimit 0/1`, `restartPolicy Never`, requeue/preemption off; control plane owns retry |

## Performance / Cost Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| OOM on long files (full-file MonoLoader decode) | Exit 137 on the longest sets only | Windowed analysis + memory requests sized from measured peak RSS | First multi-hour set in a tight-memory pod |
| Whole-backlog over-enqueue into the cluster | Job/object spike ≫ long-file count; quota saturated | Ledger-scoped routing + concurrency cap + AST guard | The moment K8s target is enabled without scope |
| Orphaned S3 objects accumulating | Bucket object count never drops; bill climbs | Terminal-outcome cleanup + bucket lifecycle TTL | After the first wave of failed/re-routed Jobs |
| Worker-slot starvation on long waits | `controller` queue backs up; crons lag | Submit-then-reconcile, never hold the slot | A few concurrent hours-long queue waits |
| Re-running the same expensive analysis | Cost/wall-clock ≫ files analyzed | Single attempt + idempotent reconcile-by-`file_id` | Any failure path with default backoff on |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Bearer token / presigned URL as pod env literal or arg | Long-lived internal-API credential / object-read URL exposed cluster-wide in manifests, describe, audit logs | Kubernetes Secret injection; short-lived per-Job tokens where feasible |
| `verify=False` on the pod→internal-API callback | Unauthenticated TLS to phaze's internal write API over a VPN | Mount internal CA into the image; verify against it |
| Cluster-admin kubeconfig for the control plane | Huge blast radius if control plane / VPN compromised | Least-privilege namespaced Role (jobs CRUD + workloads read + localqueue get) |
| Long-lived S3 keys shipped into the pod | Cluster compromise = bucket compromise | Pod is credential-free: presigned GET URL only; keys stay on control plane via `_FILE` |
| kubeconfig/SA token in env not `_FILE` secret | Cluster creds in process env / image layers | `_FILE` convention (existing); Docker/K8s secret mount |
| Bucket left as a data home | Long files persist off-host indefinitely | Ephemeral only: delete after analysis + lifecycle TTL; never canonical |

## "Looks Done But Isn't" Checklist

- [ ] **Result reconciliation:** Works in the demo via the watch — verify the `FileRecord` still reaches terminal state from the **callback alone** with the watch killed mid-run.
- [ ] **TTL vs read:** Job cleans up nicely — verify phaze still attributes success/failure correctly when the Job is GC'd **before** the next reconcile tick.
- [ ] **Presigned URL:** Fetch works in a fast test — verify it still works when the Job sits suspended **for hours** before fetching (and isn't minted from temp creds).
- [ ] **OOM:** Short tracks pass — verify the **longest real Coachella set** doesn't exit 137 under the pod's memory limit.
- [ ] **Cleanup:** Object deleted on success — verify it's **also** deleted on Job failure, eviction, and re-route-to-A1/local.
- [ ] **Quota hang:** "In progress" shown — verify phaze distinguishes `Pending` (queued) from `Inadmissible` (misconfig) and surfaces the latter.
- [ ] **Toggle:** K8s path works — verify `cloud_burst_enabled=false` blocks staging **and** submission **and** reconcile.
- [ ] **Scope:** Long file bursts — verify a full pipeline run does **not** sweep the whole backlog or short files into the cluster.
- [ ] **Transport-agnostic:** Works on Tailscale — verify no `tailscale`/`100.x` literals; same config runs on WireGuard.
- [ ] **CA trust:** Callback succeeds — verify it's via the mounted internal CA, not `verify=False`.
- [ ] **Exit codes:** Job shows Complete — verify a download-403 or analysis failure exits **non-zero** and shows Job `Failed`.

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Lost watch / stranded in-flight files | LOW | Reconcile loop re-reads Workload/Job by `file_id`; callback already durable — file advances on next tick |
| Job GC'd before status read | LOW | Treat "no Job + no callback + ledger-scheduled" as re-route; idempotent re-submit |
| Presigned URL expired before fetch | LOW | Re-mint URL (long expiry / on-admission) and re-submit the same `file_id` (idempotent) |
| OOM on long files | MEDIUM | Switch entrypoint to windowed analysis; raise memory request/quota; re-route stranded files |
| Orphaned S3 objects | LOW-MEDIUM | Sweep bucket vs in-flight `file_id`s; delete orphans; enable bucket lifecycle TTL going forward |
| Over-enqueued backlog into cluster | MEDIUM-HIGH | Purge/suspend extra Jobs; delete staged objects; re-scope to ledger; add AST guard (cf. v4.0.6/v5.0 recovery playbooks) |
| Inadmissible misconfig (bad LocalQueue) | LOW | Fix config; startup validation prevents recurrence; suspended Jobs re-submit cleanly |
| Token/URL leaked in pod manifest | MEDIUM | Rotate the compute-agent token; move to Secret injection; shorten token lifetime |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Watch as result channel (lost watch loses result) | 54 | Kill watch mid-run; `FileRecord` still reaches terminal from callback |
| Worker-slot starvation on `job.wait` | 54 | Submit task returns fast; reconcile advances state on next tick |
| TTL-vs-read race | 54 | Status attributed correctly when Job GC'd before reconcile |
| Presigned URL expiry vs queue wait | 53 (+54 on-admission mint) | Hours-suspended Job still fetches; not from temp creds |
| OOM on long files | 52 | Longest real set analyzes without exit 137 |
| Inadmissible vs Pending hang | 54 (+56 startup validation) | UI shows misconfig vs queued; bad LocalQueue fails fast |
| Job backoff / requeue vs control-plane retry | 54 (+56 runbook) | One pod attempt per Job; no duplicate callbacks |
| Orphaned S3 objects | 53 (+56 lifecycle TTL) | Object deleted on failure/eviction/re-route, not just success |
| SigV4/`endpoint_url` misconfig | 53 (+56 round-trip test) | Put→presign→GET→delete passes against operator bucket |
| Token/URL leak in pod spec | 54 (+56 rotation) | No secret in `job -o yaml`/args; Secret injection only |
| RBAC over/under-scoped | 56 (exercised 54) | Least-priv Role passes submit/watch/delete; no cluster grants |
| Pod can't trust internal CA | 52 (+54 config, 56 verify) | Callback succeeds via mounted CA, never `verify=False` |
| Exit-code semantics | 52 | Download-403/analysis-fail → non-zero → Job Failed |
| Pod eviction mid-analysis | 54 (detection P2/v6.x) | Evicted Workload re-routes, not terminal-failed |
| Whole-backlog over-enqueue | 55 | Pipeline run bursts only ledger-scoped long files; AST guard green |
| Active-target toggle misrouting | 55 | All 3 entry points honor toggle + single target resolver |
| Transport-specific leakage | 56 (convention 52–55) | No `tailscale`/`100.x` literals; same config on WireGuard |
| Object-key collision / multipart | 53 | `file_id`-scoped keys; large set uploads via multipart |

## Sources

- phaze sibling research — `.planning/research/FEATURES.md` (Kueue lifecycle, Workload conditions, TTL-vs-read ordering hazard, anti-features) and `.planning/research/STACK.md` (kr8s/aioboto3, presigned-URL-in-pod, `_FILE` secrets, v1beta2 apiVersion, integration-with-async-stack notes) — HIGH, both verified against Context7 `/kubernetes-sigs/kueue` + `/kr8s-org/kr8s` 2026-06-26
- phaze `.planning/PROJECT.md` v6.0 milestone + Key Decisions (CPU-only nodes, ephemeral object staging, ledger scoping, transport-agnostic, reuse of v5.0 compute-agent callback) — HIGH
- phaze project memory — v4.0.6 default-queue 11,428-job over-enqueue incident; v5.0 `force=True` 44.5k-job over-enqueue; v4.0.10 windowed-analysis OOM/crash on long sets; v4.0 self-signed internal CA + `_FILE` secrets invariants — HIGH (lived incidents)
- AWS S3 presigned-URL expiration limits (SigV4 max 7 days = 604800s; URLs minted from STS/role temporary credentials expire when the credential expires, typically 1–12h, regardless of requested expiry) — HIGH:
  - https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html
  - https://repost.aws/questions/QUnrDfUE8QRgCLBaV-guPalg/s3-presigned-url-is-valid-for-7-days-but-the-role-it-s-associated-with-is-only-12-hours-is-it-possible-to-make-it-last-the-actual-stated-7days
  - https://elasticscale.com/blog/why-do-s3-pre-signed-urls-expire-after-12-hours-despite-setting-a-longer-duration/

---
*Pitfalls research for: adding remote Kueue-Job submission + S3 staging to phaze's async control plane (v6.0)*
*Researched: 2026-06-26*
