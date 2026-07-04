"""Unit tests for the Phase 69 `cloud_spill_to_local_after_seconds` config knob (D-02).

`cloud_spill_to_local_after_seconds` mirrors `cloud_route_threshold_sec`: a bounded
pydantic int Field (gt=0, lt=86400) on `ControlSettings`, bound from
`PHAZE_CLOUD_SPILL_TO_LOCAL_AFTER_SECONDS` via `AliasChoices`. It is the staleness
threshold the pure `select_backend` policy compares against before letting a
full-cloud file spill to the slow local backend. An out-of-range value fails
validation at construction time (T-69-01-01) and never reaches selection. These are
pure pydantic-settings tests -- no DB, no Redis required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.config import ControlSettings


if TYPE_CHECKING:
    import pytest as _pytest


def test_cloud_spill_to_local_default() -> None:
    """An unset knob defaults to 900 seconds (15 minutes)."""
    assert ControlSettings().cloud_spill_to_local_after_seconds == 900


def test_cloud_spill_to_local_env_alias(monkeypatch: _pytest.MonkeyPatch) -> None:
    """PHAZE_CLOUD_SPILL_TO_LOCAL_AFTER_SECONDS binds to the field and parses as int."""
    monkeypatch.setenv("PHAZE_CLOUD_SPILL_TO_LOCAL_AFTER_SECONDS", "1200")
    assert ControlSettings().cloud_spill_to_local_after_seconds == 1200


def test_cloud_spill_to_local_rejects_zero() -> None:
    """A non-positive value fails validation (gt=0) -- never reaches selection."""
    with pytest.raises(ValueError, match="cloud_spill_to_local_after_seconds"):
        ControlSettings(cloud_spill_to_local_after_seconds=0)


def test_cloud_spill_to_local_rejects_too_large() -> None:
    """A value at/above the one-day cap fails validation (lt=86400)."""
    with pytest.raises(ValueError, match="cloud_spill_to_local_after_seconds"):
        ControlSettings(cloud_spill_to_local_after_seconds=86400)
