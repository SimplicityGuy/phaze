"""phaze-zxbh -- the shared #detail-pane must never pin the WRONG lane.

The lane body's own 5s own-tick (``_lane_detail.html``) and the lane-card row-click trigger
(``_lane_card.html``) both innerHTML-swap into the SAME ``#detail-pane`` target from DIFFERENT
elements. htmx's default per-element request de-duplication does not serialize two different
elements aiming at one target, so a stale own-tick response for the PREVIOUSLY selected lane can
land after a newer row click's response, silently pinning the pane -- and its own re-injected 5s
tick -- to the wrong lane forever.

The fix has two layers:

1. ``hx-sync="#detail-pane:abort"`` on both producers (the own-tick div and the row trigger), so
   a newer request aborts an in-flight older one instead of racing it to the swap.
2. A ``data-lane-id`` stamp on every ``_lane_detail.html`` response body (both the resolved-lane
   and offline branches), checked by ``_detail_pane.html``'s ``onLoaded()`` against the current
   ``?lane=`` selection -- the backstop that also covers the deep-link ``hx-trigger="load"``
   producer on ``#detail-pane`` itself, which cannot join the ``hx-sync`` group.

Like the sibling ``test_lane_detail.py`` fixture assertions, this renders the real templates (via
the router's own Jinja environment, matching ``_render_lane_detail`` there) and asserts over
template SOURCE / rendered DOM, never over a live browser race.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_LANE_CARD = _TEMPLATES / "pipeline" / "partials" / "_lane_card.html"
_LANE_DETAIL = _TEMPLATES / "pipeline" / "partials" / "_lane_detail.html"
_DETAIL_PANE = _TEMPLATES / "pipeline" / "partials" / "_detail_pane.html"


def _render_lane_detail(**context: object) -> str:
    from phaze.routers.pipeline import templates

    return templates.get_template("pipeline/partials/_lane_detail.html").render(**context)


def test_lane_card_trigger_and_own_tick_share_a_sync_group() -> None:
    """Both #detail-pane producers must declare the SAME hx-sync selector + strategy."""
    card = _LANE_CARD.read_text()
    detail = _LANE_DETAIL.read_text()
    assert 'hx-sync="#detail-pane:abort"' in card, "the lane-card row trigger no longer syncs against the shared #detail-pane target"
    assert 'hx-sync="#detail-pane:abort"' in detail, "the lane body's own-tick no longer syncs against the shared #detail-pane target"


def test_lane_detail_resolved_lane_stamps_its_own_id() -> None:
    """The resolved-lane branch must stamp data-lane-id so onLoaded() can detect a stale swap."""
    body = _render_lane_detail(
        lane={"id": "k8s-vox", "kind": "kueue", "rank": 1, "cap": 4, "in_flight": 1, "available": True, "quota_wait": 0, "inadmissible": 0},
        backend_id="k8s-vox",
        recent_completions=[],
        queue_depths={},
        queue_depths_agent=None,
        queue_depths_note=None,
        refreshed_at=None,
        recent_n=20,
    )
    assert 'data-lane-id="k8s-vox"' in body


def test_lane_detail_offline_branch_still_stamps_the_requested_id() -> None:
    """An unknown/offline backend_id must ALSO stamp its id -- a late response for it must be detectable too."""
    body = _render_lane_detail(
        lane=None,
        backend_id="__nope__",
        recent_completions=[],
        queue_depths={},
        queue_depths_agent=None,
        queue_depths_note=None,
        refreshed_at=None,
        recent_n=20,
    )
    assert 'data-lane-id="__nope__"' in body


def test_detail_pane_on_loaded_checks_identity_before_trusting_the_swap() -> None:
    """onLoaded() must compare the swapped body's stamped id against the current ?lane= selection."""
    shell = _DETAIL_PANE.read_text()
    assert "getAttribute('data-' + this.param + '-id')" in shell, (
        "onLoaded() no longer reads the swapped body's stamped identity -- a late/mismatched "
        "own-tick response can silently pin the pane to the wrong lane again"
    )
    assert "bodyId !== id" in shell, "onLoaded() no longer compares the stamped identity against the current ?lane= selection"


def test_detail_pane_self_heals_via_the_real_trigger_not_a_source_less_ajax() -> None:
    """On mismatch the pane must re-trigger the REAL row element, never htmx.ajax() with no source.

    A source-less htmx.ajax() call inherits attributes from document.body (htmx 2.0.10
    issueAjaxRequest), silently dropping whatever the correct element would have sent -- the exact
    phaze-ircc failure mode this repo just fixed on the sibling #pipeline-stats poll.
    """
    shell = _DETAIL_PANE.read_text()
    assert "window.htmx.trigger(trigger, 'click')" in shell
    assert "window.htmx.ajax(" not in shell, (
        "the mismatch self-heal must reuse the real trigger element via htmx.trigger(), not a call to htmx.ajax()"
    )


@pytest.mark.asyncio
async def test_lane_detail_endpoint_response_carries_the_requested_id_on_both_branches(client: AsyncClient, session: AsyncSession) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: both a resolved and an unresolved backend_id round-trip their own data-lane-id."""
    unknown = await client.get("/pipeline/lanes/__definitely_not_registered__")
    assert unknown.status_code == 200, unknown.text
    assert 'data-lane-id="__definitely_not_registered__"' in unknown.text
