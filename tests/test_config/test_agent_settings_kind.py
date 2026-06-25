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


def _make_settings(**overrides: object):  # type: ignore[no-untyped-def]
    from phaze.config import AgentSettings

    base: dict[str, object] = {
        "agent_api_url": _VALID_API_URL,
        "agent_token": _VALID_TOKEN,
        "scan_roots": _VALID_ROOTS,
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
