# Panako Service

FastAPI wrapper around [Panako](https://github.com/JorenSix/Panako) for tempo-robust audio fingerprinting. Used by the Phaze worker alongside audfprint for multi-engine fingerprint matching and deduplication.

## How It Works

Panako uses a tempo-robust fingerprinting algorithm that can identify audio even when playback speed has changed. It stores fingerprints in LMDB (Lightning Memory-Mapped Database) and returns match percentages for query results. The combination of Panako (tempo-robust) and audfprint (landmark-based) provides more robust identification than either engine alone.

The service wraps the Panako Java CLI via subprocess calls, with `asyncio.to_thread` to avoid blocking the event loop.

## Build

```bash
docker compose build panako
```

The Dockerfile uses a multi-stage build:
1. **Stage 1 (JDK):** Clones the Panako repository and builds the shadow JAR with Gradle
2. **Stage 2 (Runtime):** Python 3.13-slim with JRE-only runtime, FFmpeg, and the FastAPI wrapper

## API Endpoints

| Method | Path      | Description                        |
|--------|-----------|------------------------------------|
| GET    | `/health` | Health check                       |
| POST   | `/ingest` | Add file to fingerprint database   |
| POST   | `/query`  | Query database for matching tracks |

### GET /health

Returns service status and engine name.

```json
{"status": "healthy", "engine": "panako"}
```

### POST /ingest

Add an audio file to the Panako fingerprint database.

**Request:**
```json
{"file_path": "/data/music/path/to/file.mp3"}
```

**Response:**
```json
{"status": "ingested", "file_path": "/data/music/path/to/file.mp3"}
```

### POST /query

Query the fingerprint database for tracks matching the given audio file.

**Request:**
```json
{"file_path": "/data/music/path/to/file.mp3"}
```

**Response:**
```json
{
  "matches": [
    {"track_id": "/data/music/other/track.mp3", "confidence": 92.3}
  ]
}
```

Confidence scores are 0-100, derived from Panako's match percentage. The output is parsed from semicolon-separated fields including match path, score, time factor, frequency factor, and match percentage.

## Configuration

| Constant             | Default           | Description                        |
|----------------------|-------------------|------------------------------------|
| `PANAKO_JAR`         | `/app/panako.jar` | Path to Panako shadow JAR          |
| `SUBPROCESS_TIMEOUT` | `120`             | Subprocess timeout (seconds)       |

## Volumes

| Mount         | Mode      | Description                                            |
|---------------|-----------|--------------------------------------------------------|
| `/data/music` | read-only | Shared music volume (same as main app)                 |
| `/data/fprint`| read-write| Persistent fingerprint database via LMDB (named volume `panako_data`) |

## Architecture Notes

- Runs as a separate Docker container on the internal network (not exposed to host)
- Non-root user (`panako`) for security
- Multi-stage Docker build: JDK 17 for Gradle build, then JRE 21 runtime to minimize image size
- Panako uses LMDB for storage, which requires `HOME=/data/fprint` for writable access
- The Phaze worker communicates with this service via HTTP at `http://panako:8002`
- Subprocess timeout of 120 seconds per operation
