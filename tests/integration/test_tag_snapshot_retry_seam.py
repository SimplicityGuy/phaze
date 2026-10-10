"""Durable undo must precede mutation, including callback loss and fresh-process retries.

Real synthetic MP3, mutagen, agent client, authenticated callback routes, Postgres, undo
route and queue serializer. Broker delivery is simulated by calling the decoded job body.
Transport exhaustion/crashes are injected; no production files or broker are involved.
"""

from unittest.mock import patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mutagen.mp3 import MP3
from pydantic import BaseModel, ConfigDict
import pytest

from phaze.enums.tag_write import TagWriteStatus
from phaze.services.agent_client import AgentApiClientError, AgentApiServerError, PhazeAgentClient
from phaze.services.hashing import compute_sha256
from phaze.services.tag_writer import enqueue_tag_write
from phaze.tasks.tag_write import write_file_tags
from tests._queue_fakes import install_fake_queues
from tests._saq_jobs_seed import create_saq_jobs
from tests.integration.test_tag_undo_loop_seam import _applied_file, _deliver_write_file_tags, _make_agent_client
from tests.review.tasks.test_tag_write import _make_mp3, _patch_scan_roots


pytestmark = pytest.mark.integration


class SimulatedProcessDeath(BaseException):
    """Bypass ordinary Exception handlers, as an interrupted worker does."""


class LegacySnapshotPayload(BaseModel):
    """The old controller's strict request shape."""

    model_config = ConfigDict(extra="forbid")
    before_tags: dict[str, str | int | list[str] | None]


@pytest.mark.asyncio
async def test_updated_agent_against_legacy_controller_fails_before_mutation(tmp_path):
    path = _make_mp3(tmp_path / "<track-01>.mp3")
    original = path.read_bytes()
    app = FastAPI()

    @app.patch("/api/internal/agent/tag-writes/{log_id}/before-snapshot")
    async def legacy_snapshot(log_id: uuid.UUID, body: LegacySnapshotPayload):
        pytest.fail("old controller must reject include_status before invoking the route")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        agent = PhazeAgentClient(base_url="http://test", token=uuid.uuid4().hex, _client=http)
        with _patch_scan_roots([str(tmp_path)]), pytest.raises(AgentApiClientError):
            await write_file_tags(
                {"api_client": agent},
                log_id=str(uuid.uuid4()),
                file_id=str(uuid.uuid4()),
                agent_id="test-agent",
                file_path=str(path),
                tags={"artist": "New Artist"},
            )
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_committed", [False, True])
async def test_lost_snapshot_and_result_preserve_original_through_real_undo(session, seed_test_agent, tmp_path, client, snapshot_committed):
    """The snapshot and result each lose a response; a new client/context retries without an anchor reset."""
    await create_saq_jobs(session)
    owner, token = seed_test_agent
    path = _make_mp3(tmp_path / "<track-01>.mp3")
    record = await _applied_file(session, owner.id, path)
    _queue, router = install_fake_queues(client)
    log = await enqueue_tag_write(session, router, record, {"artist": "New Artist"}, source="metadata")
    [kwargs] = [body for _queue, task, body in router.captures if task == "write_file_tags"]
    router.captures.clear()
    agent = _make_agent_client(session, token)
    original_bytes = path.read_bytes()
    real_snapshot = agent.report_tag_write_before_snapshot

    async def lose_snapshot(log_id, payload):
        if snapshot_committed:
            await real_snapshot(log_id, payload)
        raise AgentApiServerError("synthetic exhausted snapshot callback")

    with (
        _patch_scan_roots([str(tmp_path)]),
        patch.object(agent, "report_tag_write_before_snapshot", side_effect=lose_snapshot),
        pytest.raises(AgentApiServerError, match="snapshot"),
    ):
        await write_file_tags({"api_client": agent}, **kwargs)
    assert path.read_bytes() == original_bytes

    # Fresh client and no retained job-local snapshot: durability belongs to the controller.
    agent = _make_agent_client(session, token)
    with (
        _patch_scan_roots([str(tmp_path)]),
        patch.object(agent, "patch_tag_write", side_effect=AgentApiServerError("synthetic exhausted result callback")),
        pytest.raises(AgentApiServerError, match="result"),
    ):
        await write_file_tags({"api_client": agent}, **kwargs)
    assert MP3(str(path)).tags["TPE1"].text == ["New Artist"]
    await session.refresh(log)
    assert log.status == TagWriteStatus.QUEUED
    assert log.before_tags["artist"] is None
    assert set(log.before_tags) == {"artist", "title", "album", "year", "genre", "track_number"}

    agent = _make_agent_client(session, token)
    with _patch_scan_roots([str(tmp_path)]):
        await write_file_tags({"api_client": agent}, **kwargs)
    await session.refresh(log)
    assert log.status == TagWriteStatus.COMPLETED
    assert log.before_tags["artist"] is None

    undo = await client.post(f"/tags/{record.id}/undo")
    assert undo.status_code == 200, undo.text
    reverted = await _deliver_write_file_tags(router, agent, tmp_path)
    assert reverted["status"] == TagWriteStatus.COMPLETED
    assert "TPE1" not in MP3(str(path)).tags

    # Late forward-job replay after undo must not overwrite the restored disk state.
    restored = path.read_bytes()
    with _patch_scan_roots([str(tmp_path)]):
        replay = await write_file_tags({"api_client": agent}, **kwargs)
    assert replay["status"] == TagWriteStatus.COMPLETED
    assert path.read_bytes() == restored


