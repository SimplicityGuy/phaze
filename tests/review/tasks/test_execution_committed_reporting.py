"""Committed outcomes survive callback loss and restart through real consumers.

The broker reload, authenticated controller routes, Redis progress script and
filesystem all run for real. Only the exhausted outage/interruption is injected.
General rule: acknowledging an irreversible operation requires a durable outcome,
and replay must consume write-ahead identity rather than repeat its mutation.
"""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import redis.asyncio as redis_async
from saq import Queue

from phaze.database import get_session
from phaze.models.file import FileRecord
from phaze.models.proposal import RenameProposal
from phaze.routers import agent_exec_batches, agent_execution, agent_proposals
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.agent_client import AgentApiServerError
from phaze.tasks import execution
from tests.db_guard import integration_dsns
from tests.review.tasks._execution_contract_scenarios import _patch_settings


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


class _RouteClient:
    def __init__(self, client: AsyncClient, failed_proposal: uuid.UUID | None) -> None:
        self.client = client
        self.failed_proposal = failed_proposal

    async def _send(self, method: str, path: str, payload: Any) -> None:
        response = await self.client.request(method, path, json=payload.model_dump(mode="json", exclude_unset=True))
        response.raise_for_status()

    async def post_execution_log(self, payload: Any) -> None:
        await self._send("POST", "/api/internal/agent/execution-log", payload)

    async def patch_execution_log(self, log_id: uuid.UUID, payload: Any) -> None:
        await self._send("PATCH", f"/api/internal/agent/execution-log/{log_id}", payload)

    async def patch_proposal_state(self, proposal_id: uuid.UUID, payload: Any) -> None:
        if proposal_id == self.failed_proposal:
            self.failed_proposal = None
            raise AgentApiServerError("synthetic exhausted inventory callback outage")
        await self._send("PATCH", f"/api/internal/agent/proposals/{proposal_id}/state", payload)

    async def post_exec_batch_progress(self, batch_id: uuid.UUID, payload: Any) -> None:
        await self._send("POST", f"/api/internal/agent/exec-batches/{batch_id}/progress", payload)


