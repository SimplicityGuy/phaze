"""Unit tests for the Phase 31 windowed-analysis AgentSettings fields.

Covers the three window-config knobs added to `phaze.config.AgentSettings`:
- `analysis_fine_window_sec` (default 30, env `PHAZE_ANALYSIS_FINE_WINDOW_SEC`)
- `analysis_coarse_window_sec` (default 180, env `PHAZE_ANALYSIS_COARSE_WINDOW_SEC`)
- `analysis_fine_min_sec` (default 15, env `PHAZE_ANALYSIS_FINE_MIN_SEC`)

Tests pass kwargs directly to `AgentSettings(...)` for the defaults, and use the
env-var monkeypatch path for the alias-binding check. No DB, no Redis required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr


if TYPE_CHECKING:
    import pytest


_VALID_API_URL = "https://api.test:8000"
_VALID_TOKEN = SecretStr("phaze_agent_test-token-abc123")
_VALID_ROOTS = ["/data/music"]


def _make_settings(**overrides: object):  # type: ignore[no-untyped-def]
    from phaze.config import AgentSettings

    base: dict[str, object] = {
        "agent_api_url": _VALID_API_URL,
        "agent_token": _VALID_TOKEN,
        "scan_roots": _VALID_ROOTS,
    }
    base.update(overrides)
    return AgentSettings(**base)


def test_window_config_defaults() -> None:
    """The three window-config fields default to 30 / 180 / 15 seconds."""
    cfg = _make_settings()
    assert cfg.analysis_fine_window_sec == 30
    assert cfg.analysis_coarse_window_sec == 180
    assert cfg.analysis_fine_min_sec == 15


def test_window_config_kwarg_override() -> None:
    """Window-config fields are overridable via direct kwargs."""
    cfg = _make_settings(
        analysis_fine_window_sec=20,
        analysis_coarse_window_sec=120,
        analysis_fine_min_sec=10,
    )
    assert cfg.analysis_fine_window_sec == 20
    assert cfg.analysis_coarse_window_sec == 120
    assert cfg.analysis_fine_min_sec == 10


def test_window_config_env_var_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """PHAZE_ANALYSIS_* env vars bind to the corresponding fields."""
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_API_URL)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-abc123")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music")
    monkeypatch.setenv("PHAZE_ANALYSIS_FINE_WINDOW_SEC", "45")
    monkeypatch.setenv("PHAZE_ANALYSIS_COARSE_WINDOW_SEC", "240")
    monkeypatch.setenv("PHAZE_ANALYSIS_FINE_MIN_SEC", "20")

    from phaze.config import AgentSettings

    cfg = AgentSettings()
    assert cfg.analysis_fine_window_sec == 45
    assert cfg.analysis_coarse_window_sec == 240
    assert cfg.analysis_fine_min_sec == 20
