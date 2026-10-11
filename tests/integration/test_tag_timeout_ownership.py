"""A real SAQ timeout must not leave a writer able to overwrite acknowledged Undo.

Synthetic MP3, real PG broker/Worker, authenticated ASGI callbacks and actual Undo route.
Event barriers bound the physical schedule; no mount hang, production data or 600-second wait.
"""

import asyncio
import fcntl
import threading
from unittest.mock import patch
import uuid

import pytest
from saq import Worker
from saq.queue.postgres import PostgresQueue
from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.services.hashing import compute_sha256
from phaze.services.tag_writer import enqueue_tag_write
import phaze.tasks.tag_write as task
from tests._queue_fakes import install_fake_queues
from tests.db_guard import integration_dsns
from tests.integration.test_tag_undo_loop_seam import (
    _FORWARD,
    _agent_settings,
    _applied_file,
    _log_rows,
    _make_agent_client,
    _raw_tags,
    _seed_original_tags,
)
from tests.review.tasks.test_tag_write import _make_mp3


pytestmark = pytest.mark.integration


async def test_worker_timeout_keeps_writer_owned_until_actual_undo(session, seed_test_agent, tmp_path, client):
    owner, token = seed_test_agent
    path = _make_mp3(tmp_path / "song.mp3")
    _seed_original_tags(path)
    original = _raw_tags(path)
    inode = path.stat().st_ino
    record = await _applied_file(session, owner.id, path)
    _, router = install_fake_queues(client)
    log = await enqueue_tag_write(session, router, record, dict(_FORWARD), source="metadata")
    [payload] = [kwargs for _, name, kwargs in router.captures if name == "write_file_tags"]
    router.captures.clear()
    api = _make_agent_client(session, token)
    dsn, _ = integration_dsns()
    queue = PostgresQueue(url=dsn, name=f"tag-ownership-{uuid.uuid4().hex}")
    worker = Worker(queue, functions=[task.write_file_tags], concurrency=1, dequeue_timeout=0.5)
    worker.context["api_client"] = api
    entered, release, finished, blocked = (threading.Event() for _ in range(4))
    actual_write, actual_flock = task._write_verify_and_rehash, fcntl.flock
    actual_owner = task._run_owned_write
    owner_settled = threading.Event()
    first_owner = True
    first = True
    writers = threading.Lock()

    def observe_owner(*args):
        nonlocal first_owner
        with writers:
            initial, first_owner = first_owner, False
        try:
            return actual_owner(*args)
        finally:
            if initial:
                owner_settled.set()

    def held_write(*args):
        nonlocal first
        with writers:
            hold, first = first, False
        if hold:
            entered.set()
            try:
                if not release.wait(10):
                    raise RuntimeError("synthetic writer release deadline exceeded")
                return actual_write(*args)
            finally:
                finished.set()
        return actual_write(*args)

    def observe_admission(fd, operation):
        try:
            return actual_flock(fd, operation)
        except BlockingIOError:
            blocked.set()  # Actual failed kernel admission, not a timeout-based assumption.
            raise

    jobs = []
    admission = None
    try:
        await queue.connect()
        with (
            patch.object(task, "get_settings", return_value=_agent_settings([str(tmp_path)])),
            patch.object(task, "_write_verify_and_rehash", side_effect=held_write),
            patch.object(task, "_run_owned_write", side_effect=observe_owner),
            patch.object(task.fcntl, "flock", side_effect=observe_admission),
        ):
            job = await queue.enqueue("write_file_tags", key=str(log.id), timeout=0.2, retries=4, retry_delay=0, **payload)
            a = asyncio.create_task(worker.process())
            jobs.append(a)
            assert await asyncio.to_thread(entered.wait, 3), "writer never entered"
            assert await asyncio.wait_for(a, 3), "Worker did not consume timeout attempt"
            retried = await queue.job(job.key)
            assert retried.status == "queued" and retried.attempts == 1
            # The physical timeout already happened. Give the retry enough time for the bounded
            # barrier schedule; production defaults are unchanged.
            await retried.update(timeout=5)
            b = asyncio.create_task(worker.process())
            jobs.append(b)
            admission = asyncio.create_task(asyncio.to_thread(blocked.wait, 3))
            done, _ = await asyncio.wait({b, admission}, timeout=3, return_when=asyncio.FIRST_COMPLETED)
            assert done, "neither retry completion nor true blocked admission observed"
            if b in done:
                # Faulty behavior must be red: finish actual Undo BEFORE releasing old writer A.
                assert await b
                assert _raw_tags(path) != original
                undo = await client.post(f"/tags/{record.id}/undo")
                assert undo.status_code == 200, undo.text
                [undo_payload] = [kwargs for _, name, kwargs in router.captures if name == "write_file_tags"]
                await queue.enqueue("write_file_tags", timeout=5, **undo_payload)
                assert await asyncio.wait_for(worker.process(), 3)
                assert _raw_tags(path) == original
                acknowledged_digest = compute_sha256(path)
                release.set()
            else:
                assert admission.result() and not b.done()
                release.set()
                assert await asyncio.wait_for(b, 3)
                assert _raw_tags(path) != original
                undo = await client.post(f"/tags/{record.id}/undo")
                assert undo.status_code == 200, undo.text
                [undo_payload] = [kwargs for _, name, kwargs in router.captures if name == "write_file_tags"]
                await queue.enqueue("write_file_tags", timeout=5, **undo_payload)
                assert await asyncio.wait_for(worker.process(), 3)
                assert _raw_tags(path) == original
                acknowledged_digest = compute_sha256(path)
            assert await asyncio.to_thread(finished.wait, 3)
            assert await asyncio.to_thread(owner_settled.wait, 3)
            assert _raw_tags(path) == original
            assert compute_sha256(path) == acknowledged_digest
            stored = await session.scalar(select(FileRecord.sha256_hash).where(FileRecord.id == record.id))
            assert stored == acknowledged_digest
            assert all(status == "completed" for status, _ in (await _log_rows(session, record.id)).values())
            assert path.stat().st_ino == inode, "mutagen changed the lock identity"
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 3)
        await asyncio.to_thread(owner_settled.wait, 3)
        for pending in jobs:
            if not pending.done():
                pending.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if admission is not None:
            await admission
        worker.pool.shutdown(wait=True)
        async with queue.pool.connection() as connection:
            await connection.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await queue.disconnect()
        await api._client.aclose()


