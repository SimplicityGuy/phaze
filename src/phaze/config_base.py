"""Shared Pydantic settings domain used by every Phaze process role."""

from typing import Any, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

from phaze.config_redis import RedisPasswordSettingsMixin
from phaze.config_secrets import SecretFileSettingsMixin
from phaze.services.analysis_sizing import derive_sizing


# How much slack the outer SAQ job heartbeat gets over the inner stall threshold.
# BaseSettings.analysis_job_heartbeat_sec documents why this is derived (phaze-w55w1).
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


# This class is implemented in a cohesive internal module but remains publicly identified by
# the facade for pickling, framework introspection, and existing type/error representations.
BaseSettings.__module__ = "phaze.config"
