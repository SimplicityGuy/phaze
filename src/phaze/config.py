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

    # Audio analysis models (per Phase 5)
    models_path: str = "/models"

    # Worker / task queue (per Phase 4 decisions D-01 through D-04)
    worker_max_jobs: int = 8  # D-01: concurrent jobs per worker
    worker_job_timeout: int = 600  # 10 min per file (generous for audio)
    worker_max_retries: int = 4  # D-03: max_tries=4 (1 initial + 3 retries)
    worker_process_pool_size: int = 4  # D-04: CPU-bound worker count
    worker_health_check_interval: int = 60  # arq health check interval in seconds
    worker_keep_result: int = 3600  # keep job results in Redis for 1 hour

    # LLM API keys
    openai_api_key: SecretStr | None = None

    # LLM configuration (Phase 6 -- per D-17, D-18, D-19)
    llm_model: str = "claude-sonnet-4-20250514"
    anthropic_api_key: SecretStr | None = None
    llm_max_rpm: int = 30  # D-19: max LLM requests per minute
    llm_batch_size: int = 10  # D-15: files per LLM call (research recommends 10)
    llm_max_companion_chars: int = 3000  # Max chars per companion file content


settings = Settings()