@pytest.mark.parametrize(
    ("fault", "first_failure"),
    [(fault, failure) for fault in ("callback", "metadata", "intent") for failure in ("hash", "transient", "none")]
    + [("failed_metadata", "transient"), ("callback", "unsupported")],
)
@pytest.mark.parametrize("with_hash", [True, False])
async def test_committed_move_reloaded_from_broker_repairs_inventory_once(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    with_hash: bool,
    first_failure: str,
) -> None:
    """Two moves, later report lost: reload durable metadata and never recount/remutate."""
    agent, token = seed_test_agent
    first_fails = first_failure != "none" and fault != "failed_metadata"
    _patch_settings(monkeypatch, [str(tmp_path)])
    files = []
    proposals = []
    items = []
    for index in range(2):
        source = tmp_path / f"source-{index}.mp3"
        source.write_bytes(f"synthetic audio {index}".encode())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        file = FileRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            original_path=str(source),
            current_path=str(source),
            original_filename=source.name,
            file_type="mp3",
            file_size=source.stat().st_size,
            sha256_hash=digest,
        )
        proposal = RenameProposal(id=uuid.uuid4(), file_id=file.id, proposed_filename=f"dest-{index}.mp3", proposed_path="", status="approved")
        session.add(file)
        session.add(proposal)
        files.append(file)
        proposals.append(proposal)
        items.append(
            ExecuteBatchProposalItem(
                proposal_id=proposal.id,
                file_id=file.id,
                source_path=str(source),
                proposed_path="",
                proposed_filename=proposal.proposed_filename,
                sha256_hash="0" * 64 if first_failure == "hash" and index == 0 else (digest if with_hash else None),
            )
        )
    await session.commit()
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id=agent.id, proposals=items)
    queue = Queue.from_url(integration_dsns()[0], name=f"committed-report-{uuid.uuid4().hex}")
    await queue.connect()
    job = await queue.enqueue("execute_approved_batch", retries=3, **payload.model_dump(mode="json"))
    assert job is not None
    job = await queue.dequeue(timeout=2.0)
    assert job is not None
    redis = redis_async.Redis.from_url(os.environ["PHAZE_REDIS_URL"], decode_responses=True)
    key = f"exec:{payload.batch_id}"
    await redis.hset(key, mapping={"total": 2, f"agent:{agent.id}:total": 2, "subjobs_expected": 1, "subjobs_completed": 0, "status": "running"})
    await redis.set(agent_exec_batches.ACTIVE_DISPATCH_KEY, str(payload.batch_id))
    app = FastAPI()
    for module in (agent_execution, agent_proposals, agent_exec_batches):
        app.include_router(module.router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.redis = redis
    moves = []
    actual_move = execution._atomic_same_fs_move
    failed_once = False

    def counted_move(source: Path, destination: Path) -> None:
        nonlocal failed_once
        if first_failure == "transient" and source == Path(items[0].source_path) and not failed_once:
            failed_once = True
            raise OSError("synthetic transient pre-move filesystem outage")
        if first_failure == "unsupported" and source == Path(items[0].source_path) and not failed_once:
            failed_once = True
            with patch.object(execution.os, "link", side_effect=OSError(errno.ENOTSUP, "synthetic unsupported exclusive publication")):
                actual_move(source, destination)
        else:
            actual_move(source, destination)
        moves.append(str(source))

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://synthetic", headers={"Authorization": f"Bearer {token}"}) as client:
            api = _RouteClient(client, proposals[1].id if fault == "callback" else None)
            actual_update = job.update

            async def interrupted_update(**kwargs: Any) -> None:
                meta = kwargs.get("meta", {})
                if fault == "failed_metadata" and f"failed_step:{proposals[0].id}" in meta:
                    raise RuntimeError("synthetic terminal-outcome broker outage")
                if fault == "intent" and f"move_intent_hash:{proposals[1].id}" in meta:
                    raise RuntimeError("synthetic write-ahead broker outage")
                if fault == "metadata" and f"moved:{proposals[1].id}" in meta:
                    raise RuntimeError("synthetic interruption after move before metadata commit")
                await actual_update(**kwargs)

            with patch.object(execution, "_atomic_same_fs_move", counted_move):
                with (
                    patch.object(job, "update", interrupted_update if fault != "callback" else actual_update),
                    pytest.raises(RuntimeError, match=r"(committed move|move intent|terminal failure)"),
                ):
                    await execution.execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))
                expected_moves = (0 if first_fails else 1) if fault == "intent" else (1 if first_fails else 2)
                if fault == "failed_metadata":
                    expected_moves = 0
                    assert Path(items[0].source_path).read_bytes() == b"synthetic audio 0"
                    assert not (tmp_path / "dest-0.mp3").exists()
                    await session.refresh(proposals[0])
                    assert proposals[0].status == "approved"
                assert len(moves) == expected_moves
                if fault in {"intent", "failed_metadata"}:
                    assert Path(items[1].source_path).read_bytes() == b"synthetic audio 1"
                    assert not (tmp_path / "dest-1.mp3").exists()
                await session.refresh(proposals[1])
                assert proposals[1].status == "approved"
                assert (await redis.hgetall(key))["status"] == "running"
                assert await redis.exists(agent_exec_batches.ACTIVE_DISPATCH_KEY)
                # The in-memory job is discarded. SAQ itself deserializes the
                # committed PostgreSQL payload, including UUIDs and move intent.
                reloaded = await queue.job(job.key)
                assert reloaded is not None and reloaded is not job
                if first_fails:
                    assert reloaded.meta[f"failed_step:{proposals[0].id}"] == ("verify" if first_failure == "hash" else "copy")
                if fault == "failed_metadata":
                    assert f"failed_step:{proposals[0].id}" not in reloaded.meta
                if fault == "metadata":
                    assert f"moved:{proposals[1].id}" not in reloaded.meta
                if fault in {"intent", "failed_metadata"}:
                    assert f"move_intent_hash:{proposals[1].id}" not in reloaded.meta
                else:
                    assert reloaded.meta[f"move_intent_hash:{proposals[1].id}"] == files[1].sha256_hash
                await queue.retry(reloaded, error="synthetic report retry")
                reloaded = await queue.dequeue(timeout=2.0)
                assert reloaded is not None
                result = await execution.execute_approved_batch({"api_client": api, "job": reloaded}, **payload.model_dump(mode="json"))
                # Duplicate callback replay is also idempotent in the real routes.
                await execution.execute_approved_batch({"api_client": api, "job": reloaded}, **payload.model_dump(mode="json"))
            assert result["status"] == ("completed_with_errors" if first_fails else "completed")
            assert len(moves) == (1 if first_fails else 2)
            for index, (file, proposal) in enumerate(zip(files, proposals, strict=True)):
                await session.refresh(file)
                await session.refresh(proposal)
                assert proposal.status == ("failed" if first_fails and index == 0 else "executed")
                assert Path(file.current_path).is_file()
                assert hashlib.sha256(Path(file.current_path).read_bytes()).hexdigest() == file.sha256_hash
            counters = await redis.hgetall(key)
            assert counters["status"] == ("complete_with_errors" if first_fails else "complete")
            assert counters["subjobs_completed"] == "1"
            assert counters["deleted"] == ("1" if first_fails else "2")
            assert counters.get("failed", "0") == ("1" if first_fails else "0")
            assert not await redis.exists(agent_exec_batches.ACTIVE_DISPATCH_KEY)
    finally:
        await redis.delete(key, agent_exec_batches.ACTIVE_DISPATCH_KEY)
        await redis.delete(*(f"exec_progress_req:{value}" for name, value in job.meta.items() if name.startswith("req_id:")))
        await redis.aclose()
        async with queue.pool.connection() as connection:
            await connection.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await queue.disconnect()


