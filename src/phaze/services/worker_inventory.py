"""Shared inventory definitions for the Agents heading and shell summary.

Inventory includes idle clusters and offline agents; it is separate from the
activity counters that gate work dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, func, or_, select
import structlog

from phaze.config import get_settings
from phaze.models.agent import Agent
from phaze.runtime_config import effective_settings
from phaze.services.agent_liveness import non_local_backend_kinds


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


def visible_agent_clause() -> ColumnElement[bool]:
    """Hide revoked agents and never-heartbeating compute callback identities."""
    return and_(Agent.revoked_at.is_(None), or_(Agent.kind != "compute", Agent.last_seen_at.is_not(None)))


async def count_visible_agents(session: AsyncSession) -> int | None:
    """Count the same rows as the Agents table without loading their ORM objects.

    An unavailable read is distinct from an empty inventory. A savepoint protects
    already-loaded objects on the shared dashboard session from a failed query.
    """
    try:
        async with session.begin_nested():
            return int((await session.execute(select(func.count(Agent.id)).where(visible_agent_clause()))).scalar_one())
    except Exception:
        logger.warning("worker_inventory_agents_unavailable", exc_info=True)
        return None


def configured_backend_counts() -> tuple[int, int] | None:
    """Return (all lanes, configured clusters), without reading job activity."""
    try:
        settings = cast("ControlSettings", effective_settings(get_settings()))
        return len(settings.backends), len(non_local_backend_kinds(settings))
    except Exception:
        logger.warning("worker_inventory_backends_unavailable", exc_info=True)
        return None
