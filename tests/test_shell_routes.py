"""Behavioral tests for the v7.0 shell routes (SHELL-01..04).

Wave-1 (Plan 57-01) seeded these six function names as collectible body-less stubs so
the ``-k`` selectors / ``::node-id`` verify commands in later plans resolve immediately.

Plan 57-02 (Task 3) fills the SHELL-01/SHELL-02-fragment/SHELL-04 behaviors below; Plan
57-03 (Task 3) fills the remaining two (``test_rail_nodes_wired`` /
``test_tabbar_removed_header_present``) once the rail + header partials land. The two
Plan-03 functions stay body-less here -- they are REPLACED (not redeclared) by Plan 03.

Function -> requirement map (see 57-VALIDATION.md "Per-Task Verification Map"):
    test_root_renders_shell_analyze_default  -> SHELL-01   (Plan 02)
    test_stage_fragment_is_bare              -> SHELL-02   (Plan 02)
    test_unknown_stage_404                   -> SHELL-02 (negative, Plan 02)
    test_rail_nodes_wired                    -> SHELL-02   (Plan 03)
    test_tabbar_removed_header_present       -> SHELL-03   (Plan 03)
    test_theme_and_store_preserved           -> SHELL-04   (Plan 02)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient


# The 12 navigable rail-node ids (VERBATIM prototype RAIL order), each wired to /s/<id>.
_RAIL_STAGES = [
    "discover",
    "metadata",
    "fingerprint",
    "analyze",
    "trackid",
    "tracklist",
    "propose",
    "rename",
    "tagwrite",
    "move",
    "dedupe",
    "cue",
]


@pytest.mark.asyncio
async def test_root_renders_shell_analyze_default(client: AsyncClient) -> None:
    """SHELL-01 -- GET / renders the shell with Analyze as the default active stage (no redirect)."""
    response = await client.get("/")
    # A plain GET / is the shell root itself -- it must render, NOT redirect anywhere.
    assert response.status_code == 200
    body = response.text
    # The single stable swap target every rail node innerHTML-swaps.
    assert 'id="stage-workspace"' in body
    # Analyze is the selected/active default: the swap target carries the stage marker AND the
    # real Analyze workspace (Phase 58 / 58-04) renders inside it -- the lane-card grid
    # (#analyze-lanes) supersedes the Phase-57 bridged dag_canvas (id="pipeline-dag"), which now
    # lives only on the legacy /pipeline/ dashboard. The rail aria-current assertion is added in Plan 03.
    assert 'data-stage="analyze"' in body
    assert 'id="analyze-lanes"' in body


@pytest.mark.asyncio
async def test_stage_fragment_is_bare(client: AsyncClient) -> None:
    """SHELL-02 -- /s/<stage> is a bare fragment on an HX request, the full shell on direct nav (D-01)."""
    hx = await client.get("/s/discover", headers={"HX-Request": "true"})
    assert hx.status_code == 200
    # Content-only: a swapped fragment NEVER carries the document wrapper or head (no
    # duplicate landmarks / skip-links injected -- the chrome persists across swaps).
    assert "<html" not in hx.text
    assert "<head" not in hx.text

    full = await client.get("/s/discover")
    assert full.status_code == 200
    # The non-HX request is the full shell (carries the swap target + chrome).
    assert 'id="stage-workspace"' in full.text


@pytest.mark.asyncio
async def test_unknown_stage_404(client: AsyncClient) -> None:
    """SHELL-02 (negative) -- an unknown stage 404s (D-02 whitelist; `stage` is never a template path)."""
    response = await client.get("/s/does-not-exist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rail_nodes_wired(client: AsyncClient) -> None:
    """SHELL-02 -- every navigable rail node carries the HTMX swap wiring; analyze is active.

    The DAG rail is the nav spine: each of the 12 prototype-order nodes swaps ONLY
    ``#stage-workspace`` (innerHTML) via ``/s/<id>`` with ``hx-push-url``. The ``/`` default
    marks the analyze node ``aria-current="page"``.
    """
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text

    # Every navigable node carries hx-get="/s/<id>" for all 12 prototype-order stages.
    for stage in _RAIL_STAGES:
        assert f'hx-get="/s/{stage}"' in body, f"rail node {stage} missing hx-get wiring"

    # The single stable swap target + push-url are present on the rail nodes.
    assert 'hx-target="#stage-workspace"' in body
    assert 'hx-swap="innerHTML"' in body
    assert 'hx-push-url="true"' in body
    # At least one swap-target attr per navigable node (12) -- the +Scan CTA adds one more.
    assert body.count('hx-target="#stage-workspace"') >= len(_RAIL_STAGES)

    # The analyze node (the / default) is the active rail node: aria-current="page" sits on
    # the same element carrying data-rail-stage="analyze".
    assert re.search(r'data-rail-stage="analyze"[^>]*aria-current="page"', body), 'analyze rail node must carry aria-current="page" on the shell root'


@pytest.mark.asyncio
async def test_tabbar_removed_header_present(client: AsyncClient) -> None:
    """SHELL-03 -- the legacy top <nav> tab-bar is gone; the ⌘K header + status strip is in.

    The shell does NOT render ``base.html``, so the legacy ``aria-label="Main navigation"``
    tab-bar and its tab hrefs are absent. The header instead carries the ⌘K command-palette
    affordance, the agent status dots, and the Agents link to ``/admin/agents``.
    """
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text

    # Legacy tab-bar removed: the base.html nav landmark + the legacy Search tab href are
    # gone. (The /proposals/ href is NOT a valid marker -- the bridged Analyze dashboard
    # content legitimately links to it; only base.html's nav landmark + /search/ tab are
    # unique to the retired tab-bar.)
    assert 'aria-label="Main navigation"' not in body
    assert 'href="/search/"' not in body

    # ⌘K header command bar present (the trigger button + the ⌘K chip).
    assert 'id="cmdk-trigger"' in body
    assert "⌘K" in body

    # Agent status strip: the dot/count bind to the existing $store.pipeline.agentOnline key,
    # and the Agents link points at the existing /admin/agents route.
    assert "agentOnline" in body
    assert 'href="/admin/agents"' in body


@pytest.mark.asyncio
async def test_theme_and_store_preserved(client: AsyncClient) -> None:
    """SHELL-04 -- theme/brand machinery lifted verbatim; $store.pipeline consumed, not redefined."""
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text
    # No-FOUC theme script + the theme store (auto/dark/light) survive in the shell <head>.
    assert "_applyTheme" in body
    assert "Alpine.store('theme'" in body
    # The Jura brand font link is preserved.
    assert "Jura:wght" in body
    # $store.pipeline is CONSUMED, never redefined: exactly one Alpine.store('pipeline' seed
    # (the embedded DAG canvas only writes $store.pipeline.<key>, it does not redefine it).
    assert body.count("Alpine.store('pipeline'") == 1
