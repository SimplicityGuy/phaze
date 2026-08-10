"""phaze-gcdih: a per-file analyze retry's enqueue exception must not silently lose the file.

``retry_analysis_failed_file`` clears + commits the durable ``analysis.failed_at`` marker BEFORE
enqueuing (CR-01, Phase 81) -- the same ordering its bulk twin ``retry_analysis_failed`` uses. The
bulk twin treats that ordering as unsafe unless a failed enqueue re-stamps the marker (its own
docstring: "That is safe only if a per-file enqueue failure can never be lost") and it restores the
marker via ``_retry_analysis_group`` for exactly the ids whose enqueue raised (phaze-4ter). Before
this fix the per-file endpoint had no such restore: a bare, unhandled ``enqueue_process_file``
exception propagated straight to a 500 (dropped silently by htmx) while the already-committed
marker-clear stood -- the file left the ANALYSIS_FAILED bucket with no replacement job and no
failure record, invisible to the operator and to ``recover_orphaned_work`` (ANALYZE is manual-only,
``ELIGIBLE_AFTER_FAILURE[ANALYZE]=False``).

The fix wraps the enqueue in try/except and, on failure, re-stamps ``failed_at`` /
``error_message`` before returning an honest error fragment -- mirroring the bulk restore.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from tests._queue_fakes import install_fake_queues, make_agent_live


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


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


async def _seed_failed_file(session: AsyncSession) -> FileRecord:
    file = _make_file()
    session.add(file)
    await session.commit()
    session.add(_make_failed_analysis(file.id))
    await session.commit()
    return file


@pytest.mark.asyncio
async def test_per_file_retry_enqueue_failure_restores_the_failure_marker(
    client: AsyncClient,
    session: AsyncSession,
    verify: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-file retry whose enqueue raises must land the file back in ANALYSIS_FAILED, not vanish."""
    file = await _seed_failed_file(session)
    await make_agent_live(session)
    install_fake_queues(client)

    import phaze.routers.pipeline as pipeline_mod

    async def _flaky_enqueue(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated transient queue-pool error")

    monkeypatch.setattr(pipeline_mod, "enqueue_process_file", _flaky_enqueue)

    response = await client.post(f"/pipeline/files/{file.id}/analysis-failed/retry")
    assert response.status_code == 200
    assert "could not re-queue" in response.text.lower()

    # Independent-session read (92-04 CLEAN-02): the request's own session already committed the
    # restore write synchronously (this endpoint is NOT backgrounded, unlike the bulk twin), but
    # ``verify`` proves it is durable rather than merely visible on the request's own session.
    row = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id == file.id))).scalar_one()
    assert row.failed_at is not None, "a file whose enqueue failed must be restored to the failed bucket, not vanish"
    assert row.error_message is not None and "phaze-gcdih" in row.error_message
    # The XOR CHECK invariant still holds.
    assert row.analysis_completed_at is None


@pytest.mark.asyncio
async def test_per_file_retry_enqueue_success_leaves_no_trace_of_the_restore_path(
    client: AsyncClient,
    session: AsyncSession,
    verify: AsyncSession,
) -> None:
    """Regression backstop: the happy path is unaffected -- the marker stays cleared, no restore fires."""
    file = await _seed_failed_file(session)
    await make_agent_live(session)
    install_fake_queues(client)

    response = await client.post(f"/pipeline/files/{file.id}/analysis-failed/retry")
    assert response.status_code == 200
    assert "re-queued 1 failed file(s) for analysis" in response.text.lower()

    row = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id == file.id))).scalar_one()
    assert row.failed_at is None
    assert row.error_message is None
