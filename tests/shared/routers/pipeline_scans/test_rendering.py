"""Controller-side tests for every `pipeline_scans` read/delete/render endpoint that isn't part
of `trigger_scan`'s own phases (split from test_pipeline_scans.py, phaze-1i0h6.6).

Covers GET /pipeline/scans/{batch_id} (HTMX poll partial), GET /pipeline/scans/agent-roots
(HTMX swap partial), the dashboard's Trigger Scan / Recent Scans sections, status pills,
GET /pipeline/scans/recent (self-arming poll + its column_sort sortable-header contract,
phaze-a6hm.6), the OOB stage-card counts piggybacked on /pipeline/stats, DELETE
/pipeline/scans/{batch_id} (cascade + terminal-state guards, phaze-ytmfm), and the Discover
workspace mount of the Recent Scans table (phaze-8f9j).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline_scans._shared import (
    FileRecord,
    ScanBatch,
    ScanStatus,
    _make_batch_file,
    _make_discovered_file,
    _path_order,
    _seed_scan_batches,
    pytest,
    select,
    uuid,
)


if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Coverage gap fills (Codecov PR #59): pipeline_scans.py:120, 207, 255-260
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scan_progress_unknown_id_renders_terminal_gone_fragment(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """GET /pipeline/scans/{unknown_id} renders a terminal `gone` fragment, not a 404.

    phaze-xsje regression: htmx 2.x's default responseHandling does not swap non-2xx
    bodies, so a 404 here would leave a previously-swapped-in RUNNING card's outerHTML
    poller (hx-get + hx-trigger="every 2s") armed in the DOM forever. Returning 200 with
    a terminal fragment (no hx-get/hx-trigger) lets the outerHTML swap replace it and
    halt the poll: a fragment whose subject vanished must stop polling, never loop.
    """
    ac, _ = smoke
    unknown_id = uuid.uuid4()
    response = await ac.get(f"/pipeline/scans/{unknown_id}")
    assert response.status_code == 200
    assert "hx-trigger" not in response.text
    assert "hx-get" not in response.text
    assert "no longer available" in response.text.lower()


@pytest.mark.asyncio
async def test_get_scan_progress_running_returns_polling_partial(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """GET /pipeline/scans/{batch_id} for RUNNING batch carries hx-trigger + hx-swap=outerHTML."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/2026/",
        status=ScanStatus.RUNNING.value,
        total_files=10,
        processed_files=3,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get(f"/pipeline/scans/{batch.id}")
    assert response.status_code == 200
    assert 'hx-trigger="every 2s"' in response.text
    assert 'hx-swap="outerHTML"' in response.text
    assert f'hx-get="/pipeline/scans/{batch.id}"' in response.text
    assert "RUNNING" in response.text


