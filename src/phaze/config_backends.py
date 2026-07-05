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

from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


def _read_secret_file(path: str, *, preserve_whitespace: bool) -> str:
    """Read an inline ``*_file`` secret path eagerly, applying the shared strip-vs-verbatim rule.

    This is the single whitespace rule Plan 02 also adopts in config.py's ``_resolve_secret_files``
    ("factor, don't fork", D-06): key material (kubeconfig / SSH-style keys) is kept verbatim so its
    required trailing newline survives; tokens/access-keys are ``.strip()``ed so a heredoc/echo
    trailing newline hashes/parses identically to an operator-typed value (mirrors config.py:143-145).
    An unreadable path fails fast with a ValueError naming the path (never echoing file contents).
    """
    try:
        contents = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"secret file {path!r} could not be read: {exc}"
        raise ValueError(msg) from exc
    return contents if preserve_whitespace else contents.strip()


def _resolve_inline_secret_files(data: Any, spec: dict[str, tuple[str, bool]]) -> Any:
    """Populate SecretStr fields from sibling inline ``*_file`` TOML paths (D-04/D-06).

    ``spec`` maps each ``*_file`` input key to ``(target_field, preserve_whitespace)``. This is the
    DISTINCT inline-TOML mechanism (Pitfall 4): the path is a TOML field VALUE, not an env
    ``<VAR>_FILE`` var, so it never touches the env-``_FILE`` field set or resolver in config.py. A
    directly-provided target value wins over its ``*_file`` sibling (mirrors config.py precedence).
    """
    if not isinstance(data, dict):
        return data
    for file_field, (target_field, preserve) in spec.items():
        if file_field not in data:
            continue
        path = data.pop(file_field)
        if data.get(target_field) is not None:
            continue  # a directly-provided value wins over the file pointer
        if path is None:
            continue
        data[target_field] = _read_secret_file(str(path), preserve_whitespace=preserve)
    return data


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
    # Phase 73 (D-01): the rsync/ssh push destination host. Optional at the type level (like agent_ref /
    # scratch_dir) so ``_require_dispatch_fields`` raises the id-tagged message rather than pydantic's
    # index-tagged "Field required". It later lands in the ssh remote spec (Plan 02), so it is required.
    # Registry-scoped mirror of the agent-side ``push_ssh_host`` (RESEARCH Open-Q3).
    push_host: str | None = None
    # Optional ssh login user for the push (D-01: "an optional ssh_user"); NO fail-fast -- an omitting
    # backend is valid and Plan 02 falls back to the fileserver's configured user.
    ssh_user: str | None = None

    @model_validator(mode="after")
    def _require_dispatch_fields(self) -> "ComputeBackend":
        """A compute backend needs all three dispatch fields -- fail fast, id-tagged.

        ``agent_ref`` names the node to dispatch to (REG-02). ``scratch_dir`` is the rsync push target
        the push pipeline interpolates per file (WR-01): without it, ``agent_push`` would build a literal
        ``"None/<file_id>.<ext>"`` scratch path and silently corrupt every push — so a missing
        ``scratch_dir`` fails construction here rather than at read time. ``push_host`` (D-01) is the ssh
        remote host the push targets; an absent value would build a ``"None:..."`` remote spec, so it
        fails construction the same id-tagged way. ``ssh_user`` stays optional (no clause).
        """
        if not self.agent_ref:
            raise ValueError(f"backend {self.id!r} (kind=compute) requires an agent_ref")
        if not self.scratch_dir:
            raise ValueError(f"backend {self.id!r} (kind=compute) requires a scratch_dir")
        if not self.push_host:
            raise ValueError(f"backend {self.id!r} (kind=compute) requires a push_host")
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
    # REG-05 per-cluster kubeconfig/context; selects a non-default context, defaults to
    # current-context when None (MKUE-01/A1). NOT a secret -- a plain kubeconfig context name.
    context: str | None = None
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

    @model_validator(mode="before")
    @classmethod
    def _resolve_inline_secret_files(cls, data: Any) -> Any:
        """Resolve inline ``kubeconfig_file`` / ``sa_token_file`` paths before field validation (D-04)."""
        return _resolve_inline_secret_files(
            data,
            {
                # key material → verbatim (OpenSSH/kubeconfig parsers require the trailing newline)
                "kubeconfig_file": ("kubeconfig", True),
                # bearer token → stripped (mirrors config.py:145)
                "sa_token_file": ("sa_token", False),
            },
        )


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

    @model_validator(mode="before")
    @classmethod
    def _resolve_inline_secret_files(cls, data: Any) -> Any:
        """Resolve inline ``access_key_id_file`` / ``secret_access_key_file`` paths (D-04)."""
        return _resolve_inline_secret_files(
            data,
            {
                # S3 access/secret keys → stripped (not key material)
                "access_key_id_file": ("access_key_id", False),
                "secret_access_key_file": ("secret_access_key", False),
            },
        )

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
