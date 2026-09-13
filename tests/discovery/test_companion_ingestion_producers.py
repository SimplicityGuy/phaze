"""Real-producer guard for companion ingestion.

Every row asserted here starts as bytes in ``tmp_path`` and reaches Postgres through
the same authenticated HTTP upsert used in production.  Deliberately do not construct
``FileRecord`` or ``FileCompanion`` artifacts in this module: doing so would recreate
the proxy-test gap that let the missing producer ship.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from sqlalchemy import select
from watchdog.events import FileCreatedEvent

from phaze.agent_watcher.debouncer import Debouncer
from phaze.agent_watcher.observer import WatcherEventHandler
from phaze.agent_watcher.poster import Poster
from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.pipeline.analysis import _enqueue_analysis_jobs
from phaze.routers.pipeline.extraction import _enqueue_extraction_jobs
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.pipeline import get_discovered_files_with_duration, get_metadata_pending_files
from phaze.tasks.scan import scan_directory


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_ACCEPTED = ("cue", "nfo", "txt", "m3u", "m3u8", "pls")
_EXCLUDED = ("jpg", "jpeg", "png", "gif", "md5", "sfv")


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _fixture_tree(root: Path) -> dict[str, list[Path]]:
    """Create the same mixed, orphan-companion, and media-only tree for both producers."""
    mixed = root / "mixed"
    orphaned = root / "orphaned"
    media_only = root / "media-only"
    mixed.mkdir()
    orphaned.mkdir()
    media_only.mkdir()

    accepted = [_write(mixed / f"accepted.{extension}", f"accepted-{extension}".encode()) for extension in _ACCEPTED]
    excluded = [_write(mixed / f"excluded.{extension}", f"excluded-{extension}".encode()) for extension in _EXCLUDED]
    orphaned_companions = [_write(orphaned / f"orphaned.{extension}", f"orphaned-{extension}".encode()) for extension in _ACCEPTED]
    media = [
        _write(mixed / "track.mp3", b"music-bytes"),
        _write(media_only / "set.mp4", b"video-bytes"),
    ]
    return {
        "accepted": accepted,
        "excluded": excluded,
        "orphaned": orphaned_companions,
        "media": media,
    }


def _agent_api(authenticated_client: AsyncClient) -> PhazeAgentClient:
    """Wrap the in-process ASGI transport in the production agent-client adapter."""
    return PhazeAgentClient(
        base_url=str(authenticated_client.base_url),
        token="unused-test-token",  # noqa: S106 -- the injected client already carries the generated test fixture token
        _client=authenticated_client,
    )


async def _persisted_rows(session: AsyncSession) -> list[FileRecord]:
    return list((await session.execute(select(FileRecord).order_by(FileRecord.original_filename))).scalars().all())


def _assert_media_rows_are_byte_exact(rows: list[FileRecord], media_paths: list[Path]) -> None:
    """Pin the pre-existing MUSIC/VIDEO record payload while companions are added."""
    by_name = {row.original_filename: row for row in rows}
    for path in media_paths:
        payload = path.read_bytes()
        row = by_name[path.name]
        assert row.original_path == str(path)
        assert row.current_path == str(path)
        assert row.original_filename == path.name
        assert row.file_type == path.suffix.lstrip(".")
        assert row.file_size == len(payload)
        assert row.sha256_hash == hashlib.sha256(payload).hexdigest()


async def test_scan_directory_persists_only_sibling_companions(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """The real scan writes six approved sibling types and skips orphans/exclusions."""
    tree = _fixture_tree(tmp_path)
    agent, _token = seed_test_agent
    batch = ScanBatch(agent_id=agent.id, scan_path=str(tmp_path), status=ScanStatus.RUNNING.value, total_files=0, processed_files=0)
    session.add(batch)
    await session.commit()

    result = await scan_directory(
        {"api_client": _agent_api(authenticated_client)},
        scan_path=str(tmp_path),
        batch_id=str(batch.id),
        agent_id=agent.id,
    )

    rows = await _persisted_rows(session)
    names = {row.original_filename for row in rows}
    assert result == {"status": "completed", "files_posted": 8}
    assert {row.file_type for row in rows if row.file_type in _ACCEPTED} == set(_ACCEPTED)
    assert {path.name for path in tree["accepted"]} <= names
    assert names.isdisjoint(path.name for path in tree["excluded"])
    assert names.isdisjoint(path.name for path in tree["orphaned"])
    assert {path.name for path in tree["media"]} <= names  # Positive control: this producer persisted media.
    _assert_media_rows_are_byte_exact(rows, tree["media"])


async def test_watcher_persists_approved_companions_including_orphans_and_never_enqueues_them(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """The real watcher chain admits orphans, rejects exclusions, and queues only media."""
    tree = _fixture_tree(tmp_path)
    agent, _token = seed_test_agent
    session.add(
        ScanBatch(
            agent_id=agent.id,
            scan_path="<watcher>",
            status=ScanStatus.LIVE.value,
            total_files=0,
            processed_files=0,
        )
    )
    await session.commit()

    agent_api = _agent_api(authenticated_client)
    poster = Poster(client=agent_api, agent_id=agent.id)
    debouncer = Debouncer()
    handler = WatcherEventHandler(loop=asyncio.get_running_loop(), debouncer_touch=debouncer.touch)
    all_paths = [path for paths in tree.values() for path in paths]
    for path in all_paths:
        handler.on_created(FileCreatedEvent(src_path=str(path)))

    # ``call_soon_threadsafe`` is the real watchdog-thread bridge. Yield once so every
    # scheduled touch reaches the asyncio-owned debouncer before its production sweep.
    await asyncio.sleep(0)
    ready, evicted = debouncer.sweep(settle_period=0.0, max_pending=3600.0)
    assert evicted == []
    for path in ready:
        await poster.post_one(path)

    rows = await _persisted_rows(session)
    names = {row.original_filename for row in rows}
    assert {row.file_type for row in rows if row.file_type in _ACCEPTED} == set(_ACCEPTED)
    assert {path.name for path in tree["accepted"]} <= names
    assert {path.name for path in tree["orphaned"]} <= names
    assert names.isdisjoint(path.name for path in tree["excluded"])
    assert {path.name for path in tree["media"]} <= names  # Positive control: the watcher reached Postgres.
    _assert_media_rows_are_byte_exact(rows, tree["media"])

    metadata_pending = await get_metadata_pending_files(session)
    analysis_pending = [row for row, _duration in await get_discovered_files_with_duration(session)]
    metadata_queue = AsyncMock()
    analysis_queue = AsyncMock()
    analysis_queue.enqueue.return_value = object()
    await _enqueue_extraction_jobs(metadata_queue, metadata_pending, agent.id)
    analysis_failures = await _enqueue_analysis_jobs(analysis_queue, analysis_pending, agent.id, "/models")

    assert analysis_failures == []
    assert {call.args[0] for call in metadata_queue.enqueue.await_args_list} == {"extract_file_metadata"}
    assert {call.args[0] for call in analysis_queue.enqueue.await_args_list} == {"process_file"}
    metadata_types = {call.kwargs["file_type"] for call in metadata_queue.enqueue.await_args_list}
    analysis_types = {call.kwargs["file_type"] for call in analysis_queue.enqueue.await_args_list}
    assert metadata_types == {"mp3", "mp4"}
    assert analysis_types == {"mp3", "mp4"}
    assert metadata_types.isdisjoint(_ACCEPTED)
    assert analysis_types.isdisjoint(_ACCEPTED)
