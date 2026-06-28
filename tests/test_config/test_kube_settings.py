"""Unit tests for the Phase 54 kube submit/reconcile config surface (KSUBMIT-01/05).

The control plane submits suspended Kueue Jobs via the kube API, watches them to
completion, and reconciles their status. The kube client surface (api url, namespace,
local-queue, job image, resource requests, workload apiVersion) plus the file-mounted
credentials (``kube_kubeconfig`` / ``kube_sa_token``) land on ``ControlSettings`` ONLY
(T-54-01 -- the agent and pod never receive kube credentials) and honor the ``<VAR>_FILE``
secret convention via the inherited ``SECRET_FILE_FIELDS`` machinery.

D-08 introduces ``cloud_submit_max_attempts`` -- a DISTINCT retry budget from
``push_max_attempts`` -- bounded ``gt=0, lt=20`` so a misconfig cannot create an unbounded
submit storm (T-54-02).

All kube_* fields are OPTIONAL in Phase 54 (default None / the apiVersion default) so an
existing Phase 53 cloud-on/no-kube deploy keeps working; the fail-fast coupling to
``cloud_burst_enabled`` is Phase 55 (KDEPLOY-02), NOT here.

These are pure pydantic-settings tests -- no DB, no Redis required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr, ValidationError
import pytest

from phaze.config import AgentSettings, ControlSettings


if TYPE_CHECKING:
    from pathlib import Path


_VALID_AGENT_URL = "http://app.test:8000"
_VALID_TOKEN = "phaze_agent_test-token-abc123"
_VALID_ROOTS = "/data/music,/data/concerts"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    from phaze.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the non-secret required agent fields so AgentSettings can construct."""
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_AGENT_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)


# --------------------------------------------------------------------------- #
# D-08: cloud_submit_max_attempts (distinct budget from push_max_attempts)
# --------------------------------------------------------------------------- #
def test_cloud_submit_max_attempts_default() -> None:
    assert ControlSettings().cloud_submit_max_attempts == 3


def test_cloud_submit_max_attempts_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_CLOUD_SUBMIT_MAX_ATTEMPTS", "7")
    assert ControlSettings().cloud_submit_max_attempts == 7


def test_cloud_submit_max_attempts_rejects_zero() -> None:
    with pytest.raises(ValidationError, match="cloud_submit_max_attempts"):
        ControlSettings(cloud_submit_max_attempts=0)


def test_cloud_submit_max_attempts_rejects_too_large() -> None:
    with pytest.raises(ValidationError, match="cloud_submit_max_attempts"):
        ControlSettings(cloud_submit_max_attempts=20)


def test_cloud_submit_max_attempts_is_distinct_from_push() -> None:
    """D-08: the two retry budgets are independent fields, not aliases of each other."""
    cfg = ControlSettings(cloud_submit_max_attempts=5, push_max_attempts=2)
    assert cfg.cloud_submit_max_attempts == 5
    assert cfg.push_max_attempts == 2


# --------------------------------------------------------------------------- #
# Kube client surface -- optional in Phase 54 (default None / apiVersion default)
# --------------------------------------------------------------------------- #
def test_kube_fields_default_none_when_unset() -> None:
    """An existing cloud-on/no-kube deploy leaves every kube client field optional."""
    cfg = ControlSettings()
    assert cfg.kube_api_url is None
    assert cfg.kube_namespace is None
    assert cfg.kube_local_queue is None
    assert cfg.kube_job_image is None
    assert cfg.kube_job_cpu_request is None
    assert cfg.kube_job_memory_request is None


def test_kube_workload_api_version_default() -> None:
    assert ControlSettings().kube_workload_api_version == "kueue.x-k8s.io/v1beta1"


def test_kube_fields_bind_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_KUBE_API_URL", "https://kube.internal:6443")
    monkeypatch.setenv("PHAZE_KUBE_NAMESPACE", "phaze")
    monkeypatch.setenv("PHAZE_KUBE_LOCAL_QUEUE", "phaze-lq")
    monkeypatch.setenv("PHAZE_KUBE_JOB_IMAGE", "ghcr.io/sguy/phaze-agent:latest")
    monkeypatch.setenv("PHAZE_KUBE_JOB_CPU_REQUEST", "2")
    monkeypatch.setenv("PHAZE_KUBE_JOB_MEMORY_REQUEST", "4Gi")
    monkeypatch.setenv("PHAZE_KUBE_WORKLOAD_API_VERSION", "kueue.x-k8s.io/v1beta2")

    cfg = ControlSettings()

    assert cfg.kube_api_url == "https://kube.internal:6443"
    assert cfg.kube_namespace == "phaze"
    assert cfg.kube_local_queue == "phaze-lq"
    assert cfg.kube_job_image == "ghcr.io/sguy/phaze-agent:latest"
    assert cfg.kube_job_cpu_request == "2"
    assert cfg.kube_job_memory_request == "4Gi"
    assert cfg.kube_workload_api_version == "kueue.x-k8s.io/v1beta2"


