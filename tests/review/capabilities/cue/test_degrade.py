"""CUE-review scenarios moved from ``tests/review/services/test_review_degrade.py``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.review import (
    get_cue_review_cards,
)
from tests._timing import qualified_timing


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


async def _seed_timed_legacy_cue_tracklist(session: AsyncSession, *, artist: str) -> Tracklist:
    """Insert an approved + applied tracklist with one track carrying formerly qualifying evidence."""
    file_id = uuid.uuid4()
    filename = f"{artist}.mp3"
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/music/{uuid.uuid4().hex}/{filename}",
            original_filename=filename,
            current_path=f"/dest/{filename}",
            file_type="mp3",
            file_size=1_000_000,
        )
    )
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=filename,
            status=ProposalStatus.EXECUTED.value,
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
        event="Test Event",
        latest_version_id=version_id,
        source="manual",
        status="approved",
    )
    session.add(tracklist)
    session.add(TracklistVersion(id=version_id, tracklist_id=tracklist_id, version_number=1))
    await session.flush()
    session.add(
        TracklistTrack(
            id=uuid.uuid4(),
            version_id=version_id,
            position=1,
            artist=f"{artist} Track",
            title="Track Title",
            timestamp="0:01:00",
            timestamp_evidence=qualified_timing("0:01:00", file_id),
        )
    )
    await session.commit()
    return tracklist


@pytest.mark.asyncio
async def test_get_cue_review_cards_degrades_to_empty_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = await get_cue_review_cards(_RaisingSession())  # type: ignore[arg-type]
    assert result == []
    assert any("cue_review_cards_degraded" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_legacy_tracklist_with_formerly_qualifying_evidence_is_a_gated_card(session: AsyncSession) -> None:
    """ADR-0027 (retire legacy timestamp qualification): every legacy card is gated, display-only.

    The seeded track carries the exact evidence the retired qualification accepted, on the latest
    version, for an approved tracklist on an applied file -- the old "eligible" input. It renders as
    a gated card with no cue text and no version pin, and still surfaces rather than being dropped.
    """
    tracklist = await _seed_timed_legacy_cue_tracklist(session, artist="Formerly Eligible")

    cards = await get_cue_review_cards(session)
    by_id = {card["tracklist_id"]: card for card in cards}

    assert tracklist.id in by_id
    assert by_id[tracklist.id]["eligible"] is False
    assert by_id[tracklist.id]["cue_text"] is None and by_id[tracklist.id]["version_id"] is None