@pytest.mark.parametrize("ext", ["mp3", "flac", "ogg", "opus", "m4a", "wma", "wav", "aiff"])
async def test_mutagen_growth_keeps_inode_owned_through_callback(tmp_path, ext):
    """Supported save strategies retain the inode and keep other processes excluded."""
    from unittest.mock import AsyncMock, MagicMock

    from phaze.enums.tag_write import TagWriteStatus
    from tests.review.services.test_tag_write_real_containers import _make_container

    path = _make_container(tmp_path, ext)
    before = path.stat()
    api = AsyncMock()
    api.report_tag_write_before_snapshot.return_value = MagicMock(status=TagWriteStatus.QUEUED)
    checked = []
    code = "import fcntl,sys; f=open(sys.argv[1], 'rb'); \ntry: fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError: sys.exit(7)"

    async def probe():
        process = await asyncio.create_subprocess_exec("uv", "run", "python", "-c", code, str(path))
        try:
            return await asyncio.wait_for(process.wait(), 5)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def callback(*_args):
        assert path.stat().st_ino == before.st_ino
        assert path.stat().st_size > before.st_size
        checked.append(await probe())

    api.patch_tag_write.side_effect = callback
    with patch.object(task, "get_settings", return_value=_agent_settings([str(tmp_path)])):
        result = await task.write_file_tags(
            {"api_client": api},
            file_id=str(uuid.uuid4()),
            log_id=str(uuid.uuid4()),
            agent_id="synthetic-agent",
            file_path=str(path),
            tags={"artist": "Artist " * 2000},
        )
    assert result["status"] == "completed"
    assert checked == [7], "another process acquired ownership before callback settled"
    assert await probe() == 0, "settled writer did not release ownership"


