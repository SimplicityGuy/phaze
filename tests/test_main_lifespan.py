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

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
import pytest

from tests._queue_fakes import FakeQueue


if TYPE_CHECKING:
    from types import ModuleType


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
    # register_before_enqueue is synchronous on a real saq.Queue -- use a sync mock
    # so the lifespan call doesn't leave an un-awaited coroutine.
    fake_queue.register_before_enqueue = MagicMock()
    fake_queue_cls = MagicMock()
    fake_queue_cls.from_url = MagicMock(return_value=fake_queue)
    monkeypatch.setattr(main_module, "Queue", fake_queue_cls)

    fake_router = AsyncMock()
    fake_router.close = AsyncMock()
    monkeypatch.setattr(main_module, "AgentTaskRouter", lambda redis_url: fake_router)  # noqa: ARG005

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

    # Phase 30: the controller queue is constructed named and gets the policy hook.
    fake_queue_cls.from_url.assert_called_once()
    _from_url_args, from_url_kwargs = fake_queue_cls.from_url.call_args
    assert from_url_kwargs.get("name") == "controller", f"controller queue must be named 'controller'; got {from_url_kwargs!r}"
    fake_queue.register_before_enqueue.assert_called_once()

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
    agents: list[object],
) -> tuple[MagicMock, MagicMock]:
    """Apply the shared lifespan monkeypatches for the SAQ-mount tests.

    Boots ``phaze.main`` in-process with no DB / Redis / SAQ network access. The controller
    queue double exposes a real ``str`` ``name == "controller"`` (so ``saq_web`` keys its
    registry on it), a sync ``register_before_enqueue``, an async ``disconnect``, and an async
    ``info`` returning a ``QueueInfo``-shaped dict so the dashboard renders. ``task_router``'s
    ``queue_for`` is a MagicMock returning one ``FakeQueue("phaze-agent-nox")`` (Wave 0 double
    with async ``info``), and the patched ``async_session``'s ``execute`` is an AsyncMock whose
    ``.scalars().all()`` yields ``agents`` (the non-revoked-agent enumeration the lifespan runs).

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

    # async_session is used twice in the lifespan: by ensure_dev_agent (patched to a no-op) and
    # by the SAQ-mount block, which does ``await session.execute(stmt)`` then ``.scalars().all()``.
    class _FakeSessionCM:
        async def __aenter__(self) -> object:
            session = MagicMock()
            session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=agents)))))
            return session

        async def __aexit__(self, *_a: object) -> None:
            pass

    monkeypatch.setattr(main_module, "run_migrations", _noop_migrations)
    monkeypatch.setattr(main_module, "ensure_dev_agent", _noop_ensure_dev_agent)
    monkeypatch.setattr(main_module, "engine", fake_engine)
    monkeypatch.setattr(main_module, "async_session", _FakeSessionCM)

    # Controller queue double: real str name + async info so saq_web keys/renders it.
    controller_queue = MagicMock()
    controller_queue.name = "controller"
    controller_queue.register_before_enqueue = MagicMock()
    controller_queue.disconnect = AsyncMock()
    controller_queue.info = AsyncMock(return_value={"workers": {}, "name": "controller", "queued": 0, "active": 0, "scheduled": 0, "jobs": []})
    fake_queue_cls = MagicMock()
    fake_queue_cls.from_url = MagicMock(return_value=controller_queue)
    monkeypatch.setattr(main_module, "Queue", fake_queue_cls)

    # task_router.queue_for -> the cached per-agent Queue (here a Wave 0 FakeQueue with .info).
    fake_router = MagicMock()
    fake_router.close = AsyncMock()
    fake_router.queue_for = MagicMock(return_value=FakeQueue("phaze-agent-nox"))
    monkeypatch.setattr(main_module, "AgentTaskRouter", lambda redis_url: fake_router)  # noqa: ARG005

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
        assert set(saq_starlette.QUEUES.keys()) == {"controller", "phaze-agent-nox"}
        # No second pool: the registry holds the SAME controller instance wired on app.state.
        assert saq_starlette.QUEUES["controller"] is app.state.controller_queue
        assert saq_starlette.QUEUES["controller"] is controller_queue
    # The per-agent queue came from task_router.queue_for("nox") (the cached enqueue-path instance).
    assert task_router.queue_for.call_args_list == [(("nox",), {})]


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
