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
    """AgentSettings raises ValueError/ValidationError when PHAZE_AGENT_API_URL is absent."""
    from phaze.config import AgentSettings

    monkeypatch.delenv("PHAZE_AGENT_API_URL", raising=False)
    monkeypatch.delenv("agent_api_url", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)

    with pytest.raises((ValueError, ValidationError)):
        AgentSettings()


def test_agent_settings_raises_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentSettings raises ValueError/ValidationError when PHAZE_AGENT_TOKEN is absent."""
    from phaze.config import AgentSettings

    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("agent_token", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)

    with pytest.raises((ValueError, ValidationError)):
        AgentSettings()


def test_agent_settings_raises_when_scan_roots_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentSettings raises ValueError/ValidationError when scan_roots resolves to empty list."""
    from phaze.config import AgentSettings

    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.delenv("PHAZE_AGENT_SCAN_ROOTS", raising=False)
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
