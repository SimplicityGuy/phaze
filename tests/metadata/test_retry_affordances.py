"""87-07 (UI-02 / D-04): per-file + bulk metadata retry affordances.

The console surfaces a per-row "Retry" on a failed metadata cell (``POST
/pipeline/files/{file_id}/metadata-failed/retry``) and a bulk "Retry all failed · Metadata" on the
failed-filter view (the pre-existing ``POST /pipeline/metadata-failed/retry``). Both re-drive files
through the SAME Phase-30-hardened guarded funnel (per-agent routing -> ``NoActiveAgentError`` guard
-> ``_enqueue_extraction_jobs`` with the COMPLETE ``ExtractMetadataPayload`` on the per-agent meta
lane, never the consumer-less default). This suite pins the per-file variant:

- it re-enqueues EXACTLY one file with the complete payload on ``phaze-agent-nox-meta``;
- D-11: it LEAVES the failure row in place (no ``failed_at`` clear — a zero-metadata file with the
  marker cleared would read DONE forever); metadata has no terminal FileState to flip;
- it is scoped to ONE file (a non-failed / unknown id is a safe no-op ack);
- the Phase-30 no-agent guard survives (amber ack, no enqueue, no default-queue fallthrough).

Uses the operator ``client`` fixture + the fake named-queue harness, mirroring the bulk retry tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.schemas.agent_tasks import ExtractMetadataPayload
from phaze.services.pipeline import get_metadata_failed_files
from tests._queue_fakes import install_fake_queues, seed_active_agent, wire_fakes


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _make_file(*, state: str = FileState.DISCOVERED) -> FileRecord:
    """Create a FileRecord with a unique id/path (mirrors the shared pipeline test helper)."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )


def _make_failed_metadata(file_id: uuid.UUID) -> FileMetadata:
    """A terminal metadata failure row exactly as the 81-03 writer persists it (failed_at set, payload NULL)."""
    return FileMetadata(file_id=file_id, failed_at=datetime.now(UTC), error_message="error: boom")


async def _seed_failed_file(session: AsyncSession) -> FileRecord:
    """Seed ONE file carrying a terminal metadata failure row; return it."""
    file = _make_file()
    session.add(file)
    await session.commit()
    session.add(_make_failed_metadata(file.id))
    await session.commit()
    return file


@pytest.mark.asyncio
async def test_per_file_retry_reenqueues_one_file_on_meta_lane(client: AsyncClient, session: AsyncSession) -> None:
    """The per-file retry routes ONE extract_file_metadata on the per-agent meta lane (never default).

    The COMPLETE ExtractMetadataPayload lands (v4.0.8 guard); the ack reports 1 and is metadata-worded.
    A second failed file MUST be untouched (proves scoping to one file_id).
    """
    file = await _seed_failed_file(session)
    other = await _seed_failed_file(session)
    await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post(f"/pipeline/files/{file.id}/metadata-failed/retry")
    assert response.status_code == 200
    assert "re-queued 1 failed file(s) for metadata extraction" in response.text.lower()
    assert "for analysis" not in response.text.lower()

    queue = task_router.queues["nox-meta"]
    assert queue.name == "phaze-agent-nox-meta"
    assert queue.name != "default"
    assert len(queue.captured) == 1
    task_name, payload = queue.captured[0]
    assert task_name == "extract_file_metadata"
    assert set(payload) == {"file_id", "original_path", "file_type", "agent_id"}
    ExtractMetadataPayload.model_validate(payload)
    assert payload["file_id"] == str(file.id)

    # Only the target file was enqueued; the other failed file is untouched.
    assert other.id != file.id


@pytest.mark.asyncio
async def test_per_file_retry_leaves_failure_row_in_place(client: AsyncClient, session: AsyncSession) -> None:
    """D-11: the per-file retry re-enqueues WITHOUT clearing ``failed_at`` (else the file reads DONE forever)."""
    file = await _seed_failed_file(session)
    fid = file.id  # capture before expiry (an expired ORM attr would lazy-reload outside greenlet)
    await seed_active_agent(session)
    install_fake_queues(client)

    response = await client.post(f"/pipeline/files/{fid}/metadata-failed/retry")
    assert response.status_code == 200

    session.expire_all()
    row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == fid))).scalar_one()
    assert row.failed_at is not None  # marker intact (payload not yet landed)
    assert row.artist is None
    # The file is still a member of the failed retry set until put_metadata's clear-on-success lands.
    still_failed = await get_metadata_failed_files(session)
    assert fid in {f.id for f in still_failed}


@pytest.mark.asyncio
async def test_per_file_retry_no_active_agent_enqueues_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """Phase-30 guard / T-87-25: no agent -> amber ack, zero enqueues, no default-queue fallthrough, row intact."""
    file = await _seed_failed_file(session)
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post(f"/pipeline/files/{file.id}/metadata-failed/retry")
    assert response.status_code == 200
    assert "no active agent" in response.text.lower()
    assert capture == []

    still_failed = await get_metadata_failed_files(session)
    assert file.id in {f.id for f in still_failed}


@pytest.mark.asyncio
async def test_per_file_retry_non_failed_file_is_noop(client: AsyncClient, session: AsyncSession) -> None:
    """T-87-27: a file with no metadata failure row (or an unknown id) is a safe no-op — no enqueue."""
    # A done-metadata file: failed_at NULL, real payload -> not in the failed set.
    done_file = _make_file()
    session.add(done_file)
    await session.commit()
    session.add(FileMetadata(file_id=done_file.id, artist="Real", title="Track"))
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    r1 = await client.post(f"/pipeline/files/{done_file.id}/metadata-failed/retry")
    assert r1.status_code == 200
    assert "no failed files to retry" in r1.text.lower()

    r2 = await client.post(f"/pipeline/files/{uuid.uuid4()}/metadata-failed/retry")
    assert r2.status_code == 200
    assert "no failed files to retry" in r2.text.lower()

    assert capture == []


@pytest.mark.asyncio
async def test_bulk_retry_reenqueues_all_failed_metadata(client: AsyncClient, session: AsyncSession) -> None:
    """Regression backstop: the bulk endpoint re-drives EVERY failed-metadata file on the meta lane."""
    files = [await _seed_failed_file(session) for _ in range(3)]
    await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/pipeline/metadata-failed/retry")
    assert response.status_code == 200
    assert "re-queued 3 failed file(s) for metadata extraction" in response.text.lower()

    queue = task_router.queues["nox-meta"]
    assert queue.name != "default"
    assert {p["file_id"] for _t, p in queue.captured} == {str(f.id) for f in files}
