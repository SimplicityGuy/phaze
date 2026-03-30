"""Tests for the shared task session module."""

import pytest

from phaze.tasks.session import get_task_session


@pytest.mark.asyncio
async def test_get_task_session_returns_async_session():
    """get_task_session returns an AsyncSession instance."""
    session = await get_task_session()
    try:
        assert session is not None
        # Check it's an AsyncSession (has execute method)
        assert hasattr(session, "execute")
        assert hasattr(session, "commit")
    finally:
        await session.close()
