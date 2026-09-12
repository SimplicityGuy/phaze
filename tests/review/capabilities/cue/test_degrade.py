"""CUE-review scenarios moved from ``tests/review/services/test_review_degrade.py``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import patch
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.cue_generator import generate_cue_content as _real_generate_cue_content
from phaze.services.review import (
    get_cue_review_cards,
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


async def _seed_eligible_cue_tracklist(session: AsyncSession, *, artist: str) -> Tracklist:
    """Insert an approved + applied tracklist with one timestamped track (an eligible cue card)."""
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
        source_url=f"https://www.1001tracklists.com/tracklist/{uuid.uuid4().hex[:6]}",
        file_id=file_id,
        match_confidence=95,
        artist=artist,
        event="Test Event",
        latest_version_id=version_id,
        source="1001tracklists",
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
async def test_get_cue_review_cards_isolates_one_bad_card_from_the_rest(session: AsyncSession, caplog: pytest.LogCaptureFixture) -> None:
    """phaze-hcsb: a single card's build failure must not blank the whole Cue review workspace.

    Before the fix, the per-card ``_build_cue_tracks``/``generate_cue_content`` calls lived inside
    the SAME ``try`` as the SAVEPOINT open -- any exception there hit the outer ``except Exception:
    return []`` and dropped EVERY other eligible + gated card, not just the offending one.
    """
    good = await _seed_eligible_cue_tracklist(session, artist="Good Artist")
    bad = await _seed_eligible_cue_tracklist(session, artist="Bad Artist")

    def _boom_for_bad(audio_filename: str, file_type: str, tracks: list) -> str:  # type: ignore[type-arg]
        if audio_filename.startswith("Bad Artist"):
            raise ValueError("simulated per-card build failure")
        return _real_generate_cue_content(audio_filename, file_type, tracks)

    with caplog.at_level(logging.WARNING), patch("phaze.services.review.generate_cue_content", side_effect=_boom_for_bad):
        cards = await get_cue_review_cards(session)

    by_id = {card["tracklist_id"]: card for card in cards}

    # The workspace is NOT blanked -- both tracklists still produce a card.
    assert good.id in by_id
    assert bad.id in by_id

    # The good card is unaffected: still eligible with a real in-memory preview.
    assert by_id[good.id]["eligible"] is True
    assert by_id[good.id]["cue_text"]

    # The bad card is distinct from a genuine missing-timestamps gate.
    assert by_id[bad.id]["eligible"] is False
    assert by_id[bad.id]["build_error"] is True
    assert by_id[bad.id]["cue_text"] is None

    assert any("cue_review_card_build_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_cue_review_cards_gates_stale_version_timestamps(session: AsyncSession) -> None:
    """phaze-dboy: review.py's gated_stmt shares the same defective any-version predicate as
    cue.py -- a tracklist whose only timestamped track is on an OLDER version than
    ``latest_version_id`` must render as a GATED card (no approve control), not be omitted from
    both sets entirely.
    """
    tracklist = await _seed_eligible_cue_tracklist(session, artist="Stale Version")

    v2_id = uuid.uuid4()
    session.add(TracklistVersion(id=v2_id, tracklist_id=tracklist.id, version_number=2))
    await session.flush()
    session.add(
        TracklistTrack(
            id=uuid.uuid4(),
            version_id=v2_id,
            position=1,
            artist="Untimed Track",
            title="Untimed Title",
            timestamp=None,
        )
    )
    tracklist.latest_version_id = v2_id
    await session.commit()

    cards = await get_cue_review_cards(session)
    by_id = {card["tracklist_id"]: card for card in cards}

    assert tracklist.id in by_id, "the tracklist must still surface -- as gated, not silently dropped"
    assert by_id[tracklist.id]["eligible"] is False
    assert by_id[tracklist.id]["cue_text"] is None
