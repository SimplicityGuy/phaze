"""Contract tests for the tag-write result callback (phaze-6bkk).

``PATCH /api/internal/agent/tag-writes/{log_id}`` is the ONLY way an on-disk tag write reaches the
control plane's ``tag_write_log`` audit table. Under DIST-01 the api container mounts no media, so
the mutagen write runs on the owning agent's ``meta`` lane and the agent -- which never touches
Postgres (D-25) -- reports here.

Smoke-app pattern (mirrors ``test_agent_push.py``): a real DB session, the router mounted on a bare
FastAPI app, and the token auth dep exercised end-to-end via ``seed_test_agent``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.database import get_session
from phaze.enums.tag_write import TagWriteStatus
from phaze.models.file import FileRecord
from phaze.models.tag_write_log import TagWriteLog
from phaze.routers.agent_tag_writes import router as agent_tag_writes_router


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_client(session: AsyncSession, token: str | None = None) -> AsyncClient:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_tag_writes_router)
    app.dependency_overrides[get_session] = lambda: session
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_queued_log(session: AsyncSession, *, source: str = "proposal") -> TagWriteLog:
    """Seed the applied file + the ``queued`` audit row the control plane creates at dispatch."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id="test-fileserver",
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/data/music/{file_id}.mp3",
            original_filename="<track-01>.mp3",
            current_path=f"/data/music/{file_id}.mp3",
            file_type="mp3",
            file_size=4096,
        )
    )
    await session.flush()
    log = TagWriteLog(
        id=uuid.uuid4(),
        file_id=file_id,
        before_tags={},
        after_tags={"artist": "New Artist"},
        source=source,
        status=TagWriteStatus.QUEUED.value,
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return log


def _body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": TagWriteStatus.COMPLETED.value,
        "before_tags": {"artist": "Old Artist", "title": None},
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_completed_callback_moves_the_row_out_of_queued(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    """The happy path: ``queued`` -> ``completed``, with the agent-captured before-tags snapshot."""
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        resp = await client.patch(f"/api/internal/agent/tag-writes/{log.id}", json=_body())

    assert resp.status_code == 200
    assert resp.json()["applied"] is True

    await session.refresh(log)
    assert log.status == TagWriteStatus.COMPLETED
    # phaze-52qd: the COMPLETE snapshot (``None`` for absent tags) is what an undo re-applies, and
    # this callback is the ONLY place it can enter the DB -- the api cannot read the file.
    assert log.before_tags == {"artist": "Old Artist", "title": None}


@pytest.mark.asyncio
async def test_discrepancy_and_error_detail_are_persisted(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-clean outcome carries its per-field detail through to the audit row."""
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        resp = await client.patch(
            f"/api/internal/agent/tag-writes/{log.id}",
            json=_body(status=TagWriteStatus.DISCREPANCY.value, discrepancies={"artist": {"expected": "A", "actual": "B"}}),
        )

    assert resp.status_code == 200
    await session.refresh(log)
    assert log.status == TagWriteStatus.DISCREPANCY
    assert log.discrepancies == {"artist": {"expected": "A", "actual": "B"}}


@pytest.mark.asyncio
async def test_nul_in_error_message_is_sanitized_before_persist(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    """A NUL passes pydantic but aborts the transaction in Postgres, stranding the row forever.

    Same class as the metadata callback's ``sanitize_pg_text`` guard: an OSError string built from a
    mangled filename can carry one, and ``CharacterNotInRepertoireError`` would roll back the whole
    callback -- leaving the audit row ``queued`` and the file silently out of every terminal count.
    """
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        resp = await client.patch(
            f"/api/internal/agent/tag-writes/{log.id}",
            json=_body(status=TagWriteStatus.FAILED.value, error_message="bad\x00path"),
        )

    assert resp.status_code == 200
    await session.refresh(log)
    assert log.status == TagWriteStatus.FAILED
    assert log.error_message is not None
    assert "\x00" not in log.error_message


@pytest.mark.asyncio
async def test_replay_is_an_idempotent_no_op(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    """A SAQ retry whose callback already landed gets a 200 with ``applied=false``, not a duplicate.

    This matters beyond tidiness: the audit row is the UNDO anchor. Letting a late duplicate
    overwrite ``before_tags`` with a snapshot taken AFTER the first write landed would make undo
    restore the WRITTEN tags instead of the original ones. Also a 409 would burn the agent's tenacity
    retries on a permanent error.
    """
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        first = await client.patch(f"/api/internal/agent/tag-writes/{log.id}", json=_body())
        second = await client.patch(
            f"/api/internal/agent/tag-writes/{log.id}",
            json=_body(before_tags={"artist": "New Artist"}),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["applied"] is False

    await session.refresh(log)
    assert log.before_tags == {"artist": "Old Artist", "title": None}, "a replay must not clobber the undo anchor"

    rows = (await session.execute(select(TagWriteLog).where(TagWriteLog.file_id == log.file_id))).scalars().all()
    assert len(rows) == 1, "the callback UPDATEs one row -- it never appends"


@pytest.mark.asyncio
async def test_queued_status_is_rejected(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    """``queued`` is the state this endpoint EXISTS to leave -- accepting it strands the row silently."""
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        resp = await client.patch(f"/api/internal/agent/tag-writes/{log.id}", json=_body(status=TagWriteStatus.QUEUED.value))

    assert resp.status_code == 422
    await session.refresh(log)
    assert log.status == TagWriteStatus.QUEUED


@pytest.mark.asyncio
async def test_unknown_log_id_404s(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    _agent, token = seed_test_agent

    async with _make_client(session, token) as client:
        resp = await client.patch(f"/api/internal/agent/tag-writes/{uuid.uuid4()}", json=_body())

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated_callback_is_refused(session: AsyncSession) -> None:
    """AUTH-01: the calling agent comes from the token, never the body -- no token, no write."""
    log = await _seed_queued_log(session)

    async with _make_client(session) as client:
        resp = await client.patch(f"/api/internal/agent/tag-writes/{log.id}", json=_body())

    assert resp.status_code in (401, 403)
    await session.refresh(log)
    assert log.status == TagWriteStatus.QUEUED


@pytest.mark.asyncio
async def test_unknown_body_field_is_rejected(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    """Phase 25 D-16: agent-supplied bodies are validated as strictly as any other request body."""
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        resp = await client.patch(f"/api/internal/agent/tag-writes/{log.id}", json=_body(unexpected="x"))

    assert resp.status_code == 422
