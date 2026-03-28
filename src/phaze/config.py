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

    # Future: LLM API keys
    openai_api_key: SecretStr | None = None


settings = Settings()
