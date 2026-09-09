"""phaze-5x8za: the set glyph survives a single-row swap response.

phaze-x1qr3.9 wired the set glyph into the PAGE renders of the Files table, Changes Review rows
and the palette Files group -- each builder eager-loads ``FileRecord.set_profile`` (a
``selectinload``, never a per-row query) and passes ``profile=`` into the shared
``pipeline/partials/_diff_row.html`` partial. It explicitly scoped OUT the single-row swap
responses in ``routers/proposals.py`` (approve/reject/undo/edit) and ``routers/tags.py``
(write_file_tags/undo_tag_write): those re-fetch the row WITHOUT the nested selectinload and never
pass ``profile`` at all, so the glyph rendered on the page vanishes the moment the operator takes
any of those actions, returning only on the next full page load.

Fixed here: every re-fetch statement behind these swap responses now chains
``selectinload(FileRecord.set_profile)`` onto the existing ``selectinload(...file)`` (ONE extra
statement for the single row the swap is about, never a per-row query -- mirrors the page
builders' own comments), and every row-context builder passes ``profile=`` through exactly like
the page render does. A file with no ``SetProfile`` row renders byte-identical to before (no
placeholder), matching ``_diff_row.html``'s existing contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.set_profile import SetProfile
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.routers.tags import _encode_tag_review_token, _get_accepted_discogs_link, _get_file_with_metadata, _get_tracklist_for_file
from phaze.services.tag_comparison import _tag_review_payload
from phaze.services.tag_proposal import compute_proposed_tags
from tests._queue_fakes import install_fake_queues


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _glyph_cells() -> list[dict[str, int | float | None]]:
    return [{"camelot_number": 8, "energy": 0.2}, {"camelot_number": 9, "energy": 0.8}]


async def _make_proposal(
    session: AsyncSession,
    *,
    with_profile: bool,
    status: str = ProposalStatus.PENDING,
) -> RenameProposal:
    """One FileRecord + RenameProposal pair, optionally with a SetProfile row on the file."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/swap-glyph.mp3",
        original_filename="swap-glyph.mp3",
        current_path="/music/swap-glyph.mp3",
        file_type="mp3",
        file_size=1_000_000,
    )
    session.add(file_record)
    await session.flush()
    if with_profile:
        session.add(SetProfile(file_id=file_id, glyph=_glyph_cells()))
    proposal = RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename="Renamed.mp3",
        proposed_path=None,
        confidence=0.95,
        status=status,
        context_used={"artist": "Test Artist", "event_name": "Test Event"},
    )
    session.add(proposal)
    await session.commit()
    return proposal


