# Panako Service

FastAPI wrapper around [Panako](https://github.com/JorenSix/Panako) for tempo-robust audio fingerprinting.

## Build

```bash
docker compose build panako
```

## API Endpoints

| Method | Path      | Description                        |
|--------|-----------|------------------------------------|
| GET    | `/health` | Health check                       |
| POST   | `/ingest` | Add file to fingerprint database   |
| POST   | `/query`  | Query database for matching tracks |

### POST /ingest

```json
{"file_path": "/data/music/path/to/file.mp3"}
```

### POST /query

```json
{"file_path": "/data/music/path/to/file.mp3"}
```

Returns matches with 0-100 confidence scores using match percentage.

## Volumes

- `/data/music` (read-only) -- shared music volume
- `/data/fprint` -- persistent fingerprint database via LMDB (named volume `panako_data`)

## Notes

- Multi-stage Docker build: JDK for Gradle build, then JRE-only runtime.
- Panako uses LMDB for storage, which requires `HOME=/data/fprint` for writable access.
- Subprocess calls use `asyncio.to_thread` to avoid blocking the event loop.
- Internal network only -- not exposed to host.
