"""Integration tests for the CUE router's legacy surfaces.

phaze-zimli: the per-tracklist ``POST /cue/{id}/generate`` route is gone, per ADR-0027 (retire legacy
timestamp qualification). Selected-source generation and its agent dispatch are covered by
``tests/integration/test_selected_cue_dispatch.py``; the agent-side write by
``tests/review/tasks/test_cue_write.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from tests._queue_fakes import install_fake_queues


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_approved_tracklist_with_file(
    session: AsyncSession,
    *,
    artist: str = "DJ Shadow",
    event: str = "Coachella 2024",
    applied: bool = True,
    with_timestamps: bool = True,
    track_count: int = 3,
    source: str = "manual",
) -> tuple[Tracklist, FileRecord]:
    """Create an approved tracklist with an applied file and timestamped tracks.

    READ-05/D-01: a file is "applied" iff an ``executed`` :class:`RenameProposal` exists for it,
    NOT ``file.state == 'executed'`` (no ``src/`` writer produces that state -- it was the whole
    reason the CUE gate was dead). The file is seeded with ``state='moved'`` (a real post-apply
    state) so these fixtures prove the CUE gate reads ``applied()`` (``proposals.status``), not
    ``files.state``. Pass ``applied=False`` to seed an ``approved`` (non-executed) proposal so the
    file is NOT applied. Track timestamps are display-only text (ADR-0027 (retire legacy timestamp qualification)).
    """
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{artist}.mp3",
        original_filename=f"{artist} - Live @ {event}.mp3",
        current_path=f"/dest/{artist} - Live @ {event}.mp3",
        file_type="mp3",
        file_size=50_000_000,
    )
    session.add(file_record)
    await session.flush()

    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=f"{artist} - Live @ {event}.mp3",
            status=ProposalStatus.EXECUTED if applied else ProposalStatus.APPROVED,
        )
    )

    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()

    tracklist = Tracklist(
        id=tracklist_id,
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        source_url=f"https://example.invalid/tracklist/{uuid.uuid4().hex[:6]}",
        file_id=file_id,
        match_confidence=95,
        artist=artist,
        event=event,
        latest_version_id=version_id,
        source=source,
        status="approved",
    )
    session.add(tracklist)

    version = TracklistVersion(
        id=version_id,
        tracklist_id=tracklist_id,
        version_number=1,
    )
    session.add(version)
    await session.flush()

    for i in range(1, track_count + 1):
        track = TracklistTrack(
            id=uuid.uuid4(),
            version_id=version_id,
            position=i,
            artist=f"Track Artist {i}",
            title=f"Track Title {i}",
            timestamp=f"0:{i * 10}:00" if with_timestamps else None,
        )
        session.add(track)

    await session.commit()
    return tracklist, file_record


@pytest.mark.asyncio
async def test_cue_list_full_page(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05): a plain GET /cue/ 302-redirects into the shell.

    The "CUE Sheets" heading + stats header + empty-state are full-page chrome on the cue
    workspace node (a Phase-57 placeholder; real content lands in 58-61). The in-page HX
    list partial stays usable (test_cue_list_htmx_partial covers it).
    """
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_cue_list_htmx_partial(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-y4s6: GET /cue/ redirects unconditionally now, even with an HX-Request header.

    The in-page HX list/pagination fragment this used to preserve (``cue/partials/cue_list.html``)
    had no live caller left post-v7-cutover and was deleted outright -- unlike the sibling
    ``/proposals/`` redirect, there is no HX-filter branch here to keep working.
    """
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_cue_list_empty_state(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05): the cue empty-state moved to the shell workspace node.

    The "No tracklists eligible for CUE generation" message is full-page chrome (the cue
    node is a Phase-57 placeholder), so a plain GET /cue/ now 302-redirects into the shell.
    """
    response = await client.get("/cue/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_cue_list_stats(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 57 (SHELL-05): the cue stats header moved to the shell workspace node.

    The "Eligible"/"Generated"/"Missing Timestamps" stats header is full-page chrome on the
    cue workspace node (a Phase-57 placeholder), so a plain GET /cue/ now 302-redirects into
    the shell.
    """
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_legacy_per_tracklist_generate_route_is_gone(client: AsyncClient, session: AsyncSession) -> None:
    """phaze-zimli / ADR-0027 (retire legacy timestamp qualification): no per-tracklist CUE write.

    ``POST /cue/{tracklist_id}/generate`` could only ever refuse once legacy timing stopped
    qualifying, so the route, its preview/error re-renders and ``cue/partials/cue_row.html`` were
    removed. An approved, applied legacy tracklist -- the exact input the route used to accept --
    now gets a 404 and nothing is enqueued. A recording's CUE is generated from its selected source
    (``POST /cue/files/{media_id}/generate``, ``tests/integration/test_selected_cue_dispatch.py``).
    """
    tracklist, _file_record = await _create_approved_tracklist_with_file(session)
    _controller_queue, router = install_fake_queues(client)

    response = await client.post(f"/cue/{tracklist.id}/generate", headers={"HX-Target": f"cue-card-{tracklist.id}"})

    assert response.status_code == 404
    assert not router.captures


# ``GET /cue/`` -- legacy bookmark redirect only (phaze-y4s6).
#
# The in-page HX list/pagination fragment this handler used to serve (``cue/partials/cue_list.html``)
# had no live caller left post-v7-cutover and was deleted outright, along with the paging/history-
# restore branches that used to distinguish a live htmx swap from a bookmark/restore. Every shape
# now redirects unconditionally -- these tests pin that down across the header combinations the
# old branching handler used to care about.


@pytest.mark.asyncio
async def test_cue_history_restore_redirects_to_the_shell(client: AsyncClient) -> None:
    """A history-restore GET ``/cue/`` redirects, same as any other shape."""
    response = await client.get("/cue/", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"


@pytest.mark.asyncio
async def test_cue_history_restore_resolves_to_a_full_document(client: AsyncClient) -> None:
    """Following that redirect yields a FULL document with chrome intact."""
    response = await client.get(
        "/cue/",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must resolve to a full document"
    assert 'aria-label="Pipeline navigation"' in body, "the page chrome must be present after a restore"


@pytest.mark.asyncio
async def test_cue_restore_header_alone_redirects_to_the_shell(client: AsyncClient) -> None:
    """The restore header alone (no HX-Request) redirects too."""
    response = await client.get("/cue/", headers={"HX-History-Restore-Request": "true"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/cue"
