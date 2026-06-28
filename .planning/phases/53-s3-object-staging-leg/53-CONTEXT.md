# Phase 53: S3 object-staging leg - Context

**Gathered:** 2026-06-27
**Status:** Ready for planning

<domain>
## Phase Boundary

Build the **S3 object-staging leg** that moves a long file from the file-server
agent into ephemeral S3-compatible object storage and back down to the Kueue Job
pod, entirely via **presigned URLs**:

> control plane presigns a multipart PUT → file-server agent streams the bytes to
> the presigned part URLs over httpx → control plane completes the multipart upload →
> (later, post-admission) the pod requests a just-in-time presigned GET → pod downloads
> and sha256-verifies → object is deleted on the result callback.

Locked by KSTAGE-01..05 (see REQUIREMENTS.md):
- control plane presigns PUT/GET + deletes objects via **aioboto3**, and **never reads
  or uploads file bytes itself** — preserving the CI-enforced DIST-01 no-media-mount
  boundary on the application server (KSTAGE-01)
- the file-server agent uploads bytes to the presigned URL over **httpx**, then
  callbacks the control plane; **no S3 SDK or bucket credentials on the agent or the
  pod** (KSTAGE-02)
- the presigned **GET** URL is minted **just-in-time at pod startup** (necessarily
  post-admission) so it never expires during a long Kueue quota wait — Phase 52 already
  built the pod-side client (`request_download_url`) and the presign response schema;
  **this phase fills in the server side** (KSTAGE-03)
- each staged object uses a **`file_id`-scoped key** and is deleted on **every** terminal
  outcome, with a bucket **lifecycle TTL** backstop (KSTAGE-04)
- S3 endpoint, bucket, addressing style, and credentials are operator-provided via
  **`_FILE` secrets** and work against any S3-compatible backend (`endpoint_url`), not
  just AWS (KSTAGE-05)

Includes the **`cloud_job` sidecar Alembic migration**. Testable end-to-end **without a
live cluster** (moto / botocore stubber + respx). The routing/`stage_cloud_window` wiring
that *triggers* this leg is **Phase 55**; the kube reconcile loop that owns eviction
cleanup is **Phase 54**. This phase builds the leg and unit-tests it standalone.

</domain>

<decisions>
## Implementation Decisions

### Upload path (KSTAGE-02)
- **D-01:** Use **presigned multipart upload**, not a single PUT. The control plane
  initiates the multipart upload and presigns each **part** URL (aioboto3); the
  file-server agent splits the file and PUTs parts over httpx; the control plane
  **completes** the multipart upload. Rationale: resumable and >5GB-capable for very
  long lossless sets, without ever putting an S3 SDK or credentials on the agent (the
  agent only sees presigned part URLs). The control plane gains multipart
  init/presign-parts/complete (and abort) seams — it still never touches file bytes
  (DIST-01 preserved: it orchestrates, the agent transfers).

### Object cleanup (KSTAGE-04)
- **D-02:** **The analysis-result callback deletes the object inline.** When the pod's
  result (success *or* failure) lands on `/api/internal/agent/*`, the control-plane
  handler deletes the staged object via aioboto3 at that moment — the point it is
  provably no longer needed. The **bucket lifecycle TTL** is the backstop for the
  no-callback case (Kueue eviction / lost pod), and Phase 54's reconcile loop may invoke
  the same delete for evicted Jobs. Phase 53 owns the delete **capability** (a
  control-plane `delete_staged_object(file_id)`-style service) plus configures the TTL;
  the eviction-path *triggering* is Phase 54's. (Note: a multipart upload that never
  completes must also be cleaned — abort-multipart-upload + TTL both cover the orphaned
  in-flight upload.)

### `cloud_job` sidecar table (this phase's migration)
- **D-03:** Create `cloud_job` **with staging-only columns now**, **one row per
  `file_id`** (unique FK — one active burst per file). Columns this phase needs: PK,
  unique FK to `file_id`, `s3_key` (file_id-scoped), a stage/upload **status enum**, the
  multipart **`upload_id`**, and timestamps. Phases 54 (`kueue_workload`/job name) and 55
  (`cloud_phase`) add their columns in **their own** migrations — matches the roadmap's
  "`cloud_phase` added in 55" note and keeps each migration scoped to its phase. Exact
  column names/enum values are planner's to finalize against the v5.0 scheduling-ledger
  precedent.

