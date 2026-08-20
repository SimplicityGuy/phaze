"""DIST-04 / DIST-05 / D-16 / D-20 / D-22 / AUTH-01 tests for POST /api/internal/agent/files.

Why local fixture overrides exist (Rule 3 deviation):
    Plan 25-03 ships ``src/phaze/routers/agent_files.py`` but does NOT wire it
    into ``main.py`` -- that is Plan 25-06's job (Wave 4). The conftest.py
    ``authenticated_client`` fixture uses ``create_app()``, so without local
    overrides every router test would return 404. The local fixtures below
    construct a self-contained FastAPI app that mounts ``agent_files.router``
    and ``health.router`` so DIST-04 / DIST-05 / D-16 / D-20 / D-22 tests can
    exercise the real handler in Wave 3, matching Plan 25-02's smoke-app
    pattern (``tests/test_routers/test_agent_auth.py::_make_smoke_app``).
    Test 8 (``test_missing_auth_returns_401``) intentionally uses the
    production ``client`` fixture to verify the route is correctly 404 on the
    production app until Plan 06 wires it.

Plan 26-12 update:
    Handler refactor swapped the inline ``Queue.from_url(...)`` for the
    lifespan-wired ``app.state.task_router`` (an ``AgentTaskRouter``). The
    smoke-app fixture installs an ``AsyncMock()`` at ``app.state.task_router``.

Phase 35 (D-06) update:
    The handler NO LONGER auto-enqueues the metadata-extraction task -- metadata
    extraction is operator-triggered only. The smoke-app's ``app.state.task_router``
    mock is retained (the fixture is shared) but the handler never calls it, so the
    enqueue-related tests now assert ``enqueue_for_agent`` is NEVER awaited and the
    response ``enqueued`` count is always 0.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import event, func as sa_func, select, update

from phaze.database import get_session
from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers import agent_files


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_smoke_app(session: AsyncSession) -> tuple[FastAPI, AsyncMock]:
    """Build a FastAPI app wiring agent_files.router so Wave-3 tests can call the real handler.

    Returns the app AND the AsyncMock installed at ``app.state.task_router`` so the
    test can introspect enqueue calls (Plan 26-12 refactor: handler now reads from
    ``request.app.state.task_router`` instead of constructing a Queue inline).
    """
    app = FastAPI(title="agent-files-smoke", version="test")
    app.include_router(agent_files.router)
    app.dependency_overrides[get_session] = lambda: session
    mock_router = AsyncMock()
    app.state.task_router = mock_router
    return app, mock_router


@pytest_asyncio.fixture
async def smoke_app_and_router(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> AsyncGenerator[tuple[AsyncClient, AsyncMock]]:
    """Smoke-app fixture exposing both the test client AND the mock task_router.

    Tests that need to assert against enqueue calls (e.g.,
    ``test_no_auto_enqueue_on_insert``) consume this fixture; tests that
    only care about the HTTP response can use ``authenticated_client`` below,
    which is a thin wrapper that drops the router handle.

    Phase 27 D-09/D-18: the upsert handler now resolves the calling agent's
    LIVE sentinel batch when ``batch_id`` is omitted on the wire. The Phase 24
    invariant says one is seeded at agent-registration time; ``seed_test_agent``
    pre-dates that flow, so we add the sentinel here to keep Phase 25/26 tests
    behaviorally unchanged (no contract regression).
    """
    agent, raw_token = seed_test_agent
    # Phase 27 D-09/D-18: pre-seed the LIVE sentinel so the upsert handler's
    # absent-batch_id branch resolves it cleanly. Mirrors the Phase 24 D-11
    # agent-registration side effect.
    session.add(
        ScanBatch(
            agent_id=agent.id,
            scan_path="<watcher>",
            status=ScanStatus.LIVE.value,
            total_files=0,
            processed_files=0,
        ),
    )
    await session.commit()
    app, mock_router = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        yield ac, mock_router


@pytest_asyncio.fixture
async def authenticated_client(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
) -> AsyncGenerator[AsyncClient]:
    """LOCAL OVERRIDE of conftest.authenticated_client: drops the router handle for tests that don't need it.

    Replaces the conftest version (which uses ``create_app()`` and therefore lacks
    the agent_files router until Plan 06). Same Authorization header convention.
    """
    client, _ = smoke_app_and_router
    yield client


def _make_record(path: str = "/test/music/a.mp3", ext: str = "mp3", size: int = 100) -> dict[str, object]:
    return {
        "sha256_hash": "0" * 64,
        "original_path": path,
        "original_filename": path.rsplit("/", 1)[-1],
        "current_path": path,
        "file_type": ext,
        "file_size": size,
    }


@pytest.mark.asyncio
async def test_upsert_happy_path(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, _ = seed_test_agent
    response = await authenticated_client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent.id
    assert body["upserted"] == 1
    assert body["inserted"] == 1
    # Phase 35 (D-06): discovery no longer auto-enqueues -- `enqueued` is always 0.
    assert body["enqueued"] == 0
    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_replay_no_duplicates(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    r1 = await authenticated_client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    r2 = await authenticated_client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    assert r1.status_code == 200
    assert r2.status_code == 200
    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_rescan_bumps_updated_at_not_created_at(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """phaze-7634: a conflicting rescan (same agent_id + original_path) bumps FileRecord.updated_at;
    created_at stays pinned.

    Same defect class as phaze-c8nz: `on_conflict_do_update`'s `set_` clause used to omit
    `updated_at`, and `TimestampMixin.updated_at`'s ORM `onupdate` hook never fires for a Core
    upsert -- so a rescanned row kept reporting the FIRST-discovery timestamp forever. Backdate
    both columns, re-POST the same record, and assert updated_at moves forward while created_at
    is untouched.
    """
    record = _make_record()
    r1 = await authenticated_client.post("/api/internal/agent/files", json={"files": [record]})
    assert r1.status_code == 200, r1.text

    # Backdate created_at/updated_at directly (bypassing the ORM/onupdate hook) to a fixed point
    # well in the past. Bind a tz-AWARE value: since phaze-cz3m / migration 049 every timestamp
    # column is timestamptz, and a NAIVE datetime bound to one is silently reinterpreted as the
    # session's local time rather than UTC -- a same-shape defect to the one 049 fixed.
    outage_time = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=12)
    await session.execute(
        update(FileRecord).where(FileRecord.original_path == record["original_path"]).values(created_at=outage_time, updated_at=outage_time)
    )
    await session.commit()

    before_rescan = datetime.now(UTC)

    r2 = await authenticated_client.post("/api/internal/agent/files", json={"files": [record]})
    assert r2.status_code == 200, r2.text

    session.expire_all()
    row = (await session.execute(select(FileRecord).where(FileRecord.original_path == record["original_path"]))).scalar_one()
    assert row.created_at == outage_time, "created_at must stay pinned to the first-discovery value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_rescan - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


@pytest.mark.asyncio
async def test_no_auto_enqueue_on_insert(smoke_app_and_router: tuple[AsyncClient, AsyncMock], seed_test_agent: tuple[Agent, str]) -> None:
    """Phase 35 (D-06): INSERTed music/video rows are NO LONGER auto-enqueued for extraction."""
    client, mock_router = smoke_app_and_router
    chunk = {"files": [_make_record(path="/test/music/a.mp3"), _make_record(path="/test/music/b.mp3")]}
    response = await client.post("/api/internal/agent/files", json=chunk)
    assert response.status_code == 200
    body = response.json()
    assert body["inserted"] == 2
    assert body["enqueued"] == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_enqueue_for_updates(smoke_app_and_router: tuple[AsyncClient, AsyncMock], seed_test_agent: tuple[Agent, str]) -> None:
    client, mock_router = smoke_app_and_router
    chunk = {"files": [_make_record()]}
    r1 = await client.post("/api/internal/agent/files", json=chunk)
    assert r1.status_code == 200
    # Phase 35 (D-06): no enqueue on INSERT either.
    assert mock_router.enqueue_for_agent.await_count == 0
    r2 = await client.post("/api/internal/agent/files", json=chunk)
    assert r2.status_code == 200
    assert mock_router.enqueue_for_agent.await_count == 0
    body = r2.json()
    assert body["inserted"] == 0
    assert body["upserted"] == 1
    assert body["enqueued"] == 0


@pytest.mark.asyncio
async def test_extra_body_field_422(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
    bad_record = {**_make_record(), "agent_id": "evil"}
    response = await authenticated_client.post("/api/internal/agent/files", json={"files": [bad_record]})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc"))[:4] == ["body", "files", 0, "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_agent_id_in_body_rejected(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
    response = await authenticated_client.post(
        "/api/internal/agent/files",
        json={"agent_id": "evil", "files": [_make_record()]},
    )
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_chunk_cap_exceeded_422(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
    chunk = {"files": [_make_record(path=f"/test/music/{i:04d}.mp3") for i in range(1001)]}
    response = await authenticated_client.post("/api/internal/agent/files", json=chunk)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_missing_auth_returns_401(client: AsyncClient) -> None:
    """AUTH-01 reaffirmed on the production route; lights up green AFTER Plan 06 wires main.py."""
    response = await client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    # Until Plan 06 wires the router, this returns 404. After wiring it returns 401.
    assert response.status_code in (401, 404)
    if response.status_code == 401:
        assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_file_size_int64_overflow_422s_without_persisting(
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-ty0o: a ``file_size`` beyond int8 is rejected 422, never a Postgres ``NumericValueOutOfRange``.

    ``files.file_size`` is ``BigInteger`` (int8, max 9223372036854775807). Pre-fix ``file_size`` only
    carried ``ge=0`` -- an unbounded Pydantic int one past int8 max would have reached Postgres and
    aborted the transaction instead of failing cleanly at the wire.
    """
    bad_record = _make_record(size=9223372036854775808)
    response = await authenticated_client.post("/api/internal/agent/files", json={"files": [bad_record]})
    assert response.status_code == 422, response.text
    assert "file_size" in response.text
    assert "less_than_equal" in response.text, response.text

    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 0, "a rejected (422) upsert must not persist any FileRecord row"


