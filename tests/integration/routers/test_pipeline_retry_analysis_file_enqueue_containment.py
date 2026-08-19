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
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, install_fake_queues, make_agent_live


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

    # phaze-oau1o: `routers/pipeline.py` is now a package. `enqueue_process_file` is bound in the
    # `analysis` submodule's namespace, so the patch must name that module -- patching the
    # facade would set an attribute nothing reads and silently no-op this test.
    import phaze.routers.pipeline.analysis as pipeline_mod

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


@pytest.mark.asyncio
async def test_per_file_retry_blocked_collision_restores_marker_and_reports_blocked(
    client: AsyncClient,
    session: AsyncSession,
    verify: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """phaze-k0rv9: a deterministic-key collision with a DEAD job must not report success.

    Pre-fix, ``enqueue_process_file``'s ``None`` return (SAQ dedup collision) was discarded
    entirely -- the endpoint unconditionally logged "re-queued" and rendered the green success
    fragment even though nothing was scheduled and the key is held by a dead job that will never
    run (``classify_process_file_collision`` -> "blocked"). The already-cleared+committed
    ``failed_at`` marker must be restored and the fragment must say so honestly.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    file = await _seed_failed_file(session)
    await make_agent_live(session)

    router = DedupFakeTaskRouter()
    app = client._transport.app  # type: ignore[union-attr]
    app.state.controller_queue = DedupFakeQueue("controller")
    app.state.task_router = router

    # Pre-register the deterministic key as already "live" so this retry's enqueue dedups to
    # None, then wire the collision probe to report a DEAD (aborting) key-holder.
    queue = router.queue_for("test-fileserver", "analyze")
    queue._live_keys.add(f"process_file:{file.id}")
    queue.job = AsyncMock(return_value=SimpleNamespace(status="aborting", stuck=False))

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        response = await client.post(f"/pipeline/files/{file.id}/analysis-failed/retry")

    assert response.status_code == 200
    assert queue.captured == []  # nothing was ever enqueued -- pure collision
    assert "stuck and holding its slot" in response.text.lower()
    assert "re-queued" not in response.text.lower()
    assert "deterministic key held by a dead job" in caplog.text

    row = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id == file.id))).scalar_one()
    assert row.failed_at is not None, "a blocked retry must restore the failure marker, not vanish"
    assert row.error_message is not None and "phaze-k0rv9" in row.error_message
    assert row.analysis_completed_at is None


@pytest.mark.asyncio
async def test_per_file_retry_collision_lookup_broker_error_degrades_to_success_not_500(
    client: AsyncClient,
    session: AsyncSession,
    verify: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """phaze-k0rv9: a raising collision probe must degrade to benign, not crash the endpoint.

    Mirrors ``deepen_analysis``'s phaze-qim6c degrade: the post-collision ``queue.job()`` lookup
    is itself a SAQ/Postgres call and can fail transiently. Its failure must not propagate as a
    raw 500 through this interactive endpoint -- degrade to "in_flight" (benign) and log.
    """
    from unittest.mock import AsyncMock

    file = await _seed_failed_file(session)
    await make_agent_live(session)

    router = DedupFakeTaskRouter()
    app = client._transport.app  # type: ignore[union-attr]
    app.state.controller_queue = DedupFakeQueue("controller")
    app.state.task_router = router

    queue = router.queue_for("test-fileserver", "analyze")
    queue._live_keys.add(f"process_file:{file.id}")
    queue.job = AsyncMock(side_effect=ConnectionError("transient broker pool error"))

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        response = await client.post(f"/pipeline/files/{file.id}/analysis-failed/retry")

    assert response.status_code == 200
    assert "re-queued 1 failed file(s) for analysis" in response.text.lower()
    assert "collision lookup failed" in caplog.text

    # Degraded to "in_flight" -- the marker stays cleared, exactly like the plain success path.
    row = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id == file.id))).scalar_one()
    assert row.failed_at is None
    assert row.error_message is None
