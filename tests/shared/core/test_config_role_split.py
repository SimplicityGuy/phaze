"""Unit tests for config role-split dispatch (Phase 26 D-14, GAP-1).

Covers 6 behaviors of `src/phaze/config.py`:
1. get_settings() returns AgentSettings when PHAZE_ROLE=agent
2. get_settings() returns ControlSettings when PHAZE_ROLE=control (default)
3. AgentSettings raises when PHAZE_AGENT_API_URL is missing
4. AgentSettings raises when PHAZE_AGENT_TOKEN is missing
5. AgentSettings raises when scan_roots is empty
6. Comma-split PHAZE_AGENT_SCAN_ROOTS=/a,/b,/c produces ["/a", "/b", "/c"]

No DB, no Redis required.
"""

from __future__ import annotations

from pydantic import ValidationError
import pytest


_VALID_URL = "http://app.test:8000"
_VALID_TOKEN = "phaze_agent_test-token-abc123"
_VALID_ROOTS = "/data/music,/data/concerts"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """lru_cache must be cleared so each test gets a fresh dispatch."""
    from phaze.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_settings_returns_agent_settings_when_role_is_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_settings() dispatches to AgentSettings when PHAZE_ROLE=agent."""
    from phaze.config import AgentSettings

    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)

    from phaze.config import get_settings

    result = get_settings()
    assert isinstance(result, AgentSettings), f"Expected AgentSettings, got {type(result).__name__}"


def test_get_settings_returns_control_settings_when_role_is_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_settings() dispatches to ControlSettings when PHAZE_ROLE=control (default)."""
    from phaze.config import ControlSettings

    monkeypatch.setenv("PHAZE_ROLE", "control")
    # Ensure no agent env vars leak in
    monkeypatch.delenv("PHAZE_AGENT_API_URL", raising=False)
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("PHAZE_AGENT_SCAN_ROOTS", raising=False)

    from phaze.config import get_settings

    result = get_settings()
    assert isinstance(result, ControlSettings), f"Expected ControlSettings, got {type(result).__name__}"


def test_get_settings_defaults_to_control_when_phaze_role_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_settings() defaults to ControlSettings when PHAZE_ROLE is absent."""
    from phaze.config import ControlSettings

    monkeypatch.delenv("PHAZE_ROLE", raising=False)
    monkeypatch.delenv("PHAZE_AGENT_API_URL", raising=False)
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("PHAZE_AGENT_SCAN_ROOTS", raising=False)

    from phaze.config import get_settings

    result = get_settings()
    assert isinstance(result, ControlSettings), f"Expected ControlSettings, got {type(result).__name__}"


def test_agent_settings_raises_when_api_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentSettings raises ValueError/ValidationError when PHAZE_AGENT_API_URL is absent.

    Empty-string setenv overrides the project ``.env`` (in docker-compose mode it
    provides a real value); ``delenv`` alone is not enough because pydantic-settings
    falls back to the ``.env`` file.
    """
    from phaze.config import AgentSettings

    monkeypatch.setenv("PHAZE_AGENT_API_URL", "")
    monkeypatch.delenv("agent_api_url", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)

    with pytest.raises((ValueError, ValidationError)):
        AgentSettings()


def test_agent_settings_raises_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentSettings raises ValueError/ValidationError when PHAZE_AGENT_TOKEN is absent.

    Empty-string setenv beats the project .env fallback in pydantic-settings.
    """
    from phaze.config import AgentSettings

    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "")
    monkeypatch.delenv("agent_token", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)

    with pytest.raises((ValueError, ValidationError)):
        AgentSettings()


def test_agent_settings_raises_when_scan_roots_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentSettings raises ValueError/ValidationError when scan_roots resolves to empty list.

    Empty-string setenv beats the project .env fallback in pydantic-settings.
    """
    from phaze.config import AgentSettings

    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "")
    monkeypatch.delenv("scan_roots", raising=False)

    with pytest.raises((ValueError, ValidationError)):
        AgentSettings()


def test_agent_settings_comma_splits_scan_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PHAZE_AGENT_SCAN_ROOTS=/a,/b,/c produces scan_roots=['/a', '/b', '/c']."""
    from phaze.config import AgentSettings

    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/a,/b,/c")

    cfg = AgentSettings()
    assert cfg.scan_roots == ["/a", "/b", "/c"], f"scan_roots mismatch: {cfg.scan_roots!r}"


# ----------------------------------------------------------------------
# Phase 27-01: Watcher / scan_chunk_size knobs on AgentSettings (D-03, D-11)
# ----------------------------------------------------------------------


def _agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helper: set the minimum env vars needed for AgentSettings() to construct.

    Tests in this module assert on the documented default values (D-03 / D-11).
    pydantic-settings reads ``os.environ`` AND ``.env`` files, and the project's
    docker-compose ``.env`` overrides several watcher knobs to non-default
    values (e.g., ``PHAZE_WATCHER_SETTLE_SECONDS=3``). ``delenv`` alone does
    NOT clear those — pydantic-settings falls back to ``.env``. Setting each
    knob to its documented default via ``setenv`` is the only way to fully
    shadow the ``.env`` layer.
    """
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)
    # Pin watcher knobs to their documented defaults so assertions in this
    # module hold regardless of what's in the project's .env file.
    monkeypatch.setenv("PHAZE_WATCHER_SETTLE_SECONDS", "10")
    monkeypatch.setenv("PHAZE_WATCHER_MAX_PENDING_SECONDS", "3600")
    monkeypatch.setenv("PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS", "2")
    monkeypatch.setenv("PHAZE_SCAN_CHUNK_SIZE", "500")
    monkeypatch.setenv("PHAZE_WATCHER_POLLING_MODE", "false")
    for name in ("watcher_settle_seconds", "watcher_max_pending_seconds", "watcher_sweep_interval_seconds", "scan_chunk_size"):
        monkeypatch.delenv(name, raising=False)


