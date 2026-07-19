"""Lifespan integration tests for phaze.main (Phase 27 UAT Gap 2 + Gap 3).

Verifies that:

1. The api lifespan startup invokes ``phaze.database.run_migrations`` BEFORE
   the queue / task_router / redis are wired -- i.e., migrations land before
   any handler is reachable.
2. The api lifespan also calls ``ensure_dev_agent`` after migrations succeed,
   so a fresh ``agents`` table gets a seeded row.

The tests monkeypatch heavyweight constructors (Redis, SAQ Queue,
AgentTaskRouter, the database engine) so the FastAPI app can boot in-process
without external services. The assertions focus on the **call order** and
**call count**, not on real DB schema state -- that's covered by
``tests/test_migrations/`` against a real Postgres.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.agent import Agent
from tests._queue_fakes import FakeQueue


if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.ext.asyncio import AsyncConnection


@pytest.mark.asyncio
async def test_api_lifespan_runs_migrations_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_migrations`` is invoked at lifespan startup BEFORE engine SELECT 1."""
    import phaze.main as main_module

    call_order: list[str] = []

    async def _record_migrations() -> None:
        call_order.append("run_migrations")

    async def _record_ensure_dev_agent(_session: object) -> None:
        call_order.append("ensure_dev_agent")
        return None

    # Track engine.begin() so we can verify ordering relative to migrations.
    async def _engine_begin() -> object:  # pragma: no cover -- patched at use
        raise AssertionError("not used")

    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock()

    class _FakeBeginCM:
        async def __aenter__(self) -> object:
            call_order.append("engine.begin")
            return fake_conn

        async def __aexit__(self, *_a: object) -> None:
            pass

    def _fake_begin() -> _FakeBeginCM:
        return _FakeBeginCM()

    # Engine.dispose is awaited at shutdown.
    fake_engine = MagicMock()
    fake_engine.begin = _fake_begin
    fake_engine.dispose = AsyncMock()

    # Wrap async_session so the ensure_dev_agent bootstrap call uses our recorder
    # rather than touching Postgres.
    class _FakeSessionCM:
        async def __aenter__(self) -> object:
            session = MagicMock()
            # Phase 33: the lifespan's SAQ-mount block runs ``await session.execute(stmt)``
            # then ``.scalars().all()`` to enumerate non-revoked agents (enable_saq_ui defaults
            # True). A plain ``MagicMock().execute(stmt)`` is NOT awaitable, so wire an AsyncMock
            # whose ``.scalars().all()`` yields zero agents -> only the controller queue mounts.
            # This keeps SAQ-mount ON in this migration-order test's path; all migration-order
            # assertions below stay intact (this is a compatibility edit, not a weakening).
            session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))
            return session

        async def __aexit__(self, *_a: object) -> None:
            pass

    def _fake_async_session() -> _FakeSessionCM:
        return _FakeSessionCM()

    # Phase 27 UAT Gap 2 + 3 patches.
    monkeypatch.setattr(main_module, "run_migrations", _record_migrations)
    monkeypatch.setattr(main_module, "ensure_dev_agent", _record_ensure_dev_agent)
    monkeypatch.setattr(main_module, "engine", fake_engine)
    monkeypatch.setattr(main_module, "async_session", _fake_async_session)

    # Stub the heavyweight side-effect constructors so lifespan doesn't try to
    # open real network connections.
    fake_queue = AsyncMock()
    fake_queue.disconnect = AsyncMock()
    # Phase 36: the lifespan opens the broker pool (await connect()); AsyncMock auto-provides it.
    fake_queue.connect = AsyncMock()
    # Phase 36: the controller queue is built via build_pipeline_queue (PostgresQueue factory),
    # which owns before_enqueue hook registration -- the lifespan no longer registers hooks.
    fake_build = MagicMock(return_value=fake_queue)
    monkeypatch.setattr(main_module, "build_pipeline_queue", fake_build)

    fake_router = AsyncMock()
    fake_router.close = AsyncMock()
    monkeypatch.setattr(main_module, "AgentTaskRouter", lambda **_kw: fake_router)

    fake_redis = AsyncMock()
    fake_redis.aclose = AsyncMock()
    monkeypatch.setattr(main_module, "redis_async", MagicMock(Redis=MagicMock(from_url=lambda _url, decode_responses: fake_redis)))  # noqa: ARG005

    app = main_module.create_app()
    # Driving the lifespan via TestClient(__enter__) is the FastAPI-recommended
    # way to exercise startup events in-process.
    with TestClient(app):
        # Phase 30: the named ``controller`` queue replaces the unnamed default
        # queue. Assert the new app.state shape WHILE the lifespan is active.
        assert hasattr(app.state, "controller_queue"), "controller_queue must be wired at startup"
        assert app.state.controller_queue is fake_queue
        assert not hasattr(app.state, "queue"), "the unnamed default queue (app.state.queue) must be gone (Phase 30)"

    # Phase 36: the controller queue is built via build_pipeline_queue with the name
    # "controller" as its first positional arg. The factory (covered by test_queue_factory.py)
    # owns before_enqueue hook registration, so the lifespan itself no longer registers hooks.
    fake_build.assert_called_once()
    assert fake_build.call_args.args[0] == "controller", f"controller queue must be named 'controller'; got {fake_build.call_args!r}"

    # Migration must precede the engine SELECT 1 (which precedes ensure_dev_agent).
    assert "run_migrations" in call_order, f"run_migrations not invoked: {call_order!r}"
    assert "ensure_dev_agent" in call_order, f"ensure_dev_agent not invoked: {call_order!r}"
    assert call_order.index("run_migrations") < call_order.index("engine.begin"), (
        f"migrations must run BEFORE engine.begin(SELECT 1); order={call_order!r}"
    )
    assert call_order.index("engine.begin") < call_order.index("ensure_dev_agent"), (
        f"ensure_dev_agent must run AFTER engine connectivity check; order={call_order!r}"
    )


