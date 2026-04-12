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
