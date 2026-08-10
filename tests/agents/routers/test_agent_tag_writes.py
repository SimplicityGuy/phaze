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


# ---------------------------------------------------------------------------
# phaze-sit4: concurrent duplicate PATCHes must not corrupt the ``before_tags`` undo anchor.
#
# The hermetic ``session`` fixture binds every session to ONE connection inside a single
# uncommitted outer transaction, so it cannot exercise a real row lock between two genuinely
# concurrent transactions. This test therefore stands up a dedicated NullPool engine against the
# same isolated test database, commits a ``queued`` row, and races two endpoint calls -- each on
# its OWN connection -- both reporting a terminal status but with DIFFERENT ``before_tags``
# snapshots (mirroring an agent's tenacity retry firing while the first callback is still in
# flight server-side). With the ``SELECT ... FOR UPDATE`` load the two PATCHes serialize on the
# row lock: the loser blocks until the winner commits, then re-reads the now-terminal status and
# takes the no-op ``applied=False`` branch, leaving ``before_tags`` exactly as the winner wrote it.
# With the old plain ``select(...)`` both transactions read the same stale ``queued`` row and the
# last commit silently overwrites the undo anchor -- the regression this bead closes.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_duplicate_callbacks_do_not_clobber_before_tags(async_engine) -> None:  # type: ignore[no-untyped-def]
    """Two concurrent duplicate PATCHes on one ``queued`` row: exactly one ``applied=true``, one ``before_tags`` write (phaze-sit4).

    Takes the session-scoped ``async_engine`` fixture purely so the schema exists on the isolated
    test database (this test builds its own committed rows on that same NullPool engine so the two
    racing PATCHes run on genuinely separate connections -- the hermetic ``session`` fixture binds
    everything to ONE connection and cannot exercise a real row lock).
    """
    import asyncio
    import hashlib
    import secrets

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from phaze.models.agent import Agent
    from phaze.routers.agent_tag_writes import router as _router

    engine = async_engine
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    raw_token = "phaze_agent_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    agent_id = "sit4-conc-agent"
    file_id = uuid.uuid4()
    log_id = uuid.uuid4()

    # get_session override: a FRESH session (and, under NullPool, a fresh connection) per request
    # so the two racing PATCHes run in genuinely separate transactions.
    async def _override_session():  # type: ignore[no-untyped-def]
        async with factory() as s:
            yield s

    app = FastAPI(title="conc-agent-tag-writes", version="test")
    app.include_router(_router)
    app.dependency_overrides[get_session] = _override_session

    async def _reset_to_queued() -> None:
        async with factory() as s:
            row = await s.get(TagWriteLog, log_id)
            row.status = TagWriteStatus.QUEUED.value
            row.before_tags = {}
            row.error_message = None
            await s.commit()

    try:
        # Seed committed prerequisites (own connection, real commit).
        async with factory() as s:
            s.add(Agent(id=agent_id, name=agent_id, token_hash=token_hash, scan_roots=["/test/music"]))
            s.add(
                FileRecord(
                    id=file_id,
                    agent_id=agent_id,
                    sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                    original_path=f"/test/conc-{file_id}.mp3",
                    original_filename="conc.mp3",
                    current_path=f"/test/conc-{file_id}.mp3",
                    file_type="mp3",
                    file_size=1234,
                )
            )
            await s.flush()
            s.add(
                TagWriteLog(
                    id=log_id,
                    file_id=file_id,
                    before_tags={},
                    after_tags={"artist": "New Artist"},
                    source="proposal",
                    status=TagWriteStatus.QUEUED.value,
                )
            )
            await s.commit()

        headers = {"Authorization": f"Bearer {raw_token}"}
        # Repeat rounds to make a stale-read interleaving overwhelmingly likely if the lock is absent.
        for _ in range(15):
            await _reset_to_queued()
            async with (
                AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac_a,
                AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac_b,
            ):
                url = f"/api/internal/agent/tag-writes/{log_id}"
                before_tags_a = {"artist": "Original A"}
                before_tags_b = {"artist": "Original B"}
                r_a, r_b = await asyncio.gather(
                    ac_a.patch(url, json=_body(before_tags=before_tags_a)),
                    ac_b.patch(url, json=_body(before_tags=before_tags_b)),
                    return_exceptions=False,
                )

            assert r_a.status_code == 200
            assert r_b.status_code == 200
            applied_flags = {r_a.json()["applied"], r_b.json()["applied"]}
            assert applied_flags == {True, False}, f"exactly one callback must apply the write, the other must be a no-op: {r_a.json()}, {r_b.json()}"

            # Whichever call's ``applied=True`` response won the race, ITS ``before_tags`` snapshot
            # (not the response body, which carries no ``before_tags`` field -- only the persisted
            # row does) must be the one persisted, never overwritten by the loser.
            winner_before_tags = before_tags_a if r_a.json()["applied"] else before_tags_b

            async with factory() as s:
                row = await s.get(TagWriteLog, log_id)
                assert row.before_tags == winner_before_tags, (
                    f"undo anchor corrupted under concurrency: before_tags={row.before_tags!r} (a stale-read PATCH overwrote the winner's snapshot)"
                )
    finally:
        # Clean up the committed rows so no residue survives into the rest of the suite.
        async with factory() as s:
            log = await s.get(TagWriteLog, log_id)
            if log is not None:
                await s.delete(log)
            fr = await s.get(FileRecord, file_id)
            if fr is not None:
                await s.delete(fr)
            ag = await s.get(Agent, agent_id)
            if ag is not None:
                await s.delete(ag)
            await s.commit()
        # NOTE: do NOT dispose ``engine`` -- it is the session-scoped ``async_engine`` fixture,
        # owned (and disposed) by conftest.


