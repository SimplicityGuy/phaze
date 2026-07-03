"""Unit tests for the Phase 67 backend registry submodels (REG-01/02/05).

The registry replaces the single ``cloud_target`` Literal with a pydantic v2
discriminated union over ``kind`` (``local`` / ``compute`` / ``kueue``). Each
variant validates its own required fields at construction and fails fast with
the offending entry ``id`` in the message (REG-02, D-13). ``BucketConfig`` carries
the S3 staging surface with a per-bucket http(s) SSRF guard on ``endpoint_url``
(REG-05, D-07). These are pure model-construction tests -- no DB, no Redis.
"""

from __future__ import annotations

from pydantic import SecretStr, TypeAdapter, ValidationError
import pytest

from phaze.config_backends import (
    BackendConfig,
    BucketConfig,
    ComputeBackend,
    KubeConfig,
    KueueBackend,
    LocalBackend,
    _default_local_registry,
)


# Literal secret/name values pulled into module constants so ruff's S106 (hardcoded password in a
# call argument) does not fire on the model-construction kwargs; test files already ignore S105.
_CA_SECRET_NAME = "phaze-internal-ca"
_ENV_SECRET_NAME = "phaze-agent-token"
_SA_TOKEN = "tok"
_SECRET_ACCESS_KEY = "secret"


# --------------------------------------------------------------------------- #
# Task 1: discriminated-union submodels + per-variant fail-fast + factory
# --------------------------------------------------------------------------- #
def test_local_backend_parses() -> None:
    """A local entry needs id/kind/rank/cap only -- no connection config (REG-01)."""
    be = LocalBackend(kind="local", id="local", rank=99, cap=1)
    assert be.kind == "local"
    assert be.id == "local"
    assert be.rank == 99
    assert be.cap == 1


def test_compute_backend_parses() -> None:
    """A compute entry carries agent_ref + optional scratch_dir (D-13)."""
    be = ComputeBackend(kind="compute", id="compute-a1", rank=10, cap=2, agent_ref="a1-node", scratch_dir="/scratch")
    assert be.agent_ref == "a1-node"
    assert be.scratch_dir == "/scratch"


def test_compute_backend_missing_agent_ref_fails_fast_with_id() -> None:
    """A compute entry without agent_ref fails fast, message contains the entry id (REG-02)."""
    with pytest.raises(ValidationError, match=r"backend 'compute-x'"):
        ComputeBackend(kind="compute", id="compute-x", rank=10, cap=2)


def test_kueue_backend_parses() -> None:
    """A kueue entry carries a nested kube config + explicit bucket id-list (D-08/D-13)."""
    be = KueueBackend(kind="kueue", id="kueue-1", rank=5, cap=4, kube=KubeConfig(api_url="https://kube.example.com"), buckets=["b1"])
    assert be.kube is not None
    assert be.buckets == ["b1"]


def test_kueue_backend_missing_kube_fails_fast_with_id() -> None:
    """A kueue entry without a [kube] config fails fast, message contains the entry id (REG-02, D-13)."""
    with pytest.raises(ValidationError, match=r"backend 'kueue-nokube'"):
        KueueBackend(kind="kueue", id="kueue-nokube", rank=5, cap=4)


def test_rank_out_of_range_rejected() -> None:
    """A negative rank is rejected at construction (bounded field, T-67-01-04)."""
    with pytest.raises(ValidationError):
        LocalBackend(kind="local", id="local", rank=-1, cap=1)


def test_cap_out_of_range_rejected() -> None:
    """A zero cap is rejected at construction (cap must allow at least one in-flight, T-67-01-04)."""
    with pytest.raises(ValidationError):
        LocalBackend(kind="local", id="local", rank=99, cap=0)


def test_unknown_kind_rejected_by_union() -> None:
    """A raw dict with an unknown kind is a discriminated-union error -- no silent accept (REG-01)."""
    adapter: TypeAdapter[BackendConfig] = TypeAdapter(BackendConfig)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "bogus", "id": "x", "rank": 1, "cap": 1})


def test_union_dispatches_on_kind() -> None:
    """The union parses a raw local dict into a LocalBackend via the kind discriminator (REG-01)."""
    adapter: TypeAdapter[BackendConfig] = TypeAdapter(BackendConfig)
    be = adapter.validate_python({"kind": "local", "id": "local", "rank": 99, "cap": 1})
    assert isinstance(be, LocalBackend)


