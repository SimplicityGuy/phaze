"""Unit tests for the Phase 51 ``cloud_burst_enabled`` master toggle (D-01, CLOUDDEPLOY-04).

``cloud_burst_enabled`` is the single switch that turns the whole cloud-burst feature on or
off. It is a plain bool ``Field`` on ``ControlSettings`` (no ``gt=/lt=`` bounds, unlike the int
cloud knobs), bound from ``PHAZE_CLOUD_BURST_ENABLED`` (or the bare ``cloud_burst_enabled``) via
``AliasChoices``, and it defaults to ``False`` so a fresh v5.0 deploy behaves all-local with zero
cloud activity until the operator explicitly opts in. These are pure pydantic-settings tests --
no DB, no Redis required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from phaze.config import ControlSettings


if TYPE_CHECKING:
    import pytest as _pytest


def test_cloud_burst_enabled_default_false() -> None:
    """An unset toggle defaults to False -- the feature ships dormant (D-01, D-03 insecure-default accept)."""
    assert ControlSettings().cloud_burst_enabled is False


def test_cloud_burst_enabled_env_alias(monkeypatch: _pytest.MonkeyPatch) -> None:
    """PHAZE_CLOUD_BURST_ENABLED=true binds to the field and parses as True."""
    monkeypatch.setenv("PHAZE_CLOUD_BURST_ENABLED", "true")
    assert ControlSettings().cloud_burst_enabled is True


def test_cloud_burst_enabled_bare_name_alias(monkeypatch: _pytest.MonkeyPatch) -> None:
    """The bare-name form cloud_burst_enabled=true also parses (AliasChoices dual form)."""
    monkeypatch.setenv("cloud_burst_enabled", "true")
    assert ControlSettings().cloud_burst_enabled is True
