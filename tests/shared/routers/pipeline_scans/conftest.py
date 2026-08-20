"""Local fixtures for the split `tests/shared/routers/pipeline_scans/test_*.py` suite.

`smoke` lives in a real `conftest.py` (rather than `_shared.py` alongside the other non-fixture
helpers) so pytest auto-discovers it for every file in this package without an explicit import --
several split files use it as a directly-requested (non-autouse) fixture parameter, and importing
a same-named object into a module that also declares a `smoke` parameter trips ruff's F811
(parameter shadows the import). `_make_smoke_app` stays in `_shared.py`: it is also called
directly (not just via this fixture) by the NFD-scan-root tests in `test_path_and_auth.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_asyncio

from tests.shared.routers.pipeline_scans._shared import Agent, ASGITransport, AsyncClient, _make_smoke_app


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def smoke(session: AsyncSession) -> AsyncGenerator[tuple[AsyncClient, AsyncMock]]:
    """Smoke client + mock task_router; seeds one non-revoked agent with scan_roots."""
    # Seed a known test agent. Use a kebab-case slug compatible with the
    # Agent.id_charset check constraint.
    agent = Agent(
        id="test-agent",
        name="Test Agent",
        token_hash=None,
        scan_roots=["/data/music", "/data/videos"],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    app, mock_router = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, mock_router
