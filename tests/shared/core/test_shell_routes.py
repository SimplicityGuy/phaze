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

from phaze.routers.response_shape import DUAL_SHAPE_RESPONSE_HEADERS


if TYPE_CHECKING:
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession


# The DAG pipeline rail-node ids (VERBATIM prototype RAIL order, with the quick-260707-sq3
# Summary landing node prepended and the Phase-87 87-09 Files stage-matrix overview inserted
# right after it), each wired to /s/<id> and resolved through routers/shell.py's
# STAGE_PARTIALS.
_RAIL_STAGES = [
    "summary",
    "files",
    "discover",
    "metadata",
    "analyze",
    "tracklist",
    "propose",
    "rename",
    "tagwrite",
    "move",
    "dedupe",
    "cue",
]

# phaze-uvmcr.1: the two below-the-line UTILITY_PANES ids (Audit Log, Compute/Agents) --
# NOT DAG pipeline stages, so kept out of _RAIL_STAGES (which mirrors STAGE_PARTIALS'
# DAG-verbatim-order contract), but wired to /s/<id> exactly like their stage siblings and
# resolved through routers/shell.py's sibling UTILITY_PANES map. Combined with _RAIL_STAGES
# below wherever a conformance assertion is meant to cover every navigable rail node, DAG or
# utility -- so registering a pane in UTILITY_PANES is what buys it this coverage, with no
# bespoke per-pane test of its own.
_UTILITY_PANE_STAGES = [
    "audit",
    "agents",
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


@pytest.mark.parametrize("stage", ["discover", "audit", "agents"])
@pytest.mark.asyncio
async def test_stage_fragment_is_bare(client: AsyncClient, stage: str) -> None:
    """SHELL-02 -- /s/<stage> is a bare fragment on an HX request, the full shell on direct nav (D-01).

    ``discover`` is a STAGE_PARTIALS (DAG) stage; ``audit``/``agents`` (phaze-uvmcr.1) are
    UTILITY_PANES stages -- both maps fork through the same ``_render_stage`` code path, so
    this single parametrized test covers all three with no bespoke per-pane copy.
    """
    hx = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
    assert hx.status_code == 200
    # Content-only: a swapped fragment NEVER carries the document wrapper or head (no
    # duplicate landmarks / skip-links injected -- the chrome persists across swaps).
    assert "<html" not in hx.text
    assert "<head" not in hx.text

    full = await client.get(f"/s/{stage}")
    assert full.status_code == 200
    # The non-HX request is the full shell (carries the swap target + chrome).
    assert 'id="stage-workspace"' in full.text


def _assert_dual_shape_headers(response: Response, *, context: str) -> None:
    """Assert ``response`` carries every header ``DUAL_SHAPE_RESPONSE_HEADERS`` demands."""
    for name, value in DUAL_SHAPE_RESPONSE_HEADERS.items():
        assert response.headers.get(name) == value, f"{context}: missing/wrong {name!r} header (got {response.headers.get(name)!r})"


@pytest.mark.parametrize("stage", _RAIL_STAGES + _UTILITY_PANE_STAGES)
@pytest.mark.asyncio
async def test_stage_route_marks_every_shape_uncacheable_by_the_browser(client: AsyncClient, stage: str) -> None:
    """phaze-r6e5m (response_shape.py contract rule 6) -- /s/<stage> forks THREE ways on the SAME
    URL (fragment / full shell / history restore), and the browser's HTTP cache keys by URL alone.
    Every branch must carry DUAL_SHAPE_RESPONSE_HEADERS so a cached fragment can never be replayed
    as the whole document on a Back/Forward navigation, or vice versa. Covers every DAG stage
    (``_RAIL_STAGES``) and utility pane (``_UTILITY_PANE_STAGES``) through the single shared
    ``_render_stage`` fork -- no bespoke per-stage copy.
    """
    live_swap = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
    assert live_swap.status_code == 200
    _assert_dual_shape_headers(live_swap, context=f"live htmx swap of /s/{stage}")

    direct_nav = await client.get(f"/s/{stage}")
    assert direct_nav.status_code == 200
    _assert_dual_shape_headers(direct_nav, context=f"direct navigation to /s/{stage}")

    restore = await client.get(f"/s/{stage}", headers=_RESTORE_HEADERS)
    assert restore.status_code == 200
    _assert_dual_shape_headers(restore, context=f"history restore of /s/{stage}")


@pytest.mark.asyncio
async def test_root_marks_every_shape_uncacheable_by_the_browser(client: AsyncClient) -> None:
    """``GET /`` shares ``_render_stage`` with ``/s/<stage>``, so it owes the same guarantee."""
    direct_nav = await client.get("/")
    assert direct_nav.status_code == 200
    _assert_dual_shape_headers(direct_nav, context="direct navigation to /")

    restore = await client.get("/", headers=_RESTORE_HEADERS)
    assert restore.status_code == 200
    _assert_dual_shape_headers(restore, context="history restore of /")


@pytest.mark.asyncio
async def test_every_rail_stage_carries_exactly_one_focus_heading(client: AsyncClient) -> None:
    """Regression (phaze-t0b8) -- every STAGE_PARTIALS/UTILITY_PANES value renders exactly one
    ``<h1 tabindex="-1">``.

    The shell's htmx:afterSwap / htmx:historyRestore handler (``shell.html``'s
    ``_focusStageHeading``) moves keyboard focus to ``#stage-workspace``'s first ``<h1>`` after
    every rail swap; with none present the handler silently no-ops, stranding keyboard/screen-
    reader operators on the rail. files_workspace.html (the /s/files host) was the ONE stage that
    skipped this: it hand-rolled its own body instead of composing ``_workspace_scaffold.html``
    like every sibling, so it carried no heading at all. This guards the whole class, not just
    that one stage, the way the bead's own fix hint asked: every navigable rail node must have
    exactly one focus-landing heading, not zero and not two -- DAG pipeline stages
    (``_RAIL_STAGES``) AND below-the-line utility panes (``_UTILITY_PANE_STAGES``, phaze-uvmcr.1)
    alike.
    """
    for stage in _RAIL_STAGES + _UTILITY_PANE_STAGES:
        frag = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert frag.status_code == 200, f"/s/{stage} must render"
        count = len(re.findall(r'<h1\b[^>]*\btabindex="-1"', frag.text))
        assert count == 1, f'/s/{stage} must carry exactly one <h1 tabindex="-1"> focus target (found {count})'


@pytest.mark.asyncio
async def test_unknown_stage_404(client: AsyncClient) -> None:
    """SHELL-02 (negative) -- an unknown stage 404s (D-02 whitelist; `stage` is never a template path)."""
    response = await client.get("/s/does-not-exist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rail_nodes_wired(client: AsyncClient) -> None:
    """SHELL-02 -- every navigable rail node carries the HTMX swap wiring; summary is active.

    The DAG rail is the nav spine: each stage node swaps ONLY ``#stage-workspace``
    (innerHTML) via ``/s/<id>`` with ``hx-push-url``. The ``/`` default marks the summary node
    ``aria-current="page"`` (quick 260707-sq3 -- it was analyze before the landing repoint).
    phaze-uvmcr.1: the two below-the-line utility panes (``_UTILITY_PANE_STAGES``) are wired
    identically and are covered here too -- registering a pane in UTILITY_PANES is what buys
    it this wiring assertion, with no bespoke per-pane copy.
    """
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text

    # Every navigable node carries hx-get="/s/<id>" for every rail-order DAG stage AND both
    # below-the-line utility panes.
    for stage in _RAIL_STAGES + _UTILITY_PANE_STAGES:
        assert f'hx-get="/s/{stage}"' in body, f"rail node {stage} missing hx-get wiring"

    # The single stable swap target + push-url are present on the rail nodes.
    assert 'hx-target="#stage-workspace"' in body
    assert 'hx-swap="innerHTML"' in body
    assert 'hx-push-url="true"' in body
    # Exactly one swap-target attr per navigable node (DAG stages + utility panes).
    assert body.count('hx-target="#stage-workspace"') >= len(_RAIL_STAGES) + len(_UTILITY_PANE_STAGES)

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


@pytest.mark.parametrize("stage", ["analyze", "files", "discover", "summary", "dedupe", "audit", "agents"])
@pytest.mark.asyncio
async def test_stage_history_restore_returns_full_shell(client: AsyncClient, stage: str) -> None:
    """A history-restore GET /s/<stage> returns the FULL shell, not the bare stage fragment.

    This is the reproduction from the bead, one stage per parametrisation. Before the fix each of
    these returned ``_stage_fragment.html`` -- the dispatcher measured /s/analyze at 5796 bytes,
    /s/files at 5656 and /s/discover at 8652, all with ``full_document=False``. ``audit``/``agents``
    (phaze-uvmcr.1) are the UTILITY_PANES additions -- the H1 history-restore trap applies to them
    identically since they fork through the same ``response_shape.wants_fragment`` check as every
    STAGE_PARTIALS stage.
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


@pytest.mark.parametrize("stage", ["analyze", "audit", "agents"])
@pytest.mark.asyncio
async def test_stage_live_htmx_swap_still_returns_bare_fragment(client: AsyncClient, stage: str) -> None:
    """The OTHER direction: a live rail swap must still get the chrome-less fragment.

    The fix must not turn every htmx request into a full page. ``_stage_fragment.html`` is swapped
    INTO ``#stage-workspace``; a full document here would nest ``<html>``/duplicate landmarks
    inside the live shell, a ROADMAP-locked anti-pattern. ``audit``/``agents`` (phaze-uvmcr.1) are
    the UTILITY_PANES additions, covered here alongside the DAG ``analyze`` stage.
    """
    response = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower(), "a live rail swap must get a fragment, not a full document"
    assert 'aria-label="Pipeline navigation"' not in body, "the fragment must not carry a second rail"
    assert 'id="stage-workspace"' not in body, "the fragment swaps INTO #stage-workspace, not around it"


@pytest.mark.asyncio
async def test_audit_rail_node_carries_aria_current_on_direct_nav(client: AsyncClient) -> None:
    """Acceptance #2 (phaze-uvmcr.3) -- the Audit rail node shows aria-current="page" on /s/audit.

    Mirrors ``test_analyze_still_reachable_at_s_analyze``'s equivalent assertion for the DAG rail;
    ``audit`` is a UTILITY_PANES node (phaze-uvmcr.1) but is wired into the SAME rail active-state
    idiom (rail.html:404), so a direct navigation there marks it, not any DAG stage.
    """
    response = await client.get("/s/audit")
    assert response.status_code == 200
    body = response.text
    assert 'data-stage="audit"' in body
    assert re.search(r'data-rail-stage="audit"[^>]*aria-current="page"', body), 'audit rail node must carry aria-current="page" on /s/audit'


async def _seed_execution_log(session: AsyncSession, *, status: str, source_path: str) -> None:
    """Seed ONE ExecutionLog row (with its prerequisite FileRecord + approved RenameProposal).

    A local, minimal cousin of ``tests/review/routers/test_execution.py``'s
    ``create_test_execution_log`` -- this module tests the SHELL side of the audit pane, not the
    execution router, so it seeds only what :func:`build_audit_log_context`'s read actually needs.
    """
    from datetime import UTC, datetime
    import uuid

    from phaze.models.execution import ExecutionLog
    from phaze.models.file import FileRecord
    from phaze.models.proposal import ProposalStatus, RenameProposal

    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/music/{uuid.uuid4().hex}/test.mp3",
            original_filename="test.mp3",
            current_path=source_path,
            file_type="music",
            file_size=1_000_000,
        )
    )
    await session.flush()
    proposal_id = uuid.uuid4()
    session.add(
        RenameProposal(
            id=proposal_id,
            file_id=file_id,
            proposed_filename="new.mp3",
            confidence=0.9,
            status=ProposalStatus.APPROVED,
            context_used={"artist": "Test"},
            reason="Test",
        )
    )
    await session.flush()
    session.add(
        ExecutionLog(
            id=uuid.uuid4(),
            proposal_id=proposal_id,
            operation="move",
            source_path=source_path,
            destination_path=source_path.replace("old", "new"),
            sha256_verified=True,
            status=status,
            executed_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_audit_stage_resolves_query_params_the_redirect_from_audit_arrives_with(client: AsyncClient, session: AsyncSession) -> None:
    """The /s/audit branch actually READS status/sort/order off the query string, not just /audit/.

    ``execution.audit_log``'s non-fragment branch 301-redirects a bookmarked/filtered
    ``/audit/?...`` URL to ``/s/audit?...`` (acceptance #3), carrying the WHOLE query string. That
    round trip is only meaningful if ``/s/audit`` itself resolves those same parameters into the
    same filtered/sorted view -- otherwise a bookmark of a filtered audit URL would silently reset
    to page 1 / all statuses / default order on arrival. Seeds one COMPLETED and one FAILED log,
    then hits ``/s/audit?status=failed`` directly (as the redirect would) and asserts the response
    reflects the FAILED-only, active-tab-marked view.
    """
    from phaze.models.execution import ExecutionStatus

    await _seed_execution_log(session, status=ExecutionStatus.COMPLETED, source_path="/music/old-completed.mp3")
    await _seed_execution_log(session, status=ExecutionStatus.FAILED, source_path="/music/old-failed.mp3")

    response = await client.get("/s/audit?status=failed")
    assert response.status_code == 200
    body = response.text
    assert "/music/old-failed.mp3" in body
    assert "/music/old-completed.mp3" not in body
    # The active tab reflects the resolved status, not the "all" default.
    assert 'hx-get="/audit/?status=all' in body


@pytest.mark.asyncio
async def test_audit_stage_degrades_a_malformed_page_and_page_size_instead_of_500ing(client: AsyncClient) -> None:
    """A hand-edited/truncated ``/s/audit`` URL degrades page/page_size to a safe default, never 500s.

    Mirrors the ``pagination.py`` contract's rule 5 (out-of-range/unparseable inputs clamp, they
    never raise) -- the SAME discipline every other paged read in this router already gets via
    ``Query(..., ge=..., le=...)``. ``/s/audit`` parses these itself (off ``request.query_params``,
    not FastAPI's per-field validation, since ``shell_stage`` stays generic across every rail node),
    so it needs its OWN degrade-safe parsing, pinned here.
    """
    response = await client.get("/s/audit?page=not-a-number&page_size=also-not-a-number")
    assert response.status_code == 200
    assert 'id="audit-content"' in response.text


@pytest.mark.asyncio
async def test_audit_pane_filter_tabs_pager_and_sort_still_wired_from_inside_the_shell(client: AsyncClient) -> None:
    """Acceptance #6 (phaze-uvmcr.3) -- the filter tabs, pager, and column-sort headers still swap
    into their targets now that their host lives inside #stage-workspace, not a standalone page.

    The swap wiring itself (``#audit-content`` target, ``GET /audit/`` endpoint) is UNCHANGED by
    this bead -- these ids/attributes are the proof it survived being re-hosted, not a new feature.
    """
    response = await client.get("/s/audit")
    assert response.status_code == 200
    body = response.text
    # The filter tabs' swap target and endpoint.
    assert 'id="audit-content"' in body
    assert 'hx-get="/audit/?status=all' in body
    assert 'hx-target="#audit-content"' in body
    # The column-sort headers resolve against the SAME target (AUDIT_SORT.target == "#audit-content").
    assert re.search(r'hx-get="/audit/\?[^"]*sort=', body), "no column-sort header hx-get found inside the shell-hosted pane"


@pytest.mark.asyncio
async def test_audit_pane_carries_no_poller_and_away_navigation_leaves_nothing_behind(client: AsyncClient) -> None:
    """H3 (phaze-uvmcr.3 epic design) -- the audit pane starts no timer of its own, and swapping
    away to another rail node leaves no orphaned poller, listener, or dangling swap target behind.

    Unlike its below-the-line sibling (the agents pane, phaze-uvmcr.4), audit carries NO self-poll:
    the filter tabs, pager, and column-sort headers are all operator-triggered ``hx-get`` on click,
    never ``hx-trigger="every ...s"`` or a ``setInterval``. This proves that directly (there is
    nothing here that COULD leak), then proves the other half of H3: an innerHTML swap of
    ``#stage-workspace`` to another stage is self-cleaning -- none of audit's own ids survive it.
    """
    audit_fragment = await client.get("/s/audit", headers={"HX-Request": "true"})
    assert audit_fragment.status_code == 200
    audit_body = audit_fragment.text
    assert 'id="audit-content"' in audit_body, "sanity: this is really the audit pane"
    # No self-poll of any kind -- every control here is operator-triggered.
    assert 'hx-trigger="every' not in audit_body
    assert "setInterval" not in audit_body

    # Swap AWAY to a sibling rail node, the same innerHTML swap of #stage-workspace a real rail
    # click performs.
    away_fragment = await client.get("/s/files", headers={"HX-Request": "true"})
    assert away_fragment.status_code == 200
    away_body = away_fragment.text
    # None of audit's ids/hooks survive the swap: htmx cancels any element-scoped trigger when its
    # element leaves the DOM, and an innerHTML replace carries none of the old subtree forward, so
    # nothing from the audit pane is still present -- or listening -- after this swap.
    assert 'id="audit-content"' not in away_body
    assert 'id="audit-table-container"' not in away_body
    assert "audit-details-trigger-" not in away_body


@pytest.mark.asyncio
async def test_pipeline_stats_poll_resume_uses_htmx_trigger_not_source_less_ajax(client: AsyncClient) -> None:
    """phaze-ircc -- the foreground-resume refresh must go through the poll element's own trigger.

    ``htmx.ajax('GET', ..., { target: ... })`` with no ``source`` element substitutes
    ``document.body`` as the attribute-inheritance root (htmx 2.0.10 ``issueAjaxRequest``), so the
    poll element's ``hx-vals`` (the selected ``lane``) was silently dropped on every
    ``visibilitychange`` foreground-resume refresh. The fix routes the resume through
    ``window.htmx.trigger(pollEl, 'refresh')`` and adds a matching ``refresh`` entry to the
    element's own ``hx-trigger``, so exactly one place (the element itself) knows what the poll
    needs to send.
    """
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text
    # The single persistent poll element now also listens for a same-element 'refresh' trigger.
    assert re.search(
        r'id="pipeline-stats"[^>]*hx-trigger="every 5s \[document\.visibilityState === \'visible\'\], refresh"',
        body,
    ), "the #pipeline-stats poll must accept a same-element 'refresh' trigger"
    # The visibilitychange handler must fire that trigger on the element itself, not a source-less
    # htmx.ajax() call (which would silently drop the element's hx-vals lane).
    assert "window.htmx.trigger(pollEl, 'refresh')" in body
    assert "window.htmx.ajax('GET', '/pipeline/stats', { target: '#pipeline-stats', swap: 'innerHTML' })" not in body
