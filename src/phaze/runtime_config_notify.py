"""Wire the DB-override layer into a process's :class:`~phaze.runtime_config.RuntimeConfigStore`,
and propagate override changes to every control-plane process via Postgres LISTEN/NOTIFY, with a
periodic fallback poll for a missed notification (phaze-mvq8z.6, ADR-0019 (runtime config hot-reload) §3/§14).

:func:`install_runtime_config_overrides` registers
:func:`phaze.services.runtime_config_overrides.get_runtime_config_overrides` as the store's
``override_provider`` -- this alone makes the DB-override layer participate in every reload
(SIGHUP, file-watch, admin API, poll), the same way :func:`phaze.services.route_control.get_route_control`
participates in the two routing gates that read it.

:func:`start_runtime_config_listener` is the propagation half: a dedicated ``asyncpg`` connection
``LISTEN``s on :data:`~phaze.services.runtime_config_overrides.RUNTIME_CONFIG_NOTIFY_CHANNEL` and
calls ``store.reload("api")`` on every notification (asyncpg 0.24+ dispatches an ``async def``
listener callback as its own task -- verified against the 0.31.0 pinned here,
``Connection._process_notification`` -- so no manual ``asyncio.create_task`` wrapping is needed),
plus a periodic poll loop that calls ``store.reload("poll")`` on a fixed interval regardless of
whether a notification arrived, healing a NOTIFY the listener missed (a connection drop between
the write's commit and the listener's reconnect, for instance). Every reload triggered either way
goes through the SAME validate-then-swap pipeline as any other trigger and is cheap
(``outcome="unchanged"``) when nothing actually changed, so redundant reloads -- the writer's own
process hearing its own NOTIFY, or a poll firing right after a NOTIFY already applied the change
-- cost an idle pass, never a double-apply.

Wired into both control-plane processes that share the reloadable-config store: the api lifespan
(``phaze.main``) and the control worker's SAQ startup/shutdown hooks (``phaze.tasks.controller``).
Agent-lane workers are OUT of scope here -- they reach the control plane over HTTP, not a shared
Postgres connection, and get their own propagation path in ``phaze-mvq8z.9``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import asyncpg
import structlog

from phaze.runtime_config import partition_overrides
from phaze.services.runtime_config_overrides import RUNTIME_CONFIG_NOTIFY_CHANNEL, get_runtime_config_overrides


if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.runtime_config import RuntimeConfigStore


logger = structlog.get_logger(__name__)

# ~30s matches the heartbeat-cadence propagation latency the operator accepted for remote agents
# (ADR-0019 (runtime config hot-reload) §14, "~30s is fine (Recommended)") -- the control-plane fallback poll reuses that same
# figure rather than inventing a second tunable an operator has to separately learn and configure.
DEFAULT_POLL_INTERVAL_SEC: Final[float] = 30.0


def install_runtime_config_overrides(store: RuntimeConfigStore, session_factory: Callable[[], AsyncSession]) -> None:
    """Wire the DB-override layer into ``store`` as its ``override_provider``.

    ``session_factory`` (an ``async_sessionmaker`` or equivalent zero-arg callable returning an
    ``AsyncSession`` context manager) is invoked fresh on EVERY reload -- never a session held open
    across reloads -- mirroring ``get_route_control``'s per-call session discipline.

    Only :data:`~phaze.runtime_config.RELOADABLE_KEYS` reach the store (phaze-mvq8z.22): a stale row
    -- a key a later build renamed or made restart-only -- would otherwise reject every reload in
    this process, forever. Its NAME is logged once each time the stale set changes (the fallback poll
    reloads every 30s), never its value; the admin pane lists it and its DELETE removes it.
    """
    reported: tuple[str, ...] = ()

    async def _read_overrides() -> dict[str, Any]:
        nonlocal reported
        async with session_factory() as session:
            overrides, ignored = partition_overrides(await get_runtime_config_overrides(session))
        if ignored != reported:
            reported = ignored
            if ignored:
                logger.warning("phaze.runtime_config_notify ignoring stale override rows", ignored=list(ignored))
        return overrides

    store.set_override_provider(_read_overrides)


def _asyncpg_dsn(database_url: str) -> str:
    """Strip a SQLAlchemy ``+asyncpg`` / ``+psycopg`` dialect suffix ``asyncpg.connect()`` rejects.

    Mirrors ``ControlSettings._strip_sqlalchemy_driver`` (``phaze/config_base.py``), which performs
    the identical normalization for the separate ``queue_url`` DSN. Kept as its own tiny copy here
    rather than importing that field-scoped private classmethod across modules -- the two DSNs
    serve different connection roles (broker vs. this LISTEN connection) and coincide only in this
    deployment's default configuration.
    """
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix) :]
    return database_url


# Bounds the initial LISTEN connect attempt so an unreachable/misconfigured database.url degrades
# this ONE feature (NOTIFY propagation -- the fallback poll still covers it) rather than holding up
# the whole process boot indefinitely, matching the "control plane boots regardless" discipline
# `tasks/controller.py` already applies to its own best-effort startup probes (D-05).
_LISTEN_CONNECT_TIMEOUT_SEC: Final[float] = 5.0


@dataclass
class RuntimeConfigListenerHandle:
    """The fallback-poll task for one process, plus its LISTEN connection when one could be
    established. ``stop()`` tears down both; ``connection`` is ``None`` on a degraded start
    (:func:`start_runtime_config_listener`'s own docstring), in which case there is nothing to
    close and the poll loop is the only propagation path still running.
    """

    connection: asyncpg.Connection | None
    poll_task: asyncio.Task[None]

    async def stop(self) -> None:
        """Cancel the poll loop and close the LISTEN connection, if any. Safe to call once, at shutdown."""
        self.poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.poll_task
        if self.connection is not None:
            with contextlib.suppress(Exception):
                await self.connection.close()


async def start_runtime_config_listener(
    *,
    store: RuntimeConfigStore,
    database_url: str,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
) -> RuntimeConfigListenerHandle:
    """Start the LISTEN connection + fallback poll loop for ``store`` in THIS process.

    Call once per process, after :func:`install_runtime_config_overrides` has registered the
    override provider (a reload before that point simply sees no overrides, same as before this
    bead existed -- not an error, just an earlier layer winning). Returns a handle whose ``stop()``
    the caller's shutdown path must await, mirroring every other resource ``phaze.main.lifespan`` /
    ``phaze.tasks.controller`` opens and closes in reverse order.

    The LISTEN connection attempt is best-effort and BOUNDED (:data:`_LISTEN_CONNECT_TIMEOUT_SEC`):
    an unreachable database (a bad ``database_url``, a network hiccup at boot, or -- the shape this
    covers in this repo's own test suite -- a lifespan test that mocks every OTHER real connection
    but not this one) degrades the process to poll-only propagation rather than aborting boot or
    hanging it, mirroring every other best-effort startup probe in ``tasks/controller.py`` (the
    Kueue LocalQueue probe, the S3 lifecycle-TTL push -- D-05's "control plane boots regardless").
    The fallback poll (below) is unaffected either way -- it never depends on this connection.
    """
    connection: asyncpg.Connection | None = None
    try:
        # A fresh, unannotated local: `connection`'s own declared type (`asyncpg.Connection |
        # None`, which `ignore_missing_imports` collapses to `Any | None`) does not narrow just
        # because it is reassigned, so calling `.add_listener` straight off that name would need
        # an extra `assert` mypy has no other reason to want here.
        new_connection = await asyncio.wait_for(asyncpg.connect(_asyncpg_dsn(database_url)), timeout=_LISTEN_CONNECT_TIMEOUT_SEC)

        async def _on_notify(_connection: asyncpg.Connection, _pid: int, _channel: str, _payload: str) -> None:
            # store.reload() NEVER raises (phaze.runtime_config's own contract) -- a bad candidate
            # rejects and keeps the last-good snapshot, so this callback needs no try/except of
            # its own; asyncpg runs an async listener callback as its own task
            # (Connection._process_notification), so an exception here would only ever surface as
            # an "unhandled task exception" log, never crash the LISTEN connection.
            await store.reload("api")

        await new_connection.add_listener(RUNTIME_CONFIG_NOTIFY_CHANNEL, _on_notify)
        connection = new_connection
    except Exception:
        logger.warning(
            "phaze.runtime_config_notify could not establish the LISTEN connection; falling back to poll-only propagation until the next restart",
            exc_info=True,
        )
        connection = None

    async def _poll_loop() -> None:
        while True:
            await asyncio.sleep(poll_interval_sec)
            try:
                await store.reload("poll")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Defensive only -- reload() does not raise -- but every other long-lived loop in
                # this codebase (phaze.main._orphan_refresh_loop) keeps last-good on an iteration
                # fault rather than letting one kill the loop, and this one is no different.
                logger.warning("phaze.runtime_config_notify poll loop iteration failed; keeping last-good", exc_info=True)

    poll_task = asyncio.create_task(_poll_loop())
    return RuntimeConfigListenerHandle(connection=connection, poll_task=poll_task)
