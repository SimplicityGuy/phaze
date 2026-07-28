# audfprint Service

FastAPI wrapper around [audfprint](https://github.com/dpwe/audfprint) for landmark-based audio fingerprinting. Used by the Phaze worker to identify and deduplicate audio files by their acoustic content.

## How It Works

Audfprint generates spectral landmark fingerprints from audio files. These fingerprints are stored in a serialized database and can be queried to find matching tracks. This enables deduplication of differently-named but acoustically identical files.

The service wraps the audfprint CLI via subprocess calls, with `asyncio.to_thread` to avoid blocking the event loop. Write operations are serialized via `asyncio.Lock` to prevent concurrent database corruption.

## Build

```bash
docker compose -f docker-compose.agent.yml build audfprint
```

> **Caveat:** `docker-compose.agent.yml` pulls the prebuilt GHCR image (`ghcr.io/simplicityguy/phaze/audfprint`) by default; its `build:` stanza is commented out. To build locally you must uncomment that `build:` block for the `audfprint` service and pass `-f docker-compose.agent.yml` (the root `docker-compose.yml` is application-server-only post-Phase-29 and does not define this service).

The Dockerfile clones the audfprint repository, installs FFmpeg for audio decoding, and sets up the FastAPI wrapper with uv.

## API Endpoints

| Method | Path      | Description                        |
|--------|-----------|------------------------------------|
| GET    | `/health` | Health check                       |
| POST   | `/ingest` | Add file to fingerprint database   |
| POST   | `/query`  | Query database for matching tracks |

### GET /health

Returns service status and engine name. The verdict is based on whether the fingerprint
database can actually be **loaded**, not on whether the file exists — a zero-byte or torn
`fprint.pklz` exists, and reporting it "present" is what kept a total engine outage invisible
for 10 days (see the phaze-p3hj.1 diagnosis). A database that has not been created yet is
healthy; one that cannot be read is a `503`.

```json
{"status": "healthy", "engine": "audfprint", "detail": "database present and loadable (428604 bytes)"}
```

```json
503 {"detail": "database at /data/fprint/fprint.pklz is zero bytes: an interrupted write left no data to load"}
```

The image declares a `HEALTHCHECK` against this endpoint, so an unusable database shows up as
an unhealthy container in `docker ps` rather than only in a log nobody reads.

### POST /ingest

Add an audio file to the fingerprint database. Write operations are serialized to prevent database corruption.

**Request:**
```json
{"file_path": "/data/music/path/to/file.mp3"}
```

**Response:**
```json
{"status": "ingested", "file_path": "/data/music/path/to/file.mp3"}
```

### POST /query

Query the fingerprint database for tracks matching the given audio file. Returns an empty list if the database does not yet exist — nothing has been ingested, so "no matches" is the true answer. A database that exists but cannot be loaded is an **outage**, not a no-match, and returns `503` so the caller records no verdict for the file.

**Request:**
```json
{"file_path": "/data/music/path/to/file.mp3"}
```

**Response:**
```json
{
  "matches": [
    {"track_id": "/data/music/other/track.mp3", "confidence": 87.5}
  ]
}
```

Confidence scores are 0-100, computed from the ratio of matched to total spectral landmark hashes.

## Configuration

| Constant                | Default                         | Description                        |
|-------------------------|---------------------------------|------------------------------------|
| `AUDFPRINT_SCRIPT`      | `/app/audfprint/audfprint.py`   | Path to audfprint CLI              |
| `FPRINT_DB`             | `/data/fprint/fprint.pklz`      | Fingerprint database path          |
| `SUBPROCESS_TIMEOUT`    | `3600`                          | Subprocess timeout (seconds, env-configurable; sized for multi-hour sets) |
| `AUDFPRINT_MEDIA_ROOTS` | *(unset — fails closed)*        | Comma-separated container-side path(s) an incoming `file_path` must resolve under (phaze-1p5q #sec). **No default**: unset or empty rejects EVERY `file_path` with `400`, rather than silently permitting an unconfined path. MUST match whatever this container's own `volumes:` actually mount — `docker-compose.agent.yml` sets it explicitly next to that service's mount declarations. Any OTHER site that launches this image (a CI smoke test, a manual `docker run`, a different compose file) must set it too, or every `/ingest`/`/query` there will 400. |

## Volumes

| Mount         | Mode      | Description                                            |
|---------------|-----------|--------------------------------------------------------|
| `/data/music` | read-only | Shared music volume (same as main app)                 |
| `/data/fprint`| read-write| Persistent fingerprint database (named volume `audfprint_data`) |

## Architecture Notes

- Runs as a separate Docker container on the internal network (not exposed to host)
- Non-root user (`audfprint`) for security
- The Phaze worker communicates with this service via HTTP at `http://audfprint:8001`
- Database is auto-created on first ingest if it does not exist, and rebuilt (loudly, at `ERROR`) if it exists but cannot be loaded
- audfprint uses its native `.pklz` serialized format for fingerprint storage
- **Ingest writes the database atomically.** Upstream's `HashTable.save` rewrites its `--dbase`
  in place with a plain `gzip.open(name, "wb")` + `pickle.dump` — no temp file, no rename — so
  the file is truncated to zero bytes before any output is flushed and any kill in that window
  leaves a permanently unloadable database. The engine is therefore pointed at a same-directory
  staging copy, which is re-probed and then `os.replace`d over the live path. Readers see the
  whole old database or the whole new one; a killed ingest can only destroy the scratch copy
- Subprocess timeout of 3600 seconds per operation (env-configurable via `SUBPROCESS_TIMEOUT`; multi-hour concert sets are the primary content)
- **`file_path` is confined before it becomes argv.** The endpoints are unauthenticated, and
  upstream parses argv with docopt — so any value starting with `-` is taken as an *option*,
  not as `<file>`. `--opfile=` alone is a plain `open(path, "w")`, i.e. one unauthenticated
  request that truncates the fingerprint database. Every `file_path` must therefore be an
  absolute path resolving under `AUDFPRINT_MEDIA_ROOTS` (`400` otherwise, fail-closed when
  unset), and both argv lists carry a `--` end-of-options terminator. Unlike the panako
  sidecar there is no staged-symlink operand: audfprint's decoder shells out with an argv list
  (no shell template to escape), and the operand is persisted verbatim by `hash_table.store`
  as the fingerprint's identity — so it must be the real archive path, not a staged name
