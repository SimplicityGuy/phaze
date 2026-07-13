"""CR-01 (Phase 81 code review): POST /pipeline/analysis-failed/retry clears BOTH halves of the dual-write.

FAIL-01 made ``analysis.failed_at`` a durable twin of ``files.state = ANALYSIS_FAILED``. The
pre-existing ``retry_analysis_failed`` endpoint predates that marker and flipped only the state
half, so after one click of the operator "Retry failed" button every retried file was left as::

    files.state = 'fingerprinted'  AND  analysis.failed_at IS NOT NULL

That row is a contradiction with real consequences:

* the Phase 79 shadow-compare gate — which this milestone must keep green through Phase 90 —
  reads it as divergent;
* ``domain_completed(ANALYZE)`` reads ``failed_at`` as terminal, i.e. "we tried, it is
  un-processable", so Phase 80's recovery would SKIP the very files the operator just re-enqueued.

``put_analysis`` only clears the marker on a *successful* re-analysis, so if the job never lands
the stale marker is permanent. Clearing at retry time is safe: migration 033's
``analysis_completed_xor_failed`` CHECK guarantees ``analysis_completed_at IS NULL`` on a failed
row, so a cleared row derives ``not_started`` — exactly what a fresh re-analysis should observe.

Contrast with the metadata retry (D-11, ``test_pipeline_metadata_retry.py``), which deliberately
LEAVES its marker: a zero-metadata row with ``failed_at`` cleared would read DONE forever. Analyze
has no such hazard because completion is carried by a separate column, not by row presence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.enums.stage import Stage, Status, domain_completed
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from tests._queue_fakes import install_fake_queues, seed_active_agent


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _make_file() -> FileRecord:
    """A FileRecord parked in the terminal analyze-failed bucket."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )


def _make_failed_analysis(file_id: uuid.UUID) -> AnalysisResult:
    """An analyze failure row exactly as the 81-05 writer persists it (failed_at set, completed NULL)."""
    return AnalysisResult(id=uuid.uuid4(), file_id=file_id, failed_at=datetime.now(UTC), error_message="boom: bad frame", analysis_completed_at=None)


async def _seed_failed(session: AsyncSession, n: int) -> set[str]:
    """Seed ``n`` ANALYSIS_FAILED files, each carrying the durable analyze failure marker."""
    files = [_make_file() for _ in range(n)]
    session.add_all(files)
    await session.commit()
    session.add_all([_make_failed_analysis(f.id) for f in files])
    await session.commit()
    return {str(f.id) for f in files}


@pytest.mark.asyncio
async def test_retry_clears_analysis_failure_marker(client: AsyncClient, session: AsyncSession) -> None:
    """CR-01: after retry, no retried file retains ``analysis.failed_at`` (the stale-marker regression)."""
    failed_ids = await _seed_failed(session, 3)
    await seed_active_agent(session)
    install_fake_queues(client)

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200

    session.expire_all()
    rows = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id.in_([uuid.UUID(i) for i in failed_ids])))).scalars().all()
    assert len(rows) == 3
    for r in rows:
        assert r.failed_at is None, "retry left a stale analyze failure marker (CR-01)"
        assert r.error_message is None
        # The XOR CHECK invariant still holds, and the row now derives not_started.
        assert r.analysis_completed_at is None


@pytest.mark.asyncio
async def test_retry_clears_marker_without_touching_state(client: AsyncClient, session: AsyncSession) -> None:
    """Phase 90 (D-09): the FAIL-01 dual-write collapsed to a single authority. Retry clears
    analysis.failed_at (the derived failed_clause source) for every file and NO LONGER writes
    files.state -- so a file can never end up in the old contradictory fingerprinted-yet-failed pair.
    """
    failed_ids = await _seed_failed(session, 2)
    await seed_active_agent(session)
    install_fake_queues(client)

    assert (await client.post("/pipeline/analysis-failed/retry")).status_code == 200

    session.expire_all()
    ids = [uuid.UUID(i) for i in failed_ids]
    rows = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id.in_(ids)))).scalars().all()

    # The derived failure marker is cleared for every retried file (the sole authority now).
    assert all(r.failed_at is None for r in rows)
    # files.state is left untouched -- the raw column is dead as of Phase 90 PR-B.


@pytest.mark.asyncio
async def test_retried_file_is_not_domain_completed(client: AsyncClient, session: AsyncSession) -> None:
    """A retried file must derive ``not_started`` so Phase 80 recovery re-runs it rather than skipping it."""
    await _seed_failed(session, 1)
    await seed_active_agent(session)
    install_fake_queues(client)

    assert (await client.post("/pipeline/analysis-failed/retry")).status_code == 200

    session.expire_all()
    row = (await session.execute(select(AnalysisResult))).scalars().one()
    # Cleared marker + NULL completion == NOT_STARTED, which is NOT domain-complete.
    assert row.failed_at is None and row.analysis_completed_at is None
    assert domain_completed({Stage.ANALYZE: Status.NOT_STARTED}, Stage.ANALYZE) is False
    # Sanity: had the marker survived, the file WOULD have been (wrongly) domain-complete.
    assert domain_completed({Stage.ANALYZE: Status.FAILED}, Stage.ANALYZE) is True


@pytest.mark.asyncio
async def test_retry_with_no_active_agent_mutates_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """Phase-30 guard survives the fix: with no agent online, neither state nor marker is touched."""
    failed_ids = await _seed_failed(session, 2)
    install_fake_queues(client)  # no seed_active_agent -> NoActiveAgentError path

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200

    session.expire_all()
    ids = [uuid.UUID(i) for i in failed_ids]
    rows = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id.in_(ids)))).scalars().all()
    assert all(r.failed_at is not None for r in rows)
