"""phaze-oxn2m: a file moved within the watched tree keeps ONE row, at its new path, with its final hash.

Real-consumer guard (CLAUDE.md rule 3). Nothing here constructs a watchdog event: a real native
observer -- inotify on Linux, which is what CI and the deployed agents run; FSEvents on a macOS
dev box -- watches ``tmp_path``, the files are written and renamed with ``os``, and the events it
reports flow through the production :class:`WatcherEventHandler` -> :class:`Debouncer` ->
``_post_ready_paths`` -> :class:`Poster` -> the real ``/api/internal/agent/files[/move]`` handlers
-> Postgres. The production incident was a download client finishing into
``incoming/incomplete/<release>/`` and then moving the whole release directory out, so the
directory move -- whose per-file events watchdog synthesizes -- is the case under test.

Timing is driven, not slept: ``settle_period=0`` sweeps whatever the observer has reported so far,
and ``_until`` waits for the observer's events to reach the debouncer.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import sys
import time
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from watchdog.observers import Observer

import phaze.agent_watcher.__main__ as watcher_main
from phaze.agent_watcher.debouncer import Debouncer
from phaze.agent_watcher.observer import WatcherEventHandler
from phaze.agent_watcher.poster import Poster
from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.agent_client import PhazeAgentClient


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


pytestmark = pytest.mark.asyncio

_PARTIAL = b"\x01" * 4096
_FINAL = _PARTIAL + b"\x02" * 4096


def test_the_native_observer_on_linux_is_inotify() -> None:
    """The bead's acceptance names inotify; on Linux the default ``Observer`` must be it, not polling."""
    if not sys.platform.startswith("linux"):
        pytest.skip("inotify exists only on Linux; this platform's native observer is exercised by the tests below")
    from watchdog.observers.inotify import InotifyObserver

    assert Observer is InotifyObserver


