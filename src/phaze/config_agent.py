"""Agent-plane Pydantic settings domain behind the phaze.config facade."""

from typing import Annotated, ClassVar, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import NoDecode

from phaze.config_base import BaseSettings


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


# Keep the supported class identity rooted at the public facade even though implementation
# ownership lives here.
AgentSettings.__module__ = "phaze.config"