# ---------------------------------------------------------------------------
# Phase 33: SAQ monitoring dashboard mounted at /saq inside the lifespan.
# ---------------------------------------------------------------------------


def _patch_saq_lifespan(
    monkeypatch: pytest.MonkeyPatch,
    main_module: ModuleType,
    *,
    agents: list[object] | None,
) -> tuple[MagicMock, MagicMock]:
    """Apply the shared lifespan monkeypatches for the SAQ-mount tests.

    Boots ``phaze.main`` in-process with no DB / Redis / SAQ network access. The controller
    queue double exposes a real ``str`` ``name == "controller"`` (so ``saq_web`` keys its
    registry on it), a sync ``register_before_enqueue``, an async ``disconnect``, and an async
    ``info`` returning a ``QueueInfo``-shaped dict so the dashboard renders. ``task_router``'s
    ``queue_for`` is a MagicMock returning one ``FakeQueue("phaze-agent-nox")`` (Wave 0 double
    with async ``info``).

    When ``agents`` is a list, ``async_session`` is ALSO patched to a fake context manager whose
    ``execute`` is an AsyncMock returning that fixed list via ``.scalars().all()`` (the
    no-real-DB path used by most of these tests). When ``agents`` is ``None``, ``async_session``
    is left UNTOUCHED so the caller can bind ``main_module.async_session`` to a real (test-DB)
    session factory instead -- phaze-k1vy's compute-exclusion regression test needs the REAL
    ``Agent.kind == "fileserver"`` WHERE clause to execute against real seeded rows, not a
    hand-rolled double that would just echo back whatever list the test hands it (which would
    beg the question the test exists to answer).

    Returns ``(controller_queue, task_router)`` so a test can assert reuse / call args.
    """

    async def _noop_migrations() -> None:
        return None

    async def _noop_ensure_dev_agent(_session: object) -> None:
        return None

    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock()

    class _FakeBeginCM:
        async def __aenter__(self) -> object:
            return fake_conn

        async def __aexit__(self, *_a: object) -> None:
            pass

    fake_engine = MagicMock()
    fake_engine.begin = _FakeBeginCM
    fake_engine.dispose = AsyncMock()

    monkeypatch.setattr(main_module, "run_migrations", _noop_migrations)
    monkeypatch.setattr(main_module, "ensure_dev_agent", _noop_ensure_dev_agent)
    monkeypatch.setattr(main_module, "engine", fake_engine)

    if agents is not None:
        # async_session is used twice in the lifespan: by ensure_dev_agent (patched to a no-op)
        # and by the SAQ-mount block, which does ``await session.execute(stmt)`` then
        # ``.scalars().all()``.
        class _FakeSessionCM:
            async def __aenter__(self) -> object:
                session = MagicMock()
                session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents)))))
                return session

            async def __aexit__(self, *_a: object) -> None:
                pass

        monkeypatch.setattr(main_module, "async_session", _FakeSessionCM)

    # Controller queue double: real str name + async info so saq_web keys/renders it.
    # Phase 36: built via build_pipeline_queue (PostgresQueue factory); the lifespan opens the
    # broker pool via await connect().
    controller_queue = MagicMock()
    controller_queue.name = "controller"
    controller_queue.disconnect = AsyncMock()
    controller_queue.connect = AsyncMock()
    controller_queue.info = AsyncMock(return_value={"workers": {}, "name": "controller", "queued": 0, "active": 0, "scheduled": 0, "jobs": []})
    # Phase 36 (WR-01): shutdown closes the factory-attached cache_redis (await aclose()).
    controller_queue.cache_redis = AsyncMock()
    monkeypatch.setattr(main_module, "build_pipeline_queue", MagicMock(return_value=controller_queue))

    # quick-260707-dh1: the /saq mount now enumerates all_lane_queues(agent) + legacy_base_queue(agent)
    # (4 lane queues + the drain-visibility base). Each is a Wave 0 FakeQueue with .info + a no-op
    # async connect the lifespan opens before mounting /saq. Cached by name so the same instances
    # are reused across the two reader calls.
    fake_router = MagicMock()
    fake_router.close = AsyncMock()
    _lane_q_cache: dict[str, FakeQueue] = {}

    def _lane_q(agent_id: str, lane: str) -> FakeQueue:
        name = f"phaze-agent-{agent_id}" if lane == "" else f"phaze-agent-{agent_id}-{lane}"
        if name not in _lane_q_cache:
            _lane_q_cache[name] = FakeQueue(name)
        return _lane_q_cache[name]

    fake_router.all_lane_queues = MagicMock(side_effect=lambda aid: [_lane_q(aid, lane) for lane in ("analyze", "fingerprint", "meta", "io")])
    fake_router.legacy_base_queue = MagicMock(side_effect=lambda aid: _lane_q(aid, ""))
    monkeypatch.setattr(main_module, "AgentTaskRouter", lambda **_kw: fake_router)

    fake_redis = AsyncMock()
    fake_redis.aclose = AsyncMock()
    monkeypatch.setattr(main_module, "redis_async", MagicMock(Redis=MagicMock(from_url=lambda _url, decode_responses: fake_redis)))  # noqa: ARG005

    return controller_queue, fake_router