def test_default_local_registry_returns_single_rank99_local() -> None:
    """Absent config resolves via the factory to one kind=local backend (id=local, rank=99, cap=1) (D-03)."""
    registry = _default_local_registry()
    assert len(registry) == 1
    only = registry[0]
    assert isinstance(only, LocalBackend)
    assert only.id == "local"
    assert only.rank == 99
    assert only.cap == 1


# --------------------------------------------------------------------------- #
# Task 2: KubeConfig + BucketConfig submodels with per-bucket SSRF guard
# --------------------------------------------------------------------------- #
def test_bucket_config_parses() -> None:
    """A bucket entry carries id/scope/endpoint_url/bucket + optional region/addressing_style (REG-05, D-07)."""
    bucket = BucketConfig(
        id="b1",
        scope="shared",
        endpoint_url="https://minio.homelab:9000",
        bucket="phaze-staging",
        region="us-west-1",
    )
    assert bucket.scope == "shared"
    assert bucket.bucket == "phaze-staging"
    assert bucket.addressing_style == "path"


def test_bucket_scope_literal_rejects_unknown() -> None:
    """scope is a Literal of exactly {shared, cluster-specific} (D-09)."""
    with pytest.raises(ValidationError):
        BucketConfig(id="b1", scope="public", endpoint_url="https://minio.homelab:9000", bucket="phaze-staging")


def test_bucket_endpoint_url_schemeless_rejected() -> None:
    """A scheme-less endpoint_url is rejected at construction (per-bucket SSRF guard, REG-05 / V5, T-67-01-01)."""
    with pytest.raises(ValidationError, match=r"endpoint_url"):
        BucketConfig(id="b1", scope="shared", endpoint_url="minio.homelab:9000", bucket="phaze-staging")


def test_bucket_endpoint_url_non_http_scheme_rejected() -> None:
    """A non-http(s) scheme (file://) is rejected at construction (SSRF guard)."""
    with pytest.raises(ValidationError, match=r"endpoint_url"):
        BucketConfig(id="b1", scope="shared", endpoint_url="file:///etc/passwd", bucket="phaze-staging")


def test_kube_config_exposes_full_field_superset() -> None:
    """KubeConfig is a per-entry superset of the flat kube_* block so Plan 04 has a home for every read (D-13)."""
    kube = KubeConfig(
        api_url="https://kube.example.com",
        namespace="phaze",
        local_queue="phaze-lq",
        job_image="ghcr.io/phaze/agent:latest",
        cpu_request="2",
        memory_request="4Gi",
        ca_secret_name=_CA_SECRET_NAME,
        env_configmap_name="phaze-agent-env",
        env_secret_name=_ENV_SECRET_NAME,
    )
    assert kube.api_url == "https://kube.example.com"
    assert kube.namespace == "phaze"
    assert kube.local_queue == "phaze-lq"
    assert kube.job_image == "ghcr.io/phaze/agent:latest"
    assert kube.cpu_request == "2"
    assert kube.memory_request == "4Gi"
    # default retained from the flat field (config.py:564-568)
    assert kube.workload_api_version == "kueue.x-k8s.io/v1beta1"
    assert kube.ca_secret_name == _CA_SECRET_NAME
    assert kube.env_configmap_name == "phaze-agent-env"
    assert kube.env_secret_name == _ENV_SECRET_NAME


def test_kube_secret_fields_are_secretstr() -> None:
    """Resolved kube credentials are SecretStr so accidental interpolation prints ******** (T-67-01-02)."""
    kube = KubeConfig(api_url="https://kube.example.com", kubeconfig="apiVersion: v1\n", sa_token=_SA_TOKEN)
    assert isinstance(kube.kubeconfig, SecretStr)
    assert isinstance(kube.sa_token, SecretStr)
    assert "apiVersion" not in repr(kube.kubeconfig)


def test_bucket_secret_fields_are_secretstr() -> None:
    """Resolved bucket S3 credentials are SecretStr (T-67-01-02)."""
    bucket = BucketConfig(
        id="b1",
        scope="shared",
        endpoint_url="https://minio.homelab:9000",
        bucket="phaze-staging",
        access_key_id="AKIA",
        secret_access_key=_SECRET_ACCESS_KEY,
    )
    assert isinstance(bucket.access_key_id, SecretStr)
    assert isinstance(bucket.secret_access_key, SecretStr)
