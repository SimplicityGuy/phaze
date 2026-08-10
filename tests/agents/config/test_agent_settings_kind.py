"""Unit tests for the Phase 48 `AgentSettings.kind` field + relaxed scan-roots gate.

`kind` is the config-layer (middle) enum of the 3-layer kind defense: CLI argparse
`choices=` (outer), `kind: Literal[...]` here (middle), `ck_agents_kind_enum` DB
CHECK (inner, Plan 01). A `compute` (cloud) agent owns no media and no scan roots,
so the empty-scan-roots startup gate is relaxed ONLY for compute; `agent_api_url`
and `agent_token` stay required for every kind (compute still bears a token over
HTTP). No DB, no Redis required — these are pure pydantic-settings construction tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr
import pytest


if TYPE_CHECKING:
    import pytest as _pytest


_VALID_API_URL = "https://api.test:8000"
_VALID_TOKEN = SecretStr("phaze_agent_test-token-abc123")
_VALID_ROOTS = ["/data/music"]
# phaze-27myl: fileserver-kind AgentSettings now fail-fasts on the default (docker-service-name)
# queue_url, so fixtures for the default/fileserver kind must supply a reachable one.
_VALID_QUEUE_URL = "postgresql://phaze:phaze@app-server.example:5432/phaze"


def _make_settings(**overrides: object):  # type: ignore[no-untyped-def]
    from phaze.config import AgentSettings

    base: dict[str, object] = {
        "agent_api_url": _VALID_API_URL,
        "agent_token": _VALID_TOKEN,
        "scan_roots": _VALID_ROOTS,
        "queue_url": _VALID_QUEUE_URL,
    }
    base.update(overrides)
    return AgentSettings(**base)


def test_kind_defaults_fileserver() -> None:
    """An unspecified `kind` defaults to 'fileserver' (back-compat with v4.0)."""
    assert _make_settings().kind == "fileserver"


def test_compute_accepts_empty_scan_roots() -> None:
    """A compute agent boots with no scan roots — the empty-roots gate is relaxed."""
    cfg = _make_settings(kind="compute", scan_roots=[])
    assert cfg.kind == "compute"
    assert cfg.scan_roots == []


def test_fileserver_still_requires_scan_roots() -> None:
    """A fileserver agent with no scan roots still fails fast at construction."""
    with pytest.raises(ValueError, match="scan_roots is required"):
        _make_settings(kind="fileserver", scan_roots=[])


def test_compute_still_requires_api_url() -> None:
    """agent_api_url stays required for compute — it still PUTs over HTTP."""
    with pytest.raises(ValueError, match="PHAZE_AGENT_API_URL is required"):
        _make_settings(kind="compute", scan_roots=[], agent_api_url="")


def test_compute_still_requires_token() -> None:
    """agent_token stays required for compute — it still bears a token."""
    with pytest.raises(ValueError, match="PHAZE_AGENT_TOKEN is required"):
        _make_settings(kind="compute", scan_roots=[], agent_token=SecretStr(""))


def test_kind_env_alias(monkeypatch: _pytest.MonkeyPatch) -> None:
    """PHAZE_AGENT_KIND binds to AgentSettings.kind via AliasChoices."""
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_API_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-abc123")
    monkeypatch.setenv("PHAZE_AGENT_KIND", "compute")
    # compute relaxes the scan-roots gate, so none are supplied via env.

    from phaze.config import AgentSettings

    cfg = AgentSettings()
    assert cfg.kind == "compute"
    assert cfg.scan_roots == []


def test_analyze_pod_env_from_documented_configmap_and_job_manifest_passes_validation(
    monkeypatch: _pytest.MonkeyPatch,
) -> None:
    """phaze-4xks ACCEPTANCE: an analyze pod built from the documented objects passes AgentSettings
    validation.

    Reconstructs the ONE-SHOT ANALYZE POD's full env exactly as the deployed objects supply it: the
    three keys docs/k8s-burst.md §6 documents on the operator-created ``phaze-agent-env`` ConfigMap
    (``PHAZE_ROLE``, ``PHAZE_AGENT_API_URL``, ``PHAZE_MODELS_DIR``) PLUS the code-injected literals
    ``kube_staging.build_job_manifest`` puts directly in the container ``env`` (``PHAZE_JOB_FILE_ID``,
    ``PHAZE_AGENT_CA_FILE``, and -- the phaze-4xks fix -- ``PHAZE_AGENT_KIND``). Before the fix, this
    env set had no ``PHAZE_AGENT_KIND``, ``AgentSettings.kind`` defaulted to ``"fileserver"``, and
    construction raised (no ``scan_roots`` for a one-shot pod, which owns no filesystem to scan).
    """
    import uuid

    from phaze.config_backends import KubeConfig
    from phaze.services import kube_staging

    manifest = kube_staging.build_job_manifest(
        uuid.uuid4(),
        KubeConfig(
            api_url="https://kube.test:6443",
            namespace="phaze",
            local_queue="phaze-lq",
            job_image="phaze/job-runner:test",
            cpu_request="2",
            memory_request="4Gi",
        ),
    )
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    # (a) the documented phaze-agent-env ConfigMap, verbatim (docs/k8s-burst.md §6).
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "https://control-plane.test:8000")
    monkeypatch.setenv("PHAZE_MODELS_DIR", "/models")
    # (b) the phaze-agent-token Secret (§5) sourced via the same envFrom.
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-abc123")
    # (c) every code-injected literal from the Job manifest's container `env` (not envFrom).
    for entry in container["env"]:
        monkeypatch.setenv(entry["name"], entry["value"])

    from phaze.config import AgentSettings

    cfg = AgentSettings()  # must not raise -- this IS the pod's settings construction

    assert cfg.kind == "compute"
    assert cfg.scan_roots == []
