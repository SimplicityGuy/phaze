"""Pydantic settings configuration for Phaze.

Phase 26 D-14: settings split into a Base class + two role-specific subclasses
(ControlSettings, AgentSettings) selected at process boot via the `PHAZE_ROLE`
env var. `get_settings()` is the single dispatch point; module-level
`settings = get_settings()` is preserved for back-compat with existing
`from phaze.config import settings` call sites.
"""

from enum import StrEnum
from functools import lru_cache
import os
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings, NoDecode, SettingsConfigDict


class Role(StrEnum):
    """v4.0 role selector. Controller = application server (fileless tasks); Agent = file server (file-bound tasks)."""

    CONTROL = "control"
    AGENT = "agent"


class BaseSettings(PydanticBaseSettings):
    """Fields shared by both roles. Every existing call site `settings.<field>` resolves here unless overridden below."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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

    # Audio analysis models
    models_path: str = "/models"

    # File execution output
    output_path: str = "/data/output"

    # Worker / task queue
    worker_max_jobs: int = 8
    worker_job_timeout: int = 600
    worker_max_retries: int = 4
    worker_process_pool_size: int = 4
    worker_health_check_interval: int = 60
    worker_keep_result: int = 3600

    # Fingerprint service URLs (Docker service names)
    audfprint_url: str = "http://audfprint:8001"
    panako_url: str = "http://panako:8002"

    @field_validator("audfprint_url", "panako_url")
    @classmethod
    def _enforce_localhost_only(cls, value: str) -> str:
        """Phase 28 D-12 / TASK-04: fingerprint sidecars MUST be local to the file server.

        Per XAGENT-01 (deferred): cross-file-server fingerprint matching is not
        supported in v4.0. Each file server's audfprint+panako indices contain
        only that file server's files. Reject any URL whose host isn't
        127.0.0.1 / localhost / a Docker-compose service name on the agent's
        private network. The Docker-compose defaults (`http://audfprint:8001`,
        `http://panako:8002`) are accepted because they resolve via the agent
        container's compose network — never cross-host.

        Lives on `BaseSettings` so both `ControlSettings` and `AgentSettings`
        inherit the guard at construction time.
        """
        parsed = urlparse(value)
        allowed_hosts = {"localhost", "127.0.0.1", "audfprint", "panako"}
        if parsed.hostname not in allowed_hosts:
            msg = (
                f"audfprint_url/panako_url must point to a host on the agent's "
                f"local Compose network (got host={parsed.hostname!r}; allowed="
                f"{sorted(allowed_hosts)}). Cross-file-server fingerprint matching "
                f"is not supported in v4.0 -- see XAGENT-01."
            )
            raise ValueError(msg)
        return value

    # Discogsography service URL (shared base; concurrency-tunable on Control)
    discogsography_url: str = "http://discogsography:8000"

    # Internal agent API (Phase 25)
    agent_token_prefix: str = "phaze_agent_"  # noqa: S105  # nosec B105
    agent_file_chunk_max: int = 1000

    # Phase 27 UAT Gap 2: auto-run alembic upgrade head on api startup. Turn off
    # in production environments where the operator wants manual migration
    # control (e.g., to gate behind a maintenance window).
    auto_migrate: bool = Field(
        default=True,
        validation_alias=AliasChoices("PHAZE_AUTO_MIGRATE", "auto_migrate"),
        description="Run `alembic upgrade head` in the api lifespan startup.",
    )

    # Phase 29 D-02: SAN list baked into the auto-generated leaf cert at api
    # entrypoint. Default covers single-host dev (`localhost`, `127.0.0.1`)
    # and the docker-compose service-name DNS (`api`) so agents on the same
    # network can verify a TLS handshake to `https://api:8000`.
    api_tls_sans: str = Field(
        default="localhost,127.0.0.1,api",
        validation_alias=AliasChoices("PHAZE_API_TLS_SANS", "api_tls_sans"),
        description="Comma-separated SAN list for the auto-generated leaf cert (Phase 29 D-02).",
    )

    # Phase 27 UAT Gap 3: seed a dev agent on a fresh DB so the watcher can
    # authenticate on first start. Disabled by default in production; the
    # operator-supplied token (if set) overrides the random one printed at
    # startup so the same token can be baked into the watcher's .env.
    dev_seed_agent: bool = Field(
        default=False,
        validation_alias=AliasChoices("PHAZE_DEV_SEED_AGENT", "dev_seed_agent"),
        description="On a fresh DB, seed a dev-agent so the watcher can authenticate.",
    )
    dev_agent_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_DEV_AGENT_TOKEN", "dev_agent_token"),
        description="Optional fixed token for the dev-seeded agent (else random).",
    )


class ControlSettings(BaseSettings):
    """Application-server role: LLM proposal generation, Discogs matching, fileless tasks."""

    # Discogsography
    discogs_match_concurrency: int = 5

    # LLM API keys + config (Phase 6)
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-4-20250514"
    llm_max_rpm: int = 30
    llm_batch_size: int = 10
    llm_max_companion_chars: int = 3000


class AgentSettings(BaseSettings):
    """File-server role: HTTP client to the application server, file-bound SAQ tasks.

    Per D-14: `agent_api_url`, `agent_token`, and `scan_roots` are required when
    `PHAZE_ROLE=agent`. The validator raises ValueError at construction time if
    any is missing/empty so the agent worker fails fast with a clear error rather
    than silently producing 401s or path-traversal rejections at runtime.

    Env var names use the documented `PHAZE_AGENT_*` / `PHAZE_AGENT_SCAN_ROOTS`
    naming via `validation_alias=AliasChoices(...)` per field. The bare field
    names (e.g., `AGENT_API_URL`) are also accepted for in-process / pytest
    monkeypatch convenience.
    """

    agent_api_url: str = Field(
        default="",
        validation_alias=AliasChoices("PHAZE_AGENT_API_URL", "agent_api_url"),
    )
    agent_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("PHAZE_AGENT_TOKEN", "agent_token"),
    )
    # Phase 29 D-06: deployment-mode selector. `production` triggers the
    # `_enforce_redis_password_in_production` model_validator below, which refuses
    # passwordless `redis_url` so a misconfigured production agent fails fast at
    # startup rather than connecting to an unsecured Redis. `dev` (the default)
    # preserves Pitfall 7: fresh clones must `docker compose up` without
    # supplying a Redis password.
    agent_env: Literal["dev", "production"] = Field(
        default="dev",
        validation_alias=AliasChoices("PHAZE_AGENT_ENV", "agent_env"),
        description="Deployment mode. Production refuses passwordless Redis URLs (Phase 29 D-06).",
    )
    scan_roots: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices("PHAZE_AGENT_SCAN_ROOTS", "scan_roots"),
        description=(
            "Absolute filesystem paths the agent is permitted to read/write. "
            "Used by execute_approved_batch (Plan 11) for path-traversal containment. "
            "Set via env var PHAZE_AGENT_SCAN_ROOTS as a comma-separated list "
            "(e.g., PHAZE_AGENT_SCAN_ROOTS=/data/music,/data/concerts). "
            "`NoDecode` + `_split_scan_roots` (below) implements the comma-split — "
            "pydantic-settings would otherwise try to JSON-decode the env value."
        ),
    )

    watcher_settle_seconds: int = Field(
        default=10,
        validation_alias=AliasChoices("PHAZE_WATCHER_SETTLE_SECONDS", "watcher_settle_seconds"),
        description="Seconds a file's mtime must be stable before the watcher posts it (D-01).",
    )
    watcher_max_pending_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices("PHAZE_WATCHER_MAX_PENDING_SECONDS", "watcher_max_pending_seconds"),
        description="Stuck-file cap; entries older than this are evicted from the pending set (D-02).",
    )
    watcher_sweep_interval_seconds: int = Field(
        default=2,
        validation_alias=AliasChoices("PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS", "watcher_sweep_interval_seconds"),
        description="How often the watcher's sweep task checks for settled files (D-01).",
    )
    watcher_polling_mode: bool = Field(
        default=False,
        validation_alias=AliasChoices("PHAZE_WATCHER_POLLING_MODE", "watcher_polling_mode"),
        description=(
            "Use watchdog's PollingObserver instead of the native inotify backend. "
            "Required for macOS docker bind mounts (rancher-desktop / Docker Desktop) "
            "where inotify events do not propagate through 9p/virtiofs. Adds modest CPU "
            "overhead (polls each watcher_sweep_interval_seconds) but works on any filesystem."
        ),
    )
    scan_chunk_size: int = Field(
        default=500,
        validation_alias=AliasChoices("PHAZE_SCAN_CHUNK_SIZE", "scan_chunk_size"),
        description="Number of FileUpsertRecord rows per chunk in scan_directory (D-11).",
    )

    # Phase 29 D-03: path to the operator-distributed CA cert that the agent's
    # httpx.AsyncClient uses to verify the application-server TLS endpoint.
    # Default `/certs/phaze-ca.crt` matches the bind-mount path inside agent
    # containers (docker-compose.agent.yml). `construct_agent_client` raises
    # RuntimeError at construction time if the file is missing or empty so
    # misconfiguration surfaces fast.
    agent_ca_file: str = Field(
        default="/certs/phaze-ca.crt",
        validation_alias=AliasChoices("PHAZE_AGENT_CA_FILE", "agent_ca_file"),
        description="Path to the operator-distributed CA cert for verifying the app-server TLS endpoint (Phase 29 D-03).",
    )

    @field_validator("scan_roots", mode="before")
    @classmethod
    def _split_scan_roots(cls, value: object) -> object:
        """Comma-split `PHAZE_AGENT_SCAN_ROOTS` env input into a list[str].

        pydantic-settings does NOT natively comma-split list[str] from env vars
        (it expects JSON by default). This validator accepts a single string and
        splits on commas, while leaving native list inputs (e.g., from
        `AgentSettings(scan_roots=["/a"])`) untouched.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _enforce_required_agent_fields(self) -> "AgentSettings":
        if not self.agent_api_url:
            raise ValueError("PHAZE_AGENT_API_URL is required when PHAZE_ROLE=agent")
        if not self.agent_token.get_secret_value():
            raise ValueError("PHAZE_AGENT_TOKEN is required when PHAZE_ROLE=agent")
        if not self.scan_roots:
            raise ValueError("AgentSettings.scan_roots is required when PHAZE_ROLE=agent (set PHAZE_AGENT_SCAN_ROOTS=/path1,/path2)")
        return self

    @model_validator(mode="after")
    def _enforce_redis_password_in_production(self) -> "AgentSettings":
        """D-06: production refuses passwordless redis_url.

        Phase 29 AUTH-03 pairs this client-side guard with the server-side
        `requirepass` + LAN-bound port hardening landing in Plan 03. Together
        they ensure a misconfigured production agent fails fast at startup
        rather than silently connecting to an unsecured Redis. `dev` (default)
        permits passwordless URLs so `docker compose up` works on a fresh clone
        without any extra env-var ceremony (RESEARCH §Pitfall 7).

        `urlparse` resolves URL-encoded passwords correctly; a truly malformed
        URL falls through to a SAQ connection failure at queue construction time.
        """
        if self.agent_env == "production":
            parsed = urlparse(self.redis_url)
            if not parsed.password:
                raise ValueError("agent_env=production requires a password in redis_url (Phase 29 D-06)")
        return self


