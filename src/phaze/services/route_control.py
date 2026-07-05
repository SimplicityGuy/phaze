"""Degrade-safe reader for the Phase-71 force-local routing override (BEUI-02).

:func:`get_route_control` reads the single ``route_control`` ``'global'`` row and returns its
``force_local`` flag. It is on TWO hot paths -- the drain cron (``stage_cloud_window``) and the
duration-router gate (``routers.pipeline``) -- so a raise would 500 the routing gate or crash the
cron (T-71-03). It therefore mirrors :func:`phaze.services.pipeline.get_stage_controls`'s degrade
discipline EXACTLY: on an absent row it returns ``False`` (cloud-enabled), and on ANY DB exception it
logs, rolls back the aborted transaction (guarded, so a failed rollback cannot mask the original
error or poison later statements on the shared session), and returns ``False``. It NEVER raises.

Defaulting to ``False`` (cloud-enabled) on error is the fail-safe: a transient DB hiccup must not
silently flip the whole registry to force-local -- the override is an explicit operator action, so
its absence/unreadability degrades to the normal cloud-enabled behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from phaze.models.route_control import RouteControl


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


async def get_route_control(session: AsyncSession) -> bool:
    """Return the force-local flag: True iff the ``'global'`` row has ``force_local`` True.

    Absent row -> ``False`` (cloud-enabled). Any DB exception -> guarded rollback -> ``False``.
    NEVER raises (the hot drain cron + routing gate depend on it -- T-71-03).
    """
    try:
        row = await session.get(RouteControl, "global")
        return bool(row.force_local) if row is not None else False
    except Exception:
        logger.warning("route_control_degraded", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.warning("route_control_rollback_failed", exc_info=True)
        return False