### Integrity gate (interacts with KJOB-02)
- **D-04:** **The pod's end-to-end sha256-verify against `FileRecord` is the single
  integrity gate.** No S3-side per-part checksums (`Content-MD5` / `x-amz-checksum`) are
  added. Rationale: mirrors the v5.0 precedent ("app-level sha256 covers integrity";
  rsync omitted `-c`). The agent streams multipart parts with no extra digest
  bookkeeping; any corruption introduced during upload, storage, or download is caught by
  the one end-to-end hash the pod already computes (Phase 52 `request_download_url`
  returns `expected_sha256`) **before** the multi-hour analysis runs. The gate sits at
  the last possible moment, on the bytes that actually feed analysis.

### Claude's Discretion
- Multipart **part size** and upload **concurrency** on the agent (and whether to stream
  parts to bound agent memory) — tuning, planner/researcher decide.
- Presigned-URL **TTLs** (PUT/part TTL vs the just-in-time GET TTL).
- Exact **upload-trigger seam** in Phase 53 (a presign+enqueue task mirroring Phase 50's
  `push_file` + `agent_push` callback, built and unit-tested here but **not** wired into
  `stage_cloud_window` until Phase 55) vs. a thinner surface — factor against the existing
  `push.py` / `agent_push.py` precedent.
