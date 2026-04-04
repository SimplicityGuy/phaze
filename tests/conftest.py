"""Shared test fixtures for Phaze test suite."""

from collections.abc import AsyncGenerator

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.base import Base


TEST_DATABASE_URL = "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test"

DB_FIXTURES = {"async_engine", "session", "client"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark tests that use database fixtures as integration tests."""
    for item in items:
        if DB_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)


@pytest_asyncio.fixture
async def async_engine():  # type: ignore[no-untyped-def]
    """Create async engine, set up tables, yield, then tear down."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(async_engine) -> AsyncGenerator[AsyncSession]:  # type: ignore[no-untyped-def]
    """Yield an async database session for testing."""
    async_session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(session) -> AsyncGenerator[AsyncClient]:  # type: ignore[no-untyped-def]
    """Yield an async HTTP test client with database session override."""
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
