"""Order-sensitive characterization for ``ControlSettings`` registry policies.

Every case breaks two adjacent cross-entry invariants.  The exact first error proves the
``mode="after"`` validator still applies the established operator-facing policy order after
the implementation was split into pure helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError
import pytest

from phaze.config import ControlSettings
from phaze.config_backends import BucketConfig, ComputeBackend, KubeConfig, KueueBackend, LocalBackend


if TYPE_CHECKING:
    from collections.abc import Sequence

    from phaze.config_backends import BackendConfig


def _bucket(bucket_id: str, *, scope: str = "shared") -> BucketConfig:
    return BucketConfig.model_validate(
        {
            "id": bucket_id,
            "scope": scope,
            "endpoint_url": "https://s3.example.com",
            "bucket": f"phaze-{bucket_id}",
        }
    )


def _compute(backend_id: str, agent_ref: str) -> ComputeBackend:
    return ComputeBackend(
        kind="compute",
        id=backend_id,
        rank=10,
        cap=2,
        agent_ref=agent_ref,
        scratch_dir=f"/scratch/{backend_id}",
        push_host=f"{backend_id}.push",
    )


def _kueue(backend_id: str, buckets: list[str]) -> KueueBackend:
    return KueueBackend(
        kind="kueue",
        id=backend_id,
        rank=20,
        cap=4,
        buckets=buckets,
        kube=KubeConfig(api_url="https://kube.example.com"),
    )


def _registry_error(
    monkeypatch: pytest.MonkeyPatch,
    backends: Sequence[BackendConfig],
    buckets: Sequence[BucketConfig],
) -> str:
    """Return the underlying exact ValueError text from public model construction."""
    monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", "/definitely/absent/registry-policy-order.toml")
    with pytest.raises(ValidationError) as excinfo:
        ControlSettings(backends=list(backends), buckets=list(buckets))
    error = excinfo.value.errors(include_url=False)[0]
    return str(error["ctx"]["error"])


def test_empty_registry_error_wins_over_duplicate_bucket_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = _bucket("bucket-a")

    assert _registry_error(monkeypatch, [], [bucket, bucket]) == "backend registry resolved to empty — refusing to start (REG-04)"


def test_duplicate_bucket_error_wins_over_duplicate_backend_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = _bucket("bucket-a")
    local = LocalBackend(kind="local", id="local", rank=99, cap=1)

    assert _registry_error(monkeypatch, [local, local], [bucket, bucket]) == (
        "duplicate bucket ids in registry: ['bucket-a'] — each [[buckets]] id must be unique (REG-05)"
    )


def test_duplicate_backend_error_wins_over_duplicate_agent_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _compute("cloud", "agent-a")
    second = _compute("cloud", "agent-a")

    assert _registry_error(monkeypatch, [first, second], []) == (
        "duplicate backend ids in registry: ['cloud'] (kinds: {'cloud': ['compute', 'compute']}) — each [[backends]] id must be unique"
    )


def test_duplicate_agent_ref_error_wins_over_invalid_bucket_list(monkeypatch: pytest.MonkeyPatch) -> None:
    backends = [_compute("cloud-a", "agent-a"), _compute("cloud-b", "agent-a"), _kueue("kueue-a", ["ghost"])]

    assert _registry_error(monkeypatch, backends, []) == (
        "duplicate compute agent_ref(s) ['agent-a'] bound by backends {'agent-a': ['cloud-a', 'cloud-b']} — "
        "each compute backend must bind a distinct agent_ref (D-04)"
    )


def test_bucket_list_error_wins_over_cluster_sharing_cardinality(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = _bucket("cluster-a", scope="cluster-specific")
    backends = [_kueue("kueue-a", ["cluster-a", "cluster-a"]), _kueue("kueue-b", ["cluster-a"])]

    assert _registry_error(monkeypatch, backends, [bucket]) == (
        "backend 'kueue-a' lists duplicate bucket ids ['cluster-a'] in its own buckets list — each id must appear once"
    )
