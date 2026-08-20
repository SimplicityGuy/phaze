"""phaze-fq9h.8: the record page's 1001Tracklists review section renders through ``GET /record/{id}``.

Composition-level regression, mirroring ``test_record_stage_pills.py``'s idiom: seed a file into
each of the three states :mod:`phaze.services.tracklist_priority`'s
:class:`~phaze.services.tracklist_priority.FileTracklistReview` distinguishes, then assert the
REAL record endpoint renders the honest, distinct copy for each -- never a collapsed "nothing to
show" for all three.

**ZERO live requests.** Every case is built from ORM rows or a directly-recorded cache outcome, at
the exact key :func:`phaze.services.tracklist_priority.get_file_tracklist_review` computes.

Must pass in the ``shared`` bucket in isolation (consumes the DB fixtures -> auto-marked integration).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from phaze.enums.tracklist_candidate import LookupOutcome
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.tracklist_candidates import CandidateSignals, group_unique_sets
from phaze.services.tracklist_lookup_cache import record_outcome
from phaze.services.tracklist_query import derive_query


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
LIVE_SET_FILENAME = "Coldwave-Live_At_Deep_Field-2025-05-02-WEB-FLAC-GRVMSTR.mp3"


async def _seed_live_set(make_file, session: AsyncSession, *, duration: float = 7200.0) -> FileRecord:  # type: ignore[no-untyped-def]
    file_rec = await make_file(original_filename=LIVE_SET_FILENAME)
    session.add(FileMetadata(file_id=file_rec.id, duration=duration))
    await session.commit()
    return file_rec


async def _set_key_for(file: FileRecord, *, duration: float) -> str:
    signals = CandidateSignals(file_id=file.id, filename=file.original_filename, sha256_hash=file.sha256_hash, duration_seconds=duration)
    derived = derive_query(signals.filename)
    signals = replace(signals, derived_query=derived.query)
    return group_unique_sets([signals])[0].key


@pytest.mark.asyncio
async def test_never_looked_up_renders_plainly(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    file_rec = await _seed_live_set(make_file, session)

    body = (await client.get(f"/record/{file_rec.id}")).text

    assert "No matched 1001Tracklists tracklist yet." in body
    assert "Not yet looked up." in body
    assert f"/pipeline/tracklists/{file_rec.id}/prioritize" in body
    assert "Look up now" in body


@pytest.mark.asyncio
async def test_a_definitive_negative_reads_distinctly_from_a_transient_failure(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    file_rec = await _seed_live_set(make_file, session)
    key = await _set_key_for(file_rec, duration=7200.0)
    await record_outcome(session, set_key=key, query_text="coldwave deep field", outcome=LookupOutcome.NOT_FOUND, now=NOW)
    await session.commit()

    body = (await client.get(f"/record/{file_rec.id}")).text

    assert "No confident match on 1001Tracklists" in body
    assert "Blocked by a Turnstile challenge" not in body


@pytest.mark.asyncio
async def test_a_transient_block_reads_distinctly_from_not_found(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    file_rec = await _seed_live_set(make_file, session)
    key = await _set_key_for(file_rec, duration=7200.0)
    await record_outcome(session, set_key=key, query_text="coldwave deep field", outcome=LookupOutcome.BLOCKED, now=NOW)
    await session.commit()

    body = (await client.get(f"/record/{file_rec.id}")).text

    assert "Blocked by a Turnstile challenge" in body
    assert "No confident match on 1001Tracklists" not in body
    assert "Look up again" in body
    assert f"/pipeline/tracklists/{file_rec.id}/prioritize" in body


@pytest.mark.asyncio
async def test_a_propagated_tracklist_never_reads_as_scraped(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    file_rec = await _seed_live_set(make_file, session)
    tracklist = Tracklist(
        external_id="ext-9",
        source_url="https://www.1001tracklists.com/tracklist/ext-9",
        file_id=file_rec.id,
        match_confidence=91,
        propagated_from_set_key="some-set-key",
        propagation_confidence="exact",
    )
    session.add(tracklist)
    await session.flush()
    version = TracklistVersion(tracklist_id=tracklist.id, version_number=1)
    session.add(version)
    await session.flush()
    tracklist.latest_version_id = version.id
    session.add(TracklistTrack(version_id=version.id, position=1, artist="Coldwave", title="Opener", timestamp="00:00"))
    await session.commit()

    body = (await client.get(f"/record/{file_rec.id}")).text

    assert "Propagated from a duplicate" in body
    assert "exact confidence link" in body
    assert "Scraped from 1001Tracklists" not in body
    assert "Opener" in body


@pytest.mark.asyncio
async def test_a_cache_suppressed_negative_hides_prioritize_and_says_so(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """phaze-z8xq7: a definitive negative still inside its 180-day TTL renders NO Prioritize
    button (it would be inert -- the drain keeps a cache-suppressed set out of the queue even
    force-flagged) and states the suppression plainly instead of leaving the operator to guess."""
    file_rec = await _seed_live_set(make_file, session)
    key = await _set_key_for(file_rec, duration=7200.0)
    await record_outcome(session, set_key=key, query_text="coldwave deep field", outcome=LookupOutcome.NOT_FOUND, now=NOW)
    await session.commit()

    body = (await client.get(f"/record/{file_rec.id}")).text

    assert "No confident match on 1001Tracklists" in body
    assert "Suppressed until" in body
    assert "Look up again becomes available" in body
    assert f"/pipeline/tracklists/{file_rec.id}/prioritize" not in body


@pytest.mark.asyncio
async def test_a_scraped_tracklist_reads_as_scraped(client: AsyncClient, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    file_rec = await _seed_live_set(make_file, session)
    tracklist = Tracklist(
        external_id="ext-10",
        source_url="https://www.1001tracklists.com/tracklist/ext-10",
        file_id=file_rec.id,
        match_confidence=95,
        artist="Nightwave",
        event="Signal Festival",
        date=date(2026, 8, 1),
    )
    session.add(tracklist)
    await session.flush()
    version = TracklistVersion(tracklist_id=tracklist.id, version_number=1)
    session.add(version)
    await session.flush()
    tracklist.latest_version_id = version.id
    session.add(
        TracklistTrack(
            version_id=version.id,
            position=1,
            artist="Coldwave",
            title="Opener",
            timestamp="00:00",
            label="Example Label",
            is_mashup=True,
            remix_info="Signal Remix",
        )
    )
    await session.commit()

    drawer = (await client.get(f"/record/{file_rec.id}")).text
    page = (await client.get(f"/files/{file_rec.id}")).text

    for body in (drawer, page):
        assert "Scraped from 1001Tracklists" in body
        assert "Propagated from a duplicate" not in body
        assert "match confidence 95" in body
        assert "Nightwave" in body
        assert "Signal Festival" in body
        assert "2026-08-01" in body
        assert "1 track" in body
        assert "00:00" in body
        assert "Coldwave" in body
        assert "Opener" in body
        assert "mashup" in body
        assert "Signal Remix" in body
        assert "Example Label" in body
