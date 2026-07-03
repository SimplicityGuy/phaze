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
import tomllib
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings, NoDecode, SettingsConfigDict

from phaze.config_backends import (
    BackendConfig,
    BucketConfig,
    _default_local_registry,
    _read_secret_file,
)


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
    # Phase 53 (KSTAGE-05): the S3 credentials honor the same `<VAR>_FILE` convention so the
    # control plane reads them from Docker/K8s secret mounts; they live on ControlSettings ONLY
    # (KSTAGE-02 -- the agent and pod never receive bucket credentials; T-53-01).
    # Phase 54 (KSUBMIT-01): the kube credentials (kubeconfig / SA token) honor the same
    # `<VAR>_FILE` convention so the control plane reads them from Docker/K8s secret mounts; they
    # live on ControlSettings ONLY (the agent and pod never receive kube credentials; T-54-01).
    SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = BaseSettings.SECRET_FILE_FIELDS | {
        "openai_api_key",
        "anthropic_api_key",
        "s3_access_key_id",
        "s3_secret_access_key",
        "kube_kubeconfig",
        "kube_sa_token",
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
        path = os.environ.get("PHAZE_BACKENDS_CONFIG_FILE", "/etc/phaze/backends.toml")
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

    # Discogsography
    discogs_match_concurrency: int = 5

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

    # Phase 55 D-02 (KROUTE-01): the single cloud-target selector that HARD-REPLACES the Phase 51
    # cloud-burst master bool. ONE setting selects the active target -- 'local' (default) ==
    # cloud OFF (pure local analysis, no cloud activity); 'a1' = the v5.0 rsync→OCI-A1 compute
    # agent; 'k8s' = the v6.0 S3→Kueue burst. It is the single source of truth that gates ALL THREE
    # cloud entry points -- the routing seam (D-02), the staging cron (D-03), and the backfill
    # trigger (D-03) -- so 'local' reverts to pure local analysis with no other change. A pydantic
    # `Literal` rejects any off-list member at construction (T-55-CFG-01); there is NO back-compat
    # alias for PHAZE_CLOUD_BURST_ENABLED (D-02). Not secret-bearing, so it is absent from
    # SECRET_FILE_FIELDS. Lives on ControlSettings because the control plane owns routing.
    cloud_target: Literal["local", "a1", "k8s"] = Field(
        default="local",
        validation_alias=AliasChoices("PHAZE_CLOUD_TARGET", "cloud_target"),
        description="Active cloud target: 'local' (default) == cloud off; 'a1' = rsync→OCI A1 compute agent; 'k8s' = S3→Kueue burst. Single source of truth (Phase 55, D-02/KROUTE-01).",
    )

    # Phase 50 D-03: the load-bearing ≤N cloud window. The staging cron tops up so that the
    # count of files in {PUSHING, PUSHED} never exceeds this; it is the only backpressure that
    # keeps an unbounded backlog off the single compute agent. Bounded (gt=0, lt=100) like
    # cloud_route_threshold_sec so an out-of-range operator value fails fast at startup
    # (T-50-config-oob). Lives on ControlSettings because the control plane owns the window.
    cloud_max_in_flight: int = Field(
        default=2,
        gt=0,
        lt=100,
        validation_alias=AliasChoices("PHAZE_CLOUD_MAX_IN_FLIGHT", "cloud_max_in_flight"),
        description="Max cloud files staged-or-in-flight (PUSHING+PUSHED); the load-bearing ≤N window (Phase 50, D-03). Default 2; bounded gt=0, lt=100.",
    )
    # Phase 50 D-12: how many times control re-drives a push that failed sha256 verification
    # before giving up and marking the file ANALYSIS_FAILED. Bounded (gt=0, lt=20) so a misconfig
    # cannot create an unbounded retry storm (T-50-config-oob).
    push_max_attempts: int = Field(
        default=3,
        gt=0,
        lt=20,
        validation_alias=AliasChoices("PHAZE_PUSH_MAX_ATTEMPTS", "push_max_attempts"),
        description="Max push attempts before a sha256-mismatched file is marked ANALYSIS_FAILED (Phase 50, D-12). Default 3; bounded gt=0, lt=20.",
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
    # Phase 50: control-side mirror of the compute agent's AgentSettings.cloud_scratch_dir.
    # The push-success callback (routers/agent_push.py, 50-05) builds the process_file
    # scratch_path from this base (`<compute_scratch_dir>/<file_id>.<ext>`). Its value MUST
    # match the compute agent's cloud_scratch_dir; a drift surfaces as a sha256/transfer failure
    # (50-04/50-05), never silent corruption (T-50-scratch-skew). Lives on ControlSettings
    # because the control plane builds the payload.
    compute_scratch_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_COMPUTE_SCRATCH_DIR", "compute_scratch_dir"),
        description="Control-side copy of the compute agent's scratch directory; used to build process_file scratch_path in the push callback (Phase 50, 50-05). MUST match the compute agent's cloud_scratch_dir.",
    )

    # Phase 53 (KSTAGE-05): operator-provided S3 object-staging surface. Works against ANY
    # S3-compatible backend via an explicit `endpoint_url` (MinIO/Backblaze/AWS/etc.), not just
    # AWS. The control plane presigns multipart PUT + just-in-time GET and deletes staged objects
    # (KSTAGE-01..04); the file-server agent and pod transfer bytes over presigned URLs and never
    # see these fields (KSTAGE-02). All optional by default so an all-local (cloud-off) deploy
    # needs zero S3 config; the `_enforce_s3_config_when_cloud_enabled` validator fails fast when
    # cloud burst is ON but the staging substrate is unconfigured.
    s3_endpoint_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_S3_ENDPOINT_URL", "s3_endpoint_url"),
        description="S3-compatible endpoint URL (e.g. https://s3.us-west-1.amazonaws.com or a MinIO/Backblaze URL). Must be a well-formed http(s) URL (Phase 53, KSTAGE-05; T-53-02).",
    )
    s3_bucket: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_S3_BUCKET", "s3_bucket"),
        description="Operator-created bucket used for ephemeral file_id-scoped staging objects (Phase 53, KSTAGE-04/05).",
    )
    s3_region: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_S3_REGION", "s3_region"),
        description="S3 region (e.g. us-west-1). Optional for many S3-compatible backends (Phase 53, KSTAGE-05).",
    )
    s3_addressing_style: Literal["path", "virtual"] = Field(
        default="path",
        validation_alias=AliasChoices("PHAZE_S3_ADDRESSING_STYLE", "s3_addressing_style"),
        description="S3 addressing style. 'path' (default) maximizes S3-compatible-backend support; 'virtual' for AWS virtual-hosted-style (Phase 53, KSTAGE-05).",
    )
    s3_access_key_id: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_S3_ACCESS_KEY_ID", "s3_access_key_id"),
        description="S3 access key id (control-plane only; resolves from PHAZE_S3_ACCESS_KEY_ID_FILE per the _FILE convention). KSTAGE-02/T-53-01.",
    )
    s3_secret_access_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_S3_SECRET_ACCESS_KEY", "s3_secret_access_key"),
        description="S3 secret access key (control-plane only; resolves from PHAZE_S3_SECRET_ACCESS_KEY_FILE per the _FILE convention). KSTAGE-02/T-53-01.",
    )
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

    # Phase 54 (KSUBMIT-01): the kube client surface the submit seam, submit task, and reconcile
    # cron read. ALL OPTIONAL (default None) in Phase 54 so an existing Phase 53 cloud-on/no-kube
    # deploy keeps working -- the fail-fast model validator that couples these to
    # `cloud_target == "k8s"` is Phase 55 (`_enforce_kube_config_when_k8s`, below), pulled forward
    # from KDEPLOY-02. Credentials (`kube_kubeconfig` / `kube_sa_token`) are SecretStr resolved
    # from `<VAR>_FILE` mounts via SECRET_FILE_FIELDS above; they live on the control plane only
    # (T-54-01) and are never logged.
    kube_api_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_KUBE_API_URL", "kube_api_url"),
        description="Kubernetes API server URL the control plane submits/watches Jobs against (Phase 54, KSUBMIT-01). Required when cloud_target is 'k8s' (Phase 55 fail-fast validator).",
    )
    kube_namespace: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_KUBE_NAMESPACE", "kube_namespace"),
        description="Namespace the Kueue Jobs are submitted into (Phase 54, KSUBMIT-01). Optional in Phase 54.",
    )
    kube_local_queue: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_KUBE_LOCAL_QUEUE", "kube_local_queue"),
        description="Kueue LocalQueue name stamped on submitted Jobs (kueue.x-k8s.io/queue-name label) (Phase 54, KSUBMIT-01). Optional in Phase 54.",
    )
    kube_job_image: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_KUBE_JOB_IMAGE", "kube_job_image"),
        description="Container image the submitted analysis Job runs (Phase 54, KSUBMIT-01). Optional in Phase 54.",
    )
    kube_job_cpu_request: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_KUBE_JOB_CPU_REQUEST", "kube_job_cpu_request"),
        description="CPU resource request stamped on the submitted Job's pod spec (e.g. '2') (Phase 54, KSUBMIT-01). Optional in Phase 54.",
    )
    kube_job_memory_request: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_KUBE_JOB_MEMORY_REQUEST", "kube_job_memory_request"),
        description="Memory resource request stamped on the submitted Job's pod spec (e.g. '4Gi') (Phase 54, KSUBMIT-01). Optional in Phase 54.",
    )
    kube_workload_api_version: str = Field(
        default="kueue.x-k8s.io/v1beta1",
        validation_alias=AliasChoices("PHAZE_KUBE_WORKLOAD_API_VERSION", "kube_workload_api_version"),
        description="apiVersion of the Kueue Workload/Job resources the control plane submits and reconciles (Phase 54, KSUBMIT-01). Default 'kueue.x-k8s.io/v1beta1'.",
    )
    kube_ca_secret_name: str = Field(
        default="phaze-internal-ca",
        validation_alias=AliasChoices("PHAZE_KUBE_CA_SECRET_NAME", "kube_ca_secret_name"),
        description="Name of the operator-created core/v1 Secret holding the internal CA cert (key 'phaze-ca.crt'). The suspended Job mounts it read-only at /certs so the one-shot pod verifies the control-plane TLS chain (Phase 56, KDEPLOY-06). The CA is NOT baked into the Job image (KJOB-05 reversed); rotation is a Secret update + re-submit, no image rebuild. phaze references this Secret by name only and never authors it.",
    )
    kube_env_configmap_name: str = Field(
        default="phaze-agent-env",
        validation_alias=AliasChoices("PHAZE_KUBE_ENV_CONFIGMAP_NAME", "kube_env_configmap_name"),
        description="Name of the operator-created core/v1 ConfigMap the suspended Job sources its static agent env from via envFrom (PHAZE_ROLE=agent, PHAZE_AGENT_API_URL, PHAZE_MODELS_DIR). The per-Job PHAZE_JOB_FILE_ID is injected separately at submit time, not from this ConfigMap. phaze references this ConfigMap by name only and never authors it.",
    )
    kube_env_secret_name: str = Field(
        default="phaze-agent-token",
        validation_alias=AliasChoices("PHAZE_KUBE_ENV_SECRET_NAME", "kube_env_secret_name"),
        description="Name of the operator-created core/v1 Secret the suspended Job sources PHAZE_AGENT_TOKEN from via envFrom; defaults to the existing compute-agent bearer-token Secret (phaze-agent-token). phaze references this Secret by name only and never authors it.",
    )
    # Phase 54 (KSUBMIT-01) credentials -- file-mounted SecretStr resolved via SECRET_FILE_FIELDS
    # above. NEVER log (T-54-01). Control plane only.
    kube_kubeconfig: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_KUBE_KUBECONFIG", "kube_kubeconfig"),
        description="Kubeconfig contents for the control plane's kube client, file-mounted via PHAZE_KUBE_KUBECONFIG_FILE (Phase 54, KSUBMIT-01). Never logged.",
    )
    kube_sa_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PHAZE_KUBE_SA_TOKEN", "kube_sa_token"),
        description="ServiceAccount bearer token for the control plane's kube client, file-mounted via PHAZE_KUBE_SA_TOKEN_FILE (Phase 54, KSUBMIT-01). Never logged.",
    )

    @field_validator("s3_endpoint_url")
    @classmethod
    def _validate_s3_endpoint_url(cls, value: str | None) -> str | None:
        """Require a well-formed http(s) URL with a netloc (T-53-02 SSRF surface).

        ``s3_endpoint_url`` is operator-controlled and feeds the aioboto3 client the control
        plane uses to presign/delete. A scheme-less value (``s3.example.com``) or a non-http
        scheme (``file://``) is rejected at construction so an SSRF-shaped endpoint can never
        reach the S3 client. ``None`` (cloud off / unset) passes through unchanged.
        """
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            msg = f"s3_endpoint_url must be a well-formed http(s) URL with a host, got {value!r}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _enforce_s3_config_when_k8s(self) -> "ControlSettings":
        """Phase 55 (D-02, KROUTE-01): the 'k8s' target requires the S3 staging substrate.

        S3 is the **k8s** byte path (the v6.0 S3→Kueue leg) -- NOT a generic "cloud on"
        concern. The staging leg presigns objects into ``s3_bucket`` at ``s3_endpoint_url``;
        with ``cloud_target == "k8s"`` but either unset, the upload presign would fail at
        runtime with no startup signal. Fail fast. This is one of THREE per-target validators
        kept deliberately separate (RESEARCH Pitfall 3 / T-55-CFG-03): collapsing them into a
        single ``!= "local"`` gate would silently change a1 fail-fast semantics. 'local' and
        'a1' keep S3 optional (a1 uses rsync, not S3), so neither needs S3 config.
        """
        if self.cloud_target == "k8s":
            if not self.s3_bucket:
                raise ValueError("PHAZE_S3_BUCKET is required when PHAZE_CLOUD_TARGET is 'k8s' (it is the S3→Kueue staging bucket)")
            if not self.s3_endpoint_url:
                raise ValueError(
                    "PHAZE_S3_ENDPOINT_URL is required when PHAZE_CLOUD_TARGET is 'k8s' (the S3-compatible endpoint the control plane presigns against)"
                )
        return self

    @model_validator(mode="after")
    def _enforce_compute_scratch_dir_when_a1(self) -> "ControlSettings":
        """Phase 55 (D-02, KROUTE-01): the 'a1' target requires a compute scratch dir.

        ``compute_scratch_dir`` is the **a1** rsync-scratch concern -- NOT a generic "cloud on"
        concern. The push-success callback (routers/agent_push.py) builds the process_file
        ``scratch_path`` as ``<compute_scratch_dir>/<file_id>.<ext>``. If
        ``cloud_target == "a1"`` but ``compute_scratch_dir`` is unset, that path becomes the
        literal ``"None/<file_id>.<ext>"``: every pushed file fails to read, routes to
        push-mismatch, and silently lands in ANALYSIS_FAILED after ``push_max_attempts`` — with
        no startup signal. Fail fast instead, mirroring the agent-side ``_require_push_config``
        guard for ``cloud_scratch_dir`` (push.py). 'local' and 'k8s' keep ``compute_scratch_dir``
        optional (k8s uses S3, not rsync scratch).
        """
        if self.cloud_target == "a1" and not self.compute_scratch_dir:
            raise ValueError(
                "PHAZE_COMPUTE_SCRATCH_DIR is required when PHAZE_CLOUD_TARGET is 'a1' "
                "(it builds the process_file scratch_path; must match the compute agent's PHAZE_CLOUD_SCRATCH_DIR)"
            )
        return self

    @model_validator(mode="after")
    def _enforce_kube_config_when_k8s(self) -> "ControlSettings":
        """Phase 55 (D-02, KROUTE-01; pulls KDEPLOY-02 forward): the 'k8s' target requires the kube surface.

        The submit seam / submit task / reconcile cron read ``kube_api_url`` (where to POST the
        Job), ``kube_namespace`` (where the Job lands), and ``kube_local_queue`` (the
        ``kueue.x-k8s.io/queue-name`` label). These exist optional today (Phase 54); with
        ``cloud_target == "k8s"`` but any unset, submission fails at runtime with no startup
        signal. Fail fast — the third per-target validator (kept separate per RESEARCH Pitfall 3).
        'local' and 'a1' keep the kube fields optional (a1 does not touch kube).
        """
        if self.cloud_target == "k8s":
            if not self.kube_api_url:
                raise ValueError(
                    "PHAZE_KUBE_API_URL is required when PHAZE_CLOUD_TARGET is 'k8s' (the kube API the control plane submits/watches Jobs against)"
                )
            if not self.kube_namespace:
                raise ValueError(
                    "PHAZE_KUBE_NAMESPACE is required when PHAZE_CLOUD_TARGET is 'k8s' (the namespace the Kueue Jobs are submitted into)"
                )
            if not self.kube_local_queue:
                raise ValueError(
                    "PHAZE_KUBE_LOCAL_QUEUE is required when PHAZE_CLOUD_TARGET is 'k8s' (the Kueue LocalQueue name stamped on submitted Jobs)"
                )
        return self


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
    # cloud_scratch_dir is the remote landing directory whose path MUST match
    # ControlSettings.compute_scratch_dir. The two timeouts bracket the transport: push_timeout_sec
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
        description="Remote scratch directory on the compute agent where pushed files land and are later read by process_file. MUST match ControlSettings.compute_scratch_dir (Phase 50, D-07).",
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
