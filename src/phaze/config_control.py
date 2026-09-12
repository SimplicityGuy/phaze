"""Control-plane Pydantic settings domain behind the phaze.config facade."""

from pathlib import Path
import tomllib
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, model_validator
import structlog

from phaze.config_backends import BackendConfig, BucketConfig, _default_local_registry
from phaze.config_base import BaseSettings
from phaze.config_registry_policies import (
    validate_backend_bucket_lists,
    validate_cluster_specific_sharing,
    validate_non_empty_registry,
    validate_unique_compute_agent_refs,
    validate_unique_registry_ids,
)
from phaze.config_secrets import _resolution_env


logger = structlog.get_logger("phaze.config")


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


# Keep the supported class identity rooted at the public facade even though implementation
# ownership lives here.
ControlSettings.__module__ = "phaze.config"
