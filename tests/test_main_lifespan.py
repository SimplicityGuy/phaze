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

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
import pytest


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
            return MagicMock()

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
    monkeypatch.setattr(main_module, "Queue", MagicMock(from_url=lambda _url: fake_queue))

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
        pass

    # Migration must precede the engine SELECT 1 (which precedes ensure_dev_agent).
    assert "run_migrations" in call_order, f"run_migrations not invoked: {call_order!r}"
    assert "ensure_dev_agent" in call_order, f"ensure_dev_agent not invoked: {call_order!r}"
    assert call_order.index("run_migrations") < call_order.index("engine.begin"), (
        f"migrations must run BEFORE engine.begin(SELECT 1); order={call_order!r}"
    )
    assert call_order.index("engine.begin") < call_order.index("ensure_dev_agent"), (
        f"ensure_dev_agent must run AFTER engine connectivity check; order={call_order!r}"
    )
