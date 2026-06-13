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
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings, NoDecode, SettingsConfigDict


def _direct_env_names(field_name: str, field_info: Any) -> list[str]:
    """Return the env-var names a field accepts directly: its ``validation_alias``
    string choices, plus the bare field name when not already covered.

    The ``<VAR>_FILE`` sibling names are derived from this set so the file-secret
    convention stays consistent with whatever aliases a field already honors.
    """
    alias = field_info.validation_alias
    if isinstance(alias, AliasChoices):
        names = [choice for choice in alias.choices if isinstance(choice, str)]
    elif isinstance(alias, str):
        names = [alias]
    else:
        names = []
    if field_name not in names:
        names.append(field_name)
    return names


def _resolution_env(model_config: SettingsConfigDict) -> dict[str, str]:
    """Build the case-insensitive name->value map used to resolve `_FILE` secrets.

    Mirrors pydantic-settings' own precedence: values from the process environment
    win over values declared in the configured `.env` file(s). Both layers are
    consulted so a `<VAR>_FILE` (or its direct sibling) declared in `.env` — the
    way every other documented var in `.env.example` is consumed — is honored, not
    just process-env vars injected by Docker/Kubernetes.
    """
    merged: dict[str, str] = {}
    env_file = model_config.get("env_file")
    if env_file:
        encoding = model_config.get("env_file_encoding") or "utf-8"
        paths = [env_file] if isinstance(env_file, (str, os.PathLike)) else list(env_file)
        for path in paths:
            if path and Path(path).is_file():
                merged.update({key: value for key, value in dotenv_values(path, encoding=encoding).items() if value is not None})
    merged.update(os.environ)  # process env wins over .env
    return {key.upper(): value for key, value in merged.items()}


class Role(StrEnum):
    """v4.0 role selector. Controller = application server (fileless tasks); Agent = file server (file-bound tasks)."""

    CONTROL = "control"
    AGENT = "agent"


