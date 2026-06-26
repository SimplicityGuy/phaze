"""Contract tests for the control-side push callbacks (Phase 50, Plan 50-05).

Two endpoints mirror the existing ``put_analysis`` / ``report_analysis_failed``
split (RESEARCH §Critical Finding 1):

- ``POST /api/internal/agent/push/{file_id}/pushed``   — the fileserver agent
  reports a successful rsync; control flips the file ``PUSHING -> PUSHED``,
  clears the ``push_file:<id>`` ledger row, and enqueues exactly one
  ``process_file`` job on the COMPUTE queue carrying the ORM-pinned
  ``expected_sha256`` (D-11) and a ``compute_scratch_dir``-rooted
  ``scratch_path`` — all in one committed transaction.
- ``POST /api/internal/agent/push/{file_id}/mismatch`` — the compute agent
  reports a sha256 mismatch; under ``push_max_attempts`` control re-drives
  ``push_file`` on the FILESERVER queue (keeping the PUSHING slot, Open-Q1) and
  increments the ``push_attempt`` counter in the ledger payload; at/over the cap
  control sets ``ANALYSIS_FAILED`` and clears the ledger (D-12).

Smoke-app pattern (mirrors ``test_agent_analysis.py``): a real DB session via the
``session`` fixture, a ``FakeTaskRouter`` on ``app.state``, and a monkeypatched
``get_settings`` so ``compute_scratch_dir`` / ``models_path`` / ``push_max_attempts``
are deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_push import router as agent_push_router
from phaze.services.scheduling_ledger import upsert_ledger_entry
from tests._queue_fakes import FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_SCRATCH_DIR = "/srv/scratch"
_MODELS_PATH = "/models"


class _StubCfg(SimpleNamespace):
    """A duck-typed ControlSettings stand-in carrying only the fields the router reads."""

    def __init__(self, *, push_max_attempts: int = 3) -> None:
        super().__init__(
            compute_scratch_dir=_SCRATCH_DIR,
            models_path=_MODELS_PATH,
            push_max_attempts=push_max_attempts,
        )


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, push_max_attempts: int = 3) -> None:
    """Pin the router's ``get_settings()`` deterministically."""
    monkeypatch.setattr(
        "phaze.routers.agent_push.get_settings",
        lambda: _StubCfg(push_max_attempts=push_max_attempts),
    )


def _make_app(session: AsyncSession, task_router: FakeTaskRouter) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_push_router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.task_router = task_router
    return app


def _make_client(session: AsyncSession, task_router: FakeTaskRouter, token: str | None = None) -> AsyncClient:
    app = _make_app(session, task_router)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_file(session: AsyncSession, agent_id: str, *, state: FileState = FileState.PUSHING) -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="a" * 64,
            original_path=f"/test/music/{file_id}.flac",
            original_filename=f"{file_id}.flac",
            current_path=f"/test/music/{file_id}.flac",
            file_type="flac",
            file_size=4096,
            state=state,
        )
    )
    await session.commit()
    return file_id


async def _seed_push_ledger(session: AsyncSession, file_id: uuid.UUID, *, push_attempt: int | None = None) -> None:
    payload: dict[str, Any] = {"file_id": str(file_id)}
    if push_attempt is not None:
        payload["push_attempt"] = push_attempt
    await upsert_ledger_entry(session, key=f"push_file:{file_id}", function="push_file", kwargs=payload)
    await session.commit()


