"""phaze-4ter: a per-file enqueue failure in the bulk analyze retry must not silently lose the file.

``retry_analysis_failed`` clears + commits ``analysis.failed_at`` for the WHOLE routed set BEFORE
backgrounding the enqueue loop (RESEARCH Pitfall 3 -- the red count must drop regardless of the
enqueue outcome). Before this fix that background loop had no per-file containment
(``_enqueue_analysis_jobs`` was a bare ``for f in files: await enqueue_process_file(...)``) and its
``asyncio.create_task`` done-callback was a bare ``_background_tasks.discard`` that never called
``task.result()``. The FIRST raised enqueue in a group therefore both aborted every remaining file
in that group AND left the exception unretrieved -- the failed file(s) had no ``analysis.failed_at``
marker (already cleared+committed), no queued job (the enqueue never ran), and no ledger row (the
``before_enqueue`` hook never fired): the file vanished from the red bucket with zero trace that it
ever failed.

The fix contains each enqueue individually (``_enqueue_analysis_jobs`` returns the ids that failed
instead of raising past the first one) and restores ``analysis.failed_at`` for exactly those ids
(``_retry_analysis_group``), so a transient enqueue error degrades to "still shows failed,
retryable" instead of silent, permanent loss -- while every file whose enqueue DID succeed is
unaffected, including ones queued in the same group after the failure.
"""

from __future__ import annotations

import asyncio
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


async def _drain_background() -> None:
    """Yield until the router's background enqueue tasks have drained (phaze-zecg / phaze-4ter).

    Mirrors ``tests/analyze/test_retry_affordances.py::_drain_background`` -- the retry endpoint's
    response returns before its background task (now ``_retry_analysis_group``, which also performs
    the marker-restore write) necessarily finishes.
    """
    import phaze.routers.pipeline as pipeline_mod

    for _ in range(500):
        pending = set(pipeline_mod._background_tasks)
        if not pending:
            return
        # `asyncio.sleep(0)` only YIELDS -- it never waits for I/O. Under full-suite load all 500
        # yields can elapse while a background task is still awaiting a database round-trip, so this
        # returned EARLY and the `session` fixture's `outer.rollback()` fired while that task still
        # held savepoints -- surfacing as `InvalidSavepointSpecificationError: savepoint
        # "sa_savepoint_N" does not exist` at TEARDOWN, and green on isolated re-run. Awaiting the
        # tasks themselves drains for real; the bounded loop stays because a draining task may
        # spawn another.
        await asyncio.wait(pending, timeout=10)


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
async def test_one_enqueue_failure_does_not_abandon_the_rest_of_the_group(
    client: AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FIRST enqueue in a group raising must not prevent later files in the SAME group from
    enqueuing (the pre-fix bare loop aborted the whole remainder on the first exception)."""
    files = await _seed_failed(session, 3)
    failing_file = files[0]
    ok_files = files[1:]
    await make_agent_live(session)
    _, task_router = install_fake_queues(client)

    # phaze-oau1o: `routers/pipeline.py` is now a package. `enqueue_process_file` is bound in the
    # `analysis` submodule's namespace, so the patch must name that module -- patching the
    # facade would set an attribute nothing reads and silently no-op this test.
    import phaze.routers.pipeline.analysis as pipeline_mod

    real_enqueue = pipeline_mod.enqueue_process_file

    async def _flaky_enqueue(queue: Any, file: FileRecord, agent_id: str, models_path: str, **kwargs: Any) -> Any:
        if file.id == failing_file.id:
            raise RuntimeError("simulated transient queue-pool error")
        return await real_enqueue(queue, file, agent_id, models_path, **kwargs)

    monkeypatch.setattr(pipeline_mod, "enqueue_process_file", _flaky_enqueue)

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200
    # The ack reflects the routed count, taken before any enqueue outcome is known.
    assert "re-queued 3 failed file(s) for analysis" in response.text.lower()

    await _drain_background()

    queue = task_router.queues["test-fileserver-analyze"]
    enqueued_ids = {p["file_id"] for _t, p in queue.captured}
    assert enqueued_ids == {str(f.id) for f in ok_files}, "the two files after the failing one must still have been enqueued"


@pytest.mark.asyncio
async def test_failed_enqueue_restores_the_failure_marker(
    client: AsyncClient,
    session: AsyncSession,
    verify: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose enqueue failed must land back in the ANALYSIS_FAILED bucket (marker restored),
    not vanish with a cleared marker and no replacement job (the phaze-4ter loss)."""
    files = await _seed_failed(session, 2)
    failing_file, ok_file = files
    await make_agent_live(session)
    install_fake_queues(client)

    # phaze-oau1o: `routers/pipeline.py` is now a package. `enqueue_process_file` is bound in the
    # `analysis` submodule's namespace, so the patch must name that module -- patching the
    # facade would set an attribute nothing reads and silently no-op this test.
    import phaze.routers.pipeline.analysis as pipeline_mod

    real_enqueue = pipeline_mod.enqueue_process_file

    async def _flaky_enqueue(queue: Any, file: FileRecord, agent_id: str, models_path: str, **kwargs: Any) -> Any:
        if file.id == failing_file.id:
            raise RuntimeError("simulated transient queue-pool error")
        return await real_enqueue(queue, file, agent_id, models_path, **kwargs)

    monkeypatch.setattr(pipeline_mod, "enqueue_process_file", _flaky_enqueue)

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200

    await _drain_background()

    # Independent-session read (the ``verify`` fixture, 92-04 CLEAN-02): the restore write happens
    # in a background task's OWN ``async_session``, never the request's, so a same-session read on
    # ``session`` would not see it either -- ``verify`` proves it actually committed.
    failing_row = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id == failing_file.id))).scalar_one()
    assert failing_row.failed_at is not None, "a file whose enqueue failed must be restored to the failed bucket, not vanish"
    assert failing_row.error_message is not None and "phaze-4ter" in failing_row.error_message

    ok_row = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id == ok_file.id))).scalar_one()
    assert ok_row.failed_at is None, "the file whose enqueue succeeded must stay cleared"


@pytest.mark.asyncio
async def test_all_enqueues_succeeding_leaves_no_trace_of_the_restore_path(
    client: AsyncClient,
    session: AsyncSession,
    verify: AsyncSession,
) -> None:
    """Regression backstop: the happy path (every enqueue succeeds) is byte-identical to before --
    no marker is (re)written by the new restore branch when there is nothing to restore."""
    files = await _seed_failed(session, 3)
    await make_agent_live(session)
    install_fake_queues(client)

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200

    await _drain_background()

    rows = (await verify.execute(select(AnalysisResult).where(AnalysisResult.file_id.in_([f.id for f in files])))).scalars().all()
    assert len(rows) == 3
    assert all(r.failed_at is None and r.error_message is None for r in rows)