@pytest.mark.asyncio
async def test_get_scan_progress_completed_omits_hx_trigger(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Pitfall 6: COMPLETED batch response OMITS hx-trigger and hx-get (HTMX halts polling)."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/2026/",
        status=ScanStatus.COMPLETED.value,
        total_files=10,
        processed_files=10,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get(f"/pipeline/scans/{batch.id}")
    assert response.status_code == 200
    # Pitfall 6 invariant: NO HTMX polling attributes in terminal-state markup.
    assert "hx-trigger" not in response.text
    assert "hx-get" not in response.text
    assert "Scan complete" in response.text
    assert "COMPLETED" in response.text


@pytest.mark.asyncio
async def test_get_scan_progress_failed_renders_error_message(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """FAILED batch renders error_message AND omits hx-trigger."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/missing/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="path missing",
    )
    session.add(batch)
    await session.commit()

    response = await ac.get(f"/pipeline/scans/{batch.id}")
    assert response.status_code == 200
    assert "path missing" in response.text
    assert "FAILED" in response.text
    assert "hx-trigger" not in response.text
    assert "hx-get" not in response.text


@pytest.mark.asyncio
async def test_agent_roots_swap_returns_partial(smoke: tuple[AsyncClient, AsyncMock]) -> None:
    """GET /pipeline/scans/agent-roots returns scan_path_picker.html with the agent's scan_roots."""
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/agent-roots", params={"agent_id": "test-agent"})
    assert response.status_code == 200
    assert '<select id="scan-root"' in response.text
    assert '<option value="/data/music">/data/music</option>' in response.text
    assert '<option value="/data/videos">/data/videos</option>' in response.text


@pytest.mark.asyncio
async def test_agent_roots_swap_unknown_agent_yields_empty_state(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """Unknown agent or empty scan_roots yields the empty-state copy."""
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/agent-roots", params={"agent_id": "totally-bogus-agent"})
    assert response.status_code == 200
    # Unknown agent renders the agent=None branch (placeholder "Select an agent first").
    assert "Select an agent first" in response.text


# ---------------------------------------------------------------------------
# HARD-03 (AR-30-03 / Phase-30 REVIEW IN-01): agent_id HTTP-boundary validation
# A malformed agent_id must 422 at the boundary instead of silently returning
# an empty picker 200. Pattern + max_length mirror the Agent.id DB CHECK
# (models/agent.py:36) and the CLI AGENT_ID_RE (cli/__init__.py:44).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_roots_swap_malformed_agent_id_returns_422(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """HARD-03: a malformed agent_id -> 422 (was a silent empty picker 200)."""
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/agent-roots", params={"agent_id": "Bad_ID!"})
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_agent_roots_swap_well_formed_agent_id_passes_validation(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """HARD-03: a well-formed agent_id still reaches the handler (not a 422)."""
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/agent-roots", params={"agent_id": "test-agent"})
    assert response.status_code != 422
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Task 2 (template / UI-SPEC) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_trigger_scan_card(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """GET /pipeline/ surfaces the Trigger Scan card heading + agent dropdown + picker slot."""
    ac, _ = smoke

    response = await ac.get("/s/discover", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'id="trigger-scan-heading"' in response.text
    assert ">Trigger Scan</h2>" in response.text
    assert '<select id="scan-agent"' in response.text
    assert 'id="scan-path-picker"' in response.text
    # Agent option populated as "{name} ({id})" per CONTEXT D-Discretion.
    assert "Test Agent (test-agent)" in response.text


@pytest.mark.asyncio
async def test_dashboard_renders_recent_scans_section(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """GET /pipeline/ surfaces the Recent Scans heading + empty state when no batches."""
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'id="recent-scans-heading"' in response.text
    assert ">Recent Scans</h2>" in response.text
    # No batches seeded -> empty state.
    assert "No scans yet" in response.text


@pytest.mark.asyncio
async def test_dashboard_recent_scans_shows_failed_row_with_inline_error(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Failed batch renders the second inline-error <tr> with red surface + error_message."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/oops/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="path missing",
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    # PR5 added an Actions column, so the inline-error row spans 7 columns.
    assert 'colspan="7"' in response.text
    assert "bg-red-50" in response.text
    assert "path missing" in response.text


@pytest.mark.asyncio
async def test_dashboard_recent_scans_excludes_live_batches(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """LIVE sentinel batches MUST be excluded from Recent Scans (CONTEXT D-05 / UI-SPEC line 401)."""
    ac, _ = smoke
    live_batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="<watcher>",
        status=ScanStatus.LIVE.value,
        total_files=0,
        processed_files=0,
    )
    session.add(live_batch)
    await session.commit()

    response = await ac.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    # The LIVE sentinel must not surface; the table renders the empty state.
    assert "<watcher>" not in response.text
    assert "No scans yet" in response.text


@pytest.mark.asyncio
async def test_status_pill_running_uses_blue_surface(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """RUNNING status pill renders with bg-blue-100 dark:bg-blue-950 + aria-label."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/",
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "bg-blue-100" in response.text
    assert "dark:bg-blue-950" in response.text
    assert 'aria-label="Status: running"' in response.text


@pytest.mark.asyncio
async def test_status_pill_completed_uses_green_surface(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """COMPLETED status pill renders with bg-green-100."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/done/",
        status=ScanStatus.COMPLETED.value,
        total_files=5,
        processed_files=5,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "bg-green-100" in response.text
    assert 'aria-label="Status: completed"' in response.text


@pytest.mark.asyncio
async def test_status_pill_failed_uses_red_surface(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """FAILED status pill renders with bg-red-100."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/oops/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="oops",
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "bg-red-100" in response.text
    assert 'aria-label="Status: failed"' in response.text


@pytest.mark.asyncio
async def test_router_registered_in_main_app() -> None:
    """pipeline_scans.router is registered in main.create_app() (production wiring)."""
    from phaze.main import create_app
    from tests._route_introspection import effective_route_paths

    app = create_app()
    paths = effective_route_paths(app)
    # All handlers must be reachable on the production app.
    assert "/pipeline/scans" in paths
    assert "/pipeline/scans/{batch_id}" in paths
    assert "/pipeline/scans/agent-roots" in paths
    assert "/pipeline/scans/recent" in paths


# ---------------------------------------------------------------------------
# GET /pipeline/scans/recent -- self-arming Recent Scans poll partial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_scans_partial_renders_table(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """GET /pipeline/scans/recent returns 200 + the Recent Scans table with the row's cells.

    Seeds a RUNNING batch (mid-scan "N / Z" is exactly the value the page-load
    render froze) and asserts the partial renders its agent name, path and the
    ``processed_files / total_files`` cell.
    """
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/live-scan/",
        status=ScanStatus.RUNNING.value,
        total_files=9000,
        processed_files=5500,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/scans/recent")
    assert response.status_code == 200, response.text
    # Root section present (HTMX outerHTML swap target).
    assert 'id="recent-scans"' in response.text
    # Known cells from the seeded row.
    assert "Test Agent" in response.text
    assert "/data/music/live-scan/" in response.text
    assert "5500" in response.text
    assert "9000" in response.text


@pytest.mark.asyncio
async def test_get_recent_scans_partial_is_self_arming(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """The /recent partial re-arms its own 5s poll on the root section (self-referential).

    Each swapped-in copy must carry hx-get/hx-trigger/hx-swap on its root so the
    poll keeps firing -- mirrors the scan_progress_card.html pattern.
    """
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/recent")
    assert response.status_code == 200
    # phaze-a6hm.6: the poll URL now carries the resolved sort (column_sort rule 4a), so it is
    # "/pipeline/scans/recent?sort=...&order=..." rather than the bare path. The endpoint is still
    # the same self-referential one -- that is what this test is about.
    assert 'hx-get="/pipeline/scans/recent?' in response.text
    assert 'hx-trigger="every 5s"' in response.text
    assert 'hx-swap="outerHTML"' in response.text


@pytest.mark.asyncio
async def test_get_recent_scans_partial_excludes_live_batches(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """The /recent partial excludes LIVE sentinel batches (same query as the dashboard)."""
    ac, _ = smoke
    live = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="<watcher>",
        status=ScanStatus.LIVE.value,
        total_files=0,
        processed_files=0,
    )
    session.add(live)
    await session.commit()

    response = await ac.get("/pipeline/scans/recent")
    assert response.status_code == 200
    assert "<watcher>" not in response.text
    assert "No scans yet" in response.text


@pytest.mark.asyncio
async def test_recent_path_not_shadowed_by_batch_id_route(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """GET /pipeline/scans/recent resolves to the partial, NOT the /{batch_id} 404 path.

    If the literal ``/recent`` route were registered AFTER ``/{batch_id}`` it would
    be captured as a UUID path param and 422 (invalid UUID). Pin the ordering.
    """
    ac, _ = smoke
    response = await ac.get("/pipeline/scans/recent")
    assert response.status_code == 200
    assert 'id="recent-scans"' in response.text


# ---------------------------------------------------------------------------
# OOB stage-card "files ready" counts piggybacked on the /pipeline/stats poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_partial_carries_oob_files_ready_counts(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """GET /pipeline/stats appends OOB paragraphs that refresh the stage-card counts.

    The OOB elements target the same ids the stage cards render (analyze-files-ready
    / proposals-files-ready) so the existing 5s stats poll refreshes the "files
    ready" counts WITHOUT re-rendering the interactive #pipeline-stages buttons.
    """
    ac, _ = smoke
    response = await ac.get("/pipeline/stats")
    assert response.status_code == 200
    assert 'id="analyze-files-ready" hx-swap-oob="true"' in response.text
    assert 'id="proposals-files-ready" hx-swap-oob="true"' in response.text
    assert "files ready" in response.text


# ---------------------------------------------------------------------------
# Stage-card button :disabled tracks the live count via $store.pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_poll_oob_counts_push_into_pipeline_store(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """The /pipeline/stats OOB count paragraphs update the SAME store the buttons read.

    On the 5s poll, Alpine inits the freshly-swapped OOB paragraphs and writes the new
    counts into $store.pipeline, so the buttons un-disable live without the poll touching
    the button subtree (#analyze-response / #proposals-response).
    """
    session.add_all([_make_discovered_file() for _ in range(4)])
    await session.commit()

    ac, _ = smoke
    response = await ac.get("/pipeline/stats")
    assert response.status_code == 200
    assert 'hx-swap-oob="true" x-init="$store.pipeline.discovered = 4"' in response.text
    assert 'hx-swap-oob="true" x-init="$store.pipeline.analyzed = 0"' in response.text
    # The poll response must not carry the interactive button subtree (no clobber).
    assert "Run Analysis" not in response.text
    assert "Generate Proposals" not in response.text


# ---------------------------------------------------------------------------
# PR5: DELETE /pipeline/scans/{batch_id} -- delete + cascade + 409 guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_completed_scan_removes_row_and_cascades(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """DELETE a completed scan -> 200, re-rendered table without the row; cascade ran."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/done-delete/",
        status=ScanStatus.COMPLETED.value,
        total_files=1,
        processed_files=1,
    )
    session.add(batch)
    await session.flush()
    file_row = _make_batch_file(batch.id, "child")
    session.add(file_row)
    await session.commit()
    batch_id, file_id = batch.id, file_row.id

    response = await ac.delete(f"/pipeline/scans/{batch_id}")
    assert response.status_code == 200, response.text
    # Response is the re-rendered Recent Scans section for the HTMX outerHTML swap.
    assert 'id="recent-scans"' in response.text
    # The deleted scan's path is absent from the re-rendered table.
    assert "/data/music/done-delete/" not in response.text

    # The batch row is gone from the DB.
    assert (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalars().all() == []
    # The cascade removed the batch's child file too.
    assert (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalars().all() == []


@pytest.mark.asyncio
async def test_recent_scans_table_delete_control_on_terminal_rows_only(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """The delete control renders only for terminal (completed/failed) rows, not running.

    Seeds a completed batch and a running batch, then renders the dashboard. The
    completed row exposes ``hx-delete`` (wired to its batch id); the running row
    does not. The Actions column header is present.
    """
    ac, _ = smoke
    completed = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/completed-row/",
        status=ScanStatus.COMPLETED.value,
        total_files=5,
        processed_files=5,
    )
    running = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/running-row/",
        status=ScanStatus.RUNNING.value,
        total_files=10,
        processed_files=3,
    )
    session.add_all([completed, running])
    await session.commit()
    completed_id, running_id = completed.id, running.id

    response = await ac.get("/pipeline/scans/recent", headers={"HX-Request": "true"})
    assert response.status_code == 200
    # Actions column header present.
    assert ">Actions</th>" in response.text
    # The completed row exposes a delete control wired to its id + the HTMX swap target.
    # phaze-a6hm.6: the delete url now carries the active sort so removing a row cannot reset the
    # table's order (column_sort rule 4a); the id-bearing path prefix is what this test is about.
    assert f'hx-delete="/pipeline/scans/{completed_id}?' in response.text
    assert 'hx-target="#recent-scans"' in response.text
    assert 'hx-swap="outerHTML"' in response.text
    assert "Delete this scan and all associated data?" in response.text
    # The running row does NOT expose a delete control.
    assert f'hx-delete="/pipeline/scans/{running_id}"' not in response.text


@pytest.mark.asyncio
async def test_delete_failed_scan_is_deletable(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """A FAILED (terminal) scan is deletable -> 200, row removed."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/failed-delete/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="boom",
    )
    session.add(batch)
    await session.commit()
    batch_id = batch.id

    response = await ac.delete(f"/pipeline/scans/{batch_id}")
    assert response.status_code == 200, response.text
    assert (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalars().all() == []


@pytest.mark.asyncio
async def test_delete_unknown_batch_renders_alert_not_dropped_404(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """DELETE an unknown batch_id -> 200 + re-rendered table with a role="alert" banner.

    phaze-ytmfm: this handler's sole caller is the trash control in
    ``recent_scans_table.html`` (``hx-target="#recent-scans"``), and htmx 2.x's stock
    ``responseHandling`` does not swap a 4xx/5xx body (response_shape.py rule 3) -- a bare
    404 here is silently dropped and the operator sees nothing. A status assertion alone
    would have passed against that bug, so this asserts the SHAPE htmx actually swaps: 200,
    the re-rendered ``#recent-scans`` section, and an announced ``role="alert"`` message.
    """
    ac, _ = smoke
    response = await ac.delete(f"/pipeline/scans/{uuid.uuid4()}")
    assert response.status_code == 200, response.text
    assert 'id="recent-scans"' in response.text
    assert 'role="alert"' in response.text
    assert "already gone" in response.text.lower()


@pytest.mark.asyncio
async def test_delete_live_batch_renders_alert_not_dropped_409(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """The LIVE watcher sentinel can NEVER be deleted -> 200 + alert banner; no rows touched.

    phaze-ytmfm: was a bare 409 htmx silently drops (response_shape.py rule 3) -- see
    ``test_delete_unknown_batch_renders_alert_not_dropped_404`` for the full rationale.
    """
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="<watcher>",
        status=ScanStatus.LIVE.value,
        total_files=0,
        processed_files=0,
    )
    session.add(batch)
    await session.commit()
    batch_id = batch.id

    response = await ac.delete(f"/pipeline/scans/{batch_id}")
    assert response.status_code == 200, response.text
    assert 'role="alert"' in response.text
    assert "live" in response.text.lower()
    # Row survives.
    assert (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalars().all() != []


@pytest.mark.asyncio
async def test_delete_running_batch_renders_alert_not_dropped_409(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """A RUNNING scan cannot be deleted (only terminal scans are) -> 200 + alert; row survives.

    Server-side recheck is authoritative: the reaper may flip a row's status, or a
    stale button may target a now-running row, so the guard lives on the server.

    phaze-ytmfm: was a bare 409 htmx silently drops (response_shape.py rule 3) -- see
    ``test_delete_unknown_batch_renders_alert_not_dropped_404`` for the full rationale.
    """
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/running/",
        status=ScanStatus.RUNNING.value,
        total_files=10,
        processed_files=3,
    )
    session.add(batch)
    await session.commit()
    batch_id = batch.id

    response = await ac.delete(f"/pipeline/scans/{batch_id}")
    assert response.status_code == 200, response.text
    assert 'role="alert"' in response.text
    assert "running" in response.text.lower()
    assert (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalars().all() != []


# ---------------------------------------------------------------------------
# phaze-a6hm.6 -- sortable Recent Scans table (routers/column_sort.py contract)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_scans_sorts_server_side_by_path(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """?sort=path&order=asc reorders the ROWS, not just the header decoration.

    Contract rule 1: the ORDER BY lands in SQL. The seeded rows' path order is the reverse of
    their created_at order, so this ordering is unreachable without a real server-side re-query.
    """
    ac, _ = smoke
    await _seed_scan_batches(session)

    response = await ac.get("/pipeline/scans/recent?sort=path&order=asc")
    assert response.status_code == 200
    paths = _path_order(response.text)
    assert paths == sorted(paths), f"rows not in ascending path order: {paths}"
    assert paths[0].endswith("alpha")

    descending = await ac.get("/pipeline/scans/recent?sort=path&order=desc")
    assert _path_order(descending.text)[0].endswith("charlie")


@pytest.mark.asyncio
async def test_recent_scans_poll_url_carries_the_active_sort(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """REGRESSION (the bead's core acceptance): the 5s poll re-requests the CHOSEN sort.

    This table is a poll first and a table second, and it swaps ``outerHTML`` -- so the response
    REPLACES the polling element and whatever its ``hx-get`` says becomes the next tick's request.
    A hard-coded ``hx-get="/pipeline/scans/recent"`` therefore does not merely skip one tick: it
    writes the SORTLESS url into the DOM, so ~5s after the operator clicks a header the table
    silently snaps back to the default order and stays there.

    Asserting the first render is sorted does NOT catch this -- that assertion passes against the
    broken implementation. The load-bearing assertion is on the url the response ARMS ITSELF with.
    """
    ac, _ = smoke
    await _seed_scan_batches(session)

    response = await ac.get("/pipeline/scans/recent?sort=path&order=asc")
    assert response.status_code == 200
    assert 'hx-get="/pipeline/scans/recent?sort=path&amp;order=asc"' in response.text
    # ...and specifically NOT the bare, sort-losing url this table used to hard-code.
    assert 'hx-get="/pipeline/scans/recent"' not in response.text


@pytest.mark.asyncio
async def test_recent_scans_sort_actually_survives_a_simulated_poll_tick(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """REGRESSION: replay the poll the way htmx would and assert the order is still the chosen one.

    Walks the real loop rather than trusting the url: sort -> read the ``hx-get`` the response
    armed -> issue THAT request (this is exactly what the 5s trigger does) -> assert the second
    response is still path-ascending AND still arms the same sorted url, so the order is stable
    across arbitrarily many ticks rather than surviving only the first.
    """
    import re

    ac, _ = smoke
    await _seed_scan_batches(session)

    first = await ac.get("/pipeline/scans/recent?sort=path&order=asc")
    assert _path_order(first.text)[0].endswith("alpha")

    match = re.search(r'hx-get="([^"]+)"', first.text)
    assert match is not None, "table did not arm a poll url at all"
    poll_url = match.group(1).replace("&amp;", "&")

    tick = await ac.get(poll_url)
    assert tick.status_code == 200
    assert _path_order(tick.text)[0].endswith("alpha"), "the poll reverted the operator's chosen sort"
    assert re.search(r'hx-get="([^"]+)"', tick.text).group(1).replace("&amp;", "&") == poll_url  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_recent_scans_unknown_sort_degrades_to_default_not_422(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Contract rule 3: an unrecognised sort renders the DEFAULT order; it does not 422 the poll.

    A stale bookmark or an evicted history entry can carry an old key innocently, and this url is
    re-requested every 5 seconds -- 422-ing it would blank the table to punish a display preference.
    """
    ac, _ = smoke
    await _seed_scan_batches(session)

    response = await ac.get("/pipeline/scans/recent?sort=drop+table&order=sideways")
    assert response.status_code == 200
    # Degrades to the contract default (started/desc = newest first = charlie, the last seeded) --
    # which is the OPPOSITE end from path-ascending, so this cannot pass by coincidence.
    assert _path_order(response.text)[0].endswith("charlie")
    assert 'hx-get="/pipeline/scans/recent?sort=started&amp;order=desc"' in response.text


@pytest.mark.asyncio
async def test_recent_scans_unknown_sort_cannot_reach_a_column(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Contract rule 2 as a REGRESSION: an unwhitelisted key never becomes a column.

    Asserting the status alone would pass against a ``getattr(ScanBatch, sort)`` implementation, so
    this asserts the resolved STATE: a key naming a real-but-unoffered ORM attribute (``error_message``,
    a real column this table deliberately does not expose) still resolves to the default key.
    """
    from phaze.routers.pipeline_scans import RECENT_SCANS_SORT

    ac, _ = smoke
    await _seed_scan_batches(session)

    # A real ScanBatch column that is NOT whitelisted -- the exact input a getattr-based
    # implementation would happily turn into an ORDER BY.
    state = RECENT_SCANS_SORT.resolve(sort="error_message", order="asc")
    assert state.key == "started"
    assert "error_message" not in str(state.order_by()[0])

    response = await ac.get("/pipeline/scans/recent?sort=error_message&order=asc")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_recent_scans_headers_announce_sort_state_via_aria(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Contract rule 5: the ACTIVE column announces its direction; other sortable columns say "none".

    Non-sortable headers (Elapsed is computed in Python, Actions is a control column) must carry NO
    aria-sort at all -- the attribute's absence means "not sortable", where ``none`` would advertise
    a sorting affordance that does not exist.
    """
    ac, _ = smoke
    await _seed_scan_batches(session)

    response = await ac.get("/pipeline/scans/recent?sort=path&order=desc")
    assert 'aria-sort="descending"' in response.text
    assert 'aria-sort="none"' in response.text
    # Exactly one column is active.
    assert response.text.count('aria-sort="descending"') == 1
    assert response.text.count('aria-sort="ascending"') == 0
    # Five whitelisted columns => five aria-sort attributes; Elapsed/Actions carry none.
    assert response.text.count("aria-sort=") == 5


@pytest.mark.asyncio
async def test_recent_scans_delete_preserves_the_active_sort(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """The delete re-render is the third producer of #recent-scans and must not reset the order.

    Deleting a row re-renders the whole section, so without the sort it would both reorder the
    table under the operator AND arm a sortless poll -- the same defect one interaction over.
    """
    ac, _ = smoke
    await _seed_scan_batches(session)
    doomed = (await session.execute(select(ScanBatch).where(ScanBatch.scan_path == "/data/music/bravo"))).scalars().one()

    response = await ac.delete(f"/pipeline/scans/{doomed.id}?sort=path&order=asc")
    assert response.status_code == 200
    assert "/data/music/bravo" not in response.text
    assert _path_order(response.text)[0].endswith("alpha")
    assert 'hx-get="/pipeline/scans/recent?sort=path&amp;order=asc"' in response.text


def test_recent_scans_contract_is_wired_at_import_time() -> None:
    """Contract rule 6: the contract is a module-level constant whose invariants hold.

    Elapsed/Actions are asserted ABSENT deliberately -- Elapsed is computed by ``elapsed_seconds``
    in Python and has no column to ORDER BY, so offering it would mean sorting the fetched rows
    after the read, which rule 1 forbids.
    """
    from phaze.routers.pipeline_scans import RECENT_SCANS_SORT

    assert RECENT_SCANS_SORT.endpoint == "/pipeline/scans/recent"
    assert RECENT_SCANS_SORT.target == "#recent-scans"
    assert RECENT_SCANS_SORT.default_key == "started"
    # Newest-first, matching the pre-sort behaviour of a table literally called "Recent Scans".
    assert RECENT_SCANS_SORT.default_order == "desc"
    assert {column.label for column in RECENT_SCANS_SORT.columns} == {"Agent", "Path", "Status", "Files", "Started"}
    assert "Elapsed" not in {column.label for column in RECENT_SCANS_SORT.columns}


# ---------------------------------------------------------------------------
# phaze-8f9j: the SERVED page, not just the endpoint body.
#
# Every test above asserts on what an endpoint RETURNS. That is exactly why the orphaning went
# unnoticed for a phase: `recent_scans_table.html` kept passing its own tests while no served
# document mounted `id="recent-scans"`, so the delete control, the failed-row error_message, the
# stall indicator and the whole sortable-header contract were unreachable in the product. These
# tests assert on a page the operator can actually open.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_workspace_mounts_the_recent_scans_table(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """GET /s/discover serves a document containing #recent-scans and its per-row delete control.

    Without this mount, DELETE /pipeline/scans/{id} has no caller anywhere in the app: the control
    exists only in this partial, and the Discover workspace rendered recent scans through the
    text-only _file_table.html instead. An operator whose scan failed half-way through an ingest
    had no in-product way to remove the batch and its partially-ingested FileRecords.
    """
    ac, _ = smoke
    completed = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/served-completed/",
        status=ScanStatus.COMPLETED.value,
        total_files=5,
        processed_files=5,
    )
    session.add(completed)
    await session.commit()
    completed_id = completed.id

    response = await ac.get("/s/discover", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text

    assert 'id="recent-scans"' in body, "no served document mounts the recent-scans table"
    assert f'hx-delete="/pipeline/scans/{completed_id}' in body, "the served page carries no delete control"
    assert "Delete this scan and all associated data?" in body
    assert ">Actions</th>" in body
    assert 'aria-label="Scan history"' in body


@pytest.mark.asyncio
async def test_discover_workspace_shows_a_failed_scans_error_message(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """A failed batch's error_message is readable on the served page, not just the bare word "failed".

    scan_progress_card.html shows the reason live to whoever triggered the scan and stayed put; this
    is the durable view -- after a reload, or for a scan the watcher (or an earlier session) started.
    """
    ac, _ = smoke
    failed = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/served-failed/",
        status=ScanStatus.FAILED.value,
        total_files=10,
        processed_files=3,
        error_message="agent scan root not mounted",
    )
    session.add(failed)
    await session.commit()

    response = await ac.get("/s/discover", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "agent scan root not mounted" in response.text, "a failed scan's reason is still invisible after a reload"


@pytest.mark.asyncio
async def test_discover_workspace_recent_scans_table_starts_no_second_poll(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """WORK-05 survives the re-mount: the workspace copy polls not at all, and says so in its URLs.

    The self-poll was the stated reason Phase 58 refused to reuse this partial. It is now opt-out,
    and the opt-out has to SURVIVE an interaction -- a header click or a delete re-requests the
    section, and if that response armed the loop the workspace would acquire a second poll one click
    after landing. Both re-render URLs therefore carry poll=0, and the endpoints honour it.
    """
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/served-nopoll/",
        status=ScanStatus.COMPLETED.value,
        total_files=1,
        processed_files=1,
    )
    session.add(batch)
    await session.commit()

    body = (await ac.get("/s/discover", headers={"HX-Request": "true"})).text
    assert 'hx-trigger="every' not in body, "the Discover workspace started a second poll loop"
    # The flag rides every URL the section can be re-requested by.
    assert "poll=0" in body

    # A header re-sort from the workspace comes back poll-free too.
    resorted = await ac.get("/pipeline/scans/recent?poll=0&sort=path&order=asc")
    assert resorted.status_code == 200
    assert 'hx-trigger="every' not in resorted.text, "the re-sorted copy re-armed the poll"
    assert "poll=0" in resorted.text, "the re-sorted copy dropped the no-poll flag"

    # ...and so does the delete re-render.
    deleted = await ac.delete(f"/pipeline/scans/{batch.id}?poll=0")
    assert deleted.status_code == 200
    assert 'hx-trigger="every' not in deleted.text, "the post-delete copy re-armed the poll"

    # Every OTHER caller is unchanged: absent flag still means poll.
    assert 'hx-trigger="every 5s"' in (await ac.get("/pipeline/scans/recent")).text
