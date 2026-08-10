"""phaze-6ib1n: ``_retry_analysis_group``'s blind ``failed_at`` restore must not trip the analysis
``analysis_completed_xor_failed`` CHECK when a raced file completes mid-loop.

``retry_analysis_failed`` clears + commits ``analysis.failed_at`` for the WHOLE routed set BEFORE
backgrounding the enqueue loop. ``_retry_analysis_group`` then re-stamps ``failed_at`` for exactly
the ids whose enqueue raised (phaze-4ter), via ONE multi-row UPDATE with no
``analysis_completed_at`` guard. If ANY of those ids raced a concurrent completion (e.g. a still-live
job -- an earlier deepen, or its own retry enqueue that actually landed on SAQ's side despite raising
here) and had ``analysis_completed_at`` stamped by ``put_analysis`` while the loop was still
grinding, the restore UPDATE produces a mixed row and the ``analysis_completed_xor_failed`` CHECK
aborts the WHOLE STATEMENT -- so every OTHER id in the same restore, whose enqueue genuinely never
happened, ALSO permanently loses its failure marker with no replacement job.

The fix scopes the restore with ``AnalysisResult.analysis_completed_at.is_(None)`` (mirroring
``report_analysis_failed``'s own conflict predicate), so a completed row is silently left alone
(correct -- it is done, not failed) and can never void the restore for the rest of the group.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select, update

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from tests._queue_fakes import install_fake_queues, make_agent_live


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


async def _drain_background() -> None:
    import phaze.routers.pipeline as pipeline_mod

    for _ in range(500):
        if not pipeline_mod._background_tasks:
            return
        await asyncio.sleep(0)


def _make_file() -> FileRecord:
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
    return AnalysisResult(id=uuid.uuid4(), file_id=file_id, failed_at=datetime.now(UTC), error_message="boom: bad frame", analysis_completed_at=None)


async def _seed_failed(session: AsyncSession, n: int) -> list[FileRecord]:
    files = [_make_file() for _ in range(n)]
    session.add_all(files)
    await session.commit()
    session.add_all([_make_failed_analysis(f.id) for f in files])
    await session.commit()
    return files


@pytest.mark.asyncio
async def test_a_raced_completed_file_does_not_void_the_restore_for_the_rest_of_the_group(
    client: AsyncClient,
    session: AsyncSession,
    verify: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One id racing to ``analysis_completed_at`` mid-loop must not sink the OTHER ids' restore.

    Without the ``analysis_completed_at IS NULL`` guard, the multi-row restore UPDATE aborts as a
    whole statement on the mixed row, and ``genuinely_failed``'s marker (and job) would be lost
    silently, caught only by the bare ``except Exception: logger.error(...)`` wrapper.
    """
    files = await _seed_failed(session, 3)
    raced_file, genuinely_failed, ok_file = files
    await make_agent_live(session)
    install_fake_queues(client)

    from phaze.database import async_session
    import phaze.routers.pipeline as pipeline_mod

    real_enqueue = pipeline_mod.enqueue_process_file

    async def _flaky_enqueue(queue: Any, file: FileRecord, agent_id: str, models_path: str, **kwargs: Any) -> Any:
        if file.id == raced_file.id:
            # Simulate a CONCURRENT completion landing mid-loop (a still-live job -- an earlier
            # deepen, or this exact retry's own enqueue actually reaching SAQ despite raising here,
            # the phaze-6ib1n scenario) -- a DIFFERENT session commits analysis_completed_at while
            # this file's OWN enqueue also fails.
            async with async_session() as race_session:
                await race_session.execute(
                    update(AnalysisResult).where(AnalysisResult.file_id == raced_file.id).values(analysis_completed_at=datetime.now(UTC)),
                )
                await race_session.commit()
            raise RuntimeError("simulated transient queue-pool error (raced file)")
        if file.id == genuinely_failed.id:
            raise RuntimeError("simulated transient queue-pool error")
        return await real_enqueue(queue, file, agent_id, models_path, **kwargs)

    monkeypatch.setattr(pipeline_mod, "enqueue_process_file", _flaky_enqueue)

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200

    await _drain_background()

    # The genuinely-failed file (no race) MUST get its failure marker restored -- this is the id the
    # unguarded restore would have lost as collateral damage from the raced row's CHECK violation.
    genuine_row = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id == genuinely_failed.id))).scalar_one()
    assert genuine_row.failed_at is not None, "an unrelated id's race must not void this file's restore (phaze-6ib1n)"
    assert genuine_row.error_message is not None and "phaze-4ter" in genuine_row.error_message

    # The raced file completed -- it must stay completed, NOT be knocked back to failed (that would
    # be a correctness regression of its own: a DONE file re-appearing in the red bucket).
    raced_row = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id == raced_file.id))).scalar_one()
    assert raced_row.analysis_completed_at is not None
    assert raced_row.failed_at is None, "a completed row must never be re-stamped failed (analysis_completed_xor_failed)"

    # The file whose enqueue succeeded is untouched.
    ok_row = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id == ok_file.id))).scalar_one()
    assert ok_row.failed_at is None
    assert ok_row.analysis_completed_at is None
