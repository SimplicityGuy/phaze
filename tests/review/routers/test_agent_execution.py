"""DIST-04 (4/5) + DIST-05 (4/5, 5/5) + D-13 + D-15 tests for /api/internal/agent/execution-log.

Tests build their own self-contained FastAPI app via `_make_authed_client` so
this Plan 25-05 suite is parallel-safe and does NOT depend on Plan 25-06 wiring
the real router into `main.py`. Mirrors Plan 25-02's smoke-app strategy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import func as sa_func, select

from phaze.database import get_session
from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord
from phaze.models.proposal import RenameProposal
from phaze.routers.agent_execution import router as agent_execution_router


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_authed_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that includes the agent_execution router.

    Why this instead of `authenticated_client`: the real `create_app()` does NOT
    include the agent_execution router until Plan 25-06 wires it in. To keep
    Plan 25-05 tests parallel-safe (and independent of Plan 06 landing order),
    we build an inline FastAPI app per-test that mounts ONLY this router and
    overrides `get_session` to use the test session.
    """
    app = FastAPI(title="smoke-agent-execution", version="test")
    app.include_router(agent_execution_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest.fixture
def authed_app(session: AsyncSession, seed_test_agent: tuple[Agent, str]) -> tuple[FastAPI, str]:
    """Return (smoke FastAPI app with agent_execution router mounted, raw bearer token).

    Depends on `seed_test_agent` so the bearer used by `_authed_client` is a
    real agents-table row; depends on `session` so the dependency override
    targets the same session that the test holds.
    """
    _agent, raw_token = seed_test_agent
    return _make_authed_app(session), raw_token


async def _authed_client(authed_app: tuple[FastAPI, str]) -> AsyncGenerator[AsyncClient]:
    """Async context-managed AsyncClient with `Authorization: Bearer <raw_token>` pre-set."""
    app, raw_token = authed_app
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        yield ac


async def _seed_proposal_chain(session: AsyncSession, agent_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed FileRecord + RenameProposal so ExecutionLog FK constraints are satisfied.

    Returns (file_id, proposal_id). Mirrors the verified shape from
    `tests/test_routers/test_execution.py:45-58`. The only NOT NULL columns on
    RenameProposal without defaults are `file_id` and `proposed_filename`; every
    other column accepts NULL or a Python-side default. The FileRecord row is
    associated with the caller's agent so multi-row tests don't trip the
    `uq_files_agent_id_original_path` unique index.
    """
    file_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="0" * 64,
            original_path=f"/test/exec-{uuid.uuid4()}.mp3",
            original_filename="test.mp3",
            current_path=f"/test/exec-{uuid.uuid4()}.mp3",
            file_type="mp3",
            file_size=1234,
        )
    )
    await session.flush()
    session.add(
        RenameProposal(
            id=proposal_id,
            file_id=file_id,
            proposed_filename="proposed.mp3",
        )
    )
    await session.commit()
    return file_id, proposal_id


def _make_create_body(proposal_id: uuid.UUID, log_id: uuid.UUID | None = None, status: str = "pending") -> dict[str, object]:
    """Build a valid POST body for /api/internal/agent/execution-log."""
    return {
        "id": str(log_id or uuid.uuid4()),
        "proposal_id": str(proposal_id),
        "operation": "move",
        "source_path": "/test/music/a.mp3",
        "destination_path": "/test/output/a.mp3",
        "sha256_verified": False,
        "status": status,
    }


@pytest.mark.asyncio
async def test_execution_log_create_and_patch(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """DIST-04 (4/5): POST creates row; PATCH advances status forward."""
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_proposal_chain(session, agent.id)
    log_id = uuid.uuid4()

    async for ac in _authed_client(authed_app):
        r_post = await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(proposal_id, log_id=log_id, status="pending"),
        )
        assert r_post.status_code == 200, r_post.text
        body_post = r_post.json()
        assert body_post["agent_id"] == agent.id
        assert body_post["execution_log_id"] == str(log_id)

        r_patch = await ac.patch(
            f"/api/internal/agent/execution-log/{log_id}",
            json={"status": "in_progress"},
        )
        assert r_patch.status_code == 200
        body_patch = r_patch.json()
        assert body_patch["status"] == "in_progress"

    result = await session.execute(select(ExecutionLog).where(ExecutionLog.id == log_id))
    row = result.scalar_one()
    assert row.status == ExecutionStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_create_replay_no_op(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """DIST-05 (4/5) + D-13: same agent-supplied id POSTed twice -> one row, no error."""
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_proposal_chain(session, agent.id)
    log_id = uuid.uuid4()
    payload = _make_create_body(proposal_id, log_id=log_id)

    async for ac in _authed_client(authed_app):
        r1 = await ac.post("/api/internal/agent/execution-log", json=payload)
        r2 = await ac.post("/api/internal/agent/execution-log", json=payload)
        assert r1.status_code == 200
        assert r2.status_code == 200

    result = await session.execute(select(sa_func.count()).select_from(ExecutionLog).where(ExecutionLog.id == log_id))
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_monotonic_regress_returns_409(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """DIST-05 (5/5) + D-15: IN_PROGRESS -> PENDING is regress -> 409 'would regress'."""
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_proposal_chain(session, agent.id)
    log_id = uuid.uuid4()

    async for ac in _authed_client(authed_app):
        await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(proposal_id, log_id=log_id, status="in_progress"),
        )

        response = await ac.patch(
            f"/api/internal/agent/execution-log/{log_id}",
            json={"status": "pending"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "execution-log status would regress"


@pytest.mark.asyncio
async def test_terminal_state_rejects_patch(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """D-15: terminal-state COMPLETED rejects further PATCH with 409 'is terminal'."""
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_proposal_chain(session, agent.id)
    log_id = uuid.uuid4()

    async for ac in _authed_client(authed_app):
        await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(proposal_id, log_id=log_id, status="completed"),
        )

        response = await ac.patch(
            f"/api/internal/agent/execution-log/{log_id}",
            json={"status": "in_progress"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "execution-log status is terminal"


@pytest.mark.asyncio
async def test_same_status_patch_allowed(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """D-15 footnote: same-status PATCH (IN_PROGRESS -> IN_PROGRESS) is allowed for retry idempotency."""
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_proposal_chain(session, agent.id)
    log_id = uuid.uuid4()

    async for ac in _authed_client(authed_app):
        await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(proposal_id, log_id=log_id, status="in_progress"),
        )

        response = await ac.patch(
            f"/api/internal/agent/execution-log/{log_id}",
            json={"status": "in_progress"},
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_patch_unknown_id_returns_404(authed_app: tuple[FastAPI, str]) -> None:
    """PATCH against a fresh uuid that does not exist -> 404."""
    async for ac in _authed_client(authed_app):
        response = await ac.patch(
            f"/api/internal/agent/execution-log/{uuid.uuid4()}",
            json={"status": "in_progress"},
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_extra_body_field_422(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """D-16: extra field on POST body -> 422 extra_forbidden."""
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_proposal_chain(session, agent.id)

    bad_body = {**_make_create_body(proposal_id), "agent_id": "evil"}
    async for ac in _authed_client(authed_app):
        response = await ac.post("/api/internal/agent/execution-log", json=bad_body)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_same_status_patch_terminal_allowed(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Gap closure CR-02 (25-VERIFICATION.md): COMPLETED -> COMPLETED PATCH must return 200.

    Canonical idempotent retry case: agent writes COMPLETED, network glitch
    swallows the 200, SAQ retries the job, agent re-sends the same PATCH.
    Before the CR-02 fix this returned `409 "execution-log status is terminal"`.
    After the fix it returns 200 and leaves the row in COMPLETED.

    The verification report named this test `test_same_status_patch_terminal_allowed`
    explicitly; this implementation matches that contract.
    """
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_proposal_chain(session, agent.id)
    log_id = uuid.uuid4()

    async for ac in _authed_client(authed_app):
        r_post = await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(proposal_id, log_id=log_id, status="completed"),
        )
        assert r_post.status_code == 200, r_post.text

        r_patch = await ac.patch(
            f"/api/internal/agent/execution-log/{log_id}",
            json={"status": "completed"},
        )
        # CR-02: same-status retry against terminal MUST be 200 (idempotent).
        assert r_patch.status_code == 200, f"CR-02 regression: COMPLETED -> COMPLETED PATCH returned {r_patch.status_code} {r_patch.text!r}"
        assert r_patch.json()["status"] == "completed"

    session.expire_all()
    result = await session.execute(select(ExecutionLog).where(ExecutionLog.id == log_id))
    row = result.scalar_one()
    assert row.status == ExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_same_status_patch_terminal_failed_allowed(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Gap closure CR-02: FAILED -> FAILED PATCH must also return 200.

    Symmetry with COMPLETED -> COMPLETED. Both terminal states must allow
    same-status idempotent retry — the carve-out in the guard is
    `cur in _TERMINAL and new != cur`, which applies equally to FAILED and
    COMPLETED.
    """
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_proposal_chain(session, agent.id)
    log_id = uuid.uuid4()

    async for ac in _authed_client(authed_app):
        r_post = await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(proposal_id, log_id=log_id, status="failed"),
        )
        assert r_post.status_code == 200, r_post.text

        r_patch = await ac.patch(
            f"/api/internal/agent/execution-log/{log_id}",
            json={"status": "failed"},
        )
        assert r_patch.status_code == 200, f"CR-02 regression: FAILED -> FAILED PATCH returned {r_patch.status_code} {r_patch.text!r}"
        assert r_patch.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_create_nonexistent_proposal_returns_404_not_500(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-stpl (request_guards.py rule 4): a well-formed but nonexistent ``proposal_id`` is a genuine
    race (stale SAQ job state, or the proposal was deleted/rolled back concurrently) -- no stricter
    Pydantic signature could have rejected it, so it is caught as ``IntegrityError`` and mapped to a
    clean 404, never an unhandled 500. ``on_conflict_do_nothing(index_elements=["id"])`` does NOT
    shield this: it only absorbs an ``id`` PK replay, not the separate ``proposal_id`` FK.
    """
    log_id = uuid.uuid4()
    nonexistent_proposal_id = uuid.uuid4()  # genuinely nonexistent -- no RenameProposal was ever seeded

    async for ac in _authed_client(authed_app):
        response = await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(nonexistent_proposal_id, log_id=log_id),
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "proposal not found"

    result = await session.execute(select(ExecutionLog).where(ExecutionLog.id == log_id))
    assert result.scalar_one_or_none() is None  # no row was left behind by the failed FK insert


@pytest.mark.asyncio
async def test_create_nonexistent_proposal_leaves_session_usable(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """request_guards.py rule 5: the caught ``IntegrityError`` must unwind only the nested SAVEPOINT
    (``session.begin_nested()``), NOT a full ``session.rollback()``. A full rollback expires every
    already-loaded ORM object on the session, so the NEXT statement on it would 500 on exactly the
    hiccup this fix was meant to survive (phaze-5tsj / phaze-yfj1). Proves the session -- the same
    object the app's ``get_session`` override hands back on every request -- survives the caught race
    and can still service a subsequent, genuinely valid create in the SAME session.
    """
    agent, _ = seed_test_agent
    _, valid_proposal_id = await _seed_proposal_chain(session, agent.id)
    doomed_log_id = uuid.uuid4()
    good_log_id = uuid.uuid4()
    nonexistent_proposal_id = uuid.uuid4()

    async for ac in _authed_client(authed_app):
        failed = await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(nonexistent_proposal_id, log_id=doomed_log_id),
        )
        assert failed.status_code == 404

        # The session must still be usable -- neither PendingRollbackError nor a refresh-against-an-
        # aborted-transaction 500 on the very next statement.
        recovered = await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(valid_proposal_id, log_id=good_log_id),
        )
        assert recovered.status_code == 200, recovered.text

    result = await session.execute(select(ExecutionLog).where(ExecutionLog.id == good_log_id))
    assert result.scalar_one().proposal_id == valid_proposal_id


@pytest.mark.asyncio
async def test_terminal_completed_to_failed_still_rejected(
    authed_app: tuple[FastAPI, str],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Gap closure CR-02 boundary: COMPLETED -> FAILED is STILL 409 'is terminal'.

    The CR-02 carve-out is narrow: ONLY same-status retries against terminal
    rows are allowed. Crossing from one terminal state to another is still a
    contract violation (the row's final disposition cannot be retroactively
    changed) and MUST 409.

    This test prevents a future refactor from over-broadening the carve-out
    (e.g., accidentally allowing `cur in _TERMINAL and new in _TERMINAL`).
    """
    agent, _ = seed_test_agent
    _, proposal_id = await _seed_proposal_chain(session, agent.id)
    log_id = uuid.uuid4()

    async for ac in _authed_client(authed_app):
        await ac.post(
            "/api/internal/agent/execution-log",
            json=_make_create_body(proposal_id, log_id=log_id, status="completed"),
        )

        response = await ac.patch(
            f"/api/internal/agent/execution-log/{log_id}",
            json={"status": "failed"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "execution-log status is terminal"
