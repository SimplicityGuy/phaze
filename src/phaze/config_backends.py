"""Typed models for the declarative ``backends.toml`` registry.

The declarative ``backends.toml`` surface parses into these typed models. This module is the
The registry is a ``list`` of a Pydantic discriminated union over ``kind``
(``local`` / ``compute`` / ``kueue``). Each variant validates its own required fields at
construction and fails fast with the offending entry ``id`` in the message (REG-02, Pitfall 3),
``KubeConfig`` and ``BucketConfig`` keep staging values scoped per entry. ``BucketConfig``
validates the per-bucket HTTP(S) SSRF surface. Inline ``*_file`` secret paths resolve eagerly at
construction via the shared ``_read_secret_file`` helper (D-04/D-06), failing fast on an unreadable
path.
"""

from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, ValidationInfo, field_validator, model_validator

from phaze.services.analysis_sizing import derive_sizing
from phaze.services.k8s_quantity import QUANTITY_FIELDS, validate_quantity


# Chars that must never reach the ssh remote spec / rsync operand (whitespace + shell metacharacters).
# Keep in sync with ``PushFilePayload._DEST_HOST_FORBIDDEN`` in ``schemas/agent_tasks.py`` — both guard
# the same ssh-remote-spec surface: this one at config-load time (WR-03), that one at HTTP-payload time.
_PUSH_DEST_FORBIDDEN: frozenset[str] = frozenset(" \t\n\r;|&$`()<>")


