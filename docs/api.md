<!-- generated-by: gsd-doc-writer -->
# API Reference

## Health

| Method | Path      | Description     |
|--------|-----------|-----------------|
| GET    | `/health` | Health check (verifies DB connectivity) |

## Scan (`/api/v1`)

| Method | Path                     | Description                          |
|--------|--------------------------|--------------------------------------|
| POST   | `/api/v1/scan`           | Start file discovery scan            |
| GET    | `/api/v1/scan/{batch_id}`| Get scan progress and status         |

## Pipeline (`/api/v1`, `/pipeline`)

| Method | Path                           | Description                              |
|--------|--------------------------------|------------------------------------------|
| POST   | `/api/v1/extract-metadata`     | Enqueue metadata extraction jobs         |
| POST   | `/api/v1/fingerprint`          | Enqueue fingerprint jobs                 |
| GET    | `/api/v1/fingerprint/progress` | Fingerprint processing progress          |
| POST   | `/api/v1/analyze`              | Enqueue audio analysis jobs              |
| POST   | `/api/v1/proposals/generate`   | Enqueue LLM proposal generation          |
| GET    | `/pipeline/`                   | Pipeline dashboard (HTML)                |
| GET    | `/pipeline/stats`              | Pipeline stats bar (HTMX partial)        |
| POST   | `/pipeline/extract-metadata`   | HTMX trigger for metadata extraction     |
| POST   | `/pipeline/fingerprint`        | HTMX trigger for fingerprinting          |
| POST   | `/pipeline/analyze`            | HTMX trigger for audio analysis          |
| POST   | `/pipeline/proposals`          | HTMX trigger for proposal generation     |

## Pipeline Scans (`/pipeline/scans`)

Admin-UI endpoints that drive the user-initiated scan flow on the pipeline dashboard. Separate from the `pipeline` router (which serves the dashboard page and pipeline-stage triggers).

| Method | Path                           | Description                                        |
|--------|--------------------------------|----------------------------------------------------|
| GET    | `/pipeline/scans/agent-roots`  | Agent scan-root selector (HTMX partial)            |
| POST   | `/pipeline/scans`              | Create a scan batch and dispatch it to an agent    |
| GET    | `/pipeline/scans/{batch_id}`   | Scan-batch progress (HTMX poll partial)            |
| DELETE | `/pipeline/scans/{batch_id}`   | Delete a terminal scan + all associated DB data (HTMX) |

Only **terminal** scans (`completed` / `failed`) are deletable; the delete runs an ordered transactional cascade that removes the `ScanBatch` and every row that hangs off its files (metadata, analysis, fingerprints, proposals + execution log, tracklists → versions → tracks → discogs links, tag-write log, file companions, files), scoped strictly to that batch. A `running` scan or the `live` watcher sentinel returns **409** and is never deleted. On success the endpoint returns the re-rendered Recent Scans table for an HTMX `outerHTML` swap into `#recent-scans`.

## Proposals (`/proposals`)

| Method | Path                          | Description                        |
|--------|-------------------------------|------------------------------------|
| GET    | `/proposals/`                 | List proposals (HTML, filterable)  |
| PATCH  | `/proposals/{id}/approve`     | Approve a proposal                 |
| PATCH  | `/proposals/{id}/reject`      | Reject a proposal                  |
| PATCH  | `/proposals/{id}/undo`        | Revert to pending                  |
| GET    | `/proposals/{id}/detail`      | Expanded detail panel              |
| PATCH  | `/proposals/bulk`             | Bulk approve/reject                |

## Execution (`/execution`, `/audit`)

| Method | Path                              | Description                          |
|--------|-----------------------------------|--------------------------------------|
| POST   | `/execution/start`                | Start batch execution (copy-verify-delete) |
| GET    | `/execution/progress/{batch_id}`  | SSE stream with real-time progress   |
| GET    | `/audit/`                         | Audit log (HTML, filterable)         |

## Duplicates (`/duplicates`)

| Method | Path                          | Description                        |
|--------|-------------------------------|------------------------------------|
| GET    | `/duplicates/`                | List duplicate groups (HTML)       |
| GET    | `/duplicates/{hash}/compare`  | Comparison table for a group       |
| POST   | `/duplicates/{hash}/resolve`  | Mark non-canonical as duplicates   |
| POST   | `/duplicates/{hash}/undo`     | Undo resolution                    |
| POST   | `/duplicates/resolve-all`     | Bulk resolve all groups            |
| POST   | `/duplicates/undo-all`        | Undo bulk resolution               |

## Tracklists (`/tracklists`)

| Method | Path                                    | Description                          |
|--------|-----------------------------------------|--------------------------------------|
| GET    | `/tracklists/`                          | List tracklists (HTML, filterable)   |
| GET    | `/tracklists/scan`                      | Show unscanned files                 |
| POST   | `/tracklists/scan`                      | Trigger fingerprint scan             |
| GET    | `/tracklists/scan/status`               | Scan progress                        |
| GET    | `/tracklists/{id}/tracks`               | View tracks in tracklist             |
| POST   | `/tracklists/{id}/link`                 | Manually link to file                |
| POST   | `/tracklists/{id}/unlink`               | Remove link                          |
| POST   | `/tracklists/{id}/rescrape`             | Re-scrape from 1001Tracklists        |
| POST   | `/tracklists/{id}/approve`              | Approve tracklist                    |
| POST   | `/tracklists/{id}/reject`               | Reject tracklist                     |
| GET    | `/tracklists/{id}/search`               | Search for better match              |
| POST   | `/tracklists/search`                    | Manual tracklist search              |
| POST   | `/tracklists/{id}/reject-low`           | Bulk reject low-confidence tracks    |
| POST   | `/tracklists/{id}/match-discogs`        | Match tracklist to Discogs           |
| POST   | `/tracklists/{id}/bulk-link`            | Bulk link tracks to Discogs          |
| POST   | `/tracklists/{id}/undo-link`            | Undo auto-link                       |
| GET    | `/tracklists/{id}/tracks/{tid}/discogs` | Get Discogs match candidates         |
| POST   | `/tracklists/discogs-links/{id}/accept` | Accept Discogs link                  |
| DELETE | `/tracklists/discogs-links/{id}`        | Dismiss Discogs link                 |
| GET    | `/tracklists/tracks/{id}/edit/{field}`  | Inline edit UI                       |
| PUT    | `/tracklists/tracks/{id}/edit/{field}`  | Save inline edit                     |
| DELETE | `/tracklists/tracks/{id}`               | Delete track                         |

