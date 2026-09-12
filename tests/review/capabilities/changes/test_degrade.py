"""Changes-review scenarios moved from ``tests/review/services/test_review_degrade.py``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.review import (
    _format_quality,
    _format_size,
    get_pending_proposal_rows,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _RaisingSession:
    """Minimal stub whose ``begin_nested`` raises the moment control enters the ``try``.

    Each ``review.py`` read helper opens ``async with session.begin_nested():`` as the first
    statement inside its ``try``. Raising synchronously from ``begin_nested`` drives control
    straight into the ``except Exception`` degrade branch (observable via the return value +
    the emitted warning key).
    """

    def begin_nested(self) -> object:
        raise RuntimeError("db down")


async def _seed_qualifying_applied_file(session: AsyncSession, *, filename: str) -> uuid.UUID:
    """Insert an APPLIED file with a ``>= 1`` change (title parsed from the filename, NULL in metadata)."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/dest/{uuid.uuid4().hex}/{filename}",
            original_filename=filename,
            current_path=f"/dest/{filename}",
            file_type="mp3",
            file_size=5_000_000,
        )
    )
    await session.flush()
    # artist matches the filename-parsed artist; title is NULL so the filename title is a real change.
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file_id, artist=filename.split(" - ", 1)[0], title=None))
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=filename,
            status=ProposalStatus.EXECUTED.value,
        )
    )
    await session.commit()
    return file_id


@pytest.mark.asyncio
async def test_get_pending_proposal_rows_degrades_to_empty_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = await get_pending_proposal_rows(_RaisingSession())  # type: ignore[arg-type]
    # phaze-rw14: the degrade branch returns an all-empty/zero PendingProposalRows bundle, not a
    # bare list -- rows AND both real-total counts degrade together.
    assert result.rows == []
    assert result.total_pending == 0
    assert result.high_confidence_pending == 0
    assert any("pending_proposal_rows_degraded" in r.getMessage() for r in caplog.records)


def test_format_size_edges() -> None:
    assert _format_size(None) == "unknown size"
    assert _format_size(0) == "unknown size"  # covers the falsy guard
    assert _format_size(22_400_000).endswith(" MB")
    assert _format_size(2**60).endswith(" PB")  # covers the loop-exhaustion branch


def test_format_quality_with_and_without_bitrate() -> None:
    # phaze-iw2k: bitrate is stored in BITS per second; _format_quality divides by 1000 for display.
    assert _format_quality({"file_size": 22_400_000, "bitrate": 320_000}).startswith("320 kbps · ")
    assert "kbps" not in _format_quality({"file_size": 22_400_000})  # covers the no-bitrate branch


@pytest.mark.asyncio
async def test_batched_lookups_pick_the_same_rows_as_the_per_file_helpers(session: AsyncSession) -> None:
    """The batch helpers must resolve EXACTLY what the per-file helpers would, ties included.

    Both are ``DISTINCT ON`` rewrites of an ``ORDER BY ... LIMIT 1``, so a mismatched ORDER BY would
    silently change which tracklist (and therefore which proposed tags) a file is reviewed against.
    Seeded with a deliberate confidence TIE so the ``id`` tiebreak is what decides.
    """
    # phaze-b4u3p: the per-file helpers stay importable from routers.tags (still used by its own
    # routes); the batch forms moved to services.tag_comparison, their only remaining caller being
    # services.review -- see that module's docstring for the layering rationale.
    from phaze.routers.tags import _get_accepted_discogs_link, _get_tracklist_for_file
    from phaze.services.tag_comparison import _get_accepted_discogs_links_for_files, _get_tracklists_for_files

    file_id = await _seed_qualifying_applied_file(session, filename="Tie Artist - Tie Title.mp3")
    version_ids = []
    for confidence in (0.5, 0.5, 0.9):
        version_id = uuid.uuid4()
        tracklist_id = uuid.uuid4()
        session.add(
            Tracklist(
                id=tracklist_id,
                external_id=f"ext-{uuid.uuid4().hex[:8]}",
                source_url="https://example.com",
                file_id=file_id,
                artist="Tie Artist",
                latest_version_id=version_id,
                source="1001tracklists",
                status="approved",
                match_confidence=confidence,
            )
        )
        session.add(TracklistVersion(id=version_id, tracklist_id=tracklist_id, version_number=1))
        version_ids.append(version_id)
    await session.commit()

    per_file = await _get_tracklist_for_file(session, file_id)
    batched = (await _get_tracklists_for_files(session, [file_id])).get(file_id)
    assert per_file is not None
    assert batched is not None
    assert batched.id == per_file.id, "the batch helper must pick the same tracklist as the per-file one"

    # Two accepted links on the winning version's tracks, tied on confidence -> id DESC decides.
    track_ids = [uuid.uuid4(), uuid.uuid4()]
    for position, track_id in enumerate(track_ids, start=1):
        session.add(TracklistTrack(id=track_id, version_id=per_file.latest_version_id, position=position, title=f"T{position}", timestamp="0:00"))
        session.add(
            DiscogsLink(
                id=uuid.uuid4(),
                track_id=track_id,
                discogs_release_id=f"rel-{position}",
                status="accepted",
                confidence=0.7,
                discogs_label="L",
                discogs_year=2024,
            )
        )
    await session.commit()

    per_file_link = await _get_accepted_discogs_link(session, file_id)
    batched_link = (await _get_accepted_discogs_links_for_files(session, {file_id: per_file})).get(file_id)
    assert per_file_link is not None
    assert batched_link is not None
    assert batched_link.id == per_file_link.id, "the batch helper must pick the same accepted link as the per-file one"
