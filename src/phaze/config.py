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
from urllib.parse import quote, urlparse, urlunparse

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings, NoDecode, SettingsConfigDict
import structlog

from phaze.config_backends import (
    BackendConfig,
    BucketConfig,
    _default_local_registry,
    _read_secret_file,
)
from phaze.config_registry_policies import (
    validate_backend_bucket_lists,
    validate_cluster_specific_sharing,
    validate_non_empty_registry,
    validate_unique_compute_agent_refs,
    validate_unique_registry_ids,
)
from phaze.services.analysis_sizing import derive_sizing


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


# How much slack the OUTER (SAQ job heartbeat) analysis deadline gets over the INNER stall
# threshold. See `BaseSettings.analysis_job_heartbeat_sec` for why this is >1 and why it is
# derived rather than separately configurable (phaze-w55w1).
_ANALYSIS_OUTER_HEARTBEAT_MULTIPLIER = 2


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

    # phaze-1g89i: the RAW (un-encoded) Redis AUTH password. Mirrors the SAME env var that
    # docker-compose.yml's `redis-server --requirepass "${REDIS_PASSWORD}"` and its
    # `redis-cli -a "${REDIS_PASSWORD}"` healthcheck consume verbatim -- both accept ANY byte
    # sequence, no URL parsing involved. `redis.asyncio.Redis.from_url(redis_url)`, by
    # contrast, RFC-3986-parses the DSN and percent-DECODES the userinfo: a password
    # containing `/`, `#`, or `?` used to break compose's raw string interpolation into
    # `redis://default:${REDIS_PASSWORD}@redis:6379/0` (truncated netloc -> `ValueError: Port
    # could not be cast to integer`), and one containing a `%XX` sequence parsed cleanly but
    # decoded to the WRONG bytes, so every AUTH silently sent the wrong password. Setting
    # REDIS_PASSWORD here (instead of pre-assembling the URL in compose) lets
    # `_apply_redis_password` below `urllib.parse.quote` it into `redis_url`'s userinfo AFTER
    # Python -- not a shell -- has the full, unmangled byte string.
    redis_password: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("REDIS_PASSWORD", "redis_password"),
        description="Raw Redis AUTH password; percent-encoded into redis_url by _apply_redis_password.",
    )

    @model_validator(mode="after")
    def _apply_redis_password(self) -> "BaseSettings":
        """phaze-1g89i: safely inject `redis_password` into `redis_url`'s userinfo.

        Runs whenever `redis_password` is set, regardless of what `redis_url` already
        contains -- mirroring the precedence compose's own interpolation used to have
        (`REDIS_URL=redis://default:${REDIS_PASSWORD}@redis:6379/0` always overrode the
        passwordless `.env` default). The password is percent-encoded with
        `safe=""` so every RFC-3986 reserved byte (`/`, `#`, `?`, `%`, `@`, `:`, ...) round-trips
        through `Redis.from_url`'s parser exactly, instead of corrupting the URL shape or
        silently decoding to different bytes.

        A `redis_url` that fails to parse (e.g. still carries a raw-embedded special-character
        password from an old-style `${REDIS_PASSWORD}` interpolation) is left untouched --
        this validator repairs the DSN going forward, it does not attempt to recover an
        already-mangled one.
        """
        if self.redis_password is None:
            return self
        parsed = urlparse(self.redis_url)
        if not parsed.hostname:
            return self
        user = parsed.username or "default"
        encoded_password = quote(self.redis_password.get_secret_value(), safe="")
        netloc = f"{quote(user, safe='')}:{encoded_password}@{parsed.hostname}"
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        self.redis_url = urlunparse(parsed._replace(netloc=netloc))
        return self

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
    # parse cleanly. The io lane is network-bound and runs off the CPU budget.
    #
    # phaze-rvcn: the ANALYZE lane's default is DERIVED, not the literal 4 it used to be.
    # It and the analyze process's TF intra-op cap are the two halves of one relationship
    # (`intra_op_threads x concurrency ~= physical_cores`) and were previously set in two
    # different files with two different literals -- `docker-compose.agent.yml` pinned
    # `TF_NUM_INTRAOP_THREADS=1` while this default said 4, giving a product of 4 on a host
    # with 8 physical cores AND parking the extractor on the 1-thread arm `phaze-7i0k` 5
    # measured at +210.6% wall for 0.001 GiB less than 4 threads. That is exactly the drift
    # `services/analysis_sizing.py` exists to make impossible: both halves now come out of
    # one `derive_sizing` call, so on the same 8-physical-core host the lane derives
    # concurrency 2 against an intra-op cap of 4 -- the same total core budget, spent where
    # the measurement says it is worth spending. `PHAZE_LANE_ANALYZE_CONCURRENCY` still
    # overrides it.
    lane_analyze_concurrency: int = Field(
        default_factory=lambda: derive_sizing().concurrency,
        validation_alias=AliasChoices("PHAZE_LANE_ANALYZE_CONCURRENCY", "lane_analyze_concurrency"),
        description="Concurrency of the analyze lane worker (process_file; essentia, CPU-bound). Defaults to the host-derived value.",
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
    # this true on all three lane workers; last_seen_at is inherently max(last_seen) across them and
    # the control plane keeps a per-lane depth breakdown. This REPLACES the former quick-260707-dh1
    # convention (true on exactly the analyze worker, false on the others): that pinned the
    # agent's whole liveness signal -- and its work-routing rank, via select_active_agent's
    # ORDER BY last_seen_at DESC -- to ONE process, so a stalled analyze worker marked the agent
    # DEAD and cost it routing while its other lanes were busy. Only the transitional
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
    # phaze-e57w: age bound past which a SAQ row stuck in status='aborting' is reaped -- deleted
    # so its deterministic key (e.g. process_file:<file_id>) is released and the file becomes
    # re-queueable. A legitimate abort completes in seconds (the worker calls finish(ABORTED)), so
    # a row still 'aborting' well past its OWN job's timeout is a zombie the owning worker will
    # never finalize. The bound is on `started` (frozen), NOT `touched` -- SAQ's sweeper bumps
    # `touched` on every abort->ABORTING pass (Queue.update), so a touched-based bound would never
    # trigger (spike phaze-qmc2.1).
    #
    # phaze-lqkz: this used to be a single ABSOLUTE bound (900s) compared directly against a row's
    # age. That is correct for the common case (jobs falling back to worker_job_timeout=600) but
    # wrong for any job enqueued with a LONGER explicit timeout -- at the time, process_file's
    # 7200s net -- because a timed-out process_file entered 'aborting' at age 7200s, already ~8x
    # past a 900s absolute bound, so the reaper's every-minute cron could steal the row
    # from a worker still mid-finalization (a live worker calling finish(ABORTED) needs up to
    # ~1-5s, not a full cron tick, but the OLD bound gave it a negative safety margin, not a small
    # positive one). The reaper now derives its bound PER ROW from that row's own serialized
    # `timeout` field (SAQ's `Job.to_dict` always includes it once a job's timeout differs from the
    # SAQ dataclass default -- true for every phaze producer via `apply_project_job_defaults`) plus
    # this SLACK, so a 7200s job gets a 7200s+slack grace window and a 600s job keeps the original
    # 900s-equivalent window when this stays at its default.
    #
    # phaze-w55w1: `process_file` no longer carries a wall-clock timeout at all (it runs
    # `timeout=0` + a progress `heartbeat`, see services/analysis_enqueue.py), so it is now one of
    # the `timeout: 0` rows tasks/_saq_reap.py deliberately EXCLUDES from both reapers. Its
    # liveness is owned by SAQ's own heartbeat-based `Job.stuck` sweep instead -- the same
    # division of labour `scan_directory` has had since phaze-mllxc.
    aborting_reap_slack_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices("PHAZE_ABORTING_REAP_SLACK_SECONDS", "aborting_reap_slack_seconds"),
        description="Seconds of grace ADDED ON TOP OF a job's own timeout before a row stuck in status='aborting' is reaped (deleted, releasing its deterministic key). The bound is per-job (job_timeout + this), not a single fixed value -- a job with no explicit timeout in its serialized blob falls back to the bare SAQ default (10s) plus this slack.",
    )

    # phaze-o0n6: the SIBLING knob, for the OTHER status outside SAQ's `_enqueue` overwrite allowlist.
    # 'active' blocks a deterministic key exactly as 'aborting' does, and `PostgresQueue._dequeue`
    # marks FAR more rows 'active' than a worker can run (it buffers them in-process), so a process
    # death strands every buffered row with nothing alive to finalize it -- 2,413 such rows against
    # worker concurrency 4 on the live deployment, 2,411 of them files never analyzed and
    # un-requeueable by any path. Same three guards as above (frozen `started`, per-row `timeout`,
    # status CAS -- all in tasks/_saq_reap.py); only the slack differs.
    #
    # WHY 900 AND NOT 300: 'aborting' is a POST-give-up status, 'active' is a LIVE one, so the cost of
    # being wrong is higher. A job whose work sits inside a C extension that does not yield to the
    # event loop cannot have asyncio.wait_for's cancellation land until the native call returns -- a
    # genuinely-running job can outlive its timeout by a bounded margin. This is that margin,
    # ADDITIVE on top of the row's own timeout. (Since phaze-w55w1 `process_file` is not the
    # motivating example any more: it runs `timeout=0` and is excluded from this reaper entirely --
    # see the sibling knob above -- but the reasoning still holds for every other long job.)
    active_reap_slack_seconds: int = Field(
        default=900,
        validation_alias=AliasChoices("PHAZE_ACTIVE_REAP_SLACK_SECONDS", "active_reap_slack_seconds"),
        description="Seconds of grace ADDED ON TOP OF a job's own timeout before a row stranded in status='active' is reaped (deleted, releasing its deterministic key; its scheduling_ledger row is KEPT -- that row is what recovery replays). The bound is per-job (job_timeout + this), not a single fixed value -- a job with no explicit timeout in its serialized blob falls back to the bare SAQ default (10s) plus this slack. Wider than the 'aborting' slack because 'active' is a live status (phaze-o0n6).",
    )

    # phaze-w55w1 (ADR-0007 §7): analysis liveness is PROGRESS-based, never wall-clock.
    #
    # This knob replaces `analysis_inner_timeout_sec` (6600s), which SIGKILLed the analysis child
    # at a fixed elapsed time, and it is the value the SAQ `process_file` job's own `heartbeat`
    # is set from (services/analysis_enqueue.py) so both supervising layers agree. Exhaustive
    # analysis means a multi-hour concert set legitimately runs for many hours; a wall clock
    # cannot tell that apart from a hang, and the repo has the incident (phaze-1b39, 2026-07-28)
    # where trying killed real work and stalled the whole burst lane. The child heartbeats every
    # window completion, chunk decode and model sweep; the supervisor kills only after this many
    # seconds of TOTAL SILENCE.
    #
    # Default 1800s (30 min) is sized against the longest legitimately silent stretch of an
    # exhaustive run -- one chunk's decode -- not against total analysis time. The worst case in
    # the corpus is a 12-hour file, whose full per-tier streaming decode measures ~631s
    # (docs/spikes/phaze-esut §8, remeasured for phaze-5lop); a chunk decode is bounded by that,
    # so 1800 leaves a ~3x margin. gt=0 (a 0 threshold would kill instantly); lt=86400 caps it at
    # a day so a misconfigured huge value cannot silently mean "no bound".
    #
    # On BaseSettings, not AgentSettings, because BOTH roles need it and must agree: the AGENT
    # arms the in-process watchdog from it (services/analysis_exec.py) and the CONTROL plane
    # derives the SAQ job's `heartbeat` from it at enqueue (services/analysis_enqueue.py). Two
    # separately-tuned values would let the outer net sweep a job the inner watchdog considers
    # healthy, which is precisely the inner-vs-outer skew Phase 43 had to reason about.
    analysis_stall_timeout_sec: int = Field(
        default=1800,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_ANALYSIS_STALL_TIMEOUT_SEC", "analysis_stall_timeout_sec"),
        description="Seconds of NO reported analysis progress before the analysis child is killed as stalled (phaze-w55w1). Bounds silence, never runtime: a progressing analysis is never killed by elapsed time. Default 1800 (30 min).",
    )

    @property
    def analysis_job_heartbeat_sec(self) -> int:
        """The SAQ ``process_file`` job heartbeat deadline -- the OUTER net, with real slack.

        **The outer deadline must be strictly greater than the inner stall threshold, and the
        two must not merely differ by rounding.** This restores, on the new mechanism, the
        inner-below-outer skew Phase 43 maintained deliberately (inner 6600 < outer 7200): the
        layer that can diagnose the failure has to reach it FIRST, so a kill is attributable
        instead of a race between two supervisors.

        Here the skew has to be wider than Phase 43's, because the two layers no longer measure
        the same signal. The inner watchdog (``services/analysis_exec.py``) resets on ANY child
        output -- protocol lines and essentia's stderr banners alike. The outer deadline resets
        only when the lane relays a touch, which is a strictly NARROWER signal and is also
        best-effort (a dropped ``job.update()`` is swallowed) and throttled. Equal deadlines
        would therefore let SAQ sweep, abort and blind-retry a multi-hour analysis the inner
        watchdog is correctly holding healthy -- a *whole file's* work discarded because the
        broker's view of liveness is coarser than the child's.

        ``2x`` gives the inner watchdog a full extra stall window to detect, kill, and let the
        lane report a real ``error_message`` before the broker forms an opinion. It is derived
        rather than separately configurable on purpose: an operator who raises the stall
        threshold must get the outer net raised with it, and an independent knob is exactly how
        that invariant gets violated in a hurry at 3am.
        """
        return self.analysis_stall_timeout_sec * _ANALYSIS_OUTER_HEARTBEAT_MULTIPLIER

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

    # phaze-fq9h.1: 1001Tracklists DETAIL pages (unlike search) deliver their track listing via JS
    # behind a Cloudflare Turnstile widget, so they are rendered by a real browser
    # (services/tracklist_render.py) rather than fetched with httpx. Spike phaze-dmvs measured the
    # constraints these defaults encode: HEADFUL is mandatory (headless fails the interstitial
    # outright) and Turnstile is FLAKY rather than deterministic -- ~6/8 pages cleared on first
    # navigation -- so a bounded reload/retry loop is the difference between a usable yield and
    # a quarter of the corpus silently unreachable.
    tracklist_render_browser_channel: str = Field(
        default="chrome",
        validation_alias=AliasChoices("PHAZE_TRACKLIST_RENDER_BROWSER_CHANNEL", "tracklist_render_browser_channel"),
        description=(
            "Patchright browser channel for the 1001Tracklists renderer. 'chrome' uses a real installed Google Chrome "
            "(Patchright's most convincing configuration); empty string falls back to Patchright's bundled patched Chromium."
        ),
    )
    tracklist_render_turnstile_attempts: int = Field(
        default=4,
        ge=1,
        le=10,
        validation_alias=AliasChoices("PHAZE_TRACKLIST_RENDER_TURNSTILE_ATTEMPTS", "tracklist_render_turnstile_attempts"),
        description=(
            "Hard cap on navigations per detail page when Turnstile keeps serving its interstitial (phaze-fq9h.1). "
            "Every attempt spends one whole-host request from the crawl-delay budget, so this is a politeness bound as much as a timeout."
        ),
    )
    tracklist_render_page_timeout_seconds: float = Field(
        default=90.0,
        gt=0,
        validation_alias=AliasChoices("PHAZE_TRACKLIST_RENDER_PAGE_TIMEOUT_SECONDS", "tracklist_render_page_timeout_seconds"),
        description=(
            "Hard wall-clock ceiling for rendering ONE detail page, covering every retry attempt and the pacing waits between them. "
            "A hung browser page must never stall a months-long drain (phaze-fq9h.1)."
        ),
    )
    tracklist_render_selector_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        validation_alias=AliasChoices("PHAZE_TRACKLIST_RENDER_SELECTOR_TIMEOUT_SECONDS", "tracklist_render_selector_timeout_seconds"),
        description="Per-attempt wait for the track container to appear before the attempt is judged interstitial-or-empty (phaze-fq9h.1).",
    )
    tracklist_render_retry_backoff_seconds: float = Field(
        default=5.0,
        ge=0,
        validation_alias=AliasChoices("PHAZE_TRACKLIST_RENDER_RETRY_BACKOFF_SECONDS", "tracklist_render_retry_backoff_seconds"),
        description=(
            "Base for the exponential backoff added ON TOP of the shared crawl-delay pacing between Turnstile retries. "
            "Zero disables the extra backoff; the crawl-delay floor still applies (phaze-fq9h.1)."
        ),
    )
    tracklist_render_xvfb: Literal["auto", "always", "never"] = Field(
        default="auto",
        validation_alias=AliasChoices("PHAZE_TRACKLIST_RENDER_XVFB", "tracklist_render_xvfb"),
        description=(
            "Whether to start an Xvfb virtual display for the headful browser. 'auto' starts one only on Linux with no DISPLAY "
            "already set -- i.e. exactly the headless-worker case (phaze-fq9h.1/phaze-fq9h.5)."
        ),
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
        """Enforce whole-registry invariants the per-variant submodels can't see (REG-04/05, D-04/D-08/D-09).

        Cross-entry checks, in order:
          * A resolved-empty registry (present-but-empty `backends = []`) fails fast rather than
            booting with no backend — the Phase-30 silent-wedge failure mode (REG-04, Pitfall 2).
          * Duplicate `[[buckets]]` ids fail fast (REG-05).
          * Duplicate `[[backends]]` ids fail fast, kind-agnostic (phaze-1sgee; see
            `validate_unique_registry_ids` in `config_registry_policies.py` for why).
          * Duplicate non-null compute `agent_ref` values fail fast without checking agent existence
            (D-04/D-05).
          * Each KueueBackend's `buckets` id-list must resolve against `self.buckets`: an unknown id
            (D-08) or an empty resolved set (D-08) fails fast, naming the offending backend id.
          * A `scope="cluster-specific"` bucket referenced by >1 kueue backend fails fast, naming the
            bucket id — the sharing-cardinality invariant (D-09). `scope="shared"` may be referenced
            by many.
        """
        validate_non_empty_registry(self.backends)
        validate_unique_registry_ids(self.backends, self.buckets)
        validate_unique_compute_agent_refs(self.backends)
        cluster_specific_refs = validate_backend_bucket_lists(self.backends, self.buckets)
        validate_cluster_specific_sharing(cluster_specific_refs)
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
    # phaze-ceuvd: bounded gt=0, matching the sibling knobs in this class (e.g.
    # cloud_route_threshold_sec above). Used as a range() step in
    # get_proposal_pending_batches (services/pipeline.py); 0 raises ValueError at trigger
    # time and a negative value silently collapses the pending set to zero batches (both
    # would previously boot green and only detonate on GENERATE ALL).
    llm_batch_size: int = Field(default=10, gt=0)
    llm_max_companion_chars: int = 3000

    # ------------------------------------------------------------------------------------
    # phaze-5fta.4: corpus-learned date-order fallback for the rename-proposal path.
    #
    # ROLLOUT HISTORY (epic phaze-5fta DESIGN note 5). `convention_date_fallback_enabled` gates the
    # WHOLE capability, not just the store lookup: with it off the proposal path adds nothing to the
    # LLM context and nothing to the stored `context_used`, so proposals are byte-identical to the
    # pre-phaze-5fta behavior. It shipped fail-closed for exactly that reason.
    #
    # phaze-5fta.5 has now run the external validation and it PASSED: 130 ambiguous corpus files
    # were read under their group's learned convention and tested against published event dates
    # from five independent sources, giving 120 discriminating matches, 9 non-discriminating
    # matches (tokens like 07-07 that read the same either way) and 0 contradictions, spanning 12
    # release groups. So the epic's "NONE of them has been checked against anything outside the
    # archive" no longer holds.
    #
    # It now DEFAULTS ON, by deliberate operator decision (2026-08-04). The two questions the
    # validation kept separate were "are the derived dates correct" (settled: yes) and "may inferred
    # dates rewrite filenames on disk" (an operator call). The second is answered here, and it is
    # narrower than it sounds: a derived date reaches a rename PROPOSAL, never the filesystem. The
    # human-in-the-loop approval workflow still gates every move, and phaze-5fta.6's approval UI --
    # in this same epic -- renders each derived date as an inference with its supporting/
    # contradicting evidence rather than as a bare fact, so the reviewer sees what they are
    # approving. Set PHAZE_CONVENTION_DATE_FALLBACK_ENABLED=false to restore the fail-closed
    # behavior on any deployment.
    #
    # Residual risk accepted with this flip: 2 of the 10 admitted groups (142/0 and 131/0, both
    # unanimous, both clearing every bar) have no externally checkable dates at any effort -- their
    # ambiguous files are obscure internet-radio streams -- so they are applied on internal
    # consistency alone. The approval UI is the mitigation.
    convention_date_fallback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("PHAZE_CONVENTION_DATE_FALLBACK_ENABLED", "convention_date_fallback_enabled"),
        description=(
            "Enable the corpus-learned release-group date-order fallback in the rename-proposal path (phaze-5fta.4). "
            "DEFAULT ON -- phaze-5fta.5's external validation passed and the operator enabled it; derived dates reach "
            "rename proposals only, which the approval workflow still gates. Set false to restore fail-closed behavior."
        ),
    )
    # The EVIDENCE bar: how many self-resolving files must support a group's convention before it
    # may resolve any of that group's ambiguous ones.
    #
    # 50 is now the VALIDATED value, not the placeholder it started as -- phaze-5fta.5 confirmed it
    # against a full sweep of the live corpus (6,911 files carrying the NN-NN-YYYY shape, 4,480
    # self-resolving, 2,431 ambiguous, 98 groups) rather than accepting it by default. What the
    # sweep showed:
    #
    #   bar   groups admitted   ambiguous files resolved (of the 1,672 that land in ANY group)
    #    50        10                 1,419  (84.9%)
    #    25        14                 1,474  (88.2%)
    #    10        23                 1,564  (93.5%)
    #     1        75                 1,617  (96.7%)
    #
    # The tail is nearly worthless and expensive: dropping 50 -> 1 multiplies the number of trusted
    # conventions by 7.5x to buy 11.8 more points of coverage, and every group it adds rests on a
    # handful of files whose "unanimity" is indistinguishable from chance. 50 sits on the knee.
    # It is also the bar the external validation actually covers: the smallest EXTERNALLY validated
    # group has 57 supporting files, and no group below 50 has meaningful external evidence.
    convention_date_min_supporting: int = Field(
        default=50,
        ge=1,
        validation_alias=AliasChoices("PHAZE_CONVENTION_DATE_MIN_SUPPORTING", "convention_date_min_supporting"),
        description=(
            "Minimum supporting_count before a learned release-group date-order convention may resolve an ambiguous date "
            "(phaze-5fta.4). Validated at 50 by phaze-5fta.5 against the live corpus."
        ),
    )
    # The PURITY bar, compared against `filename_convention.confidence` -- which IS
    # supporting/(supporting+contradicting), derived by Postgres from the counts and impossible to
    # hand-set. Separate from the evidence bar on purpose: 1,000 supporting files do not make a
    # convention safe if 400 files contradict it, and 60/0 is a different object from 60/40 even
    # though both clear the count bar.
    #
    # RAISED 0.99 -> 1.0 by phaze-5fta.5, which is the one threshold that measurement changed. Two
    # reasons, both from the corpus sweep rather than from taste:
    #
    # 1. 0.99 IS NOT A RULE, IT IS A GROUP-SIZE TEST. A single contradicting file yields 0.9939 in
    #    a 165-file group (admitted), 0.9901 in a 101-file group (admitted) and 0.9836 in a 61-file
    #    group (rejected) -- the SAME evidence, "this uploader has written the other order at least
    #    once", passing or failing on how prolific the uploader is. Unanimity is the property the
    #    epic actually measured and the only one that means the same thing at every size.
    # 2. AT THIS EVIDENCE BAR, 0.99 WAS INERT. With min_supporting=50, 0.95 and 0.99 admit an
    #    identical 10 groups; only 1.0 changes the set. So the old default was not buying the
    #    safety its comment claimed -- the count bar was doing all the work.
    #
    # The cost is measured, not hypothetical: exactly one group (164 supporting / 1 contradicting)
    # falls out, taking 97 ambiguous files with it -- 1,419 -> 1,322 resolved, 84.9% -> 79.1% of
    # the grouped ambiguous corpus. Its lone contradicting file is a genuine day-first release
    # (the day component is > 12, so it cannot be read any other way), i.e. real proof that this
    # uploader sometimes encodes the other order, not an extractor artifact. An operator who
    # accepts that documented outlier can re-admit the group with PHAZE_CONVENTION_DATE_MIN_PURITY;
    # the default should not make that choice silently.
    convention_date_min_purity: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("PHAZE_CONVENTION_DATE_MIN_PURITY", "convention_date_min_purity"),
        description=(
            "Minimum filename_convention.confidence (supporting share of supporting+contradicting) before a learned "
            "date-order convention may resolve an ambiguous date (phaze-5fta.4). Raised to 1.0 (unanimity) by phaze-5fta.5."
        ),
    )

    # Phase 49 D-07: files whose joined FileMetadata.duration is at/above this threshold
    # are routed to a cloud compute agent (held in FileState.AWAITING_CLOUD) instead of the
    # on-prem file-server. The per-file router (Plan 02), backfill (Plan 03), and release
    # cron (Plan 04) all compare against this single knob. Bounded (gt=0, lt=86400) so an
    # out-of-range operator value fails fast at startup (T-49-01) and never reaches the SQL
    # `duration >= threshold` compare. Lives on ControlSettings because the control plane
    # owns routing decisions.
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
    # phaze-1q4g: how many times control re-drives a Job whose POD DIED WITH ITS NODE before giving up
    # on cloud for that file. A THIRD budget, deliberately separate from `cloud_submit_max_attempts`:
    # node loss is an infrastructure fault, so charging it to the file's analyze retry budget would be
    # wrong -- but "not charged" had silently become "not bounded", and one pathological file used that
    # to take the burst node down eight times over five days (spike phaze-wcrb §5). The default is
    # deliberately the TIGHTEST of the three: a node-loss re-drive is the single case most likely to
    # recur, because whatever killed the node is still there and the same file is about to meet it
    # again. 1 buys the genuinely-transient case (an unrelated reboot) one free retry and stops there.
    # Bounded (gt=0, lt=20) like its two siblings so a misconfig cannot re-open the unbounded loop.
    cloud_node_loss_max_redrives: int = Field(
        default=1,
        gt=0,
        lt=20,
        validation_alias=AliasChoices("PHAZE_CLOUD_NODE_LOSS_MAX_REDRIVES", "cloud_node_loss_max_redrives"),
        description=(
            "Max re-drives of a kube Job whose pod died WITH ITS NODE before the file spills to the local safety net (phaze-1q4g). "
            "Charged to cloud_job.node_loss_redrives, NOT to attempts. Default 1; bounded gt=0, lt=20."
        ),
    )
    # --- phaze-2mwyo: the DURABLE (cross-chain) cloud budget policy ----------------------------------
    #
    # The three knobs above bound ONE cloud chain. They cannot bound the NUMBER of chains, because they
    # are all read off the `cloud_job` sidecar -- and `routers/agent_analysis`'s D-14 reaper legitimately
    # DELETES that row on both analyze-terminal seams, so a file that spent its budget, spilled to local
    # and then failed locally came back with `attempts = 0` and a whole fresh budget. That is what made
    # cloud job 713a368e eight pods over five days: TWO chains of four, four days apart, not one runaway
    # (spike phaze-wcrb; audit in tasks/reconcile_cloud_jobs.py). The budget therefore also lives on a
    # durable per-file row (`models/cloud_budget.py`) the reaper does not touch, and THESE three knobs --
    # not the schema -- decide what that record means.
    #
    # Keeping the verdict in config is deliberate: phaze-6ck1 has not yet established whether the ~4
    # runaway files are intrinsically pathological (-> a permanent marker is right) or the victims of one
    # bad node (-> a cooldown is right), and this fix cannot wait for that answer. Changing the answer
    # later changes a default here. It does not change the schema.
    cloud_budget_cooldown_days: float = Field(
        default=14.0,
        ge=0,
        le=3650,
        validation_alias=AliasChoices("PHAZE_CLOUD_BUDGET_COOLDOWN_DAYS", "cloud_budget_cooldown_days"),
        description=(
            "Days a file is barred from CLOUD backends after a cloud chain burns out its budget (phaze-2mwyo). "
            "The primary policy: rate-limits fresh chains instead of banning files, and is SELF-CLEARING, so a file "
            "grounded by a transient node fault flies again without operator action. Default 14 -- comfortably above the "
            "4-day re-chain gap observed in the incident. 0 disables the cooldown; local is never barred either way."
        ),
    )
    # The intrinsic-cause backstop, sitting BEHIND the cooldown. A file that has burned this many WHOLE
    # cloud chains (~6 weeks of evidence at the default cooldown) has demonstrated something that is not
    # transient. Persistent, but not permanent: raising this knob (or setting it to 0) re-admits the file
    # with no row edited and no migration, which is the "clearable marker" option expressed as config.
    # Deliberately NOT a per-file DB flag -- see models/cloud_budget.py on evidence-vs-verdict.
    cloud_budget_max_chains: int = Field(
        default=3,
        ge=0,
        lt=100,
        validation_alias=AliasChoices("PHAZE_CLOUD_BUDGET_MAX_CHAINS", "cloud_budget_max_chains"),
        description=(
            "Lifetime ceiling on how many cloud chains one file may burn out before it stops being offered cloud at all "
            "(phaze-2mwyo). The intrinsic-cause backstop behind cloud_budget_cooldown_days. Default 3; 0 disables. "
            "Local (the guaranteed safety net) is never excluded, so this can ground a file but never strand it."
        ),
    )
    # The node-loss half of the lifetime ceiling, kept SEPARATE from cloud_budget_max_chains on purpose:
    # phaze-1q4g split `attempts` from `node_loss_redrives` on the cloud_job row so "kept failing
    # analysis" and "kept taking the node down" stay distinguishable, and that distinction matters MORE
    # once it is the durable record an operator reads weeks later. Looser than its per-chain sibling
    # (cloud_node_loss_max_redrives=1) because ADR-0005's pod memory limit turns a runaway into a
    # pod-scoped OOMKill rather than a node crash, so this path should be rare going forward -- rare
    # enough to be generous with, not rare enough to leave unbounded (which was phaze-1q4g's whole point).
    cloud_budget_max_node_loss: int = Field(
        default=3,
        ge=0,
        lt=100,
        validation_alias=AliasChoices("PHAZE_CLOUD_BUDGET_MAX_NODE_LOSS", "cloud_budget_max_node_loss"),
        description=(
            "Lifetime ceiling on node-loss re-drives charged to one file across ALL its cloud chains (phaze-2mwyo). "
            "Separate from cloud_budget_max_chains so repeated analysis failure and repeated node loss stay legible as "
            "different situations. Default 3; 0 disables."
        ),
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
    # phaze-j7m18: the compute-lane twin of the two STAGING bounds above. A compute ``cloud_job`` row's
    # ONLY in-flight status is SUBMITTED (D-08) -- it is terminalized ONLY by the ``/pushed`` agent HTTP
    # callback (``ComputeAgentBackend.reconcile`` is a documented no-op, §4.2/D-08). A dead fileserver
    # agent host mid-rsync, a lost ``push_file`` SAQ job after its retries exhaust while the agent is
    # down, or an enqueue failure in ``flush_pending_push_file_enqueues`` (its own docstring admits the
    # gap) all leave the row SUBMITTED forever with no callback ever coming -- permanently consuming a
    # compute lane cap slot (the exact "N/N busy with zero real workloads" failure phaze-ul2v fixed for
    # the staging half). Mirrors ``cloud_uploading_stale_after_sec``'s discipline exactly: the live
    # ``push_file:<file_id>`` broker key (``get_live_job_keys``) is the PRECISE live-vs-lost
    # discriminator and is checked FIRST; this bound is only the coarse backstop for when even that
    # broker row is gone. MUST comfortably exceed the largest real ``push_file_saq_timeout_sec`` net (a
    # multi-GB rsync push over a slow link is legitimately slow and bumps no timestamp while it
    # transfers) -- default 21600 (6h), same as the UPLOADING bound it mirrors. Bounded (gt=0, lt=604800)
    # so an out-of-range operator value fails fast at startup rather than reaping a live push.
    cloud_submitted_stale_after_sec: int = Field(
        default=21600,
        gt=0,
        lt=604800,
        validation_alias=AliasChoices("PHAZE_CLOUD_SUBMITTED_STALE_AFTER_SEC", "cloud_submitted_stale_after_sec"),
        description="Seconds a compute cloud_job may sit SUBMITTED with no live push_file broker key before the reconcile reaper spills it back to awaiting (phaze-j7m18). Default 21600 (6h); MUST exceed the largest push_file SAQ net.",
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

    # phaze-6nrrf: how long the continuous-drain CronJob (``tasks.tracklist_drain_control.
    # continue_armed_tracklist_drain``) waits after one ``drain_tracklists`` slice finishes before
    # enqueueing the next, WHILE ARMED. Named a "cooldown" rather than a rate limit because the
    # real host-politeness ceiling is enforced elsewhere (``services.tracklist_scraper.
    # reserve_host_request_slot``, ~1 req/8s for the whole system) and does not need a second,
    # independent limiter here (see ``services.tracklist_drain`` module docstring, "adds NO rate
    # limiting of its own"). This knob exists so the operator can widen the gap between slices
    # beyond what the host budget alone would impose -- e.g. to spread a months-long drain more
    # gently, or to leave headroom for other controller-queue work between slices -- without a
    # redeploy. Default 600 (10 min); bounded (gt=0, lt=86400) like this module's other duration
    # knobs so a misconfigured value fails fast at startup rather than reaching the cron.
    tracklist_drain_cooldown_sec: int = Field(
        default=600,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_TRACKLIST_DRAIN_COOLDOWN_SEC", "tracklist_drain_cooldown_sec"),
        description=(
            "Seconds the continuous-drain cron waits after one armed slice finishes before enqueueing the next "
            "(phaze-6nrrf). Operator-tunable pacing, distinct from the host politeness budget enforced in "
            "reserve_host_request_slot. Default 600 (10 min); bounded gt=0, lt=86400."
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

    # Phase 57.1 (PROG-01, D-04): the parent/loop-side throttle for the mid-flight analyze
    # progress POST. analyze_file fires its progress_cb per FINE window, but the lane bridge
    # (tasks/functions.py drainer + job_runner.py cb) collapses bursts to at most one POST per
    # this interval (monotonic-keyed) and always flushes the final count. Since phaze-w55w1
    # removed the window caps a long file can emit thousands of fine bumps, so this throttle now
    # matters for LONG files too, not just short/fast ones.
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
        # phaze-27myl: queue_url's shared-base default is the docker-compose service-name DSN
        # (`postgres:5432`, BaseSettings.queue_url above) — it only resolves on the APP-SERVER
        # compose network. docker-compose.agent.yml's own invariant is "No postgres or redis
        # service here", so on a file-server host that hostname never resolves. Unlike redis_url,
        # queue_url has no production-only validator to catch this (see
        # _enforce_redis_password_in_production), so it is the one shared-base field whose
        # docker-network default silently survives into an agent process. Fail fast here instead
        # of crash-looping every lane worker (tasks/agent_worker.py, the docker-compose.agent.yml
        # SAQ worker loop) at PostgresQueue connection time.
        #
        # Scoped to kind != "compute" like the scan_roots relaxation above: the k8s one-shot
        # analyze pod's entrypoint (job_runner.py) constructs AgentSettings and calls back over
        # HTTP directly -- it never imports tasks/agent_worker.py's PostgresQueue and never reads
        # queue_url, and its documented agent-env ConfigMap (docs/k8s-burst.md §6) does not carry
        # PHAZE_QUEUE_URL. Enforcing this for compute agents too would crash-loop every burst pod.
        if self.kind != "compute" and urlparse(self.queue_url).hostname == "postgres":
            raise ValueError(
                "PHAZE_QUEUE_URL is required when PHAZE_ROLE=agent — the default queue_url points at the "
                "docker-compose service name 'postgres', which does not resolve on an agent host "
                "(set PHAZE_QUEUE_URL to a Postgres DSN reachable from this host)"
            )
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
