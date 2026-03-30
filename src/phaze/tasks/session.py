"""Shared async session factory for arq task functions."""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from phaze.config import settings


async def get_task_session() -> AsyncSession:
    """Create a one-off async session for arq task use.

    Workers don't share the FastAPI app's engine. Each task creates
    its own lightweight session.
    """
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
    session: AsyncSession = async_session()
    return session