class BaseSettings(PydanticBaseSettings):
    """Fields shared by both roles. Every existing call site `settings.<field>` resolves here unless overridden below."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # v4.0.1: secret-bearing fields that honor the `<VAR>_FILE` convention
    # (Docker/Swarm secrets, Kubernetes mounts, SOPS). Subclasses extend this set;
    # the shared `_resolve_secret_files` before-validator reads each field's
    # `<ALIAS>_FILE` siblings when the direct env var is unset. `database_url` and
    # `redis_url` live here because both carry credentials and exist on both roles.
    SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = frozenset({"database_url", "redis_url", "queue_url"})

    @model_validator(mode="before")
    @classmethod
    def _resolve_secret_files(cls, data: Any) -> Any:
        """Resolve `<VAR>_FILE` secrets before any required-field / production guard.

        For each field in `SECRET_FILE_FIELDS`, if no direct env var (or value from
        another already-merged source) is present but a `<ALIAS>_FILE` sibling is
        set, read the secret from that path. The file's surrounding whitespace is
        stripped (`.strip()`) so a heredoc/echo-created secret with a trailing
        newline hashes identically to an operator-typed env var — critical for
        `PHAZE_AGENT_TOKEN`, whose entire wire string is hashed by `hash_token`.

        Runs as `mode="before"` so the resolved value flows through field
        validation (SecretStr coercion) and into the `mode="after"` guards
        (`_enforce_required_agent_fields`, the production validators). A missing or
        unreadable `<ALIAS>_FILE` path raises `ValueError` (surfaced as a
        `ValidationError`) naming the variable and path — never a silent fallback.

        The `<ALIAS>_FILE` vars are read from the process env and the configured
        `.env` file (they are not model fields, so `extra="ignore"` never sees
        them) and matched case-insensitively to mirror pydantic-settings' default
        env handling; the process env wins over `.env`.
        """
        if not isinstance(data, dict):
            return data

        env_upper = _resolution_env(cls.model_config)
        present_upper = {str(key).upper() for key in data}

        for field_name in cls.SECRET_FILE_FIELDS:
            field_info = cls.model_fields.get(field_name)
            if field_info is None:
                continue

            env_names = _direct_env_names(field_name, field_info)
            # Precedence: an explicitly-set direct env var (or a value already
            # merged from another source into `data`) always wins over `_FILE`.
            if any(name.upper() in present_upper or name.upper() in env_upper for name in env_names):
                continue

            for env_name in env_names:
                file_var = f"{env_name.upper()}_FILE"
                if file_var not in env_upper:
                    continue
                path = env_upper[file_var]
                try:
                    contents = Path(path).read_text(encoding="utf-8")
                except OSError as exc:
                    msg = f"{file_var} points to {path!r} which could not be read: {exc}"
                    raise ValueError(msg) from exc
                # Inject under the field name; every in-scope field is matched
                # either by name (no alias) or by an AliasChoices that includes
                # the bare field name, so this key always resolves.
                data[field_name] = contents.strip()
                break

        return data

    # Database
    # Phase 29 CR-02: bind PHAZE_DATABASE_URL via validation_alias so the operator-
    # facing env-var name documented in .env.example actually overrides the default.
    # Without this, pydantic-settings only accepts the bare `DATABASE_URL` form.
    database_url: str = Field(
        default="postgresql+asyncpg://phaze:phaze@postgres:5432/phaze",
        validation_alias=AliasChoices("PHAZE_DATABASE_URL", "DATABASE_URL", "database_url"),
    )

    # Redis
    # Phase 29 CR-02: bind PHAZE_REDIS_URL via validation_alias so the agent-side
    # `_enforce_redis_password_in_production` validator actually sees operator-supplied
    # credentials. Without the alias the env var is silently ignored and the
    # production agent fails to start with the misleading "requires a password" error.
    redis_url: str = Field(
        default="redis://redis:6379/0",
        validation_alias=AliasChoices("PHAZE_REDIS_URL", "REDIS_URL", "redis_url"),
    )

    # Phase 36: PostgresQueue broker DSN. psycopg3's AsyncConnectionPool needs a RAW
    # libpq DSN (`postgresql://`), NOT the SQLAlchemy dialect form (`postgresql+asyncpg://`)
    # used by `database_url` -- psycopg3 cannot parse the `+driver` suffix. The
    # `_strip_sqlalchemy_driver` validator normalizes either dialect form to libpq so an
    # operator can paste the same DSN they use for `database_url`. Carries DB credentials,
    # so it is a member of SECRET_FILE_FIELDS (T-36-02); never log the full value.
    queue_url: str = Field(
        default="postgresql://phaze:phaze@postgres:5432/phaze",
        validation_alias=AliasChoices("PHAZE_QUEUE_URL", "queue_url"),
        description="psycopg3 (libpq) DSN for the PostgresQueue broker (Phase 36).",
    )

    @field_validator("queue_url", mode="before")
    @classmethod
    def _strip_sqlalchemy_driver(cls, value: Any) -> Any:
        """Normalize a SQLAlchemy dialect DSN to a raw libpq DSN for psycopg3 (T-36-05).

        psycopg3's ``AsyncConnectionPool`` parses a libpq connection string and rejects
        the ``postgresql+asyncpg://`` / ``postgresql+psycopg://`` dialect forms that
        SQLAlchemy uses. This before-validator rewrites either ``+driver`` prefix to a
        bare ``postgresql://`` so the operator can reuse the same DSN shape they set for
        ``database_url``. Non-string values pass through untouched (let pydantic raise).
        """
        if isinstance(value, str):
            for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
                if value.startswith(prefix):
                    return "postgresql://" + value[len(prefix) :]
        return value

    # Application
    debug: bool = False
    api_host: str = "0.0.0.0"  # noqa: S104  # nosec B104
    api_port: int = 8000

    # File discovery
    scan_path: str = "/data/music"

    # PR4 scan reliability: a RUNNING scan with no progress (no last_progress_at
    # heartbeat) for this many seconds is auto-marked FAILED by the control
    # worker's every-minute reaper cron (reap_stalled_scans). Lives on
    # BaseSettings so both roles parse it, but only the control worker registers
    # and runs the reaper. The UI flips to an amber "stalled?" warning at half
    # this threshold (12h) so the operator sees a warning before the hard reap.
    #
    # Default is 24h (86400s): scan_directory is a long-running BULK job with NO
    # fixed SAQ wall-clock timeout (it enqueues with timeout=0 -> unbounded), so
    # the progress-based stall reaper is the SOLE liveness guard. A single slow
    # chunk -- e.g. SHA-256 hashing a multi-GB concert video on a network mount --
    # can legitimately take many minutes between progress PATCHes; a generous 24h
    # window ensures such a healthy, progressing scan is never falsely reaped.
    # Override via PHAZE_SCAN_STALL_SECONDS / SCAN_STALL_SECONDS.
    scan_stall_seconds: int = Field(
        default=86400,
        validation_alias=AliasChoices("PHAZE_SCAN_STALL_SECONDS", "SCAN_STALL_SECONDS", "scan_stall_seconds"),
        description="Seconds with no progress before a RUNNING scan is reaped as stalled (default 24h).",
    )

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
    agent_token_prefix: str = "phaze_agent_"  # noqa: S105
    agent_file_chunk_max: int = 1000

    # Phase 27 UAT Gap 2: auto-run alembic upgrade head on api startup. Turn off
    # in production environments where the operator wants manual migration
    # control (e.g., to gate behind a maintenance window).
    auto_migrate: bool = Field(
        default=True,
        validation_alias=AliasChoices("PHAZE_AUTO_MIGRATE", "auto_migrate"),
        description="Run `alembic upgrade head` in the api lifespan startup.",
    )

    # Phase 33: mount the SAQ monitoring dashboard at /saq in the api lifespan.
    # Default-on so the dashboard appears with no operator action; the api
    # process is the only role that acts on it (the worker parses but ignores).
    # Set PHAZE_ENABLE_SAQ_UI=false to disable the mount with zero code change.
    enable_saq_ui: bool = Field(
        default=True,
        validation_alias=AliasChoices("PHAZE_ENABLE_SAQ_UI", "enable_saq_ui"),
        description="Mount the SAQ monitoring dashboard at /saq in the API (Phase 33).",
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

    # PR3 observability: central structlog knobs. Live on BaseSettings so both
    # ControlSettings and AgentSettings inherit them. Entry points pass these
    # through to phaze.logging_config.configure_logging(level=..., json_logs=...).
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("PHAZE_LOG_LEVEL", "log_level"),
        description="Root log level: DEBUG | INFO | WARNING | ERROR (default INFO).",
    )
    log_json: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_LOG_JSON", "log_json"),
        description="True=JSON, False=console, None=auto (JSON when stdout is not a TTY).",
    )


class ControlSettings(BaseSettings):
    """Application-server role: LLM proposal generation, Discogs matching, fileless tasks."""

    # v4.0.1: add the LLM API keys to the inherited database_url/redis_url set.
    SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = BaseSettings.SECRET_FILE_FIELDS | {"openai_api_key", "anthropic_api_key"}

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

    # v4.0.1: add the bearer token to the inherited database_url/redis_url set.
    SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = BaseSettings.SECRET_FILE_FIELDS | {"agent_token"}

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

    # Phase 31: windowed time-series audio analysis. The agent worker reads these
    # to size the per-window decode loop in services/analysis.py::analyze_file.
    analysis_fine_window_sec: int = Field(
        default=30,
        validation_alias=AliasChoices("PHAZE_ANALYSIS_FINE_WINDOW_SEC", "analysis_fine_window_sec"),
        description="Fine-tier (BPM/key) window length in seconds for windowed analysis (Phase 31).",
    )
    analysis_coarse_window_sec: int = Field(
        default=180,
        validation_alias=AliasChoices("PHAZE_ANALYSIS_COARSE_WINDOW_SEC", "analysis_coarse_window_sec"),
        description="Coarse-tier (mood/style/danceability) window length in seconds for windowed analysis (Phase 31).",
    )
    analysis_fine_min_sec: int = Field(
        default=15,
        validation_alias=AliasChoices("PHAZE_ANALYSIS_FINE_MIN_SEC", "analysis_fine_min_sec"),
        description="Minimum audio length for a trailing FINE window; shorter trailing windows are dropped except window 0 (Phase 31).",
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
    def _enforce_https_in_production(self) -> "AgentSettings":
        """Phase 29 CR-01: production refuses non-HTTPS agent_api_url.

        Agent → app-server traffic carries the bearer token in plaintext if the
        URL scheme is `http://`. `.env.example.agent` documents this guard but
        the original Plan 02 only landed the Redis-password validator. Without
        the HTTPS guard a misconfigured production agent silently posts the
        bearer in cleartext on the LAN.
        """
        if self.agent_env == "production" and not self.agent_api_url.lower().startswith("https://"):
            raise ValueError("agent_env=production requires https:// for agent_api_url (Phase 29 CR-01)")
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