async def _make_executed_file(session: AsyncSession, *, with_profile: bool) -> FileRecord:
    """An applied FileRecord (an EXECUTED proposal exists), optionally with a SetProfile row."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/dest/{uuid.uuid4().hex}/swap-glyph-tags.mp3",
        original_filename="swap-glyph-tags.mp3",
        current_path="/dest/swap-glyph-tags.mp3",
        file_type="mp3",
        file_size=5_000_000,
    )
    session.add(file_record)
    await session.flush()
    if with_profile:
        session.add(SetProfile(file_id=file_id, glyph=_glyph_cells()))
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=file_record.original_filename,
            proposed_path=None,
            confidence=0.95,
            status=ProposalStatus.EXECUTED.value,
        )
    )
    await session.commit()
    return file_record


async def _tag_review_token(session: AsyncSession, file_id: uuid.UUID) -> str:
    file_record = await _get_file_with_metadata(session, file_id)
    assert file_record is not None
    tracklist = await _get_tracklist_for_file(session, file_id)
    link = await _get_accepted_discogs_link(session, file_id)
    proposed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=link)
    return _encode_tag_review_token(_tag_review_payload(file_record, tracklist, link, proposed))


def _assert_has_glyph(body: str) -> None:
    assert "data-set-glyph" in body and "data-set-glyph-empty" not in body
    assert body.count("<rect") == len(_glyph_cells())


def _assert_no_glyph(body: str) -> None:
    assert "data-set-glyph" not in body


# ---------------------------------------------------------------------------
# routers/proposals.py -- approve / reject / undo / edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_swap_carries_the_glyph(client: AsyncClient, session: AsyncSession) -> None:
    with_profile = await _make_proposal(session, with_profile=True)
    without_profile = await _make_proposal(session, with_profile=False)

    resp = await client.patch(f"/proposals/{with_profile.id}/approve", data={"expected_updated_at": with_profile.updated_at.isoformat()})
    assert resp.status_code == 200
    _assert_has_glyph(resp.text)

    resp = await client.patch(f"/proposals/{without_profile.id}/approve", data={"expected_updated_at": without_profile.updated_at.isoformat()})
    assert resp.status_code == 200
    _assert_no_glyph(resp.text)


@pytest.mark.asyncio
async def test_reject_swap_carries_the_glyph(client: AsyncClient, session: AsyncSession) -> None:
    with_profile = await _make_proposal(session, with_profile=True)
    without_profile = await _make_proposal(session, with_profile=False)

    resp = await client.patch(f"/proposals/{with_profile.id}/reject")
    assert resp.status_code == 200
    _assert_has_glyph(resp.text)

    resp = await client.patch(f"/proposals/{without_profile.id}/reject")
    assert resp.status_code == 200
    _assert_no_glyph(resp.text)


@pytest.mark.asyncio
async def test_undo_swap_carries_the_glyph(client: AsyncClient, session: AsyncSession) -> None:
    with_profile = await _make_proposal(session, with_profile=True, status=ProposalStatus.APPROVED)
    without_profile = await _make_proposal(session, with_profile=False, status=ProposalStatus.APPROVED)

    resp = await client.patch(f"/proposals/{with_profile.id}/undo")
    assert resp.status_code == 200
    _assert_has_glyph(resp.text)

    resp = await client.patch(f"/proposals/{without_profile.id}/undo")
    assert resp.status_code == 200
    _assert_no_glyph(resp.text)


@pytest.mark.asyncio
async def test_edit_swap_carries_the_glyph(client: AsyncClient, session: AsyncSession) -> None:
    with_profile = await _make_proposal(session, with_profile=True)
    without_profile = await _make_proposal(session, with_profile=False)

    resp = await client.patch(f"/proposals/{with_profile.id}/edit", data={"proposed": "Edited.mp3", "facet": "filename"})
    assert resp.status_code == 200
    _assert_has_glyph(resp.text)

    resp = await client.patch(f"/proposals/{without_profile.id}/edit", data={"proposed": "Edited.mp3", "facet": "filename"})
    assert resp.status_code == 200
    _assert_no_glyph(resp.text)


# ---------------------------------------------------------------------------
# routers/tags.py -- write_file_tags / undo_tag_write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_tags_swap_carries_the_glyph(client: AsyncClient, session: AsyncSession) -> None:
    with_profile = await _make_executed_file(session, with_profile=True)
    without_profile = await _make_executed_file(session, with_profile=False)
    install_fake_queues(client)

    token = await _tag_review_token(session, with_profile.id)
    resp = await client.post(f"/tags/{with_profile.id}/write", data={"review_token": token})
    assert resp.status_code == 200
    _assert_has_glyph(resp.text)

    token = await _tag_review_token(session, without_profile.id)
    resp = await client.post(f"/tags/{without_profile.id}/write", data={"review_token": token})
    assert resp.status_code == 200
    _assert_no_glyph(resp.text)


@pytest.mark.asyncio
async def test_undo_tag_write_swap_carries_the_glyph(client: AsyncClient, session: AsyncSession) -> None:
    with_profile = await _make_executed_file(session, with_profile=True)
    without_profile = await _make_executed_file(session, with_profile=False)
    for fr in (with_profile, without_profile):
        session.add(
            TagWriteLog(
                id=uuid.uuid4(),
                file_id=fr.id,
                before_tags={"artist": "Original Artist"},
                after_tags={"artist": "Written Artist"},
                source="proposal",
                status=TagWriteStatus.COMPLETED.value,
            )
        )
    await session.commit()
    install_fake_queues(client)

    resp = await client.post(f"/tags/{with_profile.id}/undo")
    assert resp.status_code == 200
    _assert_has_glyph(resp.text)

    resp = await client.post(f"/tags/{without_profile.id}/undo")
    assert resp.status_code == 200
    _assert_no_glyph(resp.text)
