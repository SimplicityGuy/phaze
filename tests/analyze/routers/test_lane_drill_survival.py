"""Phase 88 (88-01, DRILL-03 / D-02 / D-09): lane-card drill-in trigger + poll-survival contract.

Locks the wave-1 lane surface against the shared _detail_pane.html shell:

* the `_lane_card.html` card root is a keyboard-accessible `role="button"` drill-in trigger with a
  STABLE `id="lane-trigger-{id}"`, `tabindex="0"`, an `aria-label`, the `onkeydown` Space handler, and
  the HTMX wiring (`hx-get="/pipeline/lanes/{id}"`, `hx-target="#detail-pane"`, `hx-push-url` `?lane=`);
* the persistent `#pipeline-stats` poll re-emits the selected-highlight (`aria-current="true"` + the
  `ring-2 ring-blue-500` ring) on the card whose id matches `?lane=` (D-02), so the ring survives every
  5s `outerHTML` swap; an unknown/absent `?lane=` highlights nothing and NEVER 500s.

The markup assertions are satisfied by Task 2 (trigger wiring); the poll-highlight assertions by Task 3
(`?lane=` threaded into the grid). RED until then; collectable from Task 1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _first_lane_id(session: AsyncSession) -> str:
    """Return a real lane id from the degrade-safe snapshot (default registry -> 'local')."""
    from phaze.services.backends import get_backend_lane_snapshot

    lanes = await get_backend_lane_snapshot(session)
    if not lanes:
        pytest.skip("no backend lanes resolved in this environment")
    return str(lanes[0]["id"])


@pytest.mark.asyncio
async def test_lane_card_trigger_markup(client: AsyncClient, session: AsyncSession) -> None:
    """The lane card renders as a keyboard-accessible role=button drill-in trigger (DRILL-03 / D-09)."""
    lane_id = await _first_lane_id(session)
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200, response.text
    body = response.text

    assert f'id="lane-trigger-{lane_id}"' in body
    assert 'role="button"' in body
    assert 'tabindex="0"' in body
    assert "aria-label=" in body
    # HTMX drill-in wiring points at the shared #detail-pane swap target.
    assert f'hx-get="/pipeline/lanes/{lane_id}"' in body
    assert 'hx-target="#detail-pane"' in body
    assert f"/s/analyze?lane={lane_id}" in body  # hx-push-url carries the ?lane= selection
    # Space activation for a role=button div is not native — the inline onkeydown handler is REQUIRED.
    assert "onkeydown" in body


@pytest.mark.asyncio
async def test_lane_card_no_unsafe_filter(client: AsyncClient, session: AsyncSession) -> None:
    """Operator-declared lane id/kind stay Jinja-autoescaped — never |safe (T-88-01)."""
    await _first_lane_id(session)
    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "|safe" not in response.text


@pytest.mark.asyncio
async def test_pipeline_stats_reemits_selected_ring(client: AsyncClient, session: AsyncSession) -> None:
    """`GET /pipeline/stats?lane={known}` re-emits aria-current + the selected ring (D-02 poll survival)."""
    lane_id = await _first_lane_id(session)
    response = await client.get("/pipeline/stats", params={"lane": lane_id})
    assert response.status_code == 200, response.text
    body = response.text
    assert 'aria-current="true"' in body
    assert "ring-2 ring-blue-500" in body


@pytest.mark.asyncio
async def test_pipeline_stats_unknown_lane_highlights_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """An unknown `?lane=` is a lookup-miss: 200 with NO highlight, never a 500 (T-88-01 known-set)."""
    await _first_lane_id(session)
    response = await client.get("/pipeline/stats", params={"lane": "__nonexistent__"})
    assert response.status_code == 200, response.text
    assert 'aria-current="true"' not in response.text
