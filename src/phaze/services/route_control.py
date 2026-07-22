"""Degrade-safe reader for the Phase-71 force-local routing override (BEUI-02).

:func:`get_route_control` reads the single ``route_control`` ``'global'`` row and returns its
``force_local`` flag. It is on TWO hot paths -- the drain cron (``stage_cloud_window``) and the
duration-router gate (``routers.pipeline``) -- so a raise would 500 the routing gate or crash the
cron (T-71-03). It therefore mirrors :func:`phaze.services.pipeline.get_stage_controls`'s degrade
discipline EXACTLY: on an absent row it returns ``False`` (cloud-enabled), and on ANY DB exception it
logs and returns ``False``. It NEVER raises.

Defaulting to ``False`` (cloud-enabled) on error is the fail-safe: a transient DB hiccup must not
silently flip the whole registry to force-local -- the override is an explicit operator action, so
its absence/unreadability degrades to the normal cloud-enabled behavior.

The read runs inside a SAVEPOINT (``session.begin_nested()``, the pattern
:func:`phaze.services.queue_introspection.summarize_active_jobs` already uses) rather than a full
``session.rollback()`` (CR-01): both hot callers -- the duration-router gate in
``routers.pipeline`` and the drain cron -- may have already loaded ORM rows (e.g. ``FileRecord``)
on this SAME session before calling here, and a full rollback would expire them, triggering a
synchronous implicit refresh -> ``MissingGreenlet`` on the very DB hiccup this degrade exists to
survive.
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

    Absent row -> ``False`` (cloud-enabled). Any DB exception -> SAVEPOINT rollback -> ``False``.
    NEVER raises (the hot drain cron + routing gate depend on it -- T-71-03).
    """
    try:
        async with session.begin_nested():
            row = await session.get(RouteControl, "global")
        return bool(row.force_local) if row is not None else False
    except Exception:
        logger.warning("route_control_degraded", exc_info=True)
        return False
