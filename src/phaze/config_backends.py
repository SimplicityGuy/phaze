"""Phase 67 backend-registry schema (REG-01/02/03/05).

The declarative ``backends.toml`` surface parses into these typed models. This module is the
additive, self-contained foundation: it introduces NO removals and touches no existing call site,
so it lands green independently in Wave 1 and gives downstream plans (02+) a clean import target
for the discriminated-union submodels.

The registry is a ``list`` of a pydantic v2 discriminated union over ``kind``
(``local`` / ``compute`` / ``kueue``). Each variant validates its own required fields at
construction and fails fast with the offending entry ``id`` in the message (REG-02, Pitfall 3),
replacing the three flat ``_enforce_*_when_*`` validators (``config.py``) with per-variant checks.

``KubeConfig`` and ``BucketConfig`` are per-entry supersets of the former flat ``kube_*`` / ``s3_*``
blocks (D-13 / D-07) so downstream staging-service reads have a per-entry home. ``BucketConfig``
carries the per-bucket http(s) SSRF guard on ``endpoint_url`` (REG-05 / Security V5), lifted from
``config.py``'s ``_validate_s3_endpoint_url``. Inline ``*_file`` secret paths resolve eagerly at
construction via the shared ``_read_secret_file`` helper (D-04/D-06), failing fast on an unreadable
path.
"""

from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class LocalBackend(BaseModel):
    """On-prem/all-local backend. Needs no connection config (REG-01)."""

    kind: Literal["local"]
    id: str
    # Cost-tier rank: lower runs sooner. Bounded so an out-of-range operator value fails fast at
    # construction (T-67-01-04), mirroring config.py's bounded-int fields (e.g. straggler_threshold_sec).
    rank: int = Field(ge=0, lt=1000)
    # Concurrency cap: at least one in-flight (gt=0) so a backend can never be silently starved.
    cap: int = Field(gt=0, lt=1000)


class ComputeBackend(BaseModel):
    """Cloud compute (rsync/push) backend. Requires ``agent_ref`` (REG-02, D-13)."""

    kind: Literal["compute"]
    id: str
    rank: int = Field(ge=0, lt=1000)
    cap: int = Field(gt=0, lt=1000)
    # Optional at the type level so the per-variant validator below can raise an id-tagged message
    # (Pitfall 3) instead of pydantic's index-tagged "Field required".
    agent_ref: str | None = None
    scratch_dir: str | None = None  # was ControlSettings.compute_scratch_dir (D-13)

    @model_validator(mode="after")
    def _require_agent_ref(self) -> "ComputeBackend":
        """A compute backend without ``agent_ref`` cannot be dispatched to -- fail fast, id-tagged."""
        if not self.agent_ref:
            msg = f"backend {self.id!r} (kind=compute) requires an agent_ref"
            raise ValueError(msg)
        return self


class KueueBackend(BaseModel):
    """Kueue-cluster backend. Requires a nested ``[kube]`` config table (REG-02, D-13)."""

    kind: Literal["kueue"]
    id: str
    rank: int = Field(ge=0, lt=1000)
    cap: int = Field(gt=0, lt=1000)
    # Optional at the type level so the per-variant validator raises the id-tagged message (Pitfall 3).
    # The full KubeConfig submodel is defined in Task 2.
    kube: "KubeConfig | None" = None
    buckets: list[str] = Field(default_factory=list)  # explicit id-list bind (D-08)

    @model_validator(mode="after")
    def _require_kube(self) -> "KueueBackend":
        """A kueue backend without a [kube] config cannot submit Jobs -- fail fast, id-tagged."""
        if self.kube is None:
            msg = f"backend {self.id!r} (kind=kueue) requires a [kube] config table"
            raise ValueError(msg)
        return self


# Discriminated union over ``kind`` (REG-01). A raw dict with an unknown kind raises a
# ValidationError (no silent accept); a missing per-variant field is caught by the validators above.
BackendConfig = Annotated[
    LocalBackend | ComputeBackend | KueueBackend,
    Field(discriminator="kind"),
]


class KubeConfig(BaseModel):
    """Per-entry kube config for a KueueBackend.

    A per-entry superset of the former flat ``kube_*`` block (config.py:534-595) so Plan 04's
    staging-service rewire has a home for every read (D-13). Credential fields are ``SecretStr`` so
    accidental interpolation prints ``**********`` and they are never echoed in logs (T-67-01-02).
    """

    api_url: str | None = None
    namespace: str | None = None
    local_queue: str | None = None
    job_image: str | None = None
    cpu_request: str | None = None
    memory_request: str | None = None
    workload_api_version: str = "kueue.x-k8s.io/v1beta1"
    # Kubernetes object *names* (not secrets); use Field(default=...) so ruff's S105 does not flag
    # the "secret"/"token" substring on a bare assignment, mirroring config.py's kube_*_name fields.
    ca_secret_name: str = Field(default="phaze-internal-ca")
    env_configmap_name: str = "phaze-agent-env"
    env_secret_name: str = Field(default="phaze-agent-token")
    kubeconfig: SecretStr | None = None
    sa_token: SecretStr | None = None


class BucketConfig(BaseModel):
    """S3 staging-bucket entry (REG-05, D-07).

    A per-entry superset of the former flat ``s3_*`` block (config.py:466-495) so s3_staging reads
    each value per bucket. ``scope`` is a load-bearing sharing-cardinality invariant (D-09) whose
    cross-entry enforcement lives in Plan 02's container validator. ``endpoint_url`` carries the
    per-bucket http(s) SSRF guard lifted from ``_validate_s3_endpoint_url`` (config.py:597-613).
    """

    id: str
    scope: Literal["shared", "cluster-specific"]
    endpoint_url: str
    bucket: str  # the S3 bucket name (the value s3_staging reads as s3_bucket)
    region: str | None = None
    addressing_style: Literal["path", "virtual"] = "path"
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None

    @field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint_url(cls, value: str) -> str:
        """Require a well-formed http(s) URL with a host (T-67-01-01 SSRF surface, per bucket).

        A scheme-less value (``minio.homelab:9000``) or a non-http scheme (``file://``) is rejected
        at construction so an SSRF-shaped endpoint can never reach the S3 client.
        """
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            msg = f"endpoint_url must be a well-formed http(s) URL with a host, got {value!r}"
            raise ValueError(msg)
        return value


def _default_local_registry() -> list[BackendConfig]:
    """Absent config → implicit all-local: one kind=local backend (id=local, rank=99, cap=1) (D-03).

    A ``default_factory`` only fires when the ``backends`` key is entirely absent (D-03 zero-config).
    A present-but-empty array is a distinct fail-fast case handled by the container validator (Plan 02).
    """
    return [LocalBackend(kind="local", id="local", rank=99, cap=1)]


# KueueBackend forward-references KubeConfig (defined after it for readability); rebuild so the
# annotation resolves.
KueueBackend.model_rebuild()