def test_kube_fields_do_not_couple_to_cloud_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 54: cloud burst ON with NO kube config must still construct.

    The fail-fast validator coupling kube_* to cloud_burst_enabled is Phase 55 (KDEPLOY-02);
    adding it here would break existing Phase 53 cloud-on/no-kube deploys.
    """
    monkeypatch.setenv("PHAZE_CLOUD_BURST_ENABLED", "true")
    monkeypatch.setenv("PHAZE_COMPUTE_SCRATCH_DIR", "/scratch")
    monkeypatch.setenv("PHAZE_S3_BUCKET", "phaze-staging")
    monkeypatch.setenv("PHAZE_S3_ENDPOINT_URL", "https://s3.example.com")
    monkeypatch.delenv("PHAZE_KUBE_API_URL", raising=False)
    monkeypatch.delenv("PHAZE_KUBE_NAMESPACE", raising=False)

    cfg = ControlSettings()

    assert cfg.cloud_burst_enabled is True
    assert cfg.kube_api_url is None
    assert cfg.kube_namespace is None


# --------------------------------------------------------------------------- #
# Credentials -- SecretStr resolved via the <VAR>_FILE convention (T-54-01)
# --------------------------------------------------------------------------- #
def test_kube_credentials_default_none() -> None:
    cfg = ControlSettings()
    assert cfg.kube_kubeconfig is None
    assert cfg.kube_sa_token is None


def test_kube_kubeconfig_and_sa_token_in_secret_file_fields() -> None:
    assert "kube_kubeconfig" in ControlSettings.SECRET_FILE_FIELDS
    assert "kube_sa_token" in ControlSettings.SECRET_FILE_FIELDS


def test_kube_credentials_resolve_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PHAZE_KUBE_KUBECONFIG_FILE / _SA_TOKEN_FILE resolve to SecretStr (stripped)."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    sa_token = tmp_path / "sa_token"
    sa_token.write_text("eyJhbGciOiExample\n", encoding="utf-8")
    monkeypatch.delenv("PHAZE_KUBE_KUBECONFIG", raising=False)
    monkeypatch.delenv("PHAZE_KUBE_SA_TOKEN", raising=False)
    monkeypatch.setenv("PHAZE_KUBE_KUBECONFIG_FILE", str(kubeconfig))
    monkeypatch.setenv("PHAZE_KUBE_SA_TOKEN_FILE", str(sa_token))

    cfg = ControlSettings()

    assert isinstance(cfg.kube_kubeconfig, SecretStr)
    assert cfg.kube_kubeconfig.get_secret_value() == "apiVersion: v1\nkind: Config"
    assert isinstance(cfg.kube_sa_token, SecretStr)
    assert cfg.kube_sa_token.get_secret_value() == "eyJhbGciOiExample"


def test_kube_sa_token_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolved kube SA token stays SecretStr and is masked in repr (T-54-01)."""
    monkeypatch.setenv("PHAZE_KUBE_SA_TOKEN", "leak-me-not-token")
    cfg = ControlSettings()
    assert isinstance(cfg.kube_sa_token, SecretStr)
    assert "leak-me-not-token" not in repr(cfg)


# --------------------------------------------------------------------------- #
# T-54-01: kube config lives ONLY on the control plane
# --------------------------------------------------------------------------- #
def test_agent_settings_has_no_kube_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """AgentSettings must expose NO kube_* field -- the agent never sees kube creds."""
    _agent_env(monkeypatch)
    agent = AgentSettings()
    kube_fields = [name for name in AgentSettings.model_fields if name.startswith("kube_")]
    assert kube_fields == [], f"AgentSettings must not carry kube fields (T-54-01): {kube_fields}"
    assert not hasattr(agent, "kube_api_url")