async def test_move_intent_does_not_adopt_a_changed_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Own intent is necessary but insufficient: a foreign destination hash refuses replay."""
    from tests.review.tasks._execution_contract_scenarios import _make_api_client_mock, _make_job_mock

    _patch_settings(monkeypatch, [str(tmp_path)])
    proposal_id = uuid.uuid4()
    source = tmp_path / "absent.mp3"
    destination = tmp_path / "foreign.mp3"
    destination.write_bytes(b"foreign occupant")
    item = ExecuteBatchProposalItem(
        proposal_id=proposal_id, file_id=uuid.uuid4(), source_path=str(source), proposed_path="", proposed_filename=destination.name
    )
    job = _make_job_mock({f"move_intent_hash:{proposal_id}": hashlib.sha256(b"original audio").hexdigest()})
    api = _make_api_client_mock()
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=[item])
    result = await execution.execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))
    assert result["status"] == "completed_with_errors"
    assert destination.read_bytes() == b"foreign occupant"
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"


@pytest.mark.parametrize("with_hash", [True, False])
async def test_fresh_job_cannot_mint_replay_intent_for_an_absent_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_hash: bool,
) -> None:
    """A same-byte foreign destination is not evidence that this fresh job moved anything."""
    from tests.review.tasks._execution_contract_scenarios import _make_api_client_mock, _make_job_mock

    _patch_settings(monkeypatch, [str(tmp_path)])
    proposal_id = uuid.uuid4()
    destination = tmp_path / "foreign.mp3"
    destination.write_bytes(b"same bytes")
    digest = hashlib.sha256(b"same bytes").hexdigest()
    item = ExecuteBatchProposalItem(
        proposal_id=proposal_id,
        file_id=uuid.uuid4(),
        source_path=str(tmp_path / "absent.mp3"),
        proposed_path="",
        proposed_filename=destination.name,
        sha256_hash=digest if with_hash else None,
    )
    job = _make_job_mock()
    api = _make_api_client_mock()
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=[item])
    result = await execution.execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))
    assert result["status"] == "completed_with_errors"
    assert destination.read_bytes() == b"same bytes"
    assert f"move_intent_hash:{proposal_id}" not in job.meta
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"


def test_move_source_hash_refuses_symlinks_and_nonregular_files(tmp_path: Path) -> None:
    target = tmp_path / "regular.mp3"
    target.write_bytes(b"audio")
    link = tmp_path / "link.mp3"
    link.symlink_to(target)
    with pytest.raises(OSError):
        execution._hash_move_source(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        execution._hash_move_source(fifo)
