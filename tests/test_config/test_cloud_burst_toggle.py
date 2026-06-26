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
    # cloud ON requires a compute scratch dir (validator below); set one so this
    # field-parsing test exercises only the toggle binding.
    monkeypatch.setenv("PHAZE_COMPUTE_SCRATCH_DIR", "/scratch")
    assert ControlSettings().cloud_burst_enabled is True


def test_cloud_burst_enabled_bare_name_alias(monkeypatch: _pytest.MonkeyPatch) -> None:
    """The bare-name form cloud_burst_enabled=true also parses (AliasChoices dual form)."""
    monkeypatch.setenv("cloud_burst_enabled", "true")
    monkeypatch.setenv("PHAZE_COMPUTE_SCRATCH_DIR", "/scratch")
    assert ControlSettings().cloud_burst_enabled is True


def test_cloud_burst_on_requires_compute_scratch_dir(monkeypatch: _pytest.MonkeyPatch) -> None:
    """cloud_burst_enabled=True with no compute_scratch_dir fails fast at construction.

    Without the guard the push callback would build a literal ``"None/<file_id>.<ext>"``
    scratch_path and every pushed file would silently dead-end in ANALYSIS_FAILED.
    """
    import pytest

    monkeypatch.setenv("PHAZE_CLOUD_BURST_ENABLED", "true")
    monkeypatch.delenv("PHAZE_COMPUTE_SCRATCH_DIR", raising=False)
    monkeypatch.delenv("compute_scratch_dir", raising=False)
    with pytest.raises(ValueError, match="PHAZE_COMPUTE_SCRATCH_DIR is required"):
        ControlSettings()


def test_cloud_burst_off_allows_missing_compute_scratch_dir(monkeypatch: _pytest.MonkeyPatch) -> None:
    """OFF (the default) needs no compute scratch dir — all-local deploys stay config-free."""
    monkeypatch.delenv("PHAZE_CLOUD_BURST_ENABLED", raising=False)
    monkeypatch.delenv("cloud_burst_enabled", raising=False)
    monkeypatch.delenv("PHAZE_COMPUTE_SCRATCH_DIR", raising=False)
    cfg = ControlSettings()
    assert cfg.cloud_burst_enabled is False
    assert cfg.compute_scratch_dir is None