async def _ledger_row(session: AsyncSession, key: str) -> SchedulingLedger | None:
    session.expire_all()
    return (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()


async def _file_row(session: AsyncSession, file_id: uuid.UUID) -> FileRecord:
    session.expire_all()
    return (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()


# ---------------------------------------------------------------------------
# /pushed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pushed_transitions_clears_ledger_and_enqueues_process_file(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pushed -> PUSHED + push ledger cleared + ONE process_file with pinned sha256 + scratch_path."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id)
    compute = await seed_active_agent(session, agent_id="compute-01", kind="compute")
    compute_id = compute.id  # capture before any expire_all() detaches the attribute

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"] == str(file_id)
    assert body["status"] == "pushed"

    # State advanced + ledger cleared in one transaction.
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHED
    sha = file_row.sha256_hash  # read before the next expire_all() to avoid a lazy reload
    assert await _ledger_row(session, f"push_file:{file_id}") is None, "push_file ledger row must be cleared"

    # Exactly one process_file enqueued on the COMPUTE queue with the pinned payload.
    compute_queue = task_router.queues[compute_id]
    assert len(compute_queue.captured) == 1
    task_name, payload = compute_queue.captured[0]
    assert task_name == "process_file"
    assert payload["expected_sha256"] == sha == "a" * 64
    assert payload["scratch_path"] == f"{_SCRATCH_DIR}/{file_id}.flac"


@pytest.mark.asyncio
async def test_pushed_holds_cleanly_when_no_compute_agent(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No compute agent online -> 200 hold (no 500), state stays PUSHING, ledger intact."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHING, "held file must stay PUSHING for the staging cron / recovery"
    assert await _ledger_row(session, f"push_file:{file_id}") is not None, "ledger row must survive a hold"
    assert task_router.queues == {}, "nothing enqueued when no compute agent is online"


@pytest.mark.asyncio
async def test_pushed_duplicate_callback_is_idempotent_noop(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-02: a duplicate/late /pushed callback must NOT clobber an already-ANALYZED file.

    A push_file SAQ retry can post /pushed twice; if the first committed and process_file has since
    finished (file now ANALYZED), the second callback must be an idempotent no-op -- it must not
    reset the row to PUSHED nor re-enqueue process_file (which would re-trigger CR-01 stranding).
    """
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch)
    # The file has already advanced all the way to ANALYZED (the first callback + analysis ran).
    file_id = await _seed_file(session, agent.id, state=FileState.ANALYZED)
    await seed_active_agent(session, agent_id="compute-01", kind="compute")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 200, r.text
    # State is untouched (NOT reset to PUSHED).
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.ANALYZED
    # Nothing re-enqueued -- the finished file is not re-analyzed.
    assert task_router.queues == {}


@pytest.mark.asyncio
async def test_pushed_missing_auth_returns_401(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Authorization header -> 401 (HTTPBearer auto_error)."""
    agent, _ = seed_test_agent
    _patch_settings(monkeypatch)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, token=None) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/pushed")

    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mismatch_under_cap_redrives_and_increments_counter(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the cap: push_attempt++ in the ledger payload + push_file re-enqueued, state stays PUSHING."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, push_max_attempts=3)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id, push_attempt=0)
    fileserver = await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")
    fileserver_id = fileserver.id  # capture before any expire_all()

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_id"] == str(file_id)
    assert body["status"] == "mismatch"
    assert body["cleared"] is False, "under the cap the push is re-driven, not cleared"

    # The file keeps its PUSHING slot (Open-Q1).
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHING

    # push_attempt incremented in the ledger payload (Pitfall 4 -- counter rides the JSONB).
    row = await _ledger_row(session, f"push_file:{file_id}")
    assert row is not None, "the ledger row must be retained on a re-drive"
    assert row.payload.get("push_attempt") == 1

    # push_file re-enqueued on the FILESERVER queue with the deterministic key.
    fileserver_queue = task_router.queues[fileserver_id]
    assert len(fileserver_queue.captured) == 1
    task_name, payload = fileserver_queue.captured[0]
    assert task_name == "push_file"
    assert payload["file_id"] == str(file_id)
    assert payload["agent_id"] == fileserver_id
    assert fileserver_queue.captured_policy[0]["key"] == f"push_file:{file_id}"


@pytest.mark.asyncio
async def test_mismatch_over_cap_fails_terminally_and_clears_ledger(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At/over the cap: state -> ANALYSIS_FAILED + ledger cleared, in one transaction (no re-drive)."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, push_max_attempts=3)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    # Already at the cap: the next attempt (4) exceeds push_max_attempts=3.
    await _seed_push_ledger(session, file_id, push_attempt=3)
    await seed_active_agent(session, agent_id="fileserver-01", kind="fileserver")

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cleared"] is True, "over the cap the file is terminally failed and the ledger cleared"

    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.ANALYSIS_FAILED
    assert await _ledger_row(session, f"push_file:{file_id}") is None, "ledger row must be cleared on terminal failure"
    # No re-drive enqueue happened.
    assert task_router.queues == {}


@pytest.mark.asyncio
async def test_mismatch_holds_when_no_fileserver_agent(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the cap but no fileserver online -> 200 hold, file stays PUSHING, ledger retained."""
    agent, raw_token = seed_test_agent
    _patch_settings(monkeypatch, push_max_attempts=3)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)
    await _seed_push_ledger(session, file_id, push_attempt=0)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is False
    file_row = await _file_row(session, file_id)
    assert file_row.state == FileState.PUSHING
    assert await _ledger_row(session, f"push_file:{file_id}") is not None
    assert task_router.queues == {}, "nothing enqueued when no fileserver agent is online"


@pytest.mark.asyncio
async def test_mismatch_missing_auth_returns_401(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Authorization header -> 401 (HTTPBearer auto_error)."""
    agent, _ = seed_test_agent
    _patch_settings(monkeypatch)
    file_id = await _seed_file(session, agent.id, state=FileState.PUSHING)

    task_router = FakeTaskRouter()
    async with _make_client(session, task_router, token=None) as ac:
        r = await ac.post(f"/api/internal/agent/push/{file_id}/mismatch")

    assert r.status_code == 401
