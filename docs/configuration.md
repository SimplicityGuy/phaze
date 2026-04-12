# Configuration

All configuration is via environment variables (or `.env` file). See [`.env.example`](../.env.example) for defaults.

## Core Settings

| Variable              | Default                                          | Description                        |
|-----------------------|--------------------------------------------------|------------------------------------|
| `DATABASE_URL`        | `postgresql+asyncpg://phaze:phaze@postgres:5432/phaze` | PostgreSQL connection string |
| `REDIS_URL`           | `redis://redis:6379/0`                           | Redis connection string            |
| `SCAN_PATH`           | `/data/music`                                    | Music directory (mounted read-only)|
| `OUTPUT_PATH`         | `/data/output`                                   | Destination for executed moves     |
| `MODELS_PATH`         | `./models`                                       | Essentia ML model directory        |
| `DEBUG`               | `false`                                          | Enable debug mode                  |
| `API_HOST`            | `0.0.0.0`                                        | API server bind address            |
| `API_PORT`            | `8000`                                           | API server port                    |

## Worker Settings

| Variable                      | Default | Description                          |
|-------------------------------|---------|--------------------------------------|
| `WORKER_MAX_JOBS`             | `8`     | Concurrent SAQ jobs per worker       |
| `WORKER_JOB_TIMEOUT`          | `600`   | Per-file timeout (seconds)           |
| `WORKER_MAX_RETRIES`          | `4`     | Max retry attempts (1 initial + 3)   |
| `WORKER_PROCESS_POOL_SIZE`    | `4`     | CPU-bound worker pool size           |
| `WORKER_HEALTH_CHECK_INTERVAL`| `60`    | SAQ health check interval (seconds)  |

## LLM Settings

| Variable                  | Default                      | Description                          |
|---------------------------|------------------------------|--------------------------------------|
| `LLM_MODEL`              | `claude-sonnet-4-20250514`       | LLM model for proposals             |
| `ANTHROPIC_API_KEY`      | --                           | Anthropic API key                    |
| `OPENAI_API_KEY`         | --                           | OpenAI API key (alternative)         |
| `LLM_MAX_RPM`            | `30`                         | Max LLM requests per minute          |
| `LLM_BATCH_SIZE`         | `10`                         | Files per LLM call                   |
| `LLM_MAX_COMPANION_CHARS`| `3000`                       | Max chars per companion file content |

## Fingerprint Settings

| Variable         | Default                   | Description                 |
|------------------|---------------------------|-----------------------------|
| `AUDFPRINT_URL`  | `http://audfprint:8001`   | Audfprint service endpoint  |
| `PANAKO_URL`     | `http://panako:8002`      | Panako service endpoint     |

## Discogs Settings

| Variable                  | Default                          | Description                     |
|---------------------------|----------------------------------|---------------------------------|
| `DISCOGSOGRAPHY_URL`      | `http://discogsography:8000`     | Discogsography service endpoint |
| `DISCOGS_MATCH_CONCURRENCY`| `5`                             | Concurrent Discogs match tasks  |