def test_agent_settings_watcher_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default values for the four new knobs match D-03 / D-11."""
    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    cfg = AgentSettings()
    assert cfg.watcher_settle_seconds == 10
    assert cfg.watcher_max_pending_seconds == 3600
    assert cfg.watcher_sweep_interval_seconds == 2
    assert cfg.scan_chunk_size == 500


@pytest.mark.parametrize(
    ("env_var", "field_name", "value"),
    [
        ("PHAZE_WATCHER_SETTLE_SECONDS", "watcher_settle_seconds", "42"),
        ("PHAZE_WATCHER_MAX_PENDING_SECONDS", "watcher_max_pending_seconds", "7200"),
        ("PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS", "watcher_sweep_interval_seconds", "5"),
        ("PHAZE_SCAN_CHUNK_SIZE", "scan_chunk_size", "250"),
    ],
)
def test_agent_settings_watcher_env_var_aliases(
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
    field_name: str,
    value: str,
) -> None:
    """Each PHAZE_* env var maps onto its bare field name via AliasChoices."""
    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    monkeypatch.setenv(env_var, value)
    cfg = AgentSettings()
    assert getattr(cfg, field_name) == int(value), f"{field_name} != {value}"


# ----------------------------------------------------------------------
# Phase 27 UAT Gap 4: .env.example documents required + new env vars
# ----------------------------------------------------------------------


def _read_env_example() -> str:
    """Read .env.example from the repo root (sibling of pyproject.toml)."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    env_path = repo_root / ".env.example"
    return env_path.read_text(encoding="utf-8")


def test_env_example_documents_all_required_agent_mode_vars() -> None:
    """``.env.example`` must mention the three required PHAZE_AGENT_* env vars.

    Gap 4: operators bringing up the watcher container had no obvious record
    of which env vars are required vs optional. The required trio
    (PHAZE_AGENT_API_URL, PHAZE_AGENT_TOKEN, PHAZE_AGENT_SCAN_ROOTS) MUST
    appear in .env.example at minimum as comment lines with example values.
    """
    text = _read_env_example()
    for key in ("PHAZE_AGENT_API_URL", "PHAZE_AGENT_TOKEN", "PHAZE_AGENT_SCAN_ROOTS"):
        assert key in text, f".env.example missing required agent-mode key: {key}"


def test_env_example_documents_auto_migrate_and_dev_seed() -> None:
    """``.env.example`` must document the Gap 2/Gap 3 startup knobs."""
    text = _read_env_example()
    for key in ("PHAZE_AUTO_MIGRATE", "PHAZE_DEV_SEED_AGENT", "PHAZE_DEV_AGENT_TOKEN"):
        assert key in text, f".env.example missing migration/seed knob: {key}"


def test_env_example_documents_cloud_target() -> None:
    """``.env.example`` must document PHAZE_CLOUD_TARGET and call out the rename.

    Phase 55 / v6.0: PHAZE_CLOUD_BURST_ENABLED is removed and replaced by the
    PHAZE_CLOUD_TARGET selector (local/a1/k8s). The rename must be LOUD so an
    operator redeploying does not keep the dead boolean and silently lose cloud
    routing. The legacy key must not appear in .env.example as a live setting.
    """
    text = _read_env_example()
    assert "PHAZE_CLOUD_TARGET" in text, ".env.example missing PHAZE_CLOUD_TARGET selector"
    for value in ("local", "a1", "k8s"):
        assert value in text, f".env.example must document cloud_target value: {value}"
    # Loud rename callout: an operator who kept the old toggle must be told to
    # delete it. The callout names the new var and flags the removal as breaking,
    # WITHOUT re-introducing the dead `PHAZE_CLOUD_BURST_ENABLED` / `cloud_burst`
    # tokens that the migration grep-gate forbids.
    assert "BREAKING RENAME" in text, ".env.example must carry a loud breaking-rename callout"
    assert "PHAZE_CLOUD_BURST_ENABLED" not in text, "the removed legacy toggle token must not appear in .env.example"
    assert "cloud_burst" not in text, "the removed legacy `cloud_burst` token must not appear in .env.example"


def test_env_example_explains_host_vs_container() -> None:
    """``.env.example`` must call out the docker-service-name vs localhost distinction.

    Operators running services with `uv run` on the host (rather than docker
    compose) must change DATABASE_URL/REDIS_URL hostnames from `postgres`/
    `redis` to `localhost`. This rule was not documented before Gap 4.
    """
    text = _read_env_example()
    assert "localhost" in text, ".env.example must explain host-vs-container hostname swap"
    assert "docker compose" in text.lower(), ".env.example must reference docker compose context"
