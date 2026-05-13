"""Always-on watcher entry point: 'uv run python -m phaze.agent_watcher' (Phase 27 D-15, D-16).

Standalone asyncio process -- NOT a SAQ worker. Boots with ``asyncio.run(main())``,
calls ``/whoami`` with bounded retry to resolve the calling agent's identity,
schedules one watchdog Observer per ``identity.scan_root``, sweeps the
:class:`Debouncer` every ``watcher_sweep_interval_seconds`` and POSTs each
settled path via :class:`Poster`. SIGINT / SIGTERM trigger graceful shutdown:
sweep loop exits, observer.stop() + observer.join() drain the watchdog thread,
and the HTTP client is closed.

Import-graph invariant (Pitfall 5 / D-22):
    This module MUST NOT import ``phaze.tasks.agent_worker``,
    ``phaze.database``, ``phaze.tasks.session``, or ``sqlalchemy.ext.asyncio``.
    Verified by ``tests/test_task_split.py::test_agent_watcher_does_not_import_phaze_database``
    (subprocess isolation; conditionally skipped until this module exists,
    then a hard gate).

Startup sequence (D-16):
    1. ``get_settings()`` -> AgentSettings (raises if PHAZE_ROLE != agent).
    2. ``construct_agent_client(cfg)`` -> :class:`PhazeAgentClient`.
    3. ``whoami_with_retry(client)`` -> :class:`AgentIdentity`. Short-circuits
       immediately on ``AgentApiAuthError`` (RESEARCH Pitfall 7) so a bad
       token fails fast instead of spinning the container in restart loops.
    4. :class:`Debouncer` + :class:`Poster` constructed; ``asyncio.Event`` for
       shutdown; SIGINT/SIGTERM hooked to ``shutdown_event.set``.
    5. :class:`watchdog.observers.Observer` constructed; one ``schedule(...)``
       per ``identity.scan_roots`` entry; ``observer.start()`` spins the
       watchdog thread. The watcher does NOT walk the existing tree on start
       (D-04) -- only post-startup events flow through the bridge.
    6. ``_sweep_loop`` blocks until ``shutdown_event`` fires.
    7. ``finally``: ``observer.stop()`` + ``observer.join()`` + ``client.close()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from watchdog.observers import Observer

from phaze.agent_watcher.debouncer import Debouncer
from phaze.agent_watcher.observer import WatcherEventHandler
from phaze.agent_watcher.poster import Poster
from phaze.config import AgentSettings, get_settings
from phaze.tasks._shared.agent_bootstrap import construct_agent_client, whoami_with_retry


logger = logging.getLogger(__name__)


async def _sweep_loop(
    debouncer: Debouncer,
    poster: Poster,
    sweep_interval: float,
    settle_period: float,
    max_pending: float,
    shutdown_event: asyncio.Event,
) -> None:
    """Drain settled / stuck entries from the debouncer until shutdown.

    Pattern (RESEARCH §Pattern 2):
        - Sweep, post readies, log evictions.
        - ``await asyncio.wait_for(shutdown_event.wait(), timeout=sweep_interval)``
          either returns early (shutdown) or raises TimeoutError (regular tick).
        - Pitfall 1: a single post raising MUST NOT crash the loop; the entry
          has already been removed from the debouncer (sweep returns by-value).
    """
    while not shutdown_event.is_set():
        try:
            ready, evicted = debouncer.sweep(settle_period=settle_period, max_pending=max_pending)
            for path in ready:
                try:
                    await poster.post_one(path)
                except Exception:
                    logger.exception("watcher: post failed; entry already removed from debouncer path=%s", path)
            for path in evicted:
                logger.warning("watcher: dropping path=%s; mtime still changing past max_pending cap", path)
        except Exception:
            logger.exception("watcher: sweep iteration failed")
        with contextlib.suppress(TimeoutError):
            # Regular tick: TimeoutError means shutdown_event not yet set; loop again.
            await asyncio.wait_for(shutdown_event.wait(), timeout=sweep_interval)


async def main() -> None:
    """Bootstrap the watcher process (D-16 startup sequence)."""
    cfg = get_settings()
    if not isinstance(cfg, AgentSettings):
        msg = f"agent_watcher requires PHAZE_ROLE=agent; got {type(cfg).__name__}"
        raise RuntimeError(msg)

    # D-13 invariant: token preview is FIRST 12 CHARS + "..." -- never the full bearer.
    # The format-string key is "auth_id_prefix" (no secret keywords) so static
    # analyzers do not flag the format literal itself as a leak.
    token_preview = cfg.agent_token.get_secret_value()[:12] + "..."  # nosec B105
    logger.info(
        "phaze.agent_watcher startup role=agent api=%s auth_id_prefix=%s",
        cfg.agent_api_url,
        token_preview,
    )

    client = construct_agent_client(cfg)
    # WR-02: wrap EVERYTHING after client construction in a try/finally so
    # ``client.close()`` runs even if ``whoami_with_retry`` raises (auth fail or
    # exhausted retry budget). Previously the client was constructed before the
    # try/finally and the underlying httpx.AsyncClient would leak (ResourceWarning)
    # on the startup-failure path -- a violation of the module-docstring's
    # deterministic-close contract.
    try:
        identity = await whoami_with_retry(client)

        debouncer = Debouncer()
        poster = Poster(client=client, agent_id=identity.agent_id)
        shutdown_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        # SIGINT / SIGTERM: both fire the same shutdown_event.set callback so the
        # graceful shutdown sequence is identical regardless of which signal arrives.
        try:
            loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
            loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
        except NotImplementedError:
            # Windows / some asyncio policies disallow signal handlers; skip
            # silently -- the container's process supervisor (compose) still
            # delivers SIGTERM to the entrypoint, and asyncio.run() handles
            # KeyboardInterrupt via its own machinery.
            logger.debug("watcher: signal handlers not supported on this platform; skipping")

        observer = Observer()
        handler = WatcherEventHandler(loop=loop, debouncer_touch=debouncer.touch)
        for root in identity.scan_roots:
            observer.schedule(handler, path=root, recursive=True)
        observer.start()

        try:
            await _sweep_loop(
                debouncer=debouncer,
                poster=poster,
                sweep_interval=float(cfg.watcher_sweep_interval_seconds),
                settle_period=float(cfg.watcher_settle_seconds),
                max_pending=float(cfg.watcher_max_pending_seconds),
                shutdown_event=shutdown_event,
            )
        finally:
            observer.stop()
            observer.join()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
