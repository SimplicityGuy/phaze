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

from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient


@pytest.mark.asyncio
async def test_root_renders_shell_analyze_default(client: AsyncClient) -> None:
    """SHELL-01 -- GET / renders the shell with Analyze as the default active stage (no redirect)."""
    response = await client.get("/")
    # A plain GET / is the shell root itself -- it must render, NOT redirect anywhere.
    assert response.status_code == 200
    body = response.text
    # The single stable swap target every rail node innerHTML-swaps.
    assert 'id="stage-workspace"' in body
    # Analyze is the selected/active default: the swap target carries the stage marker AND
    # the bridged pipeline-dashboard DAG content (dag_canvas id="pipeline-dag") renders
    # inside it (D-01). The rail aria-current assertion is added in Plan 03.
    assert 'data-stage="analyze"' in body
    assert 'id="pipeline-dag"' in body


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


def test_rail_nodes_wired() -> None:
    """SHELL-02 -- filled by Plan 57-03 Task 3."""
    ...


def test_tabbar_removed_header_present() -> None:
    """SHELL-03 -- filled by Plan 57-03 Task 3."""
    ...


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
