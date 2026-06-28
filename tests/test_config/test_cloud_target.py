"""Unit tests for the Phase 55 ``cloud_target`` selector (D-02, KROUTE-01).

``cloud_target`` is the single source of truth that selects the active cloud target. It
HARD-REPLACES the Phase 51 cloud-burst master bool: it is a pydantic
``Literal["local", "a1", "k8s"]`` ``Field`` on ``ControlSettings`` (default ``"local"`` == cloud
off), bound from ``PHAZE_CLOUD_TARGET`` (or the bare ``cloud_target``) via ``AliasChoices``. An
invalid member is rejected at construction. Three per-target ``model_validator``s fail fast on a
misconfigured target: ``a1`` requires ``compute_scratch_dir``; ``k8s`` requires the S3 staging
substrate (``s3_bucket`` + ``s3_endpoint_url``) AND the kube surface (``kube_api_url`` /
``kube_namespace`` / ``kube_local_queue``). These are pure pydantic-settings tests -- no DB, no
Redis required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.config import ControlSettings


if TYPE_CHECKING:
    import pytest as _pytest


# Every env var that can influence cloud_target resolution / the per-target validators. Cleared
# before each construction so an ambient operator env can never leak into these unit assertions.
_CLOUD_ENV_VARS = (
    "PHAZE_CLOUD_TARGET",
    "cloud_target",
    "PHAZE_COMPUTE_SCRATCH_DIR",
    "compute_scratch_dir",
    "PHAZE_S3_BUCKET",
    "s3_bucket",
    "PHAZE_S3_ENDPOINT_URL",
    "s3_endpoint_url",
    "PHAZE_KUBE_API_URL",
    "kube_api_url",
    "PHAZE_KUBE_NAMESPACE",
    "kube_namespace",
    "PHAZE_KUBE_LOCAL_QUEUE",
    "kube_local_queue",
)


def _clear_cloud_env(monkeypatch: _pytest.MonkeyPatch) -> None:
    for var in _CLOUD_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_cloud_target_default_local(monkeypatch: _pytest.MonkeyPatch) -> None:
    """Omitting all cloud env defaults cloud_target to 'local' -- the feature ships dormant (D-02)."""
    _clear_cloud_env(monkeypatch)
    assert ControlSettings().cloud_target == "local"


def test_cloud_target_env_alias_k8s(monkeypatch: _pytest.MonkeyPatch) -> None:
    """PHAZE_CLOUD_TARGET=k8s binds to the field and parses as 'k8s' (with k8s config satisfied)."""
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("PHAZE_CLOUD_TARGET", "k8s")
    # k8s ON requires the S3 staging substrate AND the kube surface (validators below); set them so
    # this field-binding test exercises only the selector parsing.
    monkeypatch.setenv("PHAZE_S3_BUCKET", "phaze-staging")
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "https://s3.example.com")
    monkeypatch.setenv("PHAZE_KUBE_API_URL", "https://kube.example.com")
    monkeypatch.setenv("PHAZE_KUBE_NAMESPACE", "phaze")
    monkeypatch.setenv("PHAZE_KUBE_LOCAL_QUEUE", "phaze-lq")
    assert ControlSettings().cloud_target == "k8s"


def test_cloud_target_bare_name_alias(monkeypatch: _pytest.MonkeyPatch) -> None:
    """The bare-name form cloud_target=a1 also parses (AliasChoices dual form)."""
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("cloud_target", "a1")
    monkeypatch.setenv("PHAZE_COMPUTE_SCRATCH_DIR", "/scratch")
    assert ControlSettings().cloud_target == "a1"


def test_cloud_target_invalid_member_rejected(monkeypatch: _pytest.MonkeyPatch) -> None:
    """An off-list member (e.g. 'a2') is rejected at construction by the Literal (V5 input validation, T-55-CFG-01)."""
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("PHAZE_CLOUD_TARGET", "a2")
    with pytest.raises(ValueError):
        ControlSettings()


def test_cloud_target_a1_requires_compute_scratch_dir(monkeypatch: _pytest.MonkeyPatch) -> None:
    """cloud_target='a1' with no compute_scratch_dir fails fast at construction.

    Without the guard the push callback would build a literal ``"None/<file_id>.<ext>"``
    scratch_path and every pushed file would silently dead-end in ANALYSIS_FAILED.
    """
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("PHAZE_CLOUD_TARGET", "a1")
    with pytest.raises(ValueError, match="PHAZE_COMPUTE_SCRATCH_DIR is required"):
        ControlSettings()


def test_cloud_target_a1_constructs_with_compute_scratch_dir(monkeypatch: _pytest.MonkeyPatch) -> None:
    """cloud_target='a1' with compute_scratch_dir set constructs; no S3/kube config required (a1 is rsync)."""
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("PHAZE_CLOUD_TARGET", "a1")
    monkeypatch.setenv("PHAZE_COMPUTE_SCRATCH_DIR", "/scratch")
    cfg = ControlSettings()
    assert cfg.cloud_target == "a1"
    assert cfg.compute_scratch_dir == "/scratch"


def test_cloud_target_k8s_requires_s3_bucket(monkeypatch: _pytest.MonkeyPatch) -> None:
    """cloud_target='k8s' missing s3_bucket fails fast (S3 is the k8s byte path)."""
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("PHAZE_CLOUD_TARGET", "k8s")
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "https://s3.example.com")
    monkeypatch.setenv("PHAZE_KUBE_API_URL", "https://kube.example.com")
    monkeypatch.setenv("PHAZE_KUBE_NAMESPACE", "phaze")
    monkeypatch.setenv("PHAZE_KUBE_LOCAL_QUEUE", "phaze-lq")
    with pytest.raises(ValueError, match="PHAZE_S3_BUCKET is required"):
        ControlSettings()


def test_cloud_target_k8s_requires_kube_local_queue(monkeypatch: _pytest.MonkeyPatch) -> None:
    """cloud_target='k8s' missing a kube field (kube_local_queue) fails fast (the new k8s kube validator)."""
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("PHAZE_CLOUD_TARGET", "k8s")
    monkeypatch.setenv("PHAZE_S3_BUCKET", "phaze-staging")
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "https://s3.example.com")
    monkeypatch.setenv("PHAZE_KUBE_API_URL", "https://kube.example.com")
    monkeypatch.setenv("PHAZE_KUBE_NAMESPACE", "phaze")
    with pytest.raises(ValueError, match="PHAZE_KUBE_LOCAL_QUEUE is required"):
        ControlSettings()


def test_cloud_target_k8s_constructs_with_full_config(monkeypatch: _pytest.MonkeyPatch) -> None:
    """cloud_target='k8s' with S3 + kube config all set constructs cleanly."""
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("PHAZE_CLOUD_TARGET", "k8s")
    monkeypatch.setenv("PHAZE_S3_BUCKET", "phaze-staging")
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "https://s3.example.com")
    monkeypatch.setenv("PHAZE_KUBE_API_URL", "https://kube.example.com")
    monkeypatch.setenv("PHAZE_KUBE_NAMESPACE", "phaze")
    monkeypatch.setenv("PHAZE_KUBE_LOCAL_QUEUE", "phaze-lq")
    cfg = ControlSettings()
    assert cfg.cloud_target == "k8s"
    assert cfg.s3_bucket == "phaze-staging"
    assert cfg.kube_local_queue == "phaze-lq"


def test_cloud_target_local_needs_no_cloud_config(monkeypatch: _pytest.MonkeyPatch) -> None:
    """cloud_target='local' (cloud off) constructs with none of the per-target requirements set."""
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("PHAZE_CLOUD_TARGET", "local")
    cfg = ControlSettings()
    assert cfg.cloud_target == "local"
    assert cfg.compute_scratch_dir is None
    assert cfg.s3_bucket is None
    assert cfg.kube_api_url is None
