"""Unit tests for the Phase 49 `cloud_route_threshold_sec` config knob (D-07)
and the code-only `FileState.AWAITING_CLOUD` held-state member (D-01).

`cloud_route_threshold_sec` mirrors `straggler_threshold_sec`: a bounded pydantic
int Field (gt=0, lt=86400) on `ControlSettings`, bound from
`PHAZE_CLOUD_ROUTE_THRESHOLD_SEC` via `AliasChoices`. An out-of-range value fails
validation at construction time (T-49-01) and never reaches the SQL `duration >=
threshold` compare. These are pure pydantic-settings / StrEnum tests — no DB, no
Redis required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.config import ControlSettings
from phaze.models.file import FileState


if TYPE_CHECKING:
    import pytest as _pytest


def test_cloud_route_threshold_default() -> None:
    """An unset knob defaults to 5400 seconds (90 minutes)."""
    assert ControlSettings().cloud_route_threshold_sec == 5400


def test_cloud_route_threshold_env_alias(monkeypatch: _pytest.MonkeyPatch) -> None:
    """PHAZE_CLOUD_ROUTE_THRESHOLD_SEC binds to the field and parses as int."""
    monkeypatch.setenv("PHAZE_CLOUD_ROUTE_THRESHOLD_SEC", "7200")
    assert ControlSettings().cloud_route_threshold_sec == 7200


def test_cloud_route_threshold_rejects_zero() -> None:
    """A non-positive value fails validation (gt=0) — never reaches the SQL compare."""
    with pytest.raises(ValueError, match="cloud_route_threshold_sec"):
        ControlSettings(cloud_route_threshold_sec=0)


def test_cloud_route_threshold_rejects_too_large() -> None:
    """A value at/above the one-day cap fails validation (lt=86400)."""
    with pytest.raises(ValueError, match="cloud_route_threshold_sec"):
        ControlSettings(cloud_route_threshold_sec=86400)


def test_awaiting_cloud_state_member() -> None:
    """AWAITING_CLOUD is a valid StrEnum member with value 'awaiting_cloud' (D-01)."""
    assert FileState.AWAITING_CLOUD == "awaiting_cloud"
    assert FileState("awaiting_cloud") is FileState.AWAITING_CLOUD
    # 'awaiting_cloud' is 14 chars — fits String(30), so it is code-only (no migration).
    assert len(FileState.AWAITING_CLOUD.value) <= 30