def _override_health_session(app: object) -> None:
    """Override ``get_session`` so ``GET /health`` (its ``SELECT 1``) needs no real Postgres.

    The raw ``create_app()`` used here lacks the conftest ``client`` fixture's DB override, so
    without this the health endpoint opens a real asyncpg connection and the request raises.
    """
    from phaze.database import get_session

    fake_session = MagicMock()
    fake_session.execute = AsyncMock()
    app.dependency_overrides[get_session] = lambda: fake_session  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_saq_mount_served_in_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enable_saq_ui True, the lifespan-mounted /saq is served and /health is unaffected."""
    import phaze.main as main_module

    _patch_saq_lifespan(monkeypatch, main_module, agents=[SimpleNamespace(id="nox", name="nox")])
    monkeypatch.setattr(main_module.settings, "enable_saq_ui", True)

    app = main_module.create_app()
    _override_health_session(app)
    with TestClient(app) as c:
        # Mount-in-lifespan is served (Starlette wraps its router by reference) AND the
        # existing routers are untouched.
        assert c.get("/saq/").status_code == 200
        assert c.get("/health").status_code == 200
    # A /saq Mount is present on the app router after startup.
    assert any(getattr(r, "path", None) == "/saq" for r in app.router.routes), "expected a /saq Mount after startup"


@pytest.mark.asyncio
async def test_saq_queues_assembled_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mount registry is the controller + one per-agent queue, built from the SAME instances."""
    import saq.web.starlette as saq_starlette

    import phaze.main as main_module

    controller_queue, task_router = _patch_saq_lifespan(monkeypatch, main_module, agents=[SimpleNamespace(id="nox", name="nox")])
    monkeypatch.setattr(main_module.settings, "enable_saq_ui", True)

    app = main_module.create_app()
    with TestClient(app):
        # saq_web stores the assembled queues in its module-global registry, keyed by name.
        # quick-260707-dh1: controller + the agent's 4 lane queues + the drain-visibility base.
        assert set(saq_starlette.QUEUES.keys()) == {
            "controller",
            "phaze-agent-nox-analyze",
            "phaze-agent-nox-fingerprint",
            "phaze-agent-nox-meta",
            "phaze-agent-nox-io",
            "phaze-agent-nox",
        }
        # No second pool: the registry holds the SAME controller instance wired on app.state.
        assert saq_starlette.QUEUES["controller"] is app.state.controller_queue
        assert saq_starlette.QUEUES["controller"] is controller_queue
    # The per-agent lane queues came from all_lane_queues("nox") + legacy_base_queue("nox").
    assert task_router.all_lane_queues.call_args_list == [(("nox",), {})]
    assert task_router.legacy_base_queue.call_args_list == [(("nox",), {})]


