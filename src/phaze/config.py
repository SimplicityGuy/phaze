"""Pydantic settings configuration for Phaze."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    database_url: str = "postgresql+asyncpg://phaze:phaze@postgres:5432/phaze"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Application
    debug: bool = False
    api_host: str = "0.0.0.0"  # noqa: S104  # nosec B104
    api_port: int = 8000

    # File discovery
    scan_path: str = "/data/music"

    # Worker / task queue (per Phase 4 decisions D-01 through D-04)
    worker_max_jobs: int = 8  # D-01: concurrent jobs per worker
    worker_job_timeout: int = 600  # 10 min per file (generous for audio)
    worker_max_retries: int = 4  # D-03: max_tries=4 (1 initial + 3 retries)
    worker_process_pool_size: int = 4  # D-04: CPU-bound worker count
    worker_health_check_interval: int = 60  # arq health check interval in seconds
    worker_keep_result: int = 3600  # keep job results in Redis for 1 hour

    # Future: LLM API keys
    openai_api_key: SecretStr | None = None


settings = Settings()