def _read_secret_file(path: str, *, preserve_whitespace: bool) -> str:
    """Read an inline ``*_file`` secret path eagerly, applying the shared strip-vs-verbatim rule.

    The same rule applies to env-based secret resolution: key material is kept verbatim so its
    required trailing newline survives; tokens/access-keys are ``.strip()``ed so a heredoc/echo
    trailing newline hashes or parses identically to an operator-typed value.
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
    # construction (T-67-01-04), mirroring config.py's bounded-int fields (e.g. cloud_route_threshold_sec).
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
    # Optional here so the model validator can report the offending registry id.
    push_host: str | None = None
    # An omitted login user falls back to the fileserver's configured user.
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
        # These values land verbatim in an SSH remote spec, so reject separators at config load.
        if any(ch in _PUSH_DEST_FORBIDDEN for ch in self.push_host):
            raise ValueError(f"backend {self.id!r} (kind=compute) push_host must not contain whitespace or shell metacharacters")
        if self.ssh_user and any(ch in _PUSH_DEST_FORBIDDEN for ch in self.ssh_user):
            raise ValueError(f"backend {self.id!r} (kind=compute) ssh_user must not contain whitespace or shell metacharacters")
        # Match the payload guard before a CloudJob can be persisted without its push enqueue.
        if not PurePosixPath(self.scratch_dir).is_absolute():
            raise ValueError(f"backend {self.id!r} (kind=compute) scratch_dir must be an absolute path")
        if any(ch in _PUSH_DEST_FORBIDDEN for ch in self.scratch_dir):
            raise ValueError(f"backend {self.id!r} (kind=compute) scratch_dir must not contain whitespace or shell metacharacters")
        return self


class KueueBackend(BaseModel):
    """Kueue-cluster backend. Requires a nested ``[kube]`` config table (REG-02, D-13)."""

    kind: Literal["kueue"]
    id: str
    rank: int = Field(ge=0, lt=1000)
    cap: int = Field(gt=0, lt=1000)
    # Optional here so the model validator can report the offending registry id.
    kube: "KubeConfig | None" = None
    buckets: list[str] = Field(default_factory=list)  # explicit id-list bind (D-08)
    # Kueue submits Jobs rather than addressing an agent queue, so it has no ``agent_ref``.

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
    """Per-entry Kueue config whose credentials redact accidental interpolation."""

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
    # Optional operator-provisioned models PVC. A plain Kubernetes object name (a PersistentVolumeClaim,
    # like the ca/env object names above) -- NOT a secret, so NO SecretStr and NO `*_file` pointer. When
    # set, build_job_manifest mounts this claim read-only at the models dir (/models == PHAZE_MODELS_DIR)
    # so the analyze pod reads its essentia weights from operator-provisioned storage; phaze creates no
    # PV/PVC and references the claim by name only. Unset (None) -> no models volume/mount is emitted
    # (byte-identical manifest, current behavior). The PVC carries ONLY model weights -- never secrets/certs.
    models_pvc_name: str | None = None
    # docs/design/0005-analyze-job-memory-limits.md keeps this opt-in. When set, build_job_manifest emits
    # ``resources.limits.memory`` on the analyze container so a runaway pod is cgroup-OOMKilled
    # (a predictable, pod-scoped fault) instead of the kernel choosing a victim node-wide
    # (``constraint=CONSTRAINT_NONE``) -- the failure mode that killed coredns/metrics-server/
    # local-path-provisioner in production (spike phaze-esut). This is a KERNEL bound only: it is
    # NEVER read by Kueue's quota accounting, which reads ``memory_request`` exclusively and is
    # unaffected by this field (the design keeps requests authoritative). Unset (None, the default)
    # -> NO ``limits`` key is emitted at all -- the manifest is byte-identical to today's
    # (regression-guarded), the same backward-compatibility posture as ``models_pvc_name`` /
    # ``active_deadline_seconds``. Stays unset until a real Linux measurement (spike follow-up C)
    # calibrates a number; a guessed default risks OOMKilling legitimate work.
    memory_limit: str | None = None
    # None emits no wall-clock deadline. Pod-state reconciliation handles wedged Jobs because a
    # 2-6 h recording may run legitimately beyond 3 h; see docs/design/0007-windowed-analysis.md.
    active_deadline_seconds: Annotated[int, Field(gt=0)] | None = None
    kubeconfig: SecretStr | None = None
    sa_token: SecretStr | None = None

    @field_validator(*QUANTITY_FIELDS)
    @classmethod
    def _validate_quantity_format(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Validate Quantity syntax without judging values or making optional fields required.

        Kubernetes schemas type a Quantity as a plain string, so schema validation cannot reject
        malformed values such as ``"4GB"``. ``None`` must pass through: an unset memory limit emits
        no limit, while required request fields get backend-specific errors later.
        """
        if value is None:
            return None
        return validate_quantity(value, field=info.field_name or "quantity")

    @model_validator(mode="before")
    @classmethod
    def _resolve_inline_secret_files(cls, data: Any) -> Any:
        """Resolve inline ``kubeconfig_file`` / ``sa_token_file`` paths before field validation (D-04)."""
        return _resolve_inline_secret_files(
            data,
            {
                # key material → verbatim (OpenSSH/kubeconfig parsers require the trailing newline)
                "kubeconfig_file": ("kubeconfig", True),
                # Bearer tokens are stripped by the same secret-normalization rule as core settings.
                "sa_token_file": ("sa_token", False),
            },
        )


class BucketConfig(BaseModel):
    """S3 staging-bucket entry (REG-05, D-07).

    ``scope`` is a load-bearing sharing-cardinality invariant enforced across entries.
    ``endpoint_url`` carries the per-bucket HTTP(S) SSRF guard.
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
    """Absent config → implicit all-local: one kind=local backend (id=local, rank=99) (D-03).

    A ``default_factory`` only fires when the ``backends`` key is entirely absent (D-03 zero-config).
    A present-but-empty array is a distinct fail-fast case handled by the container validator.

    ``derive_sizing`` chooses both concurrency and intra-op threads so their product tracks
    physical cores. It yields cap 1 on a 4-core host and 8 on a 32-core host.
    """
    return [LocalBackend(kind="local", id="local", rank=99, cap=derive_sizing().concurrency)]


# KueueBackend forward-references KubeConfig (defined after it for readability); rebuild so the
# annotation resolves.
KueueBackend.model_rebuild()