# ---------------------------------------------------------------------------
# phaze-anrw4: the pre-write snapshot endpoint + the first-write-wins guard it shares with the
# result callback.
# ---------------------------------------------------------------------------


def _snapshot_body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"before_tags": {"artist": "Original Artist"}}
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_before_snapshot_records_on_a_queued_row(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        resp = await client.patch(f"/api/internal/agent/tag-writes/{log.id}/before-snapshot", json=_snapshot_body())

    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    await session.refresh(log)
    assert log.before_tags == {"artist": "Original Artist"}
    assert log.status == TagWriteStatus.QUEUED, "the snapshot report must not advance the row's status"


@pytest.mark.asyncio
async def test_before_snapshot_first_write_wins_against_a_second_snapshot_report(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    """phaze-anrw4: a SAQ retry's re-extraction (a second snapshot report) must never win.

    Simulates the corrupted case: the first report carries the TRUE original; a later report
    (standing in for a retry that re-extracted from the already-written disk) carries the WRONG,
    post-write snapshot. Only the first must persist.
    """
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        first = await client.patch(f"/api/internal/agent/tag-writes/{log.id}/before-snapshot", json=_snapshot_body())
        second = await client.patch(
            f"/api/internal/agent/tag-writes/{log.id}/before-snapshot",
            json=_snapshot_body(before_tags={"artist": "Proposed Value"}),
        )

    assert first.json()["applied"] is True
    assert second.json()["applied"] is False
    await session.refresh(log)
    assert log.before_tags == {"artist": "Original Artist"}, "a later snapshot must never clobber the first"


@pytest.mark.asyncio
async def test_result_callback_keeps_the_before_snapshot_already_recorded(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    """The end-to-end phaze-anrw4 sequence: snapshot lands first, then the (corrupted) result callback.

    Mirrors the real failure this closes: attempt 1's write lands but its result callback never
    reaches the control plane, so the row is still ``queued`` when attempt 2's callback arrives
    carrying a ``before_tags`` re-extracted from the ALREADY-WRITTEN disk (equal to the proposed
    value). The genuine, pre-write snapshot reported earlier must win.
    """
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        await client.patch(f"/api/internal/agent/tag-writes/{log.id}/before-snapshot", json=_snapshot_body())
        resp = await client.patch(
            f"/api/internal/agent/tag-writes/{log.id}",
            json=_body(before_tags={"artist": "New Artist"}),  # the corrupted, post-write re-extraction
        )

    assert resp.status_code == 200
    assert resp.json()["applied"] is True
    await session.refresh(log)
    assert log.status == TagWriteStatus.COMPLETED
    assert log.before_tags == {"artist": "Original Artist"}, "the undo anchor must be the true original, not the retry's re-extraction"


@pytest.mark.asyncio
async def test_before_snapshot_no_op_once_the_row_is_terminal(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    _agent, token = seed_test_agent
    log = await _seed_queued_log(session)

    async with _make_client(session, token) as client:
        await client.patch(f"/api/internal/agent/tag-writes/{log.id}", json=_body())
        resp = await client.patch(f"/api/internal/agent/tag-writes/{log.id}/before-snapshot", json=_snapshot_body())

    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    await session.refresh(log)
    assert log.before_tags == {"artist": "Old Artist", "title": None}, "a late snapshot must not touch an already-terminal row"


@pytest.mark.asyncio
async def test_before_snapshot_unknown_log_id_404s(session: AsyncSession, seed_test_agent) -> None:  # type: ignore[no-untyped-def]
    _agent, token = seed_test_agent

    async with _make_client(session, token) as client:
        resp = await client.patch(f"/api/internal/agent/tag-writes/{uuid.uuid4()}/before-snapshot", json=_snapshot_body())

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_before_snapshot_unauthenticated_is_refused(session: AsyncSession) -> None:
    log = await _seed_queued_log(session)

    async with _make_client(session) as client:
        resp = await client.patch(f"/api/internal/agent/tag-writes/{log.id}/before-snapshot", json=_snapshot_body())

    assert resp.status_code in (401, 403)