## Tags (`/tags`)

| Method | Path                          | Description                        |
|--------|-------------------------------|------------------------------------|
| GET    | `/tags/`                      | List files with tag metadata (HTML)|
| GET    | `/tags/{file_id}/compare`     | Tag comparison panel               |
| GET    | `/tags/{file_id}/edit/{field}`| Inline edit input                  |
| PUT    | `/tags/{file_id}/edit/{field}`| Save inline edit                   |
| POST   | `/tags/{file_id}/write`       | Execute tag write to file          |

## CUE Sheets (`/cue`)

| Method | Path                          | Description                        |
|--------|-------------------------------|------------------------------------|
| GET    | `/cue/`                       | CUE sheet management page (HTML)   |
| POST   | `/cue/{tracklist_id}/generate`| Generate CUE file for a tracklist  |
| POST   | `/cue/generate-batch`         | Batch generate CUE files           |

## Search (`/search`)

| Method | Path        | Description                              |
|--------|-------------|------------------------------------------|
| GET    | `/search/`  | Global search page (HTML)                |

## Companion Files (`/api/v1`)

| Method | Path                    | Description                              |
|--------|-------------------------|------------------------------------------|
| POST   | `/api/v1/associate`     | Link companion files to media files      |
| GET    | `/api/v1/duplicates`    | List duplicate groups by SHA256          |

## Preview (`/preview`)

| Method | Path        | Description                              |
|--------|-------------|------------------------------------------|
| GET    | `/preview/` | Directory tree of approved proposals     |

## Agents Admin (`/admin/agents`)

Operator-facing liveness page for registered worker agents. Read-only; these endpoints serve HTML and HTMX partials and are not part of the authenticated agent contract below.

| Method | Path                   | Description                                    |
|--------|------------------------|------------------------------------------------|
| GET    | `/admin/agents`        | Agent liveness page (HTML)                     |
| GET    | `/admin/agents/_table` | Agent liveness table (HTMX poll partial, ~5s)  |

## Distributed Agent API (`/api/internal/agent`)

These endpoints form the HTTP contract used by remote worker agents. They back the distributed-execution work added in Phases 26-29 (HTTP-backed agent worker, watcher service, and distributed execution dispatch): a remote agent walks the filesystem, fingerprints and analyzes audio, and reports results back to the central server over this API rather than touching the database directly.

**Authentication:** Every endpoint in this section requires a per-agent bearer token. Send it in the `Authorization` header:

```http
Authorization: Bearer phaze_agent_<32 urlsafe-base64 bytes>
```

The server stores only `sha256(token)` (in `agents.token_hash`) and verifies each request with a single indexed lookup that excludes revoked agents. A missing or malformed header returns `401 Unauthorized` (with `WWW-Authenticate: Bearer`); a well-formed token whose hash is unknown or whose agent row has been revoked returns `403 Forbidden`. The two 403 cases are intentionally indistinguishable. Revocation takes effect on the next request with no server restart.

| Method | Path                                                  | Description                                                                 |
|--------|-------------------------------------------------------|----------------------------------------------------------------------------|
| GET    | `/api/internal/agent/whoami`                          | Agent identity probe (returns the calling agent's identity)                 |
| POST   | `/api/internal/agent/heartbeat`                       | Liveness signal; updates `last_seen_at` and `last_status` (204 No Content)  |
| POST   | `/api/internal/agent/files`                           | Idempotent chunked upsert of discovered file records (auto-enqueues work)   |
| PUT    | `/api/internal/agent/metadata/{file_id}`              | Idempotent tag-metadata write for a file                                    |
| PUT    | `/api/internal/agent/fingerprints/{file_id}/{engine}` | Idempotent fingerprint write keyed on `(file_id, engine)`                   |
| PUT    | `/api/internal/agent/analysis/{file_id}`              | Idempotent audio-analysis upsert for a file                                 |
| POST   | `/api/internal/agent/tracklists`                       | Idempotent atomic create of a tracklist + version + tracks (keyed on `request_id`) |
| PATCH  | `/api/internal/agent/proposals/{proposal_id}/state`   | Joint Proposal + FileRecord state transition in one transaction            |
| POST   | `/api/internal/agent/execution-log`                   | Create an execution-log (audit-trail) row; agent supplies the row `id`      |
| PATCH  | `/api/internal/agent/execution-log/{execution_log_id}`| Update an existing execution-log row                                        |
| POST   | `/api/internal/agent/exec-batches/{batch_id}/progress`| Report a per-proposal terminal-state event for an execution batch           |
| PATCH  | `/api/internal/agent/scan-batches/{batch_id}`         | Advance a scan-batch state-machine (with cross-tenant guard)               |
