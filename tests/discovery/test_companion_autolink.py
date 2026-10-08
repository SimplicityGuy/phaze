"""Automatic companion association on the real producers and the real association path (phaze-spd83).

Every row here starts as bytes in ``tmp_path`` and reaches Postgres through the production scan task
or watcher poster, over the authenticated HTTP routes. The routes' requests land on the fake
controller queue the test client wires; each captured request is then run through the real
``associate_agent_companions`` task, which runs the real linking chain and the real junk-review
detector. Nothing here seeds a ``FileCompanion`` or a features row by hand.

Coalescing against a real SAQ broker is pinned in
``tests/integration/test_companion_association_coalescing.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import aliased
from watchdog.events import FileCreatedEvent

from phaze.agent_watcher.debouncer import Debouncer
from phaze.agent_watcher.observer import WatcherEventHandler
from phaze.agent_watcher.poster import Poster
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.companion_autolink import ASSOCIATION_TASK, ASSOCIATION_WINDOW_SECONDS, _lock_key, association_window
from phaze.tasks.companion_association import associate_agent_companions
from phaze.tasks.scan import scan_directory
from tests._queue_fakes import FakeQueue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from phaze.models.agent import Agent


def _agent_api(client: AsyncClient) -> PhazeAgentClient:
    return PhazeAgentClient(base_url=str(client.base_url), token="unused-test-token", _client=client)  # noqa: S106 -- the client carries the fixture token


def _controller(client: AsyncClient) -> FakeQueue:
    """The fake controller queue the ``authenticated_client`` fixture wired onto the app."""
    return client._transport.app.state.controller_queue  # type: ignore[attr-defined,no-any-return]


def _requests(queue: FakeQueue, agent_id: str) -> list[dict[str, Any]]:
    return [kwargs for task, kwargs in queue.captured if task == ASSOCIATION_TASK and kwargs["agent_id"] == agent_id]


def _ctx(session: AsyncSession) -> dict[str, Any]:
    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return {"async_session": _factory, "queue": FakeQueue("controller")}


async def _run(session: AsyncSession, request: dict[str, Any]) -> dict[str, Any]:
    """Run one captured request through the real task, as the controller worker would."""
    return await associate_agent_companions(_ctx(session), **request)


async def _links(session: AsyncSession) -> set[tuple[str, str]]:
    companion = aliased(FileRecord)
    media = aliased(FileRecord)
    result = await session.execute(
        select(companion.original_filename, media.original_filename)
        .join(FileCompanion, FileCompanion.companion_id == companion.id)
        .join(media, FileCompanion.media_id == media.id)
    )
    return {(row[0], row[1]) for row in result.all()}


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


async def _watch(paths: list[Path], poster: Poster) -> None:
    """Drive the real watcher chain (event handler, debouncer, poster) for ``paths``."""
    debouncer = Debouncer()
    handler = WatcherEventHandler(loop=asyncio.get_running_loop(), debouncer_touch=debouncer.touch)
    for path in paths:
        handler.on_created(FileCreatedEvent(src_path=str(path)))
    await asyncio.sleep(0)
    ready, evicted = debouncer.sweep(settle_period=0.0, max_pending=3600.0)
    assert evicted == []
    for path in ready:
        await poster.post_one(path)


async def _live_batch(session: AsyncSession, agent: Agent) -> None:
    session.add(ScanBatch(agent_id=agent.id, scan_path="<watcher>", status=ScanStatus.LIVE.value, total_files=0, processed_files=0))
    await session.commit()


async def test_a_completed_scan_requests_association_and_the_run_links_what_it_brought_in(
    tmp_path: Path, authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """The scan-complete trigger: nothing links until the requested run runs, and that run links by the chain."""
    agent, _token = seed_test_agent
    _write(tmp_path / "rel" / "Live Set 2019.mp3", b"release-music")
    _write(tmp_path / "rel" / "info" / "live set 2019.cue", b'TITLE "Live Set"\r\nFILE "Live Set 2019.mp3" MP3\r\n  TRACK 01 AUDIO\r\n')
    _write(tmp_path / "rel" / "Downloaded from a site.txt", b"Downloaded from www.example-release-site.test\r\nVisit us for more free sets!\r\n")
    batch = ScanBatch(agent_id=agent.id, scan_path=str(tmp_path), status=ScanStatus.RUNNING.value, total_files=0, processed_files=0)
    session.add(batch)
    await session.commit()

    result = await scan_directory(
        {"api_client": _agent_api(authenticated_client)}, scan_path=str(tmp_path), batch_id=str(batch.id), agent_id=agent.id
    )

    assert result == {"status": "completed", "files_posted": 3}
    requests = _requests(_controller(authenticated_client), agent.id)
    # One chunk: its media INSERT, its stored features and the terminal PATCH each requested a run. On the
    # real broker the three share one key in one window (tests/integration/test_companion_association_coalescing.py).
    assert len(requests) == 3
    assert len({request["window"] for request in requests}) <= 2  # a window edge may split them, never more
    assert await _links(session) == set()  # never on the request path: nothing is linked until the run runs

    outcome = await _run(session, requests[-1])

    assert outcome["status"] == "associated"
    assert outcome["links_created"] == 1
    assert await _links(session) == {("live set 2019.cue", "Live Set 2019.mp3")}
    # The same run refreshed the junk-review queue: the site advert beside the media is pending review.
    pending = (await session.execute(select(CompanionJunkReview.original_path, CompanionJunkReview.reason))).all()
    assert [(path.rsplit("/", 1)[-1], reason) for path, reason in pending] == [("Downloaded from a site.txt", "site_ad")]


async def test_a_scan_that_fails_still_requests_the_run(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """A failed scan may have ingested files, so its terminal PATCH requests a run too; a retried echo of it requests nothing more."""
    agent, _token = seed_test_agent
    batch = ScanBatch(agent_id=agent.id, scan_path="/test/music", status=ScanStatus.RUNNING.value, total_files=0, processed_files=0)
    session.add(batch)
    await session.commit()

    first = await authenticated_client.patch(f"/api/internal/agent/scan-batches/{batch.id}", json={"status": "failed", "error_message": "x"})
    echo = await authenticated_client.patch(f"/api/internal/agent/scan-batches/{batch.id}", json={"status": "failed", "error_message": "x"})
    progress_only = ScanBatch(agent_id=agent.id, scan_path="/test/music/b", status=ScanStatus.RUNNING.value, total_files=0, processed_files=0)
    session.add(progress_only)
    await session.commit()
    progress = await authenticated_client.patch(f"/api/internal/agent/scan-batches/{progress_only.id}", json={"processed_files": 5})

    assert (first.status_code, echo.status_code, progress.status_code) == (200, 200, 200)
    assert len(_requests(_controller(authenticated_client), agent.id)) == 1


async def test_a_run_while_the_agent_scans_is_deferred_to_a_later_window(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """Deferral: a RUNNING scan means re-request for a later window and decide nothing now."""
    agent, _token = seed_test_agent
    session.add(ScanBatch(agent_id=agent.id, scan_path="/test/music", status=ScanStatus.RUNNING.value, total_files=0, processed_files=0))
    await session.commit()
    ctx = _ctx(session)
    stale_window = association_window(0)

    outcome = await associate_agent_companions(ctx, agent_id=agent.id, window=stale_window)

    assert outcome == {"agent_id": agent.id, "window": stale_window, "status": "deferred", "reason": "scan_running", "requeued": True}
    [(task, request)] = ctx["queue"].captured
    assert task == ASSOCIATION_TASK
    assert request["agent_id"] == agent.id
    assert request["window"] > stale_window
    assert ctx["queue"].captured_policy[0]["scheduled"] == (request["window"] + 1) * ASSOCIATION_WINDOW_SECONDS


async def test_a_run_whose_agent_lock_is_held_is_deferred(
    seed_test_agent: tuple[Agent, str], session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """Runs for one agent never overlap: a second run defers instead of racing the first."""
    agent, _token = seed_test_agent
    async with async_engine.connect() as other:
        assert (await other.execute(select(func.pg_try_advisory_lock(_lock_key(agent.id))))).scalar()
        try:
            outcome = await associate_agent_companions(_ctx(session), agent_id=agent.id, window=1)
        finally:
            await other.execute(select(func.pg_advisory_unlock(_lock_key(agent.id))))
            await other.commit()

    assert (outcome["status"], outcome["reason"]) == ("deferred", "agent_locked")
    assert (await associate_agent_companions(_ctx(session), agent_id=agent.id, window=1))["status"] == "associated"  # free again


async def test_media_arriving_after_its_companion_is_linked_by_the_run_it_requests(
    tmp_path: Path, authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """The watcher ingests a CUE whose recording is not there yet; the recording arrives later, elsewhere, and the run it requests links them."""
    agent, _token = seed_test_agent
    await _live_batch(session, agent)
    poster = Poster(client=_agent_api(authenticated_client), agent_id=agent.id)
    controller = _controller(authenticated_client)
    cue = _write(tmp_path / "sheets" / "night one.cue", b'TITLE "Night One"\r\nFILE "night one.flac" WAVE\r\n  TRACK 01 AUDIO\r\n')

    await _watch([cue], poster)
    [features_request] = _requests(controller, agent.id)  # the stored features requested a run
    first = await _run(session, features_request)
    assert (first["status"], first["links_created"], first["decided"]) == ("associated", 0, {"unlinked": 1})

    media = _write(tmp_path / "audio" / "night one.flac", b"recording-bytes")
    await _watch([media], poster)
    requests = _requests(controller, agent.id)
    assert len(requests) == 2  # the media INSERT requested the run that can now decide the waiting CUE
    second = await _run(session, requests[-1])

    assert (second["links_created"], second["decided"]) == (1, {"reference": 1})
    assert await _links(session) == {("night one.cue", "night one.flac")}


async def test_a_second_run_writes_nothing(
    tmp_path: Path, authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """Idempotency of the automatic run: the same links are re-derived and kept, none deleted or inserted."""
    agent, _token = seed_test_agent
    await _live_batch(session, agent)
    poster = Poster(client=_agent_api(authenticated_client), agent_id=agent.id)
    paths = [
        _write(tmp_path / "rel" / "set.mp3", b"set-bytes"),
        _write(tmp_path / "rel" / "set.cue", b'FILE "set.mp3" MP3\r\n  TRACK 01 AUDIO\r\n'),
        _write(tmp_path / "rel" / "release.nfo", b"Artist ......: Example Artist\r\nSource ......: FM\r\n"),
    ]
    await _watch(paths, poster)
    request = _requests(_controller(authenticated_client), agent.id)[-1]

    first = await _run(session, request)
    links = await _links(session)
    second = await _run(session, request)

    assert first["links_created"] == 2
    assert (second["links_created"], second["links_removed"], second["links_kept"]) == (0, 0, 2)
    assert second["junk_review_created"] == second["junk_review_withdrawn"] == 0
    assert await _links(session) == links == {("set.cue", "set.mp3"), ("release.nfo", "set.mp3")}


async def test_a_re_upsert_of_known_media_requests_nothing_but_a_move_does(
    tmp_path: Path, authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """Association reads paths only: a known path re-posted changes nothing, a new path (a move) can."""
    agent, _token = seed_test_agent
    await _live_batch(session, agent)
    poster = Poster(client=_agent_api(authenticated_client), agent_id=agent.id)
    controller = _controller(authenticated_client)
    media = _write(tmp_path / "rel" / "set.mp3", b"set-bytes")

    await poster.post_one(str(media))
    assert len(_requests(controller, agent.id)) == 1  # a new media path
    await poster.post_one(str(media))
    assert len(_requests(controller, agent.id)) == 1  # the same path again: nothing to re-decide

    moved = tmp_path / "rel2" / "set.mp3"
    moved.parent.mkdir()
    media.rename(moved)
    await poster.post_one(str(moved), previous_paths=[str(media)])

    assert len(_requests(controller, agent.id)) == 2
    assert (await session.execute(select(func.count()).select_from(FileRecord))).scalar_one() == 1  # re-pointed, not duplicated


async def test_a_lost_request_never_fails_the_ingest(
    tmp_path: Path, authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """The request is best-effort: a broker failure is logged and the upsert still lands."""
    agent, _token = seed_test_agent
    await _live_batch(session, agent)

    class _DownQueue(FakeQueue):
        async def enqueue(self, *_args: Any, **_kwargs: Any) -> None:  # type: ignore[override]
            raise RuntimeError("broker down")

    authenticated_client._transport.app.state.controller_queue = _DownQueue("controller")  # type: ignore[attr-defined]
    media = _write(tmp_path / "rel" / "set.mp3", b"set-bytes")

    await Poster(client=_agent_api(authenticated_client), agent_id=agent.id).post_one(str(media))

    assert (await session.execute(select(FileRecord.original_filename))).scalars().all() == ["set.mp3"]
