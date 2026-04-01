# audfprint Service

FastAPI wrapper around [audfprint](https://github.com/dpwe/audfprint) for landmark-based audio fingerprinting.

## Build

```bash
docker compose build audfprint
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

Returns matches with 0-100 confidence scores.

## Volumes

- `/data/music` (read-only) -- shared music volume
- `/data/fprint` -- persistent fingerprint database (named volume `audfprint_data`)

## Notes

- Write operations are serialized via `asyncio.Lock` to prevent database corruption.
- audfprint uses a serialized database format (`.pklz`) which is its native storage.
- Subprocess calls use `asyncio.to_thread` to avoid blocking the event loop.
- Internal network only -- not exposed to host.