async def _until(predicate: Callable[[], bool], what: str, timeout: float = 15.0) -> None:
    """Yield to the loop (where the observer's ``call_soon_threadsafe`` callbacks land) until ``predicate``."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.05)


@asynccontextmanager
async def _watching(root: Path, debouncer: Debouncer) -> AsyncIterator[None]:
    handler = WatcherEventHandler(loop=asyncio.get_running_loop(), debouncer_touch=debouncer.touch, debouncer_move=debouncer.move)
    observer = Observer()
    observer.schedule(handler, path=str(root), recursive=True)
    observer.start()
    try:
        await asyncio.sleep(0.5)  # FSEvents drops events from before its stream is live
        yield
    finally:
        observer.stop()
        observer.join(timeout=10.0)


async def _sweep_and_post(debouncer: Debouncer, poster: Poster) -> list[str]:
    ready, evicted = debouncer.sweep(settle_period=0.0, max_pending=3600.0)
    assert evicted == []
    await watcher_main._post_ready_paths(poster, ready, debouncer)
    return ready


async def _live_poster(session: AsyncSession, agent: Agent, authenticated_client: AsyncClient) -> Poster:
    session.add(ScanBatch(agent_id=agent.id, scan_path="<watcher>", status=ScanStatus.LIVE.value, total_files=0, processed_files=0))
    await session.commit()
    client = PhazeAgentClient(
        base_url=str(authenticated_client.base_url),
        token="unused-test-token",  # noqa: S106 -- the injected client already carries the fixture token
        _client=authenticated_client,
    )
    return Poster(client=client, agent_id=agent.id)


async def _rows(session: AsyncSession) -> list[FileRecord]:
    session.expire_all()
    return list((await session.execute(select(FileRecord).order_by(FileRecord.original_path))).scalars().all())


async def test_a_release_posted_mid_download_then_moved_out_of_incomplete_keeps_one_row_with_the_final_hash(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """The production sequence: posted while partial, finished in place, directory moved out."""
    agent, _token = seed_test_agent
    poster = await _live_poster(session, agent, authenticated_client)
    debouncer = Debouncer()
    staging = tmp_path / "incomplete" / "rel"
    staging.mkdir(parents=True)
    partial_path = staging / "01.mp3"
    sibling_path = staging / "02.mp3"

    async with _watching(tmp_path, debouncer):
        partial_path.write_bytes(_PARTIAL)
        sibling_path.write_bytes(_FINAL)
        await _until(lambda: debouncer.pending_count() == 2, "both partial files to be reported")
        assert sorted(await _sweep_and_post(debouncer, poster)) == [str(partial_path), str(sibling_path)]
        (first_row, _) = await _rows(session)
        first_id = first_row.id
        assert first_row.sha256_hash == hashlib.sha256(_PARTIAL).hexdigest(), "the row was hashed mid-download"

        with partial_path.open("ab") as handle:  # the download finishes in place
            handle.write(_FINAL[len(_PARTIAL) :])
        (tmp_path / "incomplete" / "rel").rename(tmp_path / "rel")  # the client moves the release out

        final_path, final_sibling = tmp_path / "rel" / "01.mp3", tmp_path / "rel" / "02.mp3"
        await _until(
            lambda: debouncer.lineage(str(final_path)) == [str(partial_path)] and debouncer.lineage(str(final_sibling)) == [str(sibling_path)],
            "the directory move's per-file events to reach the debouncer",
        )
        ready = await _sweep_and_post(debouncer, poster)

    assert str(partial_path) not in ready, "the vanished staging name is never posted"
    rows = await _rows(session)
    assert [(row.original_path, row.current_path) for row in rows] == [(str(final_path), str(final_path)), (str(final_sibling), str(final_sibling))]
    by_path = {row.original_path: row for row in rows}
    assert by_path[str(final_path)].id == first_id
    assert by_path[str(final_path)].sha256_hash == hashlib.sha256(_FINAL).hexdigest()
    assert by_path[str(final_sibling)].sha256_hash == hashlib.sha256(_FINAL).hexdigest()


async def test_a_file_moved_before_it_was_ever_posted_is_posted_once_under_its_new_name(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Race: the move lands while the old name is still pending in the debouncer."""
    agent, _token = seed_test_agent
    poster = await _live_poster(session, agent, authenticated_client)
    debouncer = Debouncer()
    (tmp_path / "incomplete").mkdir()
    old, new = tmp_path / "incomplete" / "set.mp3", tmp_path / "set.mp3"

    async with _watching(tmp_path, debouncer):
        old.write_bytes(_FINAL)
        await _until(lambda: debouncer.pending_count() == 1, "the new file to be reported")
        old.rename(new)
        await _until(lambda: debouncer.lineage(str(new)) == [str(old)], "the move to reach the debouncer")
        ready = await _sweep_and_post(debouncer, poster)

    assert ready == [str(new)]
    rows = await _rows(session)
    assert [(row.original_path, row.sha256_hash) for row in rows] == [(str(new), hashlib.sha256(_FINAL).hexdigest())]


async def test_a_rename_while_the_old_names_post_is_in_flight_still_yields_one_row(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Race: the rename is reported while the old name's POST has not completed yet.

    The rename happens from inside the poster's own POST, after the old name was hashed and
    before the controller wrote its row -- the window a real slow request leaves open. The
    observer's event lands while that post is awaiting, so the new name inherits the lineage.
    """
    agent, _token = seed_test_agent
    poster = await _live_poster(session, agent, authenticated_client)
    debouncer = Debouncer()
    old, new = tmp_path / "a.mp3", tmp_path / "b.mp3"
    real_upsert = poster._client.upsert_files

    async def _upsert_then_rename(chunk):  # type: ignore[no-untyped-def]
        old.rename(new)
        await _until(lambda: debouncer.lineage(str(new)) == [str(old)], "the rename to reach the debouncer mid-post")
        return await real_upsert(chunk)

    async with _watching(tmp_path, debouncer):
        old.write_bytes(_FINAL)
        await _until(lambda: debouncer.pending_count() == 1, "the new file to be reported")
        poster._client.upsert_files = _upsert_then_rename  # type: ignore[method-assign]
        assert await _sweep_and_post(debouncer, poster) == [str(old)]
        poster._client.upsert_files = real_upsert  # type: ignore[method-assign]
        assert await _sweep_and_post(debouncer, poster) == [str(new)]

    rows = await _rows(session)
    assert [row.original_path for row in rows] == [str(new)]
