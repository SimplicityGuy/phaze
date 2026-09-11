"""Role-specific Pydantic settings selected once at process startup."""

from enum import StrEnum
from functools import lru_cache
import os
from pathlib import Path
import tomllib
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import NoDecode, SettingsConfigDict
import structlog

from phaze.config_backends import (
    BackendConfig,
    BucketConfig,
    _default_local_registry,
)
from phaze.config_redis import RedisPasswordSettingsMixin
from phaze.config_registry_policies import (
    validate_backend_bucket_lists,
    validate_cluster_specific_sharing,
    validate_non_empty_registry,
    validate_unique_compute_agent_refs,
    validate_unique_registry_ids,
)
from phaze.config_secrets import SecretFileSettingsMixin, _resolution_env
from phaze.services.analysis_sizing import derive_sizing


logger = structlog.get_logger(__name__)


class Role(StrEnum):
    """v4.0 role selector. Controller = application server (fileless tasks); Agent = file server (file-bound tasks)."""

    CONTROL = "control"
    AGENT = "agent"


# How much slack the OUTER (SAQ job heartbeat) analysis deadline gets over the INNER stall
# threshold. See `BaseSettings.analysis_job_heartbeat_sec` for why this is >1 and why it is
# derived rather than separately configurable (phaze-w55w1).
_ANALYSIS_OUTER_HEARTBEAT_MULTIPLIER = 2


