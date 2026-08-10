"""Unit tests for phaze-27myl: `AgentSettings` must fail fast when `queue_url` is still the
shared-base default that names the docker-compose service `postgres`.

That default (`postgresql://phaze:phaze@postgres:5432/phaze`, `BaseSettings.queue_url`) only
resolves on the app-server's own compose network. `docker-compose.agent.yml`'s own invariant is
"No postgres or redis service here", so on a file-server host that hostname never resolves and
every lane worker crash-loops trying to open its PostgresQueue broker connection. Unlike
`redis_url`, `queue_url` had no agent-role validator to catch this before construction.

No DB, no Redis required -- pure pydantic-settings construction tests.
"""

from __future__ import annotations

from pydantic import SecretStr, ValidationError
import pytest


_VALID_API_URL = "https://api.test:8000"
_VALID_TOKEN = SecretStr("phaze_agent_test-token-abc123")
_VALID_ROOTS = ["/data/music"]
_DEFAULT_QUEUE_URL = "postgresql://phaze:phaze@postgres:5432/phaze"
_REACHABLE_QUEUE_URL = "postgresql://phaze:secret@app-server.example:5432/phaze"


def test_default_queue_url_rejected_for_agent_role() -> None:
    """The unresolvable docker-service-name default fails fast at settings construction."""
    from phaze.config import AgentSettings

    with pytest.raises(ValidationError) as exc_info:
        AgentSettings(
            agent_api_url=_VALID_API_URL,
            agent_token=_VALID_TOKEN,
            scan_roots=_VALID_ROOTS,
            queue_url=_DEFAULT_QUEUE_URL,
        )
    assert "PHAZE_QUEUE_URL is required" in str(exc_info.value), f"Expected the queue_url fail-fast hint; got: {exc_info.value}"


def test_agent_settings_with_no_queue_url_override_also_rejected() -> None:
    """Constructing AgentSettings with no explicit queue_url falls back to the same unreachable
    default -- must fail exactly the same way as passing it explicitly.
    """
    from phaze.config import AgentSettings

    with pytest.raises(ValidationError) as exc_info:
        AgentSettings(
            agent_api_url=_VALID_API_URL,
            agent_token=_VALID_TOKEN,
            scan_roots=_VALID_ROOTS,
        )
    assert "PHAZE_QUEUE_URL is required" in str(exc_info.value)


def test_reachable_queue_url_constructs_cleanly() -> None:
    """An operator-supplied, non-`postgres`-hostname DSN constructs without error."""
    from phaze.config import AgentSettings

    cfg = AgentSettings(
        agent_api_url=_VALID_API_URL,
        agent_token=_VALID_TOKEN,
        scan_roots=_VALID_ROOTS,
        queue_url=_REACHABLE_QUEUE_URL,
    )
    assert cfg.queue_url == _REACHABLE_QUEUE_URL


def test_phaze_queue_url_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Setting PHAZE_QUEUE_URL in the environment (as .env.example.agent now documents) is
    sufficient to clear the fail-fast guard.
    """
    from phaze.cert_bootstrap import ensure_certs_present
    from phaze.config import AgentSettings

    ensure_certs_present(tmp_path, cn="localhost", sans_csv="localhost,127.0.0.1")

    monkeypatch.setenv("PHAZE_QUEUE_URL", _REACHABLE_QUEUE_URL)
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_API_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-abc123")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music")
    monkeypatch.setenv("PHAZE_AGENT_CA_FILE", str(tmp_path / "phaze-ca.crt"))

    cfg = AgentSettings()

    assert cfg.queue_url == _REACHABLE_QUEUE_URL


def test_compute_agent_does_not_require_queue_url() -> None:
    """kind="compute" (the k8s one-shot analyze pod, job_runner.py) never imports the
    PostgresQueue machinery and calls back over HTTP directly -- its documented agent-env
    ConfigMap (docs/k8s-burst.md §6) carries no PHAZE_QUEUE_URL, so the guard must NOT apply to
    it. Mirrors the existing scan_roots relaxation for kind="compute".
    """
    from phaze.config import AgentSettings

    cfg = AgentSettings(
        kind="compute",
        agent_api_url=_VALID_API_URL,
        agent_token=_VALID_TOKEN,
        queue_url=_DEFAULT_QUEUE_URL,
    )
    assert cfg.queue_url == _DEFAULT_QUEUE_URL