@pytest.mark.asyncio
async def test_upsert_populates_repaired_filename_when_mojibake(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """phaze-x4ux: a mojibake filename gets `original_filename_repaired` set at ingest.

    `original_filename` itself stays byte-faithful (untouched) -- only the derived column is
    populated with the repaired text.
    """
    record = _make_record(path="/test/music/timewarp.mp3")
    record["original_filename"] = "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven VÃƒÂ¤th - LIVE @ Timewarp 2003.mp3"
    response = await authenticated_client.post("/api/internal/agent/files", json={"files": [record]})
    assert response.status_code == 200, response.text

    stored = (await session.execute(select(FileRecord))).scalar_one()
    assert stored.original_filename == "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven VÃƒÂ¤th - LIVE @ Timewarp 2003.mp3"
    assert stored.original_filename_repaired == "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven Väth - LIVE @ Timewarp 2003.mp3"


@pytest.mark.asyncio
async def test_upsert_populates_repaired_filename_as_no_op_when_clean(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """A clean filename still gets `original_filename_repaired` set, equal to the original."""
    response = await authenticated_client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    assert response.status_code == 200, response.text

    stored = (await session.execute(select(FileRecord))).scalar_one()
    assert stored.original_filename_repaired == stored.original_filename


@pytest.mark.asyncio
async def test_same_chunk_duplicate_paths_dedup(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    rec1 = _make_record(path="/test/music/dup.mp3")
    rec2 = {**_make_record(path="/test/music/dup.mp3"), "file_size": 999}
    response = await authenticated_client.post("/api/internal/agent/files", json={"files": [rec1, rec2]})
    assert response.status_code == 200, response.text
    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_upsert_files_locks_in_original_path_order_not_request_order(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """phaze-zfxy6: the multi-row ``ON CONFLICT DO UPDATE`` must lock rows in ``original_path``
    order, not chunk/directory-walk order.

    ``delete_scan_cascade`` (services/scan_deletion.py) locks a batch's file rows FOR UPDATE
    sorted by ``original_path`` -- the only column both sides of this cascade-vs-upsert race can
    share (the cascade only has ``batch_id``; this endpoint's natural key is
    ``(agent_id, original_path)``). If this statement's VALUES rows are NOT sorted the same way,
    the two multi-row lockers can acquire locks over an overlapping row set in different orders
    -- the classic ABBA deadlock. The request below is deliberately reverse-alphabetical to
    prove the handler sorts rather than passing chunk order straight to Postgres.

    phaze-ldu8l: this used to assert ``SELECT original_path FROM files ORDER BY ctid`` --
    physical tuple placement -- as a proxy for VALUES processing order, on the premise that "for
    a same-table INSERT with no parallel workers, physical (ctid) insertion order follows VALUES
    processing order". That premise only holds on a heap with no reusable free space. Every test
    in this suite runs inside an outer transaction + SAVEPOINT rolled back at teardown
    (tests/conftest.py), and 153 test files write to ``files``, so across a long session the
    free-space map accumulates reusable slots and a later multi-row INSERT can land in reused
    space instead of strictly appending -- ctid order then no longer reflects VALUES order.
    Measured: this assertion failed ~30% into a full-suite run while passing standalone and
    passing for its whole module, on an otherwise-unmodified tree. Worse, the failure is
    symmetric: an UNSORTED handler could also happen to produce a ctid order that looks sorted
    on a fresh, low-churn table, so a PASS never actually proved the handler sorts either. ctid
    is a storage detail, not the lock-acquisition order, and standing in for either was unsound.

    The direct, heap-state-independent replacement: capture the compiled bind parameters of the
    actual ``INSERT ... VALUES (...), (...), ...`` statement sent to Postgres (a
    ``before_cursor_execute`` listener on the test's own connection -- ``session.bind`` is the
    single per-test connection every fixture and the app share, see tests/conftest.py's D-07
    wiring) and read the ``original_path`` value out of each VALUES row in the order those rows
    appear IN THE STATEMENT. SQLAlchemy names each row's compiled bind params
    ``<column>_m<row-index>`` (``original_path_m0``, ``original_path_m1``, ...) when a statement
    is built from a list of value dicts -- that row-index is literally the VALUES-clause order,
    independent of anything Postgres does with the data afterwards. This is what the handler
    actually sends: it fails the instant ``agent_files.py`` stops sorting (verified by
    temporarily deleting the ``sorted(...)`` call: this assertion goes red), and it cannot pass
    for the wrong reason the way the ctid proxy could.

    The cascade side already carries an equivalent statement-text guard --
    ``test_cascade_locks_file_rows_in_original_path_order`` in
    tests/discovery/services/test_scan_deletion.py asserts ``"ORDER BY files.original_path" in``
    the compiled ``FOR UPDATE`` statement via a spy on ``session.execute`` -- so both halves of
    this ABBA pair are now pinned by a direct assertion on the issued SQL, not a storage proxy.
    """
    captured_params: list[dict[str, object]] = []
    sync_conn = session.bind.sync_connection

    def _capture_insert_params(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: Any,
        executemany: bool,
    ) -> None:
        if "INSERT INTO files" in statement:
            captured_params.extend(context.compiled_parameters)

    event.listen(sync_conn, "before_cursor_execute", _capture_insert_params)
    try:
        reverse_alpha_paths = ["/test/music/z.mp3", "/test/music/m.mp3", "/test/music/b.mp3", "/test/music/a.mp3"]
        records = [_make_record(path=p) for p in reverse_alpha_paths]
        response = await authenticated_client.post("/api/internal/agent/files", json={"files": records})
        assert response.status_code == 200, response.text
    finally:
        event.remove(sync_conn, "before_cursor_execute", _capture_insert_params)

    assert len(captured_params) == 1, f"expected exactly one multi-row INSERT INTO files, captured {len(captured_params)}"
    (params,) = captured_params
    row_keys = sorted(
        (k for k in params if k.startswith("original_path_m")),
        key=lambda k: int(k.removeprefix("original_path_m")),
    )
    assert len(row_keys) == len(reverse_alpha_paths), f"expected {len(reverse_alpha_paths)} VALUES rows, found {row_keys}"

    values_clause_order = [params[k] for k in row_keys]
    assert values_clause_order == sorted(reverse_alpha_paths), (
        f"the INSERT's VALUES rows must be original_path-sorted for lock-order parity with "
        f"delete_scan_cascade's FOR UPDATE sweep; got {values_clause_order}"
    )


@pytest.mark.asyncio
async def test_no_enqueue_for_non_music_file_type(smoke_app_and_router: tuple[AsyncClient, AsyncMock], seed_test_agent: tuple[Agent, str]) -> None:
    """Non-music/video file types (e.g., .txt, .jpg) INSERT cleanly and never enqueue (D-06)."""
    client, mock_router = smoke_app_and_router
    chunk = {
        "files": [
            _make_record(path="/test/docs/readme.txt", ext="txt"),
            _make_record(path="/test/docs/cover.jpg", ext="jpg"),
        ],
    }
    response = await client.post("/api/internal/agent/files", json=chunk)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inserted"] == 2
    assert body["enqueued"] == 0
    mock_router.enqueue_for_agent.assert_not_awaited()