class BaseSettings(SecretFileSettingsMixin, RedisPasswordSettingsMixin):
    """Fields shared by both roles.

    Secret and Redis behavior remains in mixins so Pydantic preserves inherited field
    resolution and validator ordering.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Keep the documented PHAZE_DATABASE_URL alias alongside the legacy bare name.
    database_url: str = Field(
        default="postgresql+asyncpg://phaze:phaze@postgres:5432/phaze",
        validation_alias=AliasChoices("PHAZE_DATABASE_URL", "DATABASE_URL", "database_url"),
    )

    # PostgresQueue needs a raw libpq DSN; the before-validator accepts either SQLAlchemy
    # dialect form. This credential-bearing value is resolved through SECRET_FILE_FIELDS.
    queue_url: str = Field(
        default="postgresql://phaze:phaze@postgres:5432/phaze",
        validation_alias=AliasChoices("PHAZE_QUEUE_URL", "queue_url"),
        description="psycopg3 (libpq) DSN for the PostgresQueue broker (Phase 36).",
    )

    @field_validator("queue_url", mode="before")
    @classmethod
    def _strip_sqlalchemy_driver(cls, value: Any) -> Any:
        """Normalize a SQLAlchemy dialect DSN to the raw libpq form psycopg3 accepts."""
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

    # scan_directory has no wall-clock timeout, so its reaper measures progress silence.
    # The UI warns at half of this 24h default; see docs/configuration.md.
    scan_stall_seconds: int = Field(
        default=86400,
        validation_alias=AliasChoices("PHAZE_SCAN_STALL_SECONDS", "SCAN_STALL_SECONDS", "scan_stall_seconds"),
        description="Seconds with no progress before a RUNNING scan is reaped as stalled (default 24h).",
    )

    # Audio analysis models
    models_path: str = "/models"

    # File execution output
    output_path: str = "/data/output"

    worker_max_jobs: int = 8
    # Derive analyze concurrency and TensorFlow intra-op threads together so their product
    # tracks physical cores. The measured sizing rationale remains in docs/configuration.md.
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
    # Every named lane heartbeats independently; the unlaned drain worker opts out so it cannot
    # erase the per-lane depth breakdown. See docs/configuration.md.
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
    # Reaping uses each row's frozen ``started`` time and serialized timeout, not ``touched``;
    # timeout-free heartbeat jobs are excluded. See docs/configuration.md for the measured bounds.
    aborting_reap_slack_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices("PHAZE_ABORTING_REAP_SLACK_SECONDS", "aborting_reap_slack_seconds"),
        description="Seconds of grace ADDED ON TOP OF a job's own timeout before a row stuck in status='aborting' is reaped (deleted, releasing its deterministic key). The bound is per-job (job_timeout + this), not a single fixed value -- a job with no explicit timeout in its serialized blob falls back to the bare SAQ default (10s) plus this slack.",
    )

    # Active rows get more slack than post-give-up aborting rows because native work can outlive
    # asyncio cancellation. The 2,413 stranded-row measurement remains in docs/configuration.md.
    active_reap_slack_seconds: int = Field(
        default=900,
        validation_alias=AliasChoices("PHAZE_ACTIVE_REAP_SLACK_SECONDS", "active_reap_slack_seconds"),
        description="Seconds of grace ADDED ON TOP OF a job's own timeout before a row stranded in status='active' is reaped (deleted, releasing its deterministic key; its scheduling_ledger row is KEPT -- that row is what recovery replays). The bound is per-job (job_timeout + this), not a single fixed value -- a job with no explicit timeout in its serialized blob falls back to the bare SAQ default (10s) plus this slack. Wider than the 'aborting' slack because 'active' is a live status (phaze-o0n6).",
    )

    # Both roles share one progress-silence bound: the agent arms the watchdog and control derives
    # the outer heartbeat from it. See docs/design/0007-windowed-analysis.md for the 1,800 s bound,
    # 631 s measured decode, and ~3x margin.
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

        The inner watchdog sees any child output; the outer broker sees only best-effort,
        throttled touches. A derived ``2x`` deadline gives the inner layer one full stall window
        to diagnose and report before the broker aborts work.
        """
        return self.analysis_stall_timeout_sec * _ANALYSIS_OUTER_HEARTBEAT_MULTIPLIER

    # Session-mode PgBouncer pins upstream connections, so both control engines and the per-lane
    # dispatch queues share conservative pool defaults. See docs/configuration.md.
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

    # Internal agent API
    agent_token_prefix: str = "phaze_agent_"  # noqa: S105
    agent_file_chunk_max: int = 1000

    # Production deployments may disable startup migration for a maintenance window.
    auto_migrate: bool = Field(
        default=True,
        validation_alias=AliasChoices("PHAZE_AUTO_MIGRATE", "auto_migrate"),
        description="Run `alembic upgrade head` in the api lifespan startup.",
    )

    # Only the API role acts on this shared setting.
    enable_saq_ui: bool = Field(
        default=True,
        validation_alias=AliasChoices("PHAZE_ENABLE_SAQ_UI", "enable_saq_ui"),
        description="Mount the SAQ monitoring dashboard at /saq in the API (Phase 33).",
    )

    # The default SANs cover local development and Compose service-name DNS.
    api_tls_sans: str = Field(
        default="localhost,127.0.0.1,api",
        validation_alias=AliasChoices("PHAZE_API_TLS_SANS", "api_tls_sans"),
        description="Comma-separated SAN list for the auto-generated leaf cert (Phase 29 D-02).",
    )

    # An explicit token makes a development seed reproducible; otherwise startup generates one.
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

    # Control-wide secrets use env ``<VAR>_FILE`` resolution; backend-specific secrets use
    # inline ``*_file`` pointers in backends.toml.
    SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = BaseSettings.SECRET_FILE_FIELDS | {
        "openai_api_key",
        "anthropic_api_key",
    }

    # The registry is TOML-only. An absent file gets the implicit local backend, while an explicit
    # empty registry fails validation; see docs/configuration.md.
    backends: list[BackendConfig] = Field(default_factory=_default_local_registry)
    buckets: list[BucketConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _load_backend_registry(cls, data: Any) -> Any:
        """Load the TOML-only registries, preserving the absent-file local default."""
        if not isinstance(data, dict):
            return data
        # Resolve through the shared environment map so process env keeps precedence over ``.env``.
        path = _resolution_env(cls.model_config).get("PHAZE_BACKENDS_CONFIG_FILE", "/etc/phaze/backends.toml")
        toml_path = Path(path)
        if not toml_path.exists():
            return data
        with toml_path.open("rb") as handle:
            parsed = tomllib.load(handle)
        # A present file is authoritative, including an empty backends list that validation rejects.
        data["backends"] = parsed.get("backends", [])
        data["buckets"] = parsed.get("buckets", [])
        return data

    @model_validator(mode="after")
    def _validate_registry(self) -> "ControlSettings":
        """Enforce whole-registry invariants the per-variant submodels can't see (REG-04/05, D-04/D-08/D-09).

        Cross-entry checks, in order:
          * A resolved-empty registry (present-but-empty `backends = []`) fails fast rather than
            booting with no backend (REG-04, Pitfall 2).
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

        Mirrors `AgentSettings._enforce_redis_password_in_production`: `production`
        fails fast at construction time; `dev` (the default)
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

        The implicit-local registry returns false; any compute or Kueue backend returns true.
        """
        return any(backend.kind != "local" for backend in self.backends)

    def log_effective_registry(self) -> None:
        """Emit a secret-free id/kind/rank/cap projection of the resolved registry at startup (REG-04, Pitfall 5).

        Logs ONLY the ``{id, kind, rank, cap}`` projection per backend — never a whole backend/bucket
        model, a ``SecretStr``, or a ``*_file`` mount path, so secret material cannot leak into logs.
        """
        projection = [{"id": backend.id, "kind": backend.kind, "rank": backend.rank, "cap": backend.cap} for backend in self.backends]
        logger.info("phaze.config effective backend registry", backends=projection, cloud_enabled=self.cloud_enabled)

    # Discogsography
    discogs_match_concurrency: int = 5

    control_env: Literal["dev", "production"] = Field(
        default="dev",
        validation_alias=AliasChoices("PHAZE_CONTROL_ENV", "control_env"),
        description="Deployment mode. Production refuses passwordless Redis URLs (phaze-hti8, mirrors agent_env D-06).",
    )

    # LLM API keys + config
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

    # The fallback contributes only proposal context; human approval still gates filesystem writes.
    # Its validation covered 130 ambiguous files from 12 groups: 120 discriminating matches,
    # 9 non-discriminating matches, and 0 contradictions. See docs/configuration.md.
    convention_date_fallback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("PHAZE_CONVENTION_DATE_FALLBACK_ENABLED", "convention_date_fallback_enabled"),
        description=(
            "Enable the corpus-learned release-group date-order fallback in the rename-proposal path (phaze-5fta.4). "
            "DEFAULT ON -- phaze-5fta.5's external validation passed and the operator enabled it; derived dates reach "
            "rename proposals only, which the approval workflow still gates. Set false to restore fail-closed behavior."
        ),
    )
    # A 6,911-file sweep measured 4,480 self-resolving and 2,431 ambiguous names across 98 groups.
    #   bar   groups admitted   ambiguous files resolved (of the 1,672 that land in ANY group)
    #    50        10                 1,419  (84.9%)
    #    25        14                 1,474  (88.2%)
    #    10        23                 1,564  (93.5%)
    #     1        75                 1,617  (96.7%)

    # The 50-file knee keeps 84.9% coverage; dropping to 1 adds 11.8 points while multiplying
    # trusted groups by 7.5x. The smallest externally validated group has 57 supporting files.
    convention_date_min_supporting: int = Field(
        default=50,
        ge=1,
        validation_alias=AliasChoices("PHAZE_CONVENTION_DATE_MIN_SUPPORTING", "convention_date_min_supporting"),
        description=(
            "Minimum supporting_count before a learned release-group date-order convention may resolve an ambiguous date "
            "(phaze-5fta.4). Validated at 50 by phaze-5fta.5 against the live corpus."
        ),
    )
    # Purity stays separate from evidence count so one contradiction means the same at every group
    # size. Requiring 1.0 excludes one 164/1 group and 97 ambiguous files: coverage moves from
    # 1,419 to 1,322 files (84.9% to 79.1%).
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

    # Control owns the single threshold used by initial routing, backfill, and release paths.
    cloud_route_threshold_sec: int = Field(
        default=5400,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_CLOUD_ROUTE_THRESHOLD_SEC", "cloud_route_threshold_sec"),
        description="Duration threshold (seconds) at/above which a file is routed to a cloud compute agent for analysis (Phase 49). Default 5400 (90 min); lt=86400 caps it at one day.",
    )

    # Push and submit failures spend distinct bounded budgets; see docs/configuration.md.
    push_max_attempts: int = Field(
        default=3,
        gt=0,
        lt=20,
        validation_alias=AliasChoices("PHAZE_PUSH_MAX_ATTEMPTS", "push_max_attempts"),
        description="Max push re-drives of a sha256-mismatched file before it spills back to AWAITING_CLOUD to fall to local (Phase 50 D-12, Phase 69 SCHED-03). Default 3; bounded gt=0, lt=20.",
    )
    cloud_submit_max_attempts: int = Field(
        default=3,
        gt=0,
        lt=20,
        validation_alias=AliasChoices("PHAZE_CLOUD_SUBMIT_MAX_ATTEMPTS", "cloud_submit_max_attempts"),
        description="Max kube Job submit attempts before a file is marked ANALYSIS_FAILED (Phase 54, D-08). A distinct budget from push_max_attempts. Default 3; bounded gt=0, lt=20.",
    )
    # Node loss has a third, tighter budget: the measured failure recurred 8 times over 5 days,
    # while a default of 1 still tolerates one unrelated reboot.
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
    # Per-file evidence survives cloud_job cleanup; configuration turns it into the cross-chain
    # verdict. The measured case formed 2 chains of 4 pods, 4 days apart. See docs/configuration.md.
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
    # This clearable policy backstop represents roughly 6 weeks at the default cooldown.
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
    # Keep lifetime node loss distinct from analysis-chain exhaustion. Memory limits make recurrence
    # pod-scoped, so this ceiling is looser than the per-chain value but remains bounded.
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
    # Full cloud backends wait for this threshold; offline backends spill to local immediately.
    cloud_spill_to_local_after_seconds: int = Field(
        default=900,
        gt=0,
        lt=86400,
        validation_alias=AliasChoices("PHAZE_CLOUD_SPILL_TO_LOCAL_AFTER_SECONDS", "cloud_spill_to_local_after_seconds"),
        description="Seconds a long file waits in AWAITING_CLOUD while higher-rank backends are FULL before slow local becomes an eligible spill target (Phase 69, D-02). Default 900 (15 min); offline backends spill immediately (D-03).",
    )
    # Callback completion is primary; this 21,600 s (6 h) safety net must exceed the largest
    # multipart-upload timeout so reconciliation cannot reap a live transfer.
    cloud_uploading_stale_after_sec: int = Field(
        default=21600,
        gt=0,
        lt=604800,
        validation_alias=AliasChoices("PHAZE_CLOUD_UPLOADING_STALE_AFTER_SEC", "cloud_uploading_stale_after_sec"),
        description="Seconds a cloud_job may sit UPLOADING with no timestamp movement before the reconcile reaper spills it back to awaiting (phaze-ul2v). Default 21600 (6h); MUST exceed the largest s3_upload SAQ net.",
    )
    # UPLOADED should advance within one transactionally enqueued controller hop, so its 900 s
    # (15 min) lost-submit bound is much tighter than the live-transfer bound.
    cloud_uploaded_stale_after_sec: int = Field(
        default=900,
        gt=0,
        lt=604800,
        validation_alias=AliasChoices("PHAZE_CLOUD_UPLOADED_STALE_AFTER_SEC", "cloud_uploaded_stale_after_sec"),
        description="Seconds a cloud_job may sit UPLOADED with no submit enqueued before the reconcile reaper spills it back to awaiting (phaze-ul2v). Default 900 (15 min).",
    )
    # A live broker key is the precise SUBMITTED liveness signal. Only after it disappears may this
    # 21,600 s (6 h) coarse backstop recover a lost push callback.
    cloud_submitted_stale_after_sec: int = Field(
        default=21600,
        gt=0,
        lt=604800,
        validation_alias=AliasChoices("PHAZE_CLOUD_SUBMITTED_STALE_AFTER_SEC", "cloud_submitted_stale_after_sec"),
        description="Seconds a compute cloud_job may sit SUBMITTED with no live push_file broker key before the reconcile reaper spills it back to awaiting (phaze-j7m18). Default 21600 (6h); MUST exceed the largest push_file SAQ net.",
    )
    # Connection and credential data is per backend; only global S3 tuning remains here.
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

    # This cooldown leaves room between slices; the independent whole-host ceiling remains
    # approximately 1 request per 8 s in ``reserve_host_request_slot``.
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

    # The shared resolver handles the bearer token and SSH files; never log their values.
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
    # D-06: deployment-mode selector. `production` triggers the
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
    # CLI choices and the database CHECK bracket this config-layer kind constraint.
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

    # The monotonic throttle collapses per-window progress bursts but always flushes the final count.
    analysis_progress_interval_sec: float = Field(
        default=5.0,
        ge=0.0,
        validation_alias=AliasChoices("PHAZE_ANALYSIS_PROGRESS_INTERVAL_SEC", "analysis_progress_interval_sec"),
        description="Minimum seconds between mid-flight analyze-progress POSTs (Phase 57.1 D-04). The final count is always flushed regardless; 0 disables throttling.",
    )

    agent_ca_file: str = Field(
        default="/certs/phaze-ca.crt",
        validation_alias=AliasChoices("PHAZE_AGENT_CA_FILE", "agent_ca_file"),
        description="Path to the operator-distributed CA cert for verifying the app-server TLS endpoint (Phase 29 D-03).",
    )

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
        # Compute agents own no media, but every kind still authenticates over HTTP.
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
        """CR-01: production refuses non-HTTPS agent_api_url.

        HTTP would carry the bearer token in plaintext on the LAN.
        """
        if self.agent_env == "production" and not self.agent_api_url.lower().startswith("https://"):
            raise ValueError("agent_env=production requires https:// for agent_api_url (Phase 29 CR-01)")
        return self

    @model_validator(mode="after")
    def _enforce_redis_password_in_production(self) -> "AgentSettings":
        """D-06: production refuses passwordless redis_url.

        AUTH-03 pairs this client-side guard with server-side ``requirepass`` and
        LAN-bound port hardening. Together
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
    call lacks provider authentication.

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
        # Agent entry points call get_settings() directly; this legacy singleton stays Control-typed.
        return ControlSettings()
    return ControlSettings()


# Module-level singleton preserves back-compat with `from phaze.config import settings`.
# Typed as ControlSettings because the legacy `Settings` class was effectively the
# Control superset; the agent worker uses `get_settings()` / `AgentSettings()` directly.
settings: ControlSettings = _build_default_settings()

# Back-compat alias for callers that still import `Settings`. Some test files
# import the class directly (e.g., `from phaze.config import Settings`). Until those
# call sites migrate to `ControlSettings` / `AgentSettings` / `get_settings()`, the
# alias keeps them working — `Settings` resolves to `ControlSettings` (the superset
# that contains every field the old monolithic class had).
Settings = ControlSettings
