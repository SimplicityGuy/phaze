"""Pydantic settings configuration for Phaze.

Phase 26 D-14: settings split into a Base class + two role-specific subclasses
(ControlSettings, AgentSettings) selected at process boot via the `PHAZE_ROLE`
env var. `get_settings()` is the single dispatch point; module-level
`settings = get_settings()` is preserved for back-compat with existing
`from phaze.config import settings` call sites.
"""

from collections import Counter
from enum import StrEnum
from functools import lru_cache
import os
from pathlib import Path
import tomllib
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings, NoDecode, SettingsConfigDict
import structlog

from phaze.config_backends import (
    BackendConfig,
    BucketConfig,
    ComputeBackend,
    KueueBackend,
    _default_local_registry,
    _read_secret_file,
)


logger = structlog.get_logger(__name__)


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

    # WR-01: secret fields whose file contents must be preserved VERBATIM (NOT ``.strip()``-ed).
    # Every other `<VAR>_FILE` secret is stripped so a heredoc/echo trailing newline hashes/parses
    # identically to an operator-typed env var -- but key material (an OpenSSH private key, a
    # known_hosts file) REQUIRES its trailing newline: OpenSSH's parser rejects a key without a
    # final newline ("invalid format" / "error in libcrypto"), so stripping it broke every push that
    # provisioned its key via PHAZE_PUSH_SSH_KEY_FILE. Subclasses extend this set; the shared
    # `_resolve_secret_files` validator consults it to decide strip-vs-verbatim per field.
    SECRET_FILE_PRESERVE_WHITESPACE: ClassVar[frozenset[str]] = frozenset()

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
                # Inject under the field name; every in-scope field is matched
                # either by name (no alias) or by an AliasChoices that includes
                # the bare field name, so this key always resolves. The shared
                # `_read_secret_file` helper (config_backends) applies the single
                # strip-vs-verbatim rule both this env-`_FILE` path and the inline
                # TOML `*_file` reader adopt (D-06: one rule, two call sites). Key
                # material (SECRET_FILE_PRESERVE_WHITESPACE) is kept verbatim so its
                # required trailing newline survives (WR-01); everything else is stripped.
                try:
                    data[field_name] = _read_secret_file(path, preserve_whitespace=field_name in cls.SECRET_FILE_PRESERVE_WHITESPACE)
                except ValueError as exc:
                    # Re-raise with the `<VAR>_FILE` name so the operator-facing message
                    # still names the variable that pointed at the unreadable path.
                    msg = f"{file_var} points to {path!r} which could not be read: {exc}"
                    raise ValueError(msg) from exc
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
    # Per-lane agent-worker concurrency knobs (quick-260707-dh1). Only the agent lane
    # worker reads these (via PHAZE_AGENT_LANE); they live on BaseSettings so both roles
    # parse cleanly. CPU-bound lanes (analyze 4 + fingerprint 2) sum to 6 of nox's 8 cores,
    # leaving headroom for the fast meta lane + sidecars + OS; io is network-bound (off CPU).
    lane_analyze_concurrency: int = Field(
        default=4,
        validation_alias=AliasChoices("PHAZE_LANE_ANALYZE_CONCURRENCY", "lane_analyze_concurrency"),
        description="Concurrency of the analyze lane worker (process_file; in-process essentia, CPU-bound).",
    )
    lane_fingerprint_concurrency: int = Field(
        default=2,
        validation_alias=AliasChoices("PHAZE_LANE_FINGERPRINT_CONCURRENCY", "lane_fingerprint_concurrency"),
        description="Concurrency of the fingerprint lane worker (fingerprint_file; via panako/audfprint, CPU-bound).",
    )
    lane_meta_concurrency: int = Field(
        default=2,
        validation_alias=AliasChoices("PHAZE_LANE_META_CONCURRENCY", "lane_meta_concurrency"),
        description="Concurrency of the meta lane worker (extract/scan/execute; light/fast).",
    )
    lane_io_concurrency: int = Field(
        default=4,
        validation_alias=AliasChoices("PHAZE_LANE_IO_CONCURRENCY", "lane_io_concurrency"),
        description="Concurrency of the io lane worker (s3_upload/push_file; network-bound, off CPU budget).",
    )
    # phaze-30fo: EVERY lane worker heartbeats, each beat tagged with its own lane. Compose sets
    # this true on all four lane workers; last_seen_at is inherently max(last_seen) across them and
    # the control plane keeps a per-lane depth breakdown. This REPLACES the former quick-260707-dh1
    # convention (true on exactly the analyze worker, false on the other three): that pinned the
    # agent's whole liveness signal -- and its work-routing rank, via select_active_agent's
    # ORDER BY last_seen_at DESC -- to ONE process, so a stalled analyze worker marked the agent
    # DEAD and cost it routing while its other three lanes were busy. Only the transitional
    # all-mode worker-drain sets this false: it is unlaned, and an untagged beat would wipe the
    # per-lane breakdown. All-mode (no lane) still defaults to True.
    agent_heartbeat_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("PHAZE_AGENT_HEARTBEAT", "agent_heartbeat_enabled"),
        description="Whether this agent worker launches the liveness heartbeat background task (every lane worker does; unlaned drain workers opt out).",
    )
    worker_job_timeout: int = 600
    worker_max_retries: int = 4
    worker_process_pool_size: int = 4
    worker_health_check_interval: int = 60
    worker_keep_result: int = 3600
    # phaze-e57w: age bound (seconds since the job's frozen `started`) past which a SAQ row
    # stuck in status='aborting' is reaped -- deleted so its deterministic key
    # (fingerprint_file:<file_id>) is released and the file becomes re-queueable. A legitimate
    # abort completes in seconds (the worker calls finish(ABORTED)), so a row still 'aborting'
    # this far past `started` is a zombie the owning worker will never finalize. The bound is on
    # `started` (frozen), NOT `touched` -- SAQ's sweeper bumps `touched` on every abort->ABORTING
    # pass (Queue.update), so a touched-based bound would never trigger (spike phaze-qmc2.1).
    # Default 900 == worker_job_timeout(600) + 300s slack; MUST exceed worker_job_timeout.
    aborting_reap_seconds: int = Field(
        default=900,
        validation_alias=AliasChoices("PHAZE_ABORTING_REAP_SECONDS", "aborting_reap_seconds"),
        description="Seconds past a job's frozen `started` before a row stuck in status='aborting' is reaped (deleted, releasing its deterministic key). Default 900 = 600s timeout + 300s slack.",
    )

    # DB connection footprint / pool hygiene (quick-260707-ryn). These live on BaseSettings
    # so BOTH the api engine (via the module-level `settings` singleton, database.py) AND the
    # control worker task_engine (via get_settings(), tasks/controller.py) source their pool
    # kwargs from one operator-tunable place; the two dispatch knobs size the control-side
    # per-(agent,lane) dispatch queues in services/agent_task_router.py.
    #
    # INCIDENT: phaze reaches Postgres through PgBouncer in SESSION mode, where every
    # app->pooler client connection PINS one upstream server connection for its whole lifetime.
    # The shared (phaze,phaze) session pool (cap ~55) deadlocked under normal multi-worker load
    # and /health hung behind the exhausted pool. These reduced + hygienic defaults cut phaze's
    # server-connection footprint; homelab raises the pooler cap to ~80 in PARALLEL, so this is
    # HEADROOM, not a hard fit. db_pool_pre_ping validates a connection before checkout (drops
    # dead server conns instead of handing one out), db_pool_recycle=1800 recycles a conn after
    # 30 min so an idle server slot is freed rather than pinned indefinitely, and db_pool_timeout
    # bounds the acquire wait so a saturated pool fails fast instead of hanging a request.
    db_pool_size: int = Field(
        default=5,
        validation_alias=AliasChoices("PHAZE_DB_POOL_SIZE", "db_pool_size"),
        description="SQLAlchemy engine pool_size for the api + control-worker engines (quick-260707-ryn). Default 5.",
    )
    db_max_overflow: int = Field(
        default=5,
        validation_alias=AliasChoices("PHAZE_DB_MAX_OVERFLOW", "db_max_overflow"),
        description="SQLAlchemy engine max_overflow for the api + control-worker engines (quick-260707-ryn). Default 5.",
    )
    db_pool_timeout: int = Field(
        default=10,
        validation_alias=AliasChoices("PHAZE_DB_POOL_TIMEOUT", "db_pool_timeout"),
        description="Seconds to wait for a pooled connection before failing fast (quick-260707-ryn). Default 10.",
    )
    db_pool_recycle: int = Field(
        default=1800,
        validation_alias=AliasChoices("PHAZE_DB_POOL_RECYCLE", "db_pool_recycle"),
        description="Recycle a pooled connection after this many seconds so idle server slots are freed (quick-260707-ryn). Default 1800 (30 min).",
    )
    db_pool_pre_ping: bool = Field(
        default=True,
        validation_alias=AliasChoices("PHAZE_DB_POOL_PRE_PING", "db_pool_pre_ping"),
        description="Validate a pooled connection before checkout, dropping dead server conns (quick-260707-ryn). Default True.",
    )
    dispatch_queue_min_size: int = Field(
        default=0,
        validation_alias=AliasChoices("PHAZE_DISPATCH_QUEUE_MIN_SIZE", "dispatch_queue_min_size"),
        description="psycopg3 min_size for each control-side per-(agent,lane) dispatch queue (quick-260707-ryn). Default 0 keeps zero idle server conns pinned.",
    )
    dispatch_queue_max_size: int = Field(
        default=2,
        validation_alias=AliasChoices("PHAZE_DISPATCH_QUEUE_MAX_SIZE", "dispatch_queue_max_size"),
        description="psycopg3 max_size for each control-side per-(agent,lane) dispatch queue (quick-260707-ryn). Default 2 caps the enqueue burst.",
    )

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

    # phaze-hu8v: 1001Tracklists' robots.txt invites general crawling but the scraper previously
    # spoofed a desktop Chrome User-Agent with no contact info, giving the site operator no way
    # to identify us or apply a phaze-specific policy. Embedded in the honest UA
    # ("phaze/<version> (+<url>)"); exposed as a setting rather than hardcoded so it can be
    # updated (e.g. to a real contact email) without a code change.
    scraper_contact_url: str = Field(
        default="https://github.com/SimplicityGuy/phaze",
        validation_alias=AliasChoices("PHAZE_SCRAPER_CONTACT_URL", "scraper_contact_url"),
        description="Contact URL embedded in the honest 1001Tracklists scraper User-Agent (phaze-hu8v).",
    )

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
    # Phase 67 (D-05, REG-04): control-plane secrets (LLM keys + inherited
    # database_url/redis_url/queue_url) stay on the env `<VAR>_FILE` path. The PER-BACKEND
    # secrets (S3 access/secret keys, kube kubeconfig / SA token) moved to inline `*_file`
    # pointers in backends.toml (config_backends `_read_secret_file`) — they are NO LONGER
    # flat ControlSettings fields, so they are gone from this set (no back-compat shim; D-12).
    SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = BaseSettings.SECRET_FILE_FIELDS | {
        "openai_api_key",
        "anthropic_api_key",
    }

    # Phase 67 (REG-01/D-01): the typed backend registry. Declared as `list[BackendConfig]`
    # (a discriminated union over `kind`, config_backends) so the parsed `[[backends]]` tables
    # validate per-variant at construction. The `default_factory` synthesizes the implicit
    # single kind=local backend when the `backends` key is ABSENT (no file) so the live all-local
    # deploy needs zero config edits (D-03). A present-but-empty `backends = []` does NOT fire the
    # factory and is failed fast by `_validate_registry` below (Pitfall 2). NOT exposed as an env
    # var: the registry is sourced ONLY from the TOML file (Pitfall 6). `buckets` is the S3
    # staging-bucket registry (REG-05); empty by default.
    backends: list[BackendConfig] = Field(default_factory=_default_local_registry)
    buckets: list[BucketConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _load_backend_registry(cls, data: Any) -> Any:
        """Idiom B: load `backends`/`buckets` from the `PHAZE_BACKENDS_CONFIG_FILE` TOML (D-01/D-02/D-03).

        Mirrors the `_resolve_secret_files` before-validator's inject-into-`data` shape. Reads the
        env pointer (default `/etc/phaze/backends.toml`); if the file exists, `tomllib.load`s it and
        injects the parsed `[[backends]]` / `[[buckets]]` tables — making the TOML file the SINGLE
        source (Pitfall 6). If the file is ABSENT, injects nothing so the `backends` default_factory
        synthesizes implicit-local (D-03 zero-config). `backends`/`buckets` are deliberately NOT
        exposed as env vars, so nothing else populates them.
        """
        if not isinstance(data, dict):
            return data
        # WR-02: resolve the pointer through the same .env-aware map every other var uses
        # (`_resolution_env`, process env wins over `.env`) rather than `os.environ` alone — otherwise a
        # `.env`-declared PHAZE_BACKENDS_CONFIG_FILE is silently ignored and all cloud config is dropped
        # in favor of implicit-local.
        path = _resolution_env(cls.model_config).get("PHAZE_BACKENDS_CONFIG_FILE", "/etc/phaze/backends.toml")
        toml_path = Path(path)
        if not toml_path.exists():
            # Absent file → inject nothing; the default_factory fires (implicit-local, D-03).
            return data
        with toml_path.open("rb") as handle:
            parsed = tomllib.load(handle)
        # Present file → the TOML is authoritative. `.get(..., [])` means a file that declares only
        # `[[buckets]]` (or is empty) resolves `backends` to a present-but-empty list, which
        # `_validate_registry` fails fast on rather than silently synthesizing local (Pitfall 2).
        data["backends"] = parsed.get("backends", [])
        data["buckets"] = parsed.get("buckets", [])
        return data

    @model_validator(mode="after")
    def _validate_registry(self) -> "ControlSettings":
        """Enforce whole-registry invariants the per-variant submodels can't see (REG-04/05, D-08/D-09).

        Cross-entry checks, in order:
          * A resolved-empty registry (present-but-empty `backends = []`) fails fast rather than
            booting with no backend — the Phase-30 silent-wedge failure mode (REG-04, Pitfall 2).
          * Each KueueBackend's `buckets` id-list must resolve against `self.buckets`: an unknown id
            (D-08) or an empty resolved set (D-08) fails fast, naming the offending backend id.
          * A `scope="cluster-specific"` bucket referenced by >1 kueue backend fails fast, naming the
            bucket id — the sharing-cardinality invariant (D-09). `scope="shared"` may be referenced
            by many.
        """
        if not self.backends:
            raise ValueError("backend registry resolved to empty — refusing to start (REG-04)")
        # WR-03: fail fast on duplicate [[buckets]] ids. `bucket_by_id` (and s3_staging.resolve_bucket_config)
        # build a `{b.id: b}` dict that silently collapses duplicates to whichever entry appears LAST in the
        # TOML list — with distinct endpoint_url/creds per entry, a copy-paste id typo would then non-
        # deterministically redirect every presign/cleanup for that id to the wrong bucket. Surface it at boot
        # like every other registry invariant here (REG-05).
        dupes = sorted(bid for bid, count in Counter(b.id for b in self.buckets).items() if count > 1)
        if dupes:
            raise ValueError(f"duplicate bucket ids in registry: {dupes} — each [[buckets]] id must be unique (REG-05)")
        # D-04: fail fast on a duplicate compute agent_ref. Plan 02 retired the ≤1-compute blanket
        # fail-fast so N distinct compute agents dispatch in parallel; without this guard two compute
        # backends naming the SAME agent_ref would silently double-bind (a copy-paste id typo routing two
        # entries at one node). STATIC check only — a Counter over config values, mirroring the bucket-id
        # idiom above. Per D-05 an agent_ref naming a not-yet-checked-in agent is LEGAL at boot (agents
        # register dynamically via check-in), so this opens NO DB session; that path degrades to a runtime
        # hold (Plan 03). Skip ``agent_ref is None`` so the per-variant ``_require_dispatch_fields``
        # "requires an agent_ref" message is never masked by this container-level guard.
        compute_agent_refs = [be.agent_ref for be in self.backends if isinstance(be, ComputeBackend) and be.agent_ref is not None]
        agent_dupes = sorted(ref for ref, count in Counter(compute_agent_refs).items() if count > 1)
        if agent_dupes:
            collisions = {ref: sorted(be.id for be in self.backends if isinstance(be, ComputeBackend) and be.agent_ref == ref) for ref in agent_dupes}
            raise ValueError(
                f"duplicate compute agent_ref(s) {agent_dupes} bound by backends {collisions} — each compute backend must bind a distinct agent_ref (D-04)"
            )
        bucket_by_id = {b.id: b for b in self.buckets}
        cluster_specific_refs: dict[str, list[str]] = {}
        for be in self.backends:
            if not isinstance(be, KueueBackend):
                continue
            missing = [bid for bid in be.buckets if bid not in bucket_by_id]
            if missing:
                raise ValueError(f"backend {be.id!r} references unknown bucket ids {missing} (D-08)")
            resolved = [bucket_by_id[bid] for bid in be.buckets]
            if not resolved:
                raise ValueError(f"backend {be.id!r} (kueue) resolves to an empty bucket set (D-08)")
            for bucket in resolved:
                if bucket.scope == "cluster-specific":
                    cluster_specific_refs.setdefault(bucket.id, []).append(be.id)
        for bid, refs in cluster_specific_refs.items():
            if len(refs) > 1:
                raise ValueError(
                    f"bucket {bid!r} is scope=cluster-specific but referenced by {len(refs)} kueue backends {refs} — at most one allowed (D-09)"
                )
        return self

    @model_validator(mode="after")
    def _enforce_redis_password_in_production(self) -> "ControlSettings":
        """phaze-hti8: production refuses passwordless redis_url on the control plane.

        docker-compose.yml starts Redis with `--requirepass ${REDIS_PASSWORD:?...}`,
        so every Redis client MUST authenticate. The control-plane clients
        (app.state.redis, the controller queue's cache_redis, and every per-agent
        queue's counter/rate-limit handle) all consume this `redis_url` verbatim,
        with NO step injecting REDIS_PASSWORD into it. Without this guard the app
        server boots green (connections are lazy) and only raises NOAUTH
        `AuthenticationError` on first Redis use.

        Mirrors `AgentSettings._enforce_redis_password_in_production` (Phase 29
        D-06): `production` fails fast at construction time; `dev` (the default)
        permits passwordless URLs so a fresh `docker compose up` needs no extra
        ceremony. `urlparse` resolves URL-encoded passwords; a truly malformed URL
        falls through to a connection failure at queue construction time.
        """
        if self.control_env == "production":
            parsed = urlparse(self.redis_url)
            if not parsed.password:
                raise ValueError("control_env=production requires a password in redis_url (phaze-hti8)")
        return self

    @property
    def cloud_enabled(self) -> bool:
        """True iff the registry holds any non-local backend (D-14/D-15).

        The single registry-derived on/off gate the Wave-3 Class-A call sites rewire against: the
        implicit-local registry has only a kind=local backend → False (pure local analysis, no cloud
        activity); any compute/kueue backend → True.
        """
        return any(backend.kind != "local" for backend in self.backends)

    # Phase 73 (MCOMP-03): ``active_compute_scratch_dir`` is RETIRED. Its last runtime reader (the
    # ``/pushed`` callback, agent_push.py) was rewired in Plan 03 to resolve scratch PER FILE from the
    # recorded ``cloud_job.backend_id`` via ``services.backends.resolve_compute_backend(cfg, backend_id)``
    # -- so an N-compute deploy attributes each file's scratch dir to the agent it was dispatched to,
    # replacing the transitional single-compute global reduction (mirrors the Phase-70 active_kube /
    # active_bucket retirements below).

    # Phase 70 (MKUE-01): ``active_kube`` is RETIRED. Each Kueue backend's ``KubeConfig`` is threaded
    # per-call from ``KueueBackend.config.kube`` into every ``kube_staging`` verb (D-04) -- one control
    # plane dispatches to N distinct clusters, so there is no single module-global kube read.

    # Phase 70 (MKUE-02): ``active_bucket`` is RETIRED. Per-file bucket selection is now deterministic
    # via ``s3_staging.pick_bucket`` at dispatch time; the chosen id is recorded on
    # ``cloud_job.staging_bucket`` and every presign/cleanup call site READS that recorded value and
    # resolves it via ``s3_staging.resolve_bucket_config`` (never a module-global bucket read).

    def log_effective_registry(self) -> None:
        """Emit a secret-free id/kind/rank/cap projection of the resolved registry at startup (REG-04, Pitfall 5).

        Logs ONLY the ``{id, kind, rank, cap}`` projection per backend — never a whole backend/bucket
        model, a ``SecretStr``, or a ``*_file`` mount path — so secret material can never leak into
        logs. Plan 05 wires the CALL into controller startup; this method only defines the projection.
        """
        projection = [{"id": backend.id, "kind": backend.kind, "rank": backend.rank, "cap": backend.cap} for backend in self.backends]
        logger.info("phaze.config effective backend registry", backends=projection, cloud_enabled=self.cloud_enabled)

    # Discogsography
    discogs_match_concurrency: int = 5

    # Deployment-mode selector for the application-server (control) role. Mirrors
    # AgentSettings.agent_env (Phase 29 D-06): `production` triggers the
    # `_enforce_redis_password_in_production` validator below, which refuses a
    # passwordless `redis_url` so a misconfigured app server fails fast at startup
    # rather than connecting NOAUTH to the `--requirepass` Redis mandated by
    # docker-compose.yml (phaze-hti8). The AgentSettings guard already protects the
    # agent role; ControlSettings had NO parallel guard, so every control-plane Redis
    # client (app.state.redis, the controller queue's cache_redis, per-agent queue
    # counters/rate-limits) silently connected unauthenticated. `dev` (the default)
    # preserves the fresh-clone convenience: `docker compose up` works without a
    # Redis password baked into redis_url.
    control_env: Literal["dev", "production"] = Field(
        default="dev",
        validation_alias=AliasChoices("PHAZE_CONTROL_ENV", "control_env"),
        description="Deployment mode. Production refuses passwordless Redis URLs (phaze-hti8, mirrors agent_env D-06).",
    )

    # LLM API keys + config (Phase 6)
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-4-20250514"
    llm_max_rpm: int = 30
    llm_batch_size: int = 10
    llm_max_companion_chars: int = 3000

    # Phase 44: how long an in-flight `process_file` analyze job may run before the dashboard
    # flags it as a STRAGGLER (still grinding, distinct from ANALYSIS_FAILED which gave up).
    # Default tied to the agent's analysis_inner_timeout_sec (6600s): a job past the
    # inner-timeout horizon is, by definition, overdue. Read by the control-plane dashboard
    # (routers/pipeline.py) via get_straggler_count in services/pipeline.py -- it lives on
    # ControlSettings because the dashboard reads the module-level (Control-typed) `settings`.
    straggler_threshold_sec: int = Field(
        default=6600,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_STRAGGLER_THRESHOLD_SEC", "straggler_threshold_sec"),
        description="Running-age threshold (seconds) above which an active process_file analyze job is flagged a straggler on the pipeline dashboard (Phase 44). Default 6600 mirrors analysis_inner_timeout_sec; lt=86400 caps it at one day.",
    )

    # Phase 49 D-07: files whose joined FileMetadata.duration is at/above this threshold
    # are routed to a cloud compute agent (held in FileState.AWAITING_CLOUD) instead of the
    # on-prem file-server. The per-file router (Plan 02), backfill (Plan 03), and release
    # cron (Plan 04) all compare against this single knob. Bounded (gt=0, lt=86400) like
    # straggler_threshold_sec so an out-of-range operator value fails fast at startup (T-49-01)
    # and never reaches the SQL `duration >= threshold` compare. Lives on ControlSettings
    # because the control plane owns routing decisions.
    cloud_route_threshold_sec: int = Field(
        default=5400,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_CLOUD_ROUTE_THRESHOLD_SEC", "cloud_route_threshold_sec"),
        description="Duration threshold (seconds) at/above which a file is routed to a cloud compute agent for analysis (Phase 49). Default 5400 (90 min); lt=86400 caps it at one day.",
    )

    # Phase 67 (REG-04, D-12): the flat cloud-target selector and the flat in-flight window field
    # were REMOVED with no shim. The active target is now derived from the typed backend registry
    # (`cloud_enabled` gate + `resolve_backends`/`resolved_non_local_kind` in services/backends.py);
    # the two transitional dispatch-selector accessors were removed in Phase 68 (BACK-01/D-07) once
    # every reader resolved through the Backend protocol. The per-backend concurrency cap comes from
    # each backend's `cap` in backends.toml. See D-11/D-12.

    # Phase 50 D-12: how many times control re-drives a push that failed sha256 verification before
    # giving up. Phase 69 (SCHED-03/D-04): at the cap the file no longer hard-fails -- it SPILLS back to
    # AWAITING_CLOUD with its cloud budget marked spent so the next drain tick routes it to local.
    # Bounded (gt=0, lt=20) so a misconfig cannot create an unbounded retry storm (T-50-config-oob).
    push_max_attempts: int = Field(
        default=3,
        gt=0,
        lt=20,
        validation_alias=AliasChoices("PHAZE_PUSH_MAX_ATTEMPTS", "push_max_attempts"),
        description="Max push re-drives of a sha256-mismatched file before it spills back to AWAITING_CLOUD to fall to local (Phase 50 D-12, Phase 69 SCHED-03). Default 3; bounded gt=0, lt=20.",
    )
    # Phase 54 D-08: how many times control re-submits a Kueue Job for a file before giving up and
    # marking it ANALYSIS_FAILED. A DISTINCT budget from push_max_attempts (the rsync push leg) --
    # the kube submit leg has its own failure modes (admission/scheduling/transient API errors), so
    # it gets its own retry budget. Bounded (gt=0, lt=20) like push_max_attempts so a misconfig
    # cannot create an unbounded submit storm (T-54-02). Lives on ControlSettings because the
    # control plane owns submission.
    cloud_submit_max_attempts: int = Field(
        default=3,
        gt=0,
        lt=20,
        validation_alias=AliasChoices("PHAZE_CLOUD_SUBMIT_MAX_ATTEMPTS", "cloud_submit_max_attempts"),
        description="Max kube Job submit attempts before a file is marked ANALYSIS_FAILED (Phase 54, D-08). A distinct budget from push_max_attempts. Default 3; bounded gt=0, lt=20.",
    )
    # Phase 69 D-02: seconds a long file waits in AWAITING_CLOUD while higher-rank backends are
    # online-but-FULL before the slow local (rank-99) backend becomes an eligible spill target. The
    # pure `select_backend` policy (services/backend_selection.py) compares (now - file.updated_at)
    # against this knob to decide whether a full-cloud file may spill to local. Offline backends spill
    # to local immediately (D-03, NOT staleness-gated). Bounded (gt=0, lt=86400) like
    # cloud_route_threshold_sec so an out-of-range operator value fails fast at startup (T-69-01-01)
    # and never reaches selection. Lives on ControlSettings because the control plane owns routing.
    cloud_spill_to_local_after_seconds: int = Field(
        default=900,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_CLOUD_SPILL_TO_LOCAL_AFTER_SECONDS", "cloud_spill_to_local_after_seconds"),
        description="Seconds a long file waits in AWAITING_CLOUD while higher-rank backends are FULL before slow local becomes an eligible spill target (Phase 69, D-02). Default 900 (15 min); offline backends spill immediately (D-03).",
    )
    # phaze-ul2v: the two STAGING-half staleness bounds the reconcile reaper
    # (``KueueBackend._reap_stranded_staging``) compares ``cloud_job.updated_at`` against. UPLOADING /
    # UPLOADED rows count toward ``in_flight_count`` (D-10) but are terminalized ONLY by the agent HTTP
    # callbacks (``/uploaded``, ``/failed``); a dead agent or a lost ``s3_upload`` SAQ job means neither
    # callback ever fires and the row occupies a lane cap slot FOREVER. These bounds are the safety net.
    # The callback path stays PRIMARY: the reaper must never fire on a row younger than its bound, so
    # ``cloud_uploading_stale_after_sec`` MUST comfortably exceed the largest real
    # ``upload_file_saq_timeout_sec(part_count)`` net (a multi-GB multipart upload is legitimately slow
    # and bumps no timestamp while it transfers). Default 21600 (6h). Bounded (gt=0, lt=604800) so an
    # out-of-range operator value fails fast at startup rather than reaping live uploads.
    cloud_uploading_stale_after_sec: int = Field(
        default=21600,
        gt=0,
        lt=604800,
        validation_alias=AliasChoices("PHAZE_CLOUD_UPLOADING_STALE_AFTER_SEC", "cloud_uploading_stale_after_sec"),
        description="Seconds a cloud_job may sit UPLOADING with no timestamp movement before the reconcile reaper spills it back to awaiting (phaze-ul2v). Default 21600 (6h); MUST exceed the largest s3_upload SAQ net.",
    )
    # The UPLOADED half: ``report_uploaded`` enqueues ``submit_cloud_job`` in the SAME transaction that
    # flips the row to UPLOADED, so an UPLOADED row is expected to advance to SUBMITTED within one
    # controller-queue hop. A row still UPLOADED long after that means the submit was lost. Much tighter
    # than the UPLOADING bound because nothing legitimately dwells here. Default 900 (15 min).
    cloud_uploaded_stale_after_sec: int = Field(
        default=900,
        gt=0,
        lt=604800,
        validation_alias=AliasChoices("PHAZE_CLOUD_UPLOADED_STALE_AFTER_SEC", "cloud_uploaded_stale_after_sec"),
        description="Seconds a cloud_job may sit UPLOADED with no submit enqueued before the reconcile reaper spills it back to awaiting (phaze-ul2v). Default 900 (15 min).",
    )
    # Phase 67 (REG-04, D-12): the flat compute scratch-dir field and the flat S3
    # connection/credential surface (endpoint / bucket / region / addressing-style / access-key /
    # secret-key) were REMOVED with no shim. Compute scratch dir now comes from the compute
    # backend's `scratch_dir`, resolved PER FILE via `resolve_compute_backend` (the transitional
    # `active_compute_scratch_dir` global was retired in Phase 73 / MCOMP-03); bucket identity/creds
    # come from the `[[buckets]]` registry (`active_bucket`, retired in Phase 70 / MKUE-02). The D-15 GLOBAL S3
    # tuning knobs below (presign TTLs / lifecycle / part-size) are NOT per-backend and REMAIN on
    # ControlSettings.

    # Bounded presign/lifecycle/part-size knobs: an out-of-range operator value fails fast at
    # startup (T-53-03) and never reaches the presign/upload code path.
    s3_presign_put_ttl_sec: int = Field(
        default=3600,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_S3_PRESIGN_PUT_TTL_SEC", "s3_presign_put_ttl_sec"),
        description="TTL (seconds) for presigned multipart PUT/part URLs minted for the upload leg (Phase 53, KSTAGE-02). Default 3600; bounded gt=0, lt=86400.",
    )
    s3_presign_get_ttl_sec: int = Field(
        default=900,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_S3_PRESIGN_GET_TTL_SEC", "s3_presign_get_ttl_sec"),
        description="TTL (seconds) for the just-in-time presigned GET URL minted at pod startup (Phase 53, KSTAGE-03). Default 900 (short -- minted post-admission so it never expires during a Kueue wait); bounded gt=0, lt=86400.",
    )
    s3_lifecycle_ttl_days: int = Field(
        default=2,
        gt=0,
        lt=30,
        validation_alias=AliasChoices("PHAZE_S3_LIFECYCLE_TTL_DAYS", "s3_lifecycle_ttl_days"),
        description="Bucket lifecycle TTL (days) -- the backstop that deletes any staged object the inline callback delete missed (Phase 53, KSTAGE-04, D-02). Default 2; bounded gt=0, lt=30.",
    )
    s3_multipart_part_size_bytes: int = Field(
        default=67108864,
        ge=5242880,
        lt=5368709120,
        validation_alias=AliasChoices("PHAZE_S3_MULTIPART_PART_SIZE_BYTES", "s3_multipart_part_size_bytes"),
        description="Multipart upload part size (bytes) the agent streams over presigned part URLs (Phase 53, D-01). Default 67108864 (64 MiB); bounded to the S3 [5 MiB, 5 GiB) part-size range.",
    )
    s3_client_timeout_sec: int = Field(
        default=30,
        gt=0,
        lt=600,
        validation_alias=AliasChoices("PHAZE_S3_CLIENT_TIMEOUT_SEC", "s3_client_timeout_sec"),
        description=(
            "phaze-1v37: explicit connect + read timeout (seconds) bounding every control-side S3 SDK call "
            "(complete/abort/delete multipart). Without it botocore's minute-scale defaults + retries let a "
            "wedged/blackholed S3 endpoint pin the calling connection for minutes; a bounded timeout caps how "
            "long any staging-callback S3 round-trip can hold resources. Default 30; bounded gt=0, lt=600."
        ),
    )

    # Phase 67 (REG-04, D-12): the flat kube cluster connection + Job-manifest surface (api-url /
    # namespace / local-queue / job-image / cpu-request / memory-request / workload-api-version /
    # ca-secret-name / env-configmap-name / env-secret-name / kubeconfig / sa-token) was REMOVED
    # with no shim. Kueue cluster config now lives in each kueue backend's `[kube]` table in
    # backends.toml (config_backends KubeConfig; `active_kube`, retained through Phase 70 / MKUE-01). The three
    # per-target fail-fast model validators and the S3-endpoint field-validator were removed too —
    # their per-variant equivalents now live on the Plan-01 submodels (KubeConfig / BucketConfig
    # required fields + endpoint validation) and the whole-registry `_validate_registry`
    # invariants above (REG-02, D-12).


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
    # Phase 50 D-05/D-07: the rsync-over-SSH push identity key and pinned known_hosts are
    # file-mounted secrets; adding them here lets the shared `_resolve_secret_files` validator
    # auto-resolve their `<VAR>_FILE` siblings with NO new resolution code. Never log their
    # values (D-13 token-preview discipline).
    SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = BaseSettings.SECRET_FILE_FIELDS | {"agent_token", "push_ssh_key", "push_known_hosts"}

    # WR-01: the SSH key + known_hosts are consumed verbatim by ssh (key material), so their
    # file-mounted contents must keep the trailing newline OpenSSH requires -- do NOT strip them.
    # ``agent_token`` is deliberately NOT here: its entire wire string is hashed, so the strip that
    # normalizes a heredoc newline is correct for it.
    SECRET_FILE_PRESERVE_WHITESPACE: ClassVar[frozenset[str]] = frozenset({"push_ssh_key", "push_known_hosts"})

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
    # Phase 48: agent capability marker. The Literal is the config-layer (middle)
    # enum of the 3-layer kind defense — CLI argparse `choices=` (outer) and the
    # `ck_agents_kind_enum` DB CHECK (inner, Plan 01) bracket it. A `compute`
    # (cloud) agent owns no media and no scan roots; this relaxes the
    # empty-scan-roots gate in `_enforce_required_agent_fields` below.
    kind: Literal["fileserver", "compute"] = Field(
        default="fileserver",
        validation_alias=AliasChoices("PHAZE_AGENT_KIND", "kind"),
        description="Agent kind. 'compute' (cloud) agents own no scan roots; relaxes the empty-scan-roots gate (Phase 48).",
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

    # Phase 43: bound per-file analysis cost (kill-on-timeout). The agent worker passes
    # analysis_inner_timeout_sec to the killable pebble ProcessPool (pool.py); the two
    # caps bound the number of windows analyze_file decodes (consumed by Plan 02/04).
    analysis_inner_timeout_sec: int = Field(
        default=6600,
        gt=0,
        lt=7200,
        validation_alias=AliasChoices("PHAZE_ANALYSIS_INNER_TIMEOUT_SEC", "analysis_inner_timeout_sec"),
        description="Inner pebble per-task analysis timeout; MUST stay below the 7200s SAQ process_file net so the kill is deterministic (Phase 43, RESEARCH Pitfall 2). Enforced lt=7200 so a misconfig can't disable the deterministic kill.",
    )
    analysis_fine_cap: int = Field(
        default=60,
        ge=2,
        validation_alias=AliasChoices("PHAZE_ANALYSIS_FINE_CAP", "analysis_fine_cap"),
        description="Maximum number of FINE-tier (BPM/key) windows analyze_file decodes per file (Phase 43). ge=2: even-stride always keeps first+last, so a cap below 2 is invalid (and would divide-by-zero in _stride_to_cap).",
    )
    analysis_coarse_cap: int = Field(
        default=30,
        ge=2,
        validation_alias=AliasChoices("PHAZE_ANALYSIS_COARSE_CAP", "analysis_coarse_cap"),
        description="Maximum number of COARSE-tier (mood/style/danceability) windows analyze_file decodes per file (Phase 43). ge=2: even-stride always keeps first+last, so a cap below 2 is invalid (and would divide-by-zero in _stride_to_cap).",
    )

    # Phase 57.1 (PROG-01, D-04): the parent/loop-side throttle for the mid-flight analyze
    # progress POST. analyze_file fires its progress_cb per FINE window, but the lane bridge
    # (tasks/functions.py drainer + job_runner.py cb) collapses bursts to at most one POST per
    # this interval (monotonic-keyed) and always flushes the final count. Given the Phase 31
    # window caps (≤~60 fine windows/file) the throttle only matters for short/fast files.
    analysis_progress_interval_sec: float = Field(
        default=5.0,
        ge=0.0,
        validation_alias=AliasChoices("PHAZE_ANALYSIS_PROGRESS_INTERVAL_SEC", "analysis_progress_interval_sec"),
        description="Minimum seconds between mid-flight analyze-progress POSTs (Phase 57.1 D-04). The final count is always flushed regardless; 0 disables throttling.",
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

    # Phase 50 D-05/D-07: rsync-over-SSH push target (the fileserver agent pushes long files to
    # the compute agent's scratch dir). The SSH host/user identify the static push target;
    # cloud_scratch_dir is the remote landing directory whose path MUST match the control-plane's
    # compute-backend scratch dir (Phase 67: the compute backend's `scratch_dir` in backends.toml,
    # resolved control-side PER FILE via `resolve_compute_backend`; the transitional
    # `active_compute_scratch_dir` accessor was retired in Phase 73 / MCOMP-03). The two
    # timeouts bracket the transport: push_timeout_sec
    # is the rsync I/O-stall timeout (must stay below the SAQ push_file job net), and
    # push_connect_timeout_sec caps the SSH connect handshake. Operator-provisioned in Phase 51;
    # Phase 50 only declares the fields.
    push_ssh_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_PUSH_SSH_HOST", "push_ssh_host"),
        description="Hostname/IP of the rsync-over-SSH push target (the compute agent). Operator-provisioned in Phase 51 (Phase 50, D-05).",
    )
    push_ssh_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_PUSH_SSH_USER", "push_ssh_user"),
        description="SSH username for the rsync push target (Phase 50, D-05).",
    )
    cloud_scratch_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_CLOUD_SCRATCH_DIR", "cloud_scratch_dir"),
        description="Remote scratch directory on the compute agent where pushed files land and are later read by process_file. MUST match the control-plane compute backend's scratch_dir in backends.toml (Phase 50, D-07; Phase 67).",
    )
    push_timeout_sec: int = Field(
        default=600,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_PUSH_TIMEOUT_SEC", "push_timeout_sec"),
        description="rsync I/O-stall timeout (seconds) for a single push_file transfer; MUST stay below the SAQ push_file job timeout so the kill is deterministic (Phase 50). Default 600; bounded gt=0, lt=86400.",
    )
    push_connect_timeout_sec: int = Field(
        default=30,
        gt=0,
        lt=3600,
        validation_alias=AliasChoices("PHAZE_PUSH_CONNECT_TIMEOUT_SEC", "push_connect_timeout_sec"),
        description="SSH connect-handshake timeout (seconds) for the rsync push (Phase 50). Default 30; bounded gt=0, lt=3600.",
    )
    # D-05/D-07 file-mounted secrets — resolved via SECRET_FILE_FIELDS above. NEVER log (D-13).
    push_ssh_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_PUSH_SSH_KEY", "push_ssh_key"),
        description="SSH identity private key for the rsync push, file-mounted via PHAZE_PUSH_SSH_KEY_FILE (Phase 50, D-05). Never logged.",
    )
    push_known_hosts: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_PUSH_KNOWN_HOSTS", "push_known_hosts"),
        description="Pinned known_hosts for strict SSH host-key checking of the push target, file-mounted via PHAZE_PUSH_KNOWN_HOSTS_FILE (Phase 50, D-07). Never logged.",
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
        # Phase 48: a compute (cloud) agent owns no media, so the scan-roots
        # requirement is relaxed ONLY for kind == "compute". api_url/token stay
        # required for every kind — a compute agent still authenticates with a
        # bearer over HTTP.
        if self.kind != "compute" and not self.scan_roots:
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


def export_llm_api_keys(*, anthropic_api_key: SecretStr | None, openai_api_key: SecretStr | None) -> None:
    """Bridge file-loaded LLM secrets into the provider env vars litellm reads.

    litellm resolves provider credentials from the process environment
    (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``), never from phaze's settings. Phaze
    loads these keys via the ``<VAR>_FILE`` secret convention into ``SecretStr`` fields
    on :class:`ControlSettings`, so without this bridge every ``litellm.acompletion``
    call raises ``AuthenticationError: Missing Anthropic API Key`` (Bug A, June 2026 --
    ``generate_proposals`` had never succeeded in deployment).

    Called once from the control worker's startup hook. Each present key is exported
    ONLY when the bare provider env var is unset, so an operator-supplied
    ``ANTHROPIC_API_KEY`` always wins. The secret value is never logged.
    """
    for env_name, secret in (
        ("ANTHROPIC_API_KEY", anthropic_api_key),
        ("OPENAI_API_KEY", openai_api_key),
    ):
        if secret is not None and not os.environ.get(env_name):
            os.environ[env_name] = secret.get_secret_value()


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