@lru_cache(maxsize=1)
def get_settings() -> BaseSettings:
    """Return the role-specific settings instance for this process.

    Reads `PHAZE_ROLE` from the env once (default: "control") and dispatches to the
    matching subclass. The instance is cached via `lru_cache` so the singleton is
    constructed exactly once per process.
    """
    role = os.environ.get("PHAZE_ROLE", "control")
    if role == Role.AGENT.value:
        return AgentSettings()
    return ControlSettings()


def _build_default_settings() -> ControlSettings:
    """Construct the module-level singleton. Splits out from `get_settings()` so the
    module-level type checks as `ControlSettings` — every existing call site reads
    `settings.llm_*` / `settings.discogs_match_concurrency`, which live on
    `ControlSettings`. When `PHAZE_ROLE=agent`, the agent worker should call
    `get_settings()` explicitly (or import `AgentSettings`) rather than reading the
    module-level singleton.
    """
    role = os.environ.get("PHAZE_ROLE", "control")
    if role == Role.AGENT.value:
        # Agent worker entry points should call get_settings() / AgentSettings()
        # directly. The module-level singleton stays Control-typed; the worker's
        # startup hook (Plan 10) will pull the AgentSettings instance via
        # get_settings() and stash it at ctx["agent_settings"].
        return ControlSettings()
    return ControlSettings()


# Module-level singleton preserves back-compat with `from phaze.config import settings`.
# 37+ existing call sites rely on this -- do NOT remove without grep'ing every caller.
# Typed as ControlSettings because the legacy `Settings` class was effectively the
# Control superset; the agent worker uses `get_settings()` / `AgentSettings()` directly.
settings: ControlSettings = _build_default_settings()

# Back-compat alias: the pre-Phase-26 class name was `Settings`. Some test files
# import the class directly (e.g., `from phaze.config import Settings`). Until those
# call sites migrate to `ControlSettings` / `AgentSettings` / `get_settings()`, the
# alias keeps them working — `Settings` resolves to `ControlSettings` (the superset
# that contains every field the old monolithic class had).
Settings = ControlSettings
