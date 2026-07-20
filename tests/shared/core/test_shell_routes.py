"""Behavioral tests for the v7.0 shell routes (SHELL-01..04).

Wave-1 (Plan 57-01) seeded these six function names as collectible body-less stubs so
the ``-k`` selectors / ``::node-id`` verify commands in later plans resolve immediately.

Plan 57-02 (Task 3) fills the SHELL-01/SHELL-02-fragment/SHELL-04 behaviors below; Plan
57-03 (Task 3) fills the remaining two (``test_rail_nodes_wired`` /
``test_tabbar_removed_header_present``) once the rail + header partials land. The two
Plan-03 functions stay body-less here -- they are REPLACED (not redeclared) by Plan 03.

Quick 260707-sq3 repointed ``GET /`` from the Analyze dashboard to the static Summary
landing placeholder (SQ3-01..03); Analyze stays reachable at ``/s/analyze``.

Function -> requirement map (see 57-VALIDATION.md "Per-Task Verification Map"):
    test_root_renders_shell_summary_default  -> SHELL-01 / SQ3-02 (Plan 02, quick 260707-sq3)
    test_analyze_still_reachable_at_s_analyze -> SQ3-03  (quick 260707-sq3)
    test_summary_stage_route_and_fragment    -> SQ3-01   (quick 260707-sq3)
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


# The 14 navigable rail-node ids (VERBATIM prototype RAIL order, with the quick-260707-sq3
# Summary landing node prepended and the Phase-87 87-09 Files stage-matrix overview inserted
# right after it), each wired to /s/<id>.
_RAIL_STAGES = [
    "summary",
    "files",
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
async def test_root_renders_shell_summary_default(client: AsyncClient) -> None:
    """SHELL-01 / SQ3-02 -- GET / renders the shell with the Summary placeholder as the default stage.

    Quick 260707-sq3 repointed the landing slot from Analyze to the static Summary placeholder.
    No file seed is needed: the first-run empty-state swap is confined to the analyze branch of
    ``_render_stage``, and Summary performs zero DB reads.
    """
    response = await client.get("/")
    # A plain GET / is the shell root itself -- it must render, NOT redirect anywhere.
    assert response.status_code == 200
    body = response.text
    # The single stable swap target every rail node innerHTML-swaps.
    assert 'id="stage-workspace"' in body
    # Summary is the selected/active default: the swap target carries the stage marker AND the
    # placeholder body renders inside it.
    assert 'data-stage="summary"' in body
    assert "data-summary-placeholder" in body
    # The Analyze dashboard is NOT what rendered. Careful: the shared scaffold's hidden seed host
    # emits an EMPTY <div id="analyze-lanes"></div> on every non-analyze workspace, so a bare
    # 'id="analyze-lanes"' substring check would be a false positive. The stage marker is the
    # unambiguous signal.
    assert 'data-stage="analyze"' not in body


@pytest.mark.asyncio
async def test_analyze_still_reachable_at_s_analyze(client: AsyncClient, make_file) -> None:  # type: ignore[no-untyped-def]
    """SQ3-03 -- repointing / to Summary leaves the real Analyze workspace intact at /s/analyze."""
    # Phase 61 (61-05, RECORD-04): with 0 files the analyze branch swaps in the first-run empty
    # state; seed a file so this exercises its actual intent -- the real Analyze dashboard.
    await make_file()
    response = await client.get("/s/analyze")
    assert response.status_code == 200
    body = response.text
    # The real Analyze workspace: the stage marker plus the lane-card grid (Phase 58 / 58-04).
    assert 'data-stage="analyze"' in body
    assert 'id="analyze-lanes"' in body
    # And Analyze -- not Summary -- is the active rail node here.
    assert re.search(r'data-rail-stage="analyze"[^>]*aria-current="page"', body), 'analyze rail node must carry aria-current="page" on /s/analyze'


@pytest.mark.asyncio
async def test_summary_stage_route_and_fragment(client: AsyncClient) -> None:
    """SQ3-01 -- /s/summary serves the full shell on direct nav and a bare fragment on an HX swap.

    The bare fragment must also carry the shared scaffold's hidden OOB seed host: the shell's ONE
    persistent /pipeline/stats poll runs on the landing page too, and every fragment it re-emits
    needs a pre-existing landing target or htmx logs ``htmx:oobErrorNoTarget`` every 5s. Summary
    must NOT start a second poll loop of its own (single-poll discipline, WORK-05 / R-2).
    """
    full = await client.get("/s/summary")
    assert full.status_code == 200
    assert 'id="stage-workspace"' in full.text
    assert "data-summary-placeholder" in full.text

    hx = await client.get("/s/summary", headers={"HX-Request": "true"})
    assert hx.status_code == 200
    fragment = hx.text
    assert "data-summary-placeholder" in fragment
    # Content-only: a swapped fragment NEVER carries the document wrapper or head.
    assert "<html" not in fragment
    assert "<head" not in fragment
    # The OOB seed host rides in via _workspace_scaffold.html -> _workspace_poll_seeds.html.
    assert 'id="straggler-failed-card"' in fragment
    # No second poll loop.
    assert 'hx-trigger="every' not in fragment
    assert "setInterval" not in fragment


@pytest.mark.asyncio
async def test_files_stage_route_and_fragment(client: AsyncClient, make_file) -> None:  # type: ignore[no-untyped-def]
    """UI-01/UI-02 (87-09) -- /s/files serves the full shell on direct nav and a bare fragment on an HX swap.

    The derived per-file stage-matrix files page was fully built + tested (87-04) but UNREACHABLE:
    no rail entry pointed at it and a direct hit on /pipeline/files returned a chrome-less fragment.
    Surfacing it as a real rail stage inherits the _render_stage fork for free -- a direct navigation
    gets the full shell.html chrome (with the workspace stage marker) and an HX rail swap gets the
    bare, content-only files_table_view.html. Seed one file so the derived matrix actually renders a
    row (a _stage_pill), not just the empty state.
    """
    await make_file()

    full = await client.get("/s/files")
    assert full.status_code == 200
    full_body = full.text
    # Full shell chrome: the persistent swap target with the files stage marker on it.
    assert 'id="stage-workspace"' in full_body
    assert 'data-stage="files"' in full_body
    # The distinctive derived-matrix markup: the files table root + at least one rendered stage pill.
    assert 'id="files-table-view"' in full_body
    assert "aria-label=" in full_body and "not started" in full_body  # a _stage_pill token rendered a row

    hx = await client.get("/s/files", headers={"HX-Request": "true"})
    assert hx.status_code == 200
    fragment = hx.text
    # The files matrix rides in as content-only: no document wrapper / head (a rail swap never injects
    # duplicate landmarks or skip-links -- the chrome persists across swaps).
    assert 'id="files-table-view"' in fragment
    assert "<html" not in fragment
    assert "<head" not in fragment


@pytest.mark.asyncio
async def test_files_rail_node_is_reachable_and_accessible(client: AsyncClient) -> None:
    """UI-01 (87-09) -- the shipped shell exposes a keyboard-accessible Files rail node wired to /s/files.

    The gap this closes: the files matrix was unreachable because NOTHING navigated to it. Assert the
    rail carries a Files node whose hx-get points at /s/files, that it is a native <button> (keyboard-
    operable, focus-visible) carrying its visible+sr-only label, an aria-hidden glyph, a title tooltip,
    and the aria-current binding -- matching the sibling nav nodes' a11y exactly.
    """
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text

    # The Files node is wired to the reachable route.
    assert 'hx-get="/s/files"' in body

    # Locate the Files node's opening <button ...> tag and assert its a11y contract.
    node = re.search(r'<button\b[^>]*data-rail-stage="files"[^>]*>', body, re.DOTALL)
    assert node is not None, 'no data-rail-stage="files" nav <button> in the rail'
    attrs = node.group(0)
    assert 'hx-get="/s/files"' in attrs, "Files node not wired to /s/files"
    assert 'hx-target="#stage-workspace"' in attrs and 'hx-push-url="true"' in attrs
    assert 'title="Files"' in attrs, "Files node missing its native title tooltip"
    assert "focus-visible:" in attrs, "Files node missing a focus-visible ring (keyboard a11y)"

    # The label span carries max-lg:sr-only (screen-reader-navigable when collapsed), NEVER max-lg:hidden.
    label = re.search(r'data-rail-stage="files".*?<span[^>]*>Files</span>', body, re.DOTALL)
    assert label is not None, "Files node missing its 'Files' label span"
    assert "max-lg:sr-only" in label.group(0), "Files label must collapse via max-lg:sr-only (CUT-04 ↔ CUT-01)"
    assert "max-lg:hidden" not in label.group(0), "Files label must NOT use max-lg:hidden (strips it from the a11y tree)"
    # An aria-hidden inline-SVG glyph rides between the button open tag and the label.
    glyph = re.search(r'data-rail-stage="files".*?<svg[^>]*aria-hidden="true"[^>]*>', body, re.DOTALL)
    assert glyph is not None, "Files node missing its aria-hidden inline-SVG glyph"


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
    """SHELL-02 -- every navigable rail node carries the HTMX swap wiring; summary is active.

    The DAG rail is the nav spine: each of the 14 nodes swaps ONLY ``#stage-workspace``
    (innerHTML) via ``/s/<id>`` with ``hx-push-url``. The ``/`` default marks the summary node
    ``aria-current="page"`` (quick 260707-sq3 -- it was analyze before the landing repoint).
    """
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text

    # Every navigable node carries hx-get="/s/<id>" for all 14 rail-order stages.
    for stage in _RAIL_STAGES:
        assert f'hx-get="/s/{stage}"' in body, f"rail node {stage} missing hx-get wiring"

    # The single stable swap target + push-url are present on the rail nodes.
    assert 'hx-target="#stage-workspace"' in body
    assert 'hx-swap="innerHTML"' in body
    assert 'hx-push-url="true"' in body
    # Exactly one swap-target attr per navigable stage node (the 14 /s/ stages).
    assert body.count('hx-target="#stage-workspace"') >= len(_RAIL_STAGES)

    # The summary node (the / default) is the active rail node: aria-current="page" sits on
    # the same element carrying data-rail-stage="summary".
    assert re.search(r'data-rail-stage="summary"[^>]*aria-current="page"', body), 'summary rail node must carry aria-current="page" on the shell root'


@pytest.mark.asyncio
async def test_sync_rail_selection_root_maps_to_summary(client: AsyncClient) -> None:
    """phaze-iunq -- the client-side rail reconciler maps "/" to the SAME stage the server renders.

    ``syncRailSelection(path)`` in shell.html is the ONLY thing that re-applies
    ``aria-current="page"`` after an HTMX navigation, and the active rail visual (blue tint +
    inset bar) plus the screen-reader "current page" semantic both follow from that attribute.
    Its ``/`` branch was left at ``analyze`` when quick 260707-sq3 (SQ3-02) repointed the
    landing stage to Summary server-side, so a browser Back to ``/`` fired ``htmx:historyRestore``
    -> ``syncRailSelection('/')`` -> the Analyze node got highlighted while the restored Summary
    workspace was on screen.

    This asserts the JS root branch agrees with the server: whatever stage ``GET /`` marks
    ``aria-current="page"`` server-side is the stage the JS re-applies it to on history restore.
    """
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text

    # The stage the SERVER treats as the "/" landing -- read off the rail node it marks active.
    server_landing = re.search(r'data-rail-stage="([^"]+)"[^>]*aria-current="page"', body)
    assert server_landing is not None, 'no rail node carries aria-current="page" on the shell root'
    server_stage = server_landing.group(1)
    assert server_stage == "summary", f'GET / should render the summary landing, not "{server_stage}"'

    # The stage the CLIENT-side reconciler assigns for path "/" in syncRailSelection.
    js_root_branch = re.search(r"""if \(path === ['"]/['"].*?\)\s*\{\s*stage = ['"]([^'"]+)['"]""", body, re.DOTALL)
    assert js_root_branch is not None, "syncRailSelection has no recognizable root-path branch in shell.html"
    js_stage = js_root_branch.group(1)

    assert js_stage == server_stage, (
        f'syncRailSelection maps "/" to stage "{js_stage}" but GET / renders "{server_stage}" -- '
        "browser Back to the root will highlight the wrong rail node (phaze-iunq)"
    )


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


@pytest.mark.asyncio
async def test_header_agent_count_sums_agent_online_and_compute_lanes_active(client: AsyncClient) -> None:
    """COMPUTE-02: the header dot/count sum agentOnline + computeLanesActive (both keys, additive).

    agentOnline's own 0-degrade fail-safe semantics are untouched -- computeLanesActive is a NEW
    additive key, never a replacement.
    """
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "computeLanesActive: 0" in body, "shell store must seed computeLanesActive to int 0 (no undefined flash)"
    assert "($store.pipeline.agentOnline + $store.pipeline.computeLanesActive) > 0" in body
    assert "$store.pipeline.agentOnline + $store.pipeline.computeLanesActive" in body


# ---------------------------------------------------------------------------
# History-restore response shape for the /s/* rail (phaze-64uy) -- the WORST instance of the
# defect class routers/response_shape.py names.
#
# Every rail node in shell/partials/rail.html carries
#     hx-get="/s/<stage>" hx-target="#stage-workspace" hx-swap="innerHTML" hx-push-url="true"
# so EVERY stage the operator visits pushes a /s/* URL into history. Press Back with that snapshot
# evicted from htmx's 10-entry historyCacheSize (routine -- a fresh session or cleared localStorage
# does it too) and htmx re-fetches the URL as a RESTORE carrying BOTH HX-Request: true and
# HX-History-Restore-Request: true. On a restore htmx IGNORES hx-target and swaps the response into
# the history element -- <body>, since nothing in this repo carries [hx-history-elt].
#
# _render_stage used to branch on the raw HX-Request header, so it answered that restore with the
# chrome-less shell/_stage_fragment.html: the rail, header, palette launcher and status strip were
# all destroyed, leaving a bare workspace with no navigation and no way out but a manual reload.
# Reachable from every stage in the app.
#
# Each test below asserts the CHROME, never merely a 200 -- the buggy handler returned 200 too, so
# a status-only assertion passes against the bug.
# ---------------------------------------------------------------------------


_RESTORE_HEADERS = {"HX-Request": "true", "HX-History-Restore-Request": "true"}


def _assert_full_shell(body: str, *, context: str) -> None:
    """Assert ``body`` is the complete shell document, chrome and all."""
    assert "<html" in body.lower(), f"{context}: must be a full document, not a chrome-less fragment"
    assert 'aria-label="Pipeline navigation"' in body, f"{context}: the rail must survive"
    assert 'aria-label="Pipeline stages"' in body, f"{context}: the rail's stage nav must survive"
    assert 'id="stage-workspace"' in body, f"{context}: the swap target every rail node aims at must exist"


@pytest.mark.parametrize("stage", ["analyze", "files", "discover", "summary", "dedupe"])
@pytest.mark.asyncio
async def test_stage_history_restore_returns_full_shell(client: AsyncClient, stage: str) -> None:
    """A history-restore GET /s/<stage> returns the FULL shell, not the bare stage fragment.

    This is the reproduction from the bead, one stage per parametrisation. Before the fix each of
    these returned ``_stage_fragment.html`` -- the dispatcher measured /s/analyze at 5796 bytes,
    /s/files at 5656 and /s/discover at 8652, all with ``full_document=False``.
    """
    response = await client.get(f"/s/{stage}", headers=_RESTORE_HEADERS)
    assert response.status_code == 200
    _assert_full_shell(response.text, context=f"history restore of /s/{stage}")


@pytest.mark.asyncio
async def test_stage_restore_header_alone_returns_full_shell(client: AsyncClient) -> None:
    """The restore header DOMINATES even without ``HX-Request`` (response_shape rule 2).

    Pins the fourth shape the contract enumerates so a refactor cannot quietly drop the ``not``
    from ``is_htmx_request(request) and not is_history_restore(request)``.
    """
    response = await client.get("/s/analyze", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 200
    _assert_full_shell(response.text, context="restore header alone on /s/analyze")


@pytest.mark.asyncio
async def test_root_history_restore_returns_full_shell(client: AsyncClient) -> None:
    """``GET /`` shares ``_render_stage``, so the shell root owes the same guarantee."""
    response = await client.get("/", headers=_RESTORE_HEADERS)
    assert response.status_code == 200
    _assert_full_shell(response.text, context="history restore of /")


@pytest.mark.asyncio
async def test_stage_live_htmx_swap_still_returns_bare_fragment(client: AsyncClient) -> None:
    """The OTHER direction: a live rail swap must still get the chrome-less fragment.

    The fix must not turn every htmx request into a full page. ``_stage_fragment.html`` is swapped
    INTO ``#stage-workspace``; a full document here would nest ``<html>``/duplicate landmarks
    inside the live shell, a ROADMAP-locked anti-pattern.
    """
    response = await client.get("/s/analyze", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower(), "a live rail swap must get a fragment, not a full document"
    assert 'aria-label="Pipeline navigation"' not in body, "the fragment must not carry a second rail"
    assert 'id="stage-workspace"' not in body, "the fragment swaps INTO #stage-workspace, not around it"
