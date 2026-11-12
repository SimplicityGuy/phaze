"""phaze-tdton: CUE-linked media are scheduled ahead of the general analyze backlog, and their coverage is countable."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderSourceObject, ProviderSourceObservation
from phaze.services.pipeline import get_discovered_files_with_duration
from phaze.services.pipeline.common import cue_linked_analysis_coverage_stmt
from tests.shared.services.pipeline._shared import _make_pipeline_file


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


async def _link(session: AsyncSession, media: FileRecord, source_format: str, status: str = "pending") -> None:
    """Give ``media`` a recording candidate pointing at a ``source_format`` observation."""
    obj = ProviderSourceObject(provider_id="local", native_id=f"n:{uuid.uuid4().hex}", native_digest=uuid.uuid4().hex)
    session.add(obj)
    await session.flush()
    obs = ProviderSourceObservation(
        object_id=obj.id,
        status="ok",
        code="ok",
        revision_scope="full",
        parser_version="local-tracklist-v4",
        content_digest=uuid.uuid4().hex,
        payload={"source_format": source_format},
        retrieved_at=datetime.now(UTC),
    )
    session.add(obs)
    await session.flush()
    session.add(ProviderRecordingCandidate(media_id=media.id, observation_id=obs.id, status=status))
    await session.flush()


@pytest.mark.asyncio
async def test_cue_linked_media_are_enqueued_ahead_of_the_general_backlog(session: AsyncSession) -> None:
    plain = [_make_pipeline_file() for _ in range(5)]
    cue_files = [_make_pipeline_file() for _ in range(2)]
    non_cue_linked = _make_pipeline_file()  # a candidate from a non-CUE source must NOT jump the queue
    session.add_all([*plain, *cue_files, non_cue_linked])
    await session.flush()
    for f in cue_files:
        await _link(session, f, "cue", status="approved")
    await _link(session, non_cue_linked, "m3u")
    await session.commit()

    rows = await get_discovered_files_with_duration(session)

    ids = [record.id for record, _ in rows]
    assert len(ids) == 8, "ordering must not change the pending membership"
    assert set(ids[:2]) == {f.id for f in cue_files}
    assert non_cue_linked.id in ids[2:]


@pytest.mark.asyncio
async def test_cue_linked_coverage_counts_total_with_and_without_analysis(session: AsyncSession) -> None:
    analysed, failed, pending, unlinked = (_make_pipeline_file() for _ in range(4))
    session.add_all([analysed, failed, pending, unlinked])
    await session.flush()
    for f in (analysed, failed, pending):
        await _link(session, f, "cue")
    session.add(AnalysisResult(file_id=analysed.id, analysis_completed_at=datetime.now(UTC)))
    session.add(AnalysisResult(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    session.add(AnalysisResult(file_id=unlinked.id, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    row = (await session.execute(cue_linked_analysis_coverage_stmt())).one()

    assert (row.total, row.with_analysis, row.without_analysis) == (3, 1, 2)
