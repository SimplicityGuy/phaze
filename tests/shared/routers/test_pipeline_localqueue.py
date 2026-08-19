"""Dashboard LocalQueue-unreachable alert tests (Phase 56, KDEPLOY-04 / D-05) -- REVISED phaze-6r39.

Rewritten for phaze-6r39: the alert used to be driven by patching the router's imported
``get_localqueue_unreachable`` (a Redis-key reader written once, at controller boot, by
``tasks/controller.py``'s startup probe). That mechanism is retired entirely -- it never cleared once
connectivity was restored (the reported bug) and never fired at all for an outage that began AFTER
boot (the silent, more dangerous half). See the bead's DESIGN and
``services/backends.py::derive_localqueue_unreachable``. The alert is now derived purely from the SAME
live lane snapshot (``get_backend_lane_snapshot``) both render paths already fetch on every poll, so
every test here drives state by patching ``phaze.routers.pipeline.dashboard_stats.get_backend_lane_snapshot`` with a
fake lane list instead of touching Redis at all.

Acceptance criterion 3 (phaze-6r39) is the one that matters most and is easy to get wrong: a cluster
reachable at boot that goes unreachable AFTER boot, with NO process restart, must show the banner on
the NEXT poll. A test that merely checks "no banner when nothing has ever failed" would pass against
the OLD Redis-flag code too, since the old code never showed the banner on a fresh boot either -- its
defect was that it *also* never showed the banner after a LATER failure. So
``test_localqueue_alert_appears_after_boot_without_restart`` below drives the SAME lane-snapshot mock
through two DIFFERENT return values across two sequential requests on the SAME app/client (no restart
between them) and asserts the banner state flips from absent to present.

Verified per the bead's instruction: reverting the two production call sites
(``services/backends.py::derive_localqueue_unreachable`` and its callers in ``routers/pipeline.py``)
back to the pre-fix ``get_localqueue_unreachable(app.state.redis)`` reads while keeping this test file
makes ``test_localqueue_alert_appears_after_boot_without_restart`` FAIL -- see that test's docstring
for the exact failure observed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient


# Locked amber copy (56-UI-SPEC §Copywriting / D-05): warn + surface, app stays up (NOT red).
_ALERT_COPY = "K8s LocalQueue unreachable"
_CARD_ID = 'id="localqueue-card"'


def _kueue_lane(*, available: bool, lane_id: str = "kueue-1") -> dict[str, Any]:
    """One minimal kueue lane dict in the ``get_backend_lane_snapshot`` shape."""
    return {"id": lane_id, "kind": "kueue", "rank": 10, "cap": 2, "in_flight": 0, "available": available, "quota_wait": 0, "inadmissible": 0}


def _local_lane() -> dict[str, Any]:
    """A LocalBackend lane -- always available, and never counted toward the kueue-only alert."""
    return {"id": "local", "kind": "local", "rank": 99, "cap": 1, "in_flight": 0, "available": True, "quota_wait": 0, "inadmissible": 0}


def _patch_snapshot(monkeypatch: pytest.MonkeyPatch, lanes: list[dict[str, Any]]) -> None:
    """Drive the alert by patching the router's imported ``get_backend_lane_snapshot`` (GREEN-compatible target).

    Both ``build_dashboard_context`` and ``pipeline_stats_partial`` resolve ``get_backend_lane_snapshot``
    off the router module's globals (a plain call in the first, inside a lambda closure fed to
    ``asyncio.gather`` in the second), so patching the module attribute drives both call sites
    identically -- and ``derive_localqueue_unreachable`` (a pure function) recomputes the flag from
    whatever ``lanes`` this returns on every call, with no cross-request state at all.
    """
    monkeypatch.setattr("phaze.routers.pipeline.dashboard_stats.get_backend_lane_snapshot", AsyncMock(return_value=lanes))


@pytest.mark.asyncio
async def test_localqueue_alert_empty_when_all_kueue_lanes_reachable(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every kueue lane available -> no amber alert (degrade-safe silent, mirrors the old contract)."""
    _patch_snapshot(monkeypatch, [_local_lane(), _kueue_lane(available=True)])

    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert _ALERT_COPY not in response.text