@pytest.mark.asyncio
@pytest.mark.parametrize("after_write", [False, True])
async def test_process_interruption_keeps_durable_anchor(session, seed_test_agent, tmp_path, client, after_write):
    """Death before save or after save resumes from the committed original, never the new disk tags."""
    await create_saq_jobs(session)
    owner, token = seed_test_agent
    path = _make_mp3(tmp_path / "<track-01>.mp3")
    record = await _applied_file(session, owner.id, path)
    _queue, router = install_fake_queues(client)
    log = await enqueue_tag_write(session, router, record, {"artist": "New Artist"}, source="metadata")
    [kwargs] = [body for _queue, task, body in router.captures if task == "write_file_tags"]
    agent = _make_agent_client(session, token)
    interrupt_target = agent if after_write else "phaze.tasks.tag_write._write_verify_and_rehash"
    interruption = (
        patch.object(interrupt_target, "patch_tag_write", side_effect=SimulatedProcessDeath)
        if after_write
        else patch(interrupt_target, side_effect=SimulatedProcessDeath)
    )
    with _patch_scan_roots([str(tmp_path)]), interruption, pytest.raises(SimulatedProcessDeath):
        await write_file_tags({"api_client": agent}, **kwargs)
    await session.refresh(log)
    assert log.before_tags["artist"] is None
    agent = _make_agent_client(session, token)
    with _patch_scan_roots([str(tmp_path)]):
        await write_file_tags({"api_client": agent}, **kwargs)
    await session.refresh(log)
    assert log.status == TagWriteStatus.COMPLETED
    assert log.before_tags["artist"] is None


@pytest.mark.asyncio
async def test_retry_snapshot_read_failure_refreshes_already_written_bytes(session, seed_test_agent, tmp_path, client):
    """Lost result then an unreadable tag snapshot must retain undo and observe the landed digest."""
    await create_saq_jobs(session)
    owner, token = seed_test_agent
    path = _make_mp3(tmp_path / "<track-01>.mp3")
    record = await _applied_file(session, owner.id, path)
    _queue, router = install_fake_queues(client)
    log = await enqueue_tag_write(session, router, record, {"artist": "New Artist"}, source="metadata")
    [kwargs] = [body for _queue, task, body in router.captures if task == "write_file_tags"]
    agent = _make_agent_client(session, token)
    with (
        _patch_scan_roots([str(tmp_path)]),
        patch.object(agent, "patch_tag_write", side_effect=AgentApiServerError("lost result")),
        pytest.raises(AgentApiServerError),
    ):
        await write_file_tags({"api_client": agent}, **kwargs)
    landed_bytes = path.read_bytes()
    assert compute_sha256(path) != record.sha256_hash
    with _patch_scan_roots([str(tmp_path)]), patch("phaze.tasks.tag_write._extract_before_tags", side_effect=ValueError("unreadable tags")):
        result = await write_file_tags({"api_client": _make_agent_client(session, token)}, **kwargs)
    await session.refresh(log)
    await session.refresh(record)
    assert result["status"] == log.status == TagWriteStatus.FAILED
    assert log.before_tags["artist"] is None
    assert record.sha256_hash == compute_sha256(path)
    assert path.read_bytes() == landed_bytes
