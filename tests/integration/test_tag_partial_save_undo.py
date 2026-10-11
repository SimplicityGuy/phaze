"""Real partial ID3 save, authenticated callbacks, SQL audit and actual retry/Undo.

Only the post-v2 ID3v1 operation fails. Broker delivery uses the existing production
serializer stand-in. SQL reads bypass the ORM identity map; fixture commits are savepoints.
"""

from datetime import UTC, datetime
from unittest.mock import patch

from mutagen.id3 import ID3
import pytest
from sqlalchemy import select

from phaze.enums.tag_write import TagWriteStatus
from phaze.models.file import FileRecord
from phaze.models.tag_write_log import TagWriteLog
from phaze.services.hashing import compute_sha256
from phaze.services.tag_writer import enqueue_tag_write
from tests._queue_fakes import install_fake_queues
from tests._saq_jobs_seed import create_saq_jobs
from tests.integration.test_tag_undo_loop_seam import (
    _applied_file,
    _deliver_write_file_tags,
    _make_agent_client,
    _raw_tags,
    _seed_original_tags,
)
from tests.review.tasks.test_tag_write import _make_mp3


pytestmark = pytest.mark.integration


@pytest.mark.parametrize("retry", [False, True])
async def test_partial_save_preserves_original_anchor_through_retry_and_undo(session, seed_test_agent, tmp_path, client, retry):
    await create_saq_jobs(session)
    owner, token = seed_test_agent
    path = _make_mp3(tmp_path / "song.mp3")
    _seed_original_tags(path)
    original = _raw_tags(path)
    record = await _applied_file(session, owner.id, path)
    _, router = install_fake_queues(client)
    agent = _make_agent_client(session, token)
    first = await enqueue_tag_write(session, router, record, {"artist": "New Artist"}, source="metadata")
    try:
        with patch.object(ID3, "_ID3__save_v1", side_effect=OSError("synthetic failure after v2 write")):
            result = await _deliver_write_file_tags(router, agent, tmp_path)
        assert result["status"] == TagWriteStatus.SAVE_FAILED
        assert _raw_tags(path)["TPE1"] == ["New Artist"], "real ID3v2 bytes must land before failure"
        status, before = (await session.execute(select(TagWriteLog.status, TagWriteLog.before_tags).where(TagWriteLog.id == first.id))).one()
        assert status == TagWriteStatus.SAVE_FAILED
        assert before["artist"] == "Orig Artist"
        assert await session.scalar(select(FileRecord.sha256_hash).where(FileRecord.id == record.id)) == compute_sha256(path)
        # The fixture uses one outer transaction, so now() ties across savepoint commits.
        # Give this synthetic first attempt an explicit chronology before the retry.
        first.written_at = datetime(2000, 1, 1, tzinfo=UTC)
        await session.commit()
        if retry:
            second = await enqueue_tag_write(session, router, record, {"artist": "New Artist"}, source="metadata")
            assert (await _deliver_write_file_tags(router, agent, tmp_path))["status"] == TagWriteStatus.COMPLETED
            retry_before = await session.scalar(select(TagWriteLog.before_tags).where(TagWriteLog.id == second.id))
            assert retry_before["artist"] == "New Artist"
        response = await client.post(f"/tags/{record.id}/undo")
        assert response.status_code == 200, response.text
        assert (await _deliver_write_file_tags(router, agent, tmp_path))["status"] == TagWriteStatus.COMPLETED
        assert _raw_tags(path) == original
    finally:
        await agent._client.aclose()


@pytest.mark.parametrize("failure", ["snapshot", "prepare"])
async def test_pre_save_failure_does_not_offer_undo(session, seed_test_agent, tmp_path, client, failure):
    await create_saq_jobs(session)
    owner, token = seed_test_agent
    path = _make_mp3(tmp_path / "song.mp3")
    _seed_original_tags(path)
    original = path.read_bytes()
    record = await _applied_file(session, owner.id, path)
    _, router = install_fake_queues(client)
    agent = _make_agent_client(session, token)
    log = await enqueue_tag_write(session, router, record, {"artist": "New Artist"}, source="metadata")
    target = "phaze.tasks.tag_write._extract_before_tags" if failure == "snapshot" else "phaze.services.tag_write_disk.resolve_tag_format"
    try:
        with patch(target, side_effect=OSError("synthetic pre-save failure")):
            assert (await _deliver_write_file_tags(router, agent, tmp_path))["status"] == TagWriteStatus.FAILED
        assert path.read_bytes() == original
        assert await session.scalar(select(TagWriteLog.status).where(TagWriteLog.id == log.id)) == TagWriteStatus.FAILED
        response = await client.post(f"/tags/{record.id}/undo")
        assert response.status_code == 200
        assert not router.captures, "pre-save failures must not create an Undo job"
    finally:
        await agent._client.aclose()