@pytest.mark.asyncio
async def test_localqueue_alert_renders_when_kueue_lane_unreachable(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 1: a kueue lane whose live probe FAILS -> the amber banner renders on first load."""
    _patch_snapshot(monkeypatch, [_local_lane(), _kueue_lane(available=False)])

    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert _CARD_ID in response.text
    assert _ALERT_COPY in response.text


@pytest.mark.asyncio
async def test_localqueue_alert_clears_on_next_poll_when_probe_recovers(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 2 (the reported bug, the STICKY half): unreachable -> reachable, NO restart, clears the banner.

    This is the primary regression test. The old Redis-flag mechanism was written ONCE at controller
    boot and never rewritten on recovery, so once set it stayed set until the control worker restarted
    -- exactly the reported symptom. Here the SAME client/app (no restart at all) polls twice; the
    second poll's lane snapshot reports the kueue lane reachable again, and the banner must be gone.
    """
    snapshot = AsyncMock(side_effect=[[_local_lane(), _kueue_lane(available=False)], [_local_lane(), _kueue_lane(available=True)]])
    monkeypatch.setattr("phaze.routers.pipeline.dashboard_stats.get_backend_lane_snapshot", snapshot)

    first = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    second = await client.get("/pipeline/stats", headers={"HX-Request": "true"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert _ALERT_COPY in first.text
    assert _ALERT_COPY not in second.text


@pytest.mark.asyncio
async def test_localqueue_alert_appears_after_boot_without_restart(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 3 (the SILENT half, phaze-6r39): reachable at boot, unreachable afterwards, NO restart -> alert appears.

    Deliberately NOT a trivial "no banner while healthy" assertion. It asserts the banner is ABSENT on
    the first poll and PRESENT on the second, on the SAME client/app throughout (never a restart) --
    exactly the "cluster goes down after boot" scenario the old mechanism was structurally incapable of
    surfacing at all (nothing wrote the Redis key except the one-shot startup probe).

    Verified against the pre-fix tree per the bead's instruction: with
    ``services/backends.py::derive_localqueue_unreachable`` removed and both ``routers/pipeline.py``
    call sites reverted to ``get_localqueue_unreachable(getattr(app_state, "redis", None))`` (the
    pre-phaze-6r39 shape), this test FAILS on the second poll with
    ``AssertionError: assert 'K8s LocalQueue unreachable' in '<...second.text...>'`` -- the old code
    never re-reads cluster reachability after controller startup, and the test client's
    ``app.state.redis`` is absent (the lifespan is skipped in tests), so
    ``get_localqueue_unreachable`` degrades to False on BOTH polls regardless of this test's lane
    snapshot, which the old code path never even consults.
    """
    snapshot = AsyncMock(side_effect=[[_local_lane(), _kueue_lane(available=True)], [_local_lane(), _kueue_lane(available=False)]])
    monkeypatch.setattr("phaze.routers.pipeline.dashboard_stats.get_backend_lane_snapshot", snapshot)

    first = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    second = await client.get("/pipeline/stats", headers={"HX-Request": "true"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert _ALERT_COPY not in first.text
    assert _ALERT_COPY in second.text


@pytest.mark.asyncio
async def test_localqueue_alert_silent_with_zero_kueue_backends(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 4 (WR-01): zero kueue lanes (all-local / compute-only) -> no banner, ever."""
    _patch_snapshot(monkeypatch, [_local_lane()])

    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert _ALERT_COPY not in response.text


@pytest.mark.asyncio
async def test_localqueue_alert_never_500s_when_snapshot_is_fully_degraded(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 6: a fully degraded lane snapshot (get_backend_lane_snapshot's own -> [] contract) never 500s the poll.

    ``get_backend_lane_snapshot`` already owns the never-500 degrade for any probe timeout/kube
    exception (collapses to ``[]``); this asserts the alert derivation composes safely with that
    contract -- an empty snapshot means "nothing known to be unreachable", not a 500.
    """
    _patch_snapshot(monkeypatch, [])

    response = await client.get("/pipeline/stats", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert _ALERT_COPY not in response.text


@pytest.mark.asyncio
async def test_localqueue_alert_oob_on_stats(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The card carrier has a stable id on BOTH first-load and the 5s OOB poll, with hx-swap-oob on the poll."""
    _patch_snapshot(monkeypatch, [_kueue_lane(available=False)])

    first = await client.get("/pipeline/stats", headers={"HX-Request": "true"})
    stats = await client.get("/pipeline/stats")

    assert first.status_code == 200
    assert stats.status_code == 200
    assert _CARD_ID in first.text
    assert _CARD_ID in stats.text
    # The OOB re-push carries hx-swap-oob so HTMX swaps the out-of-band card on the poll.
    assert 'hx-swap-oob="true"' in stats.text
    assert _ALERT_COPY in stats.text


def test_derive_localqueue_unreachable_any_kueue_lane_unavailable() -> None:
    """Unit coverage for the pure aggregate: ANY unreachable kueue lane -> True; degrade-safe over ``[]``."""
    from phaze.services.backends import derive_localqueue_unreachable

    assert derive_localqueue_unreachable([]) is False
    assert derive_localqueue_unreachable([_local_lane()]) is False
    assert derive_localqueue_unreachable([_local_lane(), _kueue_lane(available=True)]) is False
    assert derive_localqueue_unreachable([_local_lane(), _kueue_lane(available=False)]) is True
    # ANY-unreachable aggregation (D-05/D-06 semantic preserved): one bad cluster among several
    # configured kueue backends still lights the alert for the whole deploy.
    assert derive_localqueue_unreachable([_kueue_lane(available=True, lane_id="a"), _kueue_lane(available=False, lane_id="b")]) is True