async def test_cancelled_waiter_never_writes_after_owner_releases(tmp_path):
    """Cancellation before actual admission must leave no latent physical writer."""
    from unittest.mock import AsyncMock, MagicMock

    from phaze.enums.tag_write import TagWriteStatus

    path = _make_mp3(tmp_path / "song.mp3")
    original = path.read_bytes()
    api = AsyncMock()
    api.report_tag_write_before_snapshot.return_value = MagicMock(status=TagWriteStatus.QUEUED)
    blocked, settled = threading.Event(), threading.Event()
    flock, owned = fcntl.flock, task._run_owned_write

    def observe(fd, operation):
        try:
            return flock(fd, operation)
        except BlockingIOError:
            blocked.set()
            raise

    def observe_settlement(*args):
        try:
            return owned(*args)
        finally:
            settled.set()

    with path.open("rb") as lease:
        flock(lease.fileno(), fcntl.LOCK_EX)
        with (
            patch.object(task, "get_settings", return_value=_agent_settings([str(tmp_path)])),
            patch.object(task.fcntl, "flock", side_effect=observe),
            patch.object(task, "_run_owned_write", side_effect=observe_settlement),
        ):
            attempt = asyncio.create_task(
                task.write_file_tags(
                    {"api_client": api},
                    file_id=str(uuid.uuid4()),
                    log_id=str(uuid.uuid4()),
                    agent_id="synthetic-agent",
                    file_path=str(path),
                    tags={"artist": "New Artist"},
                )
            )
            try:
                assert await asyncio.to_thread(blocked.wait, 3)
                attempt.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await attempt
                assert await asyncio.to_thread(settled.wait, 3)
            finally:
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
                flock(lease.fileno(), fcntl.LOCK_UN)
    assert path.read_bytes() == original
    api.report_tag_write_before_snapshot.assert_not_awaited()
    api.patch_tag_write.assert_not_awaited()


async def test_dead_process_owner_releases_kernel_lease(tmp_path):
    """No stale application registry can strand the inode after the owning process exits."""
    from unittest.mock import AsyncMock, MagicMock

    from phaze.enums.tag_write import TagWriteStatus

    path = _make_mp3(tmp_path / "song.mp3")
    code = "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f,fcntl.LOCK_EX); print('owned',flush=True); sys.stdin.read(1)"
    process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "python",
        "-c",
        code,
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    api = AsyncMock()
    api.report_tag_write_before_snapshot.return_value = MagicMock(status=TagWriteStatus.QUEUED)
    attempt = None
    blocked = threading.Event()
    actual = fcntl.flock

    def observe(fd, operation):
        try:
            return actual(fd, operation)
        except BlockingIOError:
            blocked.set()
            raise

    try:
        assert await asyncio.wait_for(process.stdout.readline(), 3) == b"owned\n"
        with (
            patch.object(task, "get_settings", return_value=_agent_settings([str(tmp_path)])),
            patch.object(task.fcntl, "flock", side_effect=observe),
        ):
            attempt = asyncio.create_task(
                task.write_file_tags(
                    {"api_client": api},
                    file_id=str(uuid.uuid4()),
                    log_id=str(uuid.uuid4()),
                    agent_id="synthetic-agent",
                    file_path=str(path),
                    tags={"artist": "New Artist"},
                )
            )
            assert await asyncio.to_thread(blocked.wait, 3)
            api.report_tag_write_before_snapshot.assert_not_awaited()
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 3) == 0
            assert (await asyncio.wait_for(attempt, 3))["status"] == "completed"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if attempt is not None:
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)


async def test_unsupported_locking_refuses_mutation(tmp_path):
    from unittest.mock import AsyncMock

    path = _make_mp3(tmp_path / "song.mp3")
    original = path.read_bytes()
    api = AsyncMock()
    with (
        patch.object(task, "get_settings", return_value=_agent_settings([str(tmp_path)])),
        patch.object(task.fcntl, "flock", side_effect=OSError("unsupported filesystem lock")),
        pytest.raises(OSError, match="unsupported"),
    ):
        await task.write_file_tags(
            {"api_client": api},
            file_id=str(uuid.uuid4()),
            log_id=str(uuid.uuid4()),
            agent_id="synthetic-agent",
            file_path=str(path),
            tags={"artist": "New Artist"},
        )
    assert path.read_bytes() == original
    api.report_tag_write_before_snapshot.assert_not_awaited()
    api.patch_tag_write.assert_not_awaited()
