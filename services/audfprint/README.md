# audfprint Service

FastAPI wrapper around [audfprint](https://github.com/dpwe/audfprint) for landmark-based audio fingerprinting. Used by the Phaze worker to identify and deduplicate audio files by their acoustic content.

## How It Works

Audfprint generates spectral landmark fingerprints from audio files. These fingerprints are stored in a serialized database and can be queried to find matching tracks. This enables deduplication of differently-named but acoustically identical files.

The service wraps the audfprint CLI via subprocess calls, with `asyncio.to_thread` to avoid blocking the event loop. Write operations are serialized via `asyncio.Lock` to prevent concurrent database corruption.

## Build

```bash
docker compose build audfprint
```

The Dockerfile clones the audfprint repository, installs FFmpeg for audio decoding, and sets up the FastAPI wrapper with uv.

## API Endpoints

| Method | Path      | Description                        |
|--------|-----------|------------------------------------|
| GET    | `/health` | Health check                       |
| POST   | `/ingest` | Add file to fingerprint database   |
| POST   | `/query`  | Query database for matching tracks |

### GET /health

Returns service status and engine name.

```json
{"status": "healthy", "engine": "audfprint"}
```

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

Query the fingerprint database for tracks matching the given audio file. Returns an empty list if the database does not yet exist.

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

| Constant             | Default                         | Description                        |
|----------------------|---------------------------------|------------------------------------|
| `AUDFPRINT_SCRIPT`   | `/app/audfprint/audfprint.py`   | Path to audfprint CLI              |
| `FPRINT_DB`          | `/data/fprint/fprint.pklz`      | Fingerprint database path          |
| `SUBPROCESS_TIMEOUT` | `120`                           | Subprocess timeout (seconds)       |

## Volumes

| Mount         | Mode      | Description                                            |
|---------------|-----------|--------------------------------------------------------|
| `/data/music` | read-only | Shared music volume (same as main app)                 |
| `/data/fprint`| read-write| Persistent fingerprint database (named volume `audfprint_data`) |

## Architecture Notes

- Runs as a separate Docker container on the internal network (not exposed to host)
- Non-root user (`audfprint`) for security
- The Phaze worker communicates with this service via HTTP at `http://audfprint:8001`
- Database is auto-created on first ingest if it does not exist
- audfprint uses its native `.pklz` serialized format for fingerprint storage
- Subprocess timeout of 120 seconds per operation
