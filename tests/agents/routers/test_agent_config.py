"""Tests for GET /api/internal/agent/config (phaze-mvq8z.9, ADR-0019 (runtime config hot-reload) §14).

Mirrors ``tests/shared/routers/test_admin_runtime_config.py``'s process-wide-store wiring: the
route reads through the module-level ``get_runtime_config_store()`` singleton, so
``_wire_runtime_config_store`` rebuilds it fresh per test and binds its ``override_provider`` to
THIS test's own session (the same session ``authenticated_client``'s ``get_session`` override
hands the route), and tears the singleton down afterward so no override leaks into an unrelated
test module.

Auth is the same bearer-token dependency every ``/api/internal/agent/*`` route uses
(``get_authenticated_agent``) -- the 401/403 cases here mirror
``tests/agents/routers/test_agent_heartbeat.py``'s coverage of that dependency rather than
re-deriving it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import update
from sqlalchemy.sql import func as sa_func

from phaze.config import ControlSettings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.runtime_config_override import RuntimeConfigOverride
from phaze.routers.agent_config import router as agent_config_router
from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore, get_runtime_config_store
from phaze.schemas.agent_config import compute_overrides_digest
from phaze.services.agent_client import AgentApiServerError, PhazeAgentClient
from phaze.services.runtime_config_overrides import get_runtime_config_overrides
from phaze.tasks.heartbeat import _poll_runtime_config


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_config_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest.fixture(autouse=True)
def _wire_runtime_config_store(session: AsyncSession):  # type: ignore[no-untyped-def]
    """Fresh global store per test, its override provider bound to THIS test's own session."""
    get_runtime_config_store.cache_clear()
    store = get_runtime_config_store()
    store.set_override_provider(lambda: get_runtime_config_overrides(session))
    yield store
    get_runtime_config_store.cache_clear()


@pytest.mark.asyncio
async def test_missing_bearer_returns_401(session: AsyncSession) -> None:
    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/internal/agent/config")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_revoked_agent_returns_403(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, raw_token = seed_test_agent
    await session.execute(update(Agent).where(Agent.id == agent.id).values(revoked_at=sa_func.now()))
    await session.commit()

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.get("/api/internal/agent/config")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_no_overrides_returns_empty_map_with_matching_digest(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    _agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.get("/api/internal/agent/config")

    assert response.status_code == 200
    body = response.json()
    assert body["overrides"] == {}
    assert body["digest"] == compute_overrides_digest({})


@pytest.mark.asyncio
async def test_reloadable_override_is_reported(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """An override set through the DB layer (mirrors the admin API's write) is visible here."""
    _agent, raw_token = seed_test_agent
    session.add(RuntimeConfigOverride(key="worker_max_jobs", value=12))
    await session.flush()

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.get("/api/internal/agent/config")

    assert response.status_code == 200
    body = response.json()
    assert body["overrides"] == {"worker_max_jobs": 12}
    assert body["digest"] == compute_overrides_digest({"worker_max_jobs": 12})


@pytest.mark.asyncio
async def test_non_reloadable_override_row_is_filtered_out_defensively(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A key that should never be in this table (the admin router previews before writing) is still
    dropped here rather than trusted -- this endpoint proves its own claim about RELOADABLE_KEYS
    rather than inheriting it from an upstream caller it cannot see (module docstring).
    """
    _agent, raw_token = seed_test_agent
    session.add(RuntimeConfigOverride(key="database_url", value="postgresql://not-a-real-override"))
    session.add(RuntimeConfigOverride(key="worker_max_jobs", value=3))
    await session.flush()

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.get("/api/internal/agent/config")

    assert response.status_code == 200
    body = response.json()
    assert body["overrides"] == {"worker_max_jobs": 3}


@pytest.mark.asyncio
async def test_a_failed_override_read_is_a_5xx_and_the_agent_keeps_its_last_good_override(
    seed_test_agent: tuple[Agent, str], session: AsyncSession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """phaze-mvq8z.19 finding 1, the agent half, driven through the agent's REAL consumer
    (``PhazeAgentClient.get_config`` + the heartbeat's ``_poll_runtime_config``).

    A DB failure reading the override table must not be served as an empty override set: that
    comes with a NEW digest, so every polling agent would reload and drop every override it had.
    The endpoint fails instead (5xx), the client raises, and the agent's store keeps last-good.
    """
    _agent, raw_token = seed_test_agent
    session.add(RuntimeConfigOverride(key="worker_max_jobs", value=12))
    await session.flush()

    app = _make_smoke_app(session)
    http = AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test", headers={"Authorization": f"Bearer {raw_token}"}
    )

    async def _no_backoff(_delay: float) -> None:
        return None

    client = PhazeAgentClient("http://test", raw_token, _client=http, _retry_sleep=_no_backoff)
    store = RuntimeConfigStore(ControlSettings(), runtime_toml=tmp_path / RUNTIME_TOML_NAME, physical_cores=lambda: 64)
    await store.reload("startup")
    ctx: dict[str, Any] = {"runtime_config_store": store}
    try:
        await _poll_runtime_config(ctx, client)
        assert store.current().worker_max_jobs == 12
        before = store.snapshot()

        real_execute = session.execute

        async def _override_read_fails(statement: Any, *args: Any, **kwargs: Any) -> Any:
            # Only the override table's read fails -- agent authentication still reads the DB.
            if "runtime_config_override" in str(statement):
                raise RuntimeError("simulated DB failure")
            return await real_execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", _override_read_fails)
        with pytest.raises(AgentApiServerError):
            await client.get_config()
        await _poll_runtime_config(ctx, client)

        assert store.snapshot() is before
        assert store.current().worker_max_jobs == 12
    finally:
        await client.close()
