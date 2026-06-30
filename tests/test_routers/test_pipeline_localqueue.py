"""Dashboard LocalQueue-unreachable alert tests (Phase 56, KDEPLOY-04 / D-05).

RED until 56-01 adds ``phaze.services.pipeline.get_localqueue_unreachable`` and 56-02 wires the flag
into both pipeline render paths + the new ``localqueue_card.html`` partial. Clones
``test_pipeline_inadmissible.py``: the amber alert is empty when healthy (degrade-safe -> reachable),
renders the locked copy when flagged, and is re-pushed OOB on the 5s ``/pipeline/stats`` poll. The
flag is driven by patching the router's ``get_localqueue_unreachable`` reference (``raising=False`` so
the tests run before the import exists); a final unit test pins the degrade-to-False read contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient


# Locked amber copy (56-UI-SPEC §Copywriting / D-05): warn + surface, app stays up (NOT red).
_ALERT_COPY = "K8s LocalQueue unreachable"
_CARD_ID = 'id="localqueue-card"'


def _patch_flag(monkeypatch: pytest.MonkeyPatch, *, unreachable: bool) -> None:
    """Drive the dashboard flag by patching the router's degrade-safe read (GREEN-compatible target).

    The implementation router does ``from phaze.services.pipeline import get_localqueue_unreachable``
    and seeds the result into BOTH render contexts; patching that name flips the alert. ``raising=False``
    lets the test set the attribute before 56-02 adds the import (RED: the card simply never renders).
    """
    monkeypatch.setattr(
        "phaze.routers.pipeline.get_localqueue_unreachable",
        AsyncMock(return_value=unreachable),
        raising=False,
    )


@pytest.mark.asyncio
async def test_localqueue_alert_empty_when_reachable(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachable (flag absent) -> the dashboard renders 200 with NO amber alert body (degrade-safe silent)."""
    _patch_flag(monkeypatch, unreachable=False)

    response = await client.get("/pipeline/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert _ALERT_COPY not in response.text


@pytest.mark.asyncio
async def test_localqueue_alert_renders_when_flagged(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flagged unreachable -> the locked amber copy "K8s LocalQueue unreachable" appears on first load."""
    _patch_flag(monkeypatch, unreachable=True)

    response = await client.get("/pipeline/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert _CARD_ID in response.text
    assert _ALERT_COPY in response.text


@pytest.mark.asyncio
async def test_localqueue_alert_oob_on_stats(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The card carrier has a stable id on BOTH first-load GET /pipeline and the 5s OOB GET /pipeline/stats."""
    _patch_flag(monkeypatch, unreachable=True)

    first = await client.get("/pipeline/", headers={"HX-Request": "true"})
    stats = await client.get("/pipeline/stats")

    assert first.status_code == 200
    assert stats.status_code == 200
    assert _CARD_ID in first.text
    assert _CARD_ID in stats.text
    # The OOB re-push carries hx-swap-oob so HTMX swaps the out-of-band card on the poll.
    assert 'hx-swap-oob="true"' in stats.text
    assert _ALERT_COPY in stats.text


@pytest.mark.asyncio
async def test_get_localqueue_unreachable_degrades_to_false() -> None:
    """The Redis read degrades to False on a missing handle (None) AND on any Redis error (T-54-10).

    A missing ``app.state.redis`` (test client skips the lifespan) or a Redis hiccup must NEVER 500 the
    hot 5s poll -- the alert simply stays silent (reachable). RED until 56-01 adds the function.
    """
    from phaze.services import pipeline

    # Missing handle -> False (silent/reachable).
    assert await pipeline.get_localqueue_unreachable(None) is False

    # A raising Redis handle -> False (degrade-safe, never propagates).
    bad_redis = AsyncMock()
    bad_redis.exists = AsyncMock(side_effect=RuntimeError("redis down"))
    assert await pipeline.get_localqueue_unreachable(bad_redis) is False