@pytest.mark.asyncio
async def test_saq_disabled_flag_skips_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enable_saq_ui False, no /saq route is registered and /health still serves."""
    import phaze.main as main_module

    _patch_saq_lifespan(monkeypatch, main_module, agents=[SimpleNamespace(id="nox", name="nox")])
    monkeypatch.setattr(main_module.settings, "enable_saq_ui", False)

    app = main_module.create_app()
    _override_health_session(app)
    with TestClient(app) as c:
        assert not any(getattr(r, "path", None) == "/saq" for r in app.router.routes), "no /saq Mount when the flag is off"
        assert c.get("/saq/").status_code == 404
        assert c.get("/health").status_code == 200


@pytest.mark.asyncio
async def test_saq_mount_excludes_compute_agents(session: AsyncSession, _db_connection: AsyncConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-k1vy: the ``/saq`` mount query must be scoped to ``kind == "fileserver"``.

    Regression for the 2026-07-17 "phantom queue" misread: ``kind="compute"`` agents (the
    bearer-token callback identities for kueue burst clusters, e.g. k8s-vox/k8s-xenolab) never
    run a SAQ worker -- their work bypasses SAQ entirely (KSUBMIT-06). Before the fix the mount
    query (``select(Agent).where(Agent.revoked_at.is_(None))``) enumerated EVERY non-revoked
    agent with no kind filter, so a registered compute agent got all 5 of its lane queues
    mounted (and a psycopg pool opened for each) despite NEVER having a SAQ worker attached --
    the dashboard showed 5 permanently-0/0/0 phantom queues per compute agent.

    Unlike the other tests in this module (which hand a canned agent list to a fake
    ``async_session`` double), THIS test seeds real ``Agent`` rows via the DB-backed ``session``
    fixture and rebinds ``main_module.async_session`` to a real session factory on the SAME
    per-test connection/transaction (mirroring the ``_route_stats_fanout`` idiom), so the
    lifespan's ACTUAL ``select(Agent).where(...)`` statement executes against real rows -- a
    fake double that just echoed back a hand-picked list would beg the question this regression
    test exists to answer.

    Seeds one compute agent (``k8s-vox``) and TWO fileserver agents (``nox``, ``lux``) so the
    acceptance criteria's three clauses are all provable in one assertion: fileserver agents
    keep getting their 5 queues each (including "a genuinely-registered second fileserver"),
    while the compute agent contributes NONE -- proving no pool is ever opened for it (the
    ``task_router.all_lane_queues``/``legacy_base_queue`` doubles are never even invoked with
    its id, so no ``FakeQueue.connect()`` call happens for it either). The ``async_engine``
    fixture (a transitive dependency via ``session`` -> ``_db_connection``) also seeds one
    permanent, real ``kind="fileserver"`` ``test-fileserver`` row for the whole test session, so
    the mounted-queue assertions below check for the expected names being PRESENT (a subset
    check) rather than exact-set equality -- this test does not own that row and must not assert
    away its existence.
    """
    import phaze.main as main_module

    session.add_all(
        [
            Agent(id="nox", name="nox", kind="fileserver", scan_roots=[]),
            Agent(id="lux", name="lux", kind="fileserver", scan_roots=[]),
            Agent(id="k8s-vox", name="k8s-vox", kind="compute", scan_roots=[]),
        ]
    )
    await session.flush()

    import saq.web.starlette as saq_starlette

    controller_queue, task_router = _patch_saq_lifespan(monkeypatch, main_module, agents=None)
    monkeypatch.setattr(main_module.settings, "enable_saq_ui", True)

    # Neutralise the HYG-01 background orphan-count refresher (incidental to this test): it opens
    # its OWN session via `phaze.services.pipeline`'s module-level `async_session` against the
    # SAME shared `_db_connection`, and a concurrent operation on that one asyncpg connection
    # while our own session is mid-query raises "another operation is in progress". Mirrors
    # `tests/integration/test_lifespan_orphan_task.py`'s "neutralise incidental collaborators"
    # idiom (there it's the target under test; here it's just noise this test doesn't own).
    async def _noop_orphan_loop() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "_orphan_refresh_loop", _noop_orphan_loop)

    # Bind main_module.async_session (the SAQ-mount block's own reference, imported at module
    # load time -- see main.py:16) to a REAL session factory on the same per-test connection the
    # `session` fixture just wrote to, so the lifespan's query sees the seeded-but-uncommitted
    # rows (same "create_savepoint" idiom `_route_stats_fanout` uses for the production fan-out).
    real_session_factory = async_sessionmaker(
        bind=_db_connection, class_=AsyncSession, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    monkeypatch.setattr(main_module, "async_session", real_session_factory)

    # Drive the lifespan directly (Starlette's own runner) in THIS test's event loop, rather than
    # via `TestClient` -- `TestClient` runs the lifespan on an `anyio` portal thread with its OWN
    # event loop, and an asyncpg connection is bound to the loop that opened it (the `_db_connection`
    # fixture's connection lives in the pytest-asyncio test loop), so a portal-thread session would
    # crash with "attached to a different loop" the moment it touched that connection. This mirrors
    # `tests/integration/test_lifespan_orphan_task.py`'s established idiom for the same reason.
    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        queue_names = set(saq_starlette.QUEUES.keys())
        assert "controller" in queue_names
        assert saq_starlette.QUEUES["controller"] is controller_queue
        for agent_id in ("nox", "lux"):
            for lane in ("analyze", "fingerprint", "meta", "io"):
                assert f"phaze-agent-{agent_id}-{lane}" in queue_names
            assert f"phaze-agent-{agent_id}" in queue_names
        # The compute agent contributes NO queue of any name -- not just its exact id, but any
        # queue name derived from it (defensive against a lane-naming scheme change).
        assert not any("k8s-vox" in name for name in queue_names)

    # The lane-queue builders were invoked for the two fileserver agents just seeded (plus
    # whatever pre-existing fileserver rows the shared fixtures seed) -- but NEVER for the
    # compute agent, proving no pool is ever opened for it.
    called_ids = {call.args[0] for call in task_router.all_lane_queues.call_args_list}
    assert {"nox", "lux"} <= called_ids
    assert "k8s-vox" not in called_ids
    legacy_called_ids = {call.args[0] for call in task_router.legacy_base_queue.call_args_list}
    assert {"nox", "lux"} <= legacy_called_ids
    assert "k8s-vox" not in legacy_called_ids
