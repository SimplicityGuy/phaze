"""FAIL-03 (81-06): POST /pipeline/metadata-failed/retry bulk operator retry path.

The 81-03 writer persists a terminal metadata failure as a ``metadata`` row with ``failed_at``
set and the payload columns NULL, so ``done(metadata)`` derives FAILED -- a permanent dead-end
that (pre-FAIL-03) blocked the file from ever reaching ``propose`` (gap G-01 / SC#3). This suite
pins the operator retry that dissolves that dead-end:

- it re-enqueues EVERY ``metadata.failed_at IS NOT NULL`` file with the COMPLETE
  ExtractMetadataPayload on the per-agent ``meta`` lane (never the consumer-less default queue);
- D-11: it LEAVES the failure row in place -- clearing ``failed_at`` here would make a
  zero-metadata file read DONE forever; only ``put_metadata``'s clear-on-success wipes it when
  real metadata lands;
- Phase-30 guard: with no active agent it enqueues nothing, mutates nothing, and never falls
  through to the default queue.

Uses the plain operator ``client`` fixture (tests/conftest.py) + the fake named-queue capture
harness (tests/_queue_fakes.py), mirroring the retry_analysis_failed tests. The whole
``tests/integration/`` package is auto-marked ``integration`` by ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.schemas.agent_tasks import ExtractMetadataPayload
from phaze.services.pipeline import get_metadata_failed_files
from tests._queue_fakes import install_fake_queues, seed_active_agent, wire_fakes


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


async def _drain_background() -> None:
    """Yield until the router's background enqueue tasks have drained (phaze-zecg).

    ``retry_metadata_failed`` now backgrounds its enqueue loop via ``asyncio.create_task`` +
    ``_background_tasks`` (matching every other caller of ``_enqueue_extraction_jobs``), so the
    HTTP response returns before the loop necessarily finishes.
    """
    import phaze.routers.pipeline as pipeline_mod

    for _ in range(500):
        if not pipeline_mod._background_tasks:
            return
        await asyncio.sleep(0)


def _make_file() -> FileRecord:
    """Create a FileRecord with a unique id/path (mirrors the shared pipeline test helper)."""
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


def _make_failed_metadata(file_id: uuid.UUID) -> FileMetadata:
    """A terminal metadata failure row exactly as the 81-03 writer persists it.

    ``failed_at`` set, payload columns NULL -- so ``done(metadata)`` derives FAILED and the file
    is a member of the get_metadata_failed_files retry set.
    """
    return FileMetadata(file_id=file_id, failed_at=datetime.now(UTC), error_message="error: boom")


async def _seed_failed(session: AsyncSession, n: int) -> set[str]:
    """Seed ``n`` files each carrying a terminal metadata failure row. Return their id strings."""
    files = [_make_file() for _ in range(n)]
    session.add_all(files)
    await session.commit()
    session.add_all([_make_failed_metadata(f.id) for f in files])
    await session.commit()
    return {str(f.id) for f in files}


@pytest.mark.asyncio
async def test_retry_reenqueues_all_failed_metadata(client: AsyncClient, session: AsyncSession) -> None:
    """POST retry enqueues extract_file_metadata for every failed-metadata file on the per-agent meta lane.

    All N files land on ``phaze-agent-nox-meta`` (never the default queue) carrying the COMPLETE
    ExtractMetadataPayload; the ack reports N and is metadata-worded (not "for analysis").
    """
    failed_ids = await _seed_failed(session, 3)
    await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/pipeline/metadata-failed/retry")
    assert response.status_code == 200
    assert "re-queued 3 failed file(s) for metadata extraction" in response.text.lower()
    assert "for analysis" not in response.text.lower()

    await _drain_background()  # phaze-zecg: the enqueue loop now runs as a background task
    queue = task_router.queues["nox-meta"]
    assert queue.name == "phaze-agent-nox-meta"
    assert queue.name != "default"
    assert len(queue.captured) == 3
    captured_ids = set()
    for task_name, payload in queue.captured:
        assert task_name == "extract_file_metadata"
        # Complete payload (v4.0.8 guard): the four required fields validate under extra='forbid'.
        assert set(payload) == {"file_id", "original_path", "file_type", "agent_id"}
        ExtractMetadataPayload.model_validate(payload)
        captured_ids.add(payload["file_id"])
    assert captured_ids == failed_ids


@pytest.mark.asyncio
async def test_retry_leaves_failure_rows_in_place(client: AsyncClient, session: AsyncSession) -> None:
    """D-11: after a retry that has not yet succeeded, every failure row still exists (failed_at NOT NULL).

    The retry re-enqueues WITHOUT clearing ``failed_at`` -- clearing it in place would make a
    zero-metadata file read DONE forever. Only ``put_metadata``'s clear-on-success wipes the marker.
    """
    failed_ids = await _seed_failed(session, 2)
    await seed_active_agent(session)
    install_fake_queues(client)

    response = await client.post("/pipeline/metadata-failed/retry")
    assert response.status_code == 200

    rows = (await session.execute(select(FileMetadata).where(FileMetadata.failed_at.isnot(None)))).scalars().all()
    assert {str(r.file_id) for r in rows} == failed_ids
    # The rows survive with the marker intact (payload columns still NULL, not "landed").
    for r in rows:
        assert r.failed_at is not None
        assert r.artist is None


@pytest.mark.asyncio
async def test_retry_no_active_agent_enqueues_nothing_and_mutates_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """Phase-30 guard: no active agent -> zero enqueues, no default-queue fallthrough, rows untouched."""
    failed_ids = await _seed_failed(session, 2)
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/pipeline/metadata-failed/retry")
    assert response.status_code == 200
    assert "no active agent" in response.text.lower()

    # Nothing enqueued anywhere -- never the default queue.
    assert capture == []
    # No mutation: the failure set is unchanged (metadata carries no scalar state to flip).
    still_failed = await get_metadata_failed_files(session)
    assert {str(f.id) for f in still_failed} == failed_ids


@pytest.mark.asyncio
async def test_retry_zero_failed_is_noop(client: AsyncClient, session: AsyncSession) -> None:
    """No failed-metadata files -> 200, zero enqueues, "no failed files to retry" ack."""
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/metadata-failed/retry")
    assert response.status_code == 200
    assert "no failed files to retry" in response.text.lower()
    assert capture == []


@pytest.mark.asyncio
async def test_get_metadata_failed_files_returns_exactly_failed(client: AsyncClient, session: AsyncSession) -> None:
    """get_metadata_failed_files returns EXACTLY the files with a metadata failure row.

    A done-metadata file (failed_at NULL, payload present) and a file with no metadata row at all
    are both excluded -- only ``failed_at IS NOT NULL`` qualifies.
    """
    failed_ids = await _seed_failed(session, 2)

    # A DONE-metadata file: failed_at NULL, real payload -> excluded.
    done_file = _make_file()
    session.add(done_file)
    await session.commit()
    session.add(FileMetadata(file_id=done_file.id, artist="Real", title="Track"))
    # A file with NO metadata row at all -> excluded.
    bare_file = _make_file()
    session.add(bare_file)
    await session.commit()

    result = await get_metadata_failed_files(session)
    assert {str(f.id) for f in result} == failed_ids
