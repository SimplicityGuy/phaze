# Phase 53: S3 object-staging leg - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-27
**Phase:** 53-s3-object-staging-leg
**Areas discussed:** Large-file upload path, Object cleanup seam & TTL, cloud_job table scope, Upload integrity gate

---

## Large-file upload path

| Option | Description | Selected |
|--------|-------------|----------|
| Single PUT | One presigned PUT URL, httpx streams the body; simplest, ≤5GB cap, no resume; size-guard backstop | |
| Presigned multipart | Control plane presigns each part URL + completes; resumable, >5GB-capable; agent does part-splitting | ✓ |
| You decide | Defer to planner/researcher | |

**User's choice:** Presigned multipart
**Notes:** Resumability and the >5GB ceiling for very long lossless sets outweighed the simpler single-PUT path. Control plane still never touches bytes (presign + complete only), preserving DIST-01.

---

## Object cleanup seam & TTL

| Option | Description | Selected |
|--------|-------------|----------|
| Service fn + TTL; Ph54 wires terminal deletes | delete_staged_object service + lifecycle TTL; Phase 54 reconcile wires success/eviction deletes | |
| Lifecycle-TTL only for now | Only set the TTL; no explicit deletes until Phase 54 | |
| Callback-handler deletes inline | The `/api/internal/agent/*` result callback deletes the object on success + failure; TTL backstops eviction | ✓ |

**User's choice:** Callback-handler deletes inline
**Notes:** Delete at the instant the result is known. TTL (and Phase 54 reconcile) covers the eviction/no-callback case; orphaned in-flight multipart uploads also need abort + TTL.

---

## cloud_job table scope

| Option | Description | Selected |
|--------|-------------|----------|
| Staging columns only, one row per file_id | PK + unique FK to file_id, s3_key, status enum, multipart upload_id, timestamps; 54/55 extend | ✓ |
| Full lifecycle schema up front | Model staging + Kueue admission + cloud_phase now, even though 54/55 populate them | |
| You decide | Defer column set to planner/researcher | |

**User's choice:** Staging columns only, one row per file_id
**Notes:** Each migration stays scoped to its phase; matches the roadmap's "cloud_phase added in Phase 55" note. Exact column names/enum members left to planner.

---

## Upload integrity gate

| Option | Description | Selected |
|--------|-------------|----------|
| Pod sha256 is the single gate | Rely solely on the pod's GET-then-sha256-verify against FileRecord (v5.0 precedent) | ✓ |
| Add S3 per-part checksums too | x-amz-checksum / Content-MD5 per multipart part, in addition to the end-to-end sha256 | |
| You decide | Defer to planner/researcher | |

**User's choice:** Pod sha256 is the single gate
**Notes:** One end-to-end hash on the exact bytes feeding analysis, at the last possible moment (Phase 52 already computes it). No redundant S3-side checksum bookkeeping; mirrors v5.0 (rsync omitted `-c`).

---

## Claude's Discretion

- Multipart part size + upload concurrency / memory-streaming on the agent.
- Presigned-URL TTLs (PUT/part vs just-in-time GET).
- Upload-trigger seam shape (presign+enqueue task mirroring Phase 50 `push_file`/`agent_push`, built + unit-tested here, wired into `stage_cloud_window` in Phase 55) vs a thinner surface.
- Exact `cloud_job` column names, status enum members, and the upload-failure / re-drive loop.
- S3 client/config surface (`endpoint_url`, addressing style, region) on `ControlSettings` via `_FILE` secrets.

## Deferred Ideas

None — discussion stayed within phase scope. Adjacent work is already on the roadmap (Phase 54 reconcile/eviction cleanup, Phase 55 routing seam + `cloud_phase` column).
