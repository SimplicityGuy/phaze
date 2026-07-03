"""Unit tests for the Phase 67 backend registry submodels (REG-01/02/05).

The registry replaces the single ``cloud_target`` Literal with a pydantic v2
discriminated union over ``kind`` (``local`` / ``compute`` / ``kueue``). Each
variant validates its own required fields at construction and fails fast with
the offending entry ``id`` in the message (REG-02, D-13). ``BucketConfig`` carries
the S3 staging surface with a per-bucket http(s) SSRF guard on ``endpoint_url``
(REG-05, D-07). These are pure model-construction tests -- no DB, no Redis.
"""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError
import pytest

from phaze.config_backends import (
    BackendConfig,
    ComputeBackend,
    KubeConfig,
    KueueBackend,
    LocalBackend,
    _default_local_registry,
)


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
