"""The DB-override layer's reader/writer + Postgres NOTIFY (phaze-mvq8z.6, ADR-0019 (runtime config hot-reload) §3/§10/§14).

Three surfaces over :class:`~phaze.models.runtime_config_override.RuntimeConfigOverride`:

* :func:`get_runtime_config_overrides` -- the DEGRADE-SAFE reader, wired into
  :class:`phaze.runtime_config.RuntimeConfigStore` as its ``override_provider``
  (``phaze.runtime_config_notify.install_runtime_config_overrides``). It is on the reload hot path
  (every SIGHUP, file-watch tick, admin-API write, and fallback poll calls it), so it mirrors
  :func:`phaze.services.route_control.get_route_control`'s degrade discipline EXACTLY: a SAVEPOINT
  read, absent table/rows -> ``{}`` (no overrides -- every key falls through to the next layer),
  ANY DB exception -> ``{}``, NEVER raises. A raise here would turn a transient DB hiccup into a
  crashed reload instead of the last-good snapshot the core is built to keep.
* :func:`set_runtime_config_override` -- the WRITE path the admin API calls only AFTER
  ``RuntimeConfigStore.preview()`` has already validated the candidate off the loop (the admin
  router owns that ordering; this function has no opinion on validity, matching ``routing.py``'s
  thin-endpoint discipline of doing the DB write and letting a genuine failure surface as a 500).
* :func:`clear_runtime_config_override` -- removes a key's override row outright (there is no
  "unset" sentinel value in this table -- see the model's docstring), so a cleared key falls back
  to the ``file`` / ``env`` / ``default`` layers underneath it on the next reload.

Both writers ``pg_notify`` the SAME channel a row's change takes effect on
(:data:`RUNTIME_CONFIG_NOTIFY_CHANNEL`) INSIDE the write transaction, before ``commit()`` --
Postgres queues a transaction's ``NOTIFY`` payloads and delivers them only once that transaction
commits, so a rolled-back write never fires a notification for a change that never happened.
Every LISTENer (``phaze.runtime_config_notify``, wired into both the api and control-worker
processes, ADR-0019 (runtime config hot-reload) §14) reacts by calling ``store.reload("api")`` -- including the SAME process
that issued the write, since Postgres delivers a NOTIFY to every session with an active LISTEN on
the channel, itself included. A reload whose override set is value-identical to what the store
already resolved is a cheap ``outcome="unchanged"`` no-op, so this "hearing your own write twice"
is harmless by construction, not a defect to work around.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select, text
import structlog

from phaze.models.runtime_config_override import RuntimeConfigOverride


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

#: The Postgres NOTIFY channel every DB-override write signals on (ADR-0019 (runtime config hot-reload) §14). A single
#: process-wide constant -- both the writer (this module) and every listener
#: (``phaze.runtime_config_notify``) reference it, so the two sides cannot drift apart.
RUNTIME_CONFIG_NOTIFY_CHANNEL: Final = "phaze_runtime_config"


async def get_runtime_config_overrides(session: AsyncSession) -> dict[str, Any]:
    """Return every currently-set override as ``{key: value}``. NEVER raises (see module docstring).

    Absent table/rows -> ``{}``. Any DB exception -> SAVEPOINT rollback -> ``{}`` (route_control's
    exact degrade shape, generalized from a single boolean row to a full key/value map).
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(select(RuntimeConfigOverride))).scalars().all()
        return {row.key: row.value for row in rows}
    except Exception:
        logger.warning("runtime_config_overrides_degraded", exc_info=True)
        return {}


async def _notify(session: AsyncSession) -> None:
    """Queue a ``NOTIFY`` on the override channel for delivery when this transaction commits."""
    await session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": RUNTIME_CONFIG_NOTIFY_CHANNEL})


async def set_runtime_config_override(session: AsyncSession, key: str, value: Any) -> None:
    """Upsert ``key``'s override to ``value`` and commit. Caller has already validated ``value``
    (``RuntimeConfigStore.preview()``) -- this function does not re-validate; a DB failure here
    propagates to the caller as an ordinary exception (matching ``routing.py``'s thin-write
    discipline), not a degrade.
    """
    row = await session.get(RuntimeConfigOverride, key)
    if row is None:
        row = RuntimeConfigOverride(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    await _notify(session)
    await session.commit()


async def clear_runtime_config_override(session: AsyncSession, key: str) -> bool:
    """Remove ``key``'s override row, if any, and commit. Returns whether a row was removed.

    A missing row is not an error -- DELETE is idempotent: "no override for this key" is the
    post-condition either way. Only a removal that actually changed something notifies; clearing
    an already-absent override is a true no-op, not an extra reload for every listener to churn
    through.
    """
    row = await session.get(RuntimeConfigOverride, key)
    if row is None:
        return False
    await session.delete(row)
    await _notify(session)
    await session.commit()
    return True