- Exact `cloud_job` column names, the status enum members, and the upload-failure /
  re-drive loop shape (mirror Phase 50's `push_max_attempts` / `/mismatch` pattern).
- S3 client/config surface shape (`endpoint_url`, addressing style, region) on
  `ControlSettings`, resolved via the `_FILE` convention.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase scope & requirements
- `.planning/ROADMAP.md` — Phase 53 line + the v6.0 Kubernetes Burst Analysis intro (the
  image → legs → pipeline → routing → deploy spine; Phase 53 is the staging leg). Note
  the dependency: Phase 52 defines the pod's httpx-GET download interface this leg feeds;
  Phase 54 bakes the presigned GET URL into the Job spec; Phase 55 wires the trigger.
- `.planning/REQUIREMENTS.md` §"S3 object-staging leg (KSTAGE)" — KSTAGE-01..05 are the
  locked requirements; also see the v6.0 out-of-scope list (reverses v5.0's "no object
  storage" decision — object storage is now in scope, ephemeral only).
- `.planning/phases/52-job-runner-image-one-shot-entrypoint/52-CONTEXT.md` — the
  immediately-prior phase; the pod entrypoint, `request_download_url`, and the HTTP-only
  agent boundary this leg plugs into.

### Presign client + schema (Phase 52 stubs this phase completes)
- `src/phaze/services/agent_client.py` — `request_download_url(file_id)` (line ~282): the
  pod-side client that POSTs `/api/internal/agent/files/{file_id}/presign-download` and
  expects `(download_url, expected_sha256)`. **This phase implements the missing server
  route + the aioboto3 presign behind it.**
- `src/phaze/schemas/agent_analysis.py` (§ presign response, ~line 108) — the presigned-
  download response schema (`download_url`, `expected_sha256`) already defined Phase 52;
  the server response must match.
- `src/phaze/routers/agent_analysis.py` — the `/api/internal/agent/*` result callback
  target; **D-02 hooks the inline object delete into this handler's success + failure
  paths.**

### Staging-leg precedent (v5.0 Phase 50 push pipeline)
- `src/phaze/tasks/push.py` — `push_file` task: the v5.0 file-server-initiated transfer
  leg (rsync). Precedent for an agent-side transfer task with the Postgres-free import
  boundary, outer/inner timeout layering, and TERMINAL-vs-retryable error handling. The
  S3 upload task mirrors its shape (httpx PUT parts instead of rsync).
- `src/phaze/routers/agent_push.py` — `/api/internal/agent/push/{file_id}/{pushed,mismatch}`:
  the control-side push callbacks. Precedent for the upload-complete / upload-failure
  callbacks (state flip + ledger clear + re-drive loop; AUTH-01 `file_id`-on-path
  discipline).
- `src/phaze/schemas/agent_tasks.py` / `src/phaze/schemas/agent_push.py` — payload +
  callback response schemas to mirror.
- `src/phaze/services/enqueue_router.py` — `resolve_queue_for_task` / `select_active_agent`:
  every enqueue routes through here (Phase 30 invariant). The presign-upload trigger task
  must too (enforced by the AST guard test — see Phase 55).

### Config / secrets
- `src/phaze/config.py` — `BaseSettings` `_FILE` secret resolution (`SECRET_FILE_FIELDS`,
  `_resolve_secret_files`); `ControlSettings`/`AgentSettings` split. The S3
  endpoint/bucket/key/secret fields land on `ControlSettings` (control plane presigns;
  agent gets none of them) and honor the `_FILE` convention (KSTAGE-05).

### Database
- `src/phaze/models/` + Alembic migrations dir — the `cloud_job` model + migration land
  here; follow the existing 14-migration / 14-model conventions and the v5.0
  `scheduling_ledger` precedent for a per-`file_id` sidecar.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `agent_client.request_download_url` + the presign response schema (Phase 52) — the
  pod-side half of the GET leg already exists; this phase only adds the server route +
  aioboto3 presign behind it.
- `tasks/push.py` + `routers/agent_push.py` (Phase 50) — a complete, battle-tested
  file-server-initiated transfer-leg pattern (task + control-side callbacks + re-drive
  loop) to adapt from rsync to httpx-multipart-PUT.
- `config.py` `_FILE`-secret machinery — adding S3 secret fields to
  `SECRET_FILE_FIELDS` auto-resolves their `_FILE` siblings with no new resolution code.
- `enqueue_router.resolve_queue_for_task` — the single enqueue seam the upload-trigger
  task must route through.

### Established Patterns
- **DIST-01 no-media-mount boundary (CI-enforced):** the application server never reads
  file bytes. The control plane presigns and orchestrates; the agent (which owns the
  media mount) transfers. KSTAGE-01 is this boundary applied to S3 — design must keep
  bytes off the control plane.
- **HTTP-only agent boundary (v4.0):** the agent and pod are Postgres-free; all state
  changes go through `/api/internal/agent/*`. The upload task carries no DB/ORM imports
  (mirror the `test_task_split.py` import-boundary test); the upload-complete/-failure
  outcome reports via a token-authed callback, not a direct enqueue.
- **`_FILE`-convention secrets (v4.0.1):** S3 creds reach the control plane via
  file-based secrets, never request bodies; never logged.
- **Reconcile-by-`file_id`:** keys, callbacks, and the `cloud_job` row are all
  `file_id`-scoped — no pod/Job-scoped identity in the staging leg.
- **Single enqueue seam (Phase 30):** no consumer-less default-queue enqueues; the
  presign-upload trigger routes through `enqueue_router`.

### Integration Points
- New server route `POST /api/internal/agent/files/{file_id}/presign-download` (GET-side,
  satisfies the Phase 52 client) + the multipart-PUT presign/complete surface (PUT-side).
- Inline object delete hooked into the `agent_analysis` result callback (D-02).
- New `cloud_job` table + migration.
- The upload-trigger task is built + unit-tested here; **wired into `stage_cloud_window`
  in Phase 55**, and the presigned GET URL is baked into the Job spec in **Phase 54**.

</code_context>

<specifics>
## Specific Ideas

- The presigned GET must be minted *at pod startup*, not at submit time — a long Kueue
  quota wait would expire a submit-time URL (drove the KSTAGE-03 just-in-time design;
  Phase 52's `request_download_url` is exactly this call).
- Protect the expensive artifact: integrity is verified on the bytes that feed analysis,
  at the last moment, by the hash the pod already computes — no redundant S3 checksums
  (drove D-04).
- Cleanup should happen the instant the result is known (callback), with the TTL as a
  "nothing leaks even if a path is missed" safety net (drove D-02).

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope. Adjacent work explicitly owned by other
phases (not deferred, already on the roadmap): the `stage_cloud_window` K8s branch +
routing seam (Phase 55), the Kueue submit/watch + reconcile-driven eviction cleanup
(Phase 54), and `cloud_phase`/`kueue_workload` columns on `cloud_job` (Phases 55/54).

</deferred>

---

*Phase: 53-s3-object-staging-leg*
*Context gathered: 2026-06-27*
