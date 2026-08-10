"""phaze-ysz16: per-item containment in the fire-and-forget pipeline producer loops.

``_enqueue_proposal_jobs`` / ``_enqueue_extraction_jobs`` / ``_enqueue_match_jobs`` were bare
loops with NO per-item ``try``/``except`` -- the first exception (a transient broker/pool error)
aborted every remaining item in the group. Because every caller detaches these via
``asyncio.create_task`` with only a bare ``_background_tasks.discard`` done-callback
(``task.result()`` never called), the failure surfaced only as asyncio's uncorrelated GC-time
"Task exception was never retrieved" log -- never tied to the request -- while the HTTP response
had already reported the FULL count as enqueued. This mirrors phaze-4ter's fix for
``_enqueue_analysis_jobs`` (see ``test_enqueue_analysis_jobs_logs_a_blocked_collision`` in
``test_pipeline.py``): per-item containment plus an explicit correlated log.

These are pure unit tests against the private producer coroutines directly -- no DB, no HTTP
client -- since the functions only touch their ``queue`` argument and the attributes read off
each item.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
import uuid

import pytest

import phaze.routers.pipeline as pipeline_mod


class _RaisingOnceQueue:
    """A minimal queue double whose ``enqueue`` raises on one chosen call index, then succeeds.

    ``captured`` mirrors ``tests._queue_fakes.FakeQueue``'s shape (``(task_name, kwargs)``
    pairs) so assertions read the same way as the rest of the suite -- a raising call is NOT
    appended, matching the real SAQ contract (a raise means nothing was enqueued).
    """

    def __init__(self, fail_at: int) -> None:
        self._fail_at = fail_at
        self._calls = 0
        self.captured: list[tuple[str, dict[str, Any]]] = []

    async def enqueue(self, task_name: str, **kwargs: Any) -> None:
        call = self._calls
        self._calls += 1
        if call == self._fail_at:
            raise ConnectionError("simulated transient broker/pool error")
        self.captured.append((task_name, kwargs))


def _make_file(idx: int) -> SimpleNamespace:
    uid = uuid.uuid4()
    return SimpleNamespace(id=uid, original_path=f"/music/{idx}-{uid.hex}.mp3", file_type="mp3")


@pytest.mark.asyncio
async def test_enqueue_proposal_jobs_contains_a_raising_batch(caplog: pytest.LogCaptureFixture) -> None:
    """One batch's enqueue raising must not abort the rest of the group (was: bare loop, aborts all)."""
    queue = _RaisingOnceQueue(fail_at=1)
    batches = [["a"], ["b"], ["c"]]

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        await pipeline_mod._enqueue_proposal_jobs(queue, batches)

    # Batch 1 raised; batches 0 and 2 still enqueued -- the group survives one failure.
    assert [kwargs["batch_index"] for _, kwargs in queue.captured] == [0, 2]
    assert "failed to enqueue generate_proposals batch" in caplog.text
    assert "batches dropped from this run" in caplog.text


@pytest.mark.asyncio
async def test_enqueue_proposal_jobs_all_succeed_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Regression backstop: the happy path stays quiet -- no spurious drop warning."""
    queue = _RaisingOnceQueue(fail_at=-1)
    batches = [["a"], ["b"]]

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        await pipeline_mod._enqueue_proposal_jobs(queue, batches)

    assert len(queue.captured) == 2
    assert "dropped from this run" not in caplog.text


@pytest.mark.asyncio
async def test_enqueue_extraction_jobs_contains_a_raising_file(caplog: pytest.LogCaptureFixture) -> None:
    """One file's enqueue raising must not abort the rest of the group."""
    queue = _RaisingOnceQueue(fail_at=0)
    files = [_make_file(0), _make_file(1)]

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        await pipeline_mod._enqueue_extraction_jobs(queue, files, "test-fileserver")

    assert len(queue.captured) == 1
    assert queue.captured[0][1]["file_id"] == str(files[1].id)
    assert "failed to enqueue extract_file_metadata job" in caplog.text
    assert "files dropped from this run" in caplog.text


@pytest.mark.asyncio
async def test_enqueue_extraction_jobs_all_succeed_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Regression backstop: the happy path stays quiet -- no spurious drop warning."""
    queue = _RaisingOnceQueue(fail_at=-1)
    files = [_make_file(0), _make_file(1)]

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        await pipeline_mod._enqueue_extraction_jobs(queue, files, "test-fileserver")

    assert len(queue.captured) == 2
    assert "dropped from this run" not in caplog.text


@pytest.mark.asyncio
async def test_enqueue_match_jobs_contains_a_raising_tracklist(caplog: pytest.LogCaptureFixture) -> None:
    """One tracklist's enqueue raising must not abort the rest of the group."""
    queue = _RaisingOnceQueue(fail_at=0)
    tracklists = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        await pipeline_mod._enqueue_match_jobs(queue, tracklists)  # type: ignore[arg-type]

    assert len(queue.captured) == 1
    assert queue.captured[0][1]["tracklist_id"] == str(tracklists[1].id)
    assert "failed to enqueue match_tracklist_to_discogs job" in caplog.text
    assert "tracklists dropped from this run" in caplog.text


@pytest.mark.asyncio
async def test_enqueue_match_jobs_all_succeed_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Regression backstop: the happy path stays quiet -- no spurious drop warning."""
    queue = _RaisingOnceQueue(fail_at=-1)
    tracklists = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]

    with caplog.at_level("WARNING", logger="phaze.routers.pipeline"):
        await pipeline_mod._enqueue_match_jobs(queue, tracklists)  # type: ignore[arg-type]

    assert len(queue.captured) == 2
    assert "dropped from this run" not in caplog.text
