"""The control-plane-unreachable breaker is operator-visible on the Analyze page (phaze-j0ixx, acceptance c).

The lane snapshot carries each backend's breaker state; the lane card and the Analysis Health card render
a held lane as HELD with its reason; the Cloud Routing hold reason stops counting a held lane's slots as
capacity the next drain tick would use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from phaze.models.backend_breaker import BackendBreaker
from phaze.services.backends import ComputeAgentBackend, KueueBackend, derive_cloud_hold_reason, lane_snapshot as backends_mod
from phaze.services.backends.lane_snapshot import get_backend_lane_snapshot
from tests._queue_fakes import seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_REASON = "3 files exited 14 (control plane unreachable) within 15 min"


async def _trip(session: AsyncSession, backend_id: str) -> datetime:
    now = datetime.now(UTC)
    session.add(BackendBreaker(backend_id=backend_id, tripped_at=now, trip_reason=_REASON, next_probe_at=now + timedelta(minutes=15)))
    await session.commit()
    return now


def _cloud_on(monkeypatch: pytest.MonkeyPatch, *backends: Any) -> None:
    monkeypatch.setattr(backends_mod, "get_settings", lambda: type("S", (), {"cloud_enabled": True})())
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: list(backends))
    monkeypatch.setattr(backends_mod, "_probe_availability", AsyncMock(return_value={b.id: True for b in backends}))


@pytest.mark.asyncio
async def test_snapshot_marks_only_the_tripped_lane_held(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The held lane carries its reason and next probe; every other lane reads closed."""
    kueue, compute = KueueBackend(id="burst", rank=20, cap=4), ComputeAgentBackend(id="a1", rank=10, cap=2)
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [kueue, compute])
    monkeypatch.setattr(backends_mod, "_probe_availability", AsyncMock(return_value={"burst": True, "a1": True}))
    tripped = await _trip(session, "burst")

    by_id = {lane["id"]: lane for lane in await get_backend_lane_snapshot(session)}

    assert by_id["burst"]["breaker_held"] is True
    assert by_id["burst"]["breaker_reason"] == _REASON
    assert by_id["burst"]["breaker_tripped_at"] == tripped
    assert by_id["burst"]["breaker_next_probe_at"] == tripped + timedelta(minutes=15)
    assert (by_id["a1"]["breaker_held"], by_id["a1"]["breaker_reason"]) == (False, None)
    assert by_id["burst"]["available"] is True  # held is not offline


@pytest.mark.asyncio
async def test_hold_reason_names_the_breaker_when_every_reachable_lane_is_held(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the only cloud lane held, the caption names the breaker rather than claiming free slots."""
    _cloud_on(monkeypatch, KueueBackend(id="burst", rank=20, cap=4))
    await _trip(session, "burst")

    assert await derive_cloud_hold_reason(session) == "held — control plane unreachable from burst pods (breaker open, probing every 15 min)"


@pytest.mark.asyncio
async def test_hold_reason_counts_only_the_unheld_lanes_slots(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """One held and one healthy lane: the free-slot figure is the healthy lane's alone."""
    _cloud_on(monkeypatch, KueueBackend(id="burst", rank=20, cap=4), ComputeAgentBackend(id="a1", rank=10, cap=2))
    await _trip(session, "burst")
    await seed_active_agent(session, kind="fileserver")

    assert await derive_cloud_hold_reason(session) == "queued — 2 free slots, dispatching on next drain tick (~5 min)"


def _held_lane(**overrides: Any) -> dict[str, Any]:
    lane = {
        "id": "burst",
        "kind": "kueue",
        "rank": 20,
        "cap": 4,
        "in_flight": 0,
        "available": True,
        "quota_wait": 0,
        "inadmissible": 0,
        "queued": 0,
        "working": 0,
        "active": 0,
        "processed_24h": 0,
        "processed_lifetime": 0,
        "breaker_held": True,
        "breaker_reason": _REASON,
        "breaker_tripped_at": datetime(2026, 9, 26, 21, 7, tzinfo=UTC),
        "breaker_next_probe_at": datetime(2026, 9, 26, 21, 22, tzinfo=UTC),
    }
    lane.update(overrides)
    return lane


def test_lane_card_renders_a_held_lane_as_an_alert() -> None:
    """HELD badge plus an alert that says why, that nothing is charged, and when the probe runs."""
    from phaze.routers.pipeline import templates

    body = templates.get_template("pipeline/partials/_lane_card.html").render(lane=_held_lane(), selected_lane=None)

    assert "HELD" in body
    assert 'role="alert"' in body
    assert _REASON in body
    assert "No attempts are being charged" in body
    assert "from 21:22 UTC" in body


def test_lane_card_without_breaker_keys_is_not_held() -> None:
    """A lane dict from before this bead (no breaker keys) renders exactly as before -- no HELD."""
    from phaze.routers.pipeline import templates

    lane = {k: v for k, v in _held_lane().items() if not k.startswith("breaker_")}
    body = templates.get_template("pipeline/partials/_lane_card.html").render(lane=lane, selected_lane=None)

    assert "HELD" not in body
    assert "READY" in body


def test_health_card_leads_with_a_held_lane() -> None:
    """The Analysis Health card turns red and names the held lane and its reason."""
    from phaze.routers.pipeline import templates

    body = templates.get_template("pipeline/partials/analysis_failed_card.html").render(lanes=[_held_lane(breaker_reason=None)], oob=False)

    assert 'role="alert"' in body
    assert "border-red-300" in body
    assert "KUEUE · burst cannot reach the control plane" in body
    assert "Breaker open." in body
