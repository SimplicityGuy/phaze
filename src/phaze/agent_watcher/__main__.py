"""Always-on watcher entry point: ``uv run python -m phaze.agent_watcher``.

Standalone asyncio process -- NOT a SAQ worker. Boots with ``asyncio.run(main())``,
calls ``/whoami`` with bounded retry to resolve the calling agent's identity,
schedules one watchdog observer per scan root so one root's failure cannot abort the
others, sweeps the :class:`Debouncer` every ``watcher_sweep_interval_seconds``, and
POSTs each settled path via :class:`Poster`. SIGINT / SIGTERM trigger graceful
shutdown: sweep loop exits, every root observer's stop() + join() drain its watchdog
thread, and the HTTP client is closed.

Import-graph invariant:
    This module MUST NOT import ``phaze.tasks.agent_worker``,
    ``phaze.database``, ``phaze.tasks.session``, or ``sqlalchemy.ext.asyncio``.
    Enforced by ``tests/shared/core/test_task_split.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError
import structlog
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from phaze.agent_watcher.debouncer import Debouncer
from phaze.agent_watcher.observer import WatcherEventHandler
from phaze.agent_watcher.poster import Poster
from phaze.config import AgentSettings, get_settings
from phaze.logging_config import configure_logging
from phaze.tasks._shared.agent_bootstrap import construct_agent_client, whoami_with_retry
from phaze.telemetry import configure_telemetry, shutdown_telemetry


if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver


logger = structlog.get_logger(__name__)


def _log_settings_validation_error(exc: ValidationError) -> None:
    """Log one actionable line per invalid setting and retain the full error at DEBUG."""
    logger.error("phaze.agent_watcher: agent settings failed validation (%d issue(s))", len(exc.errors()))
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = err.get("msg", "<no message>")
        # Pydantic reports field names, so the environment hint is best-effort.
        env_hint = f"PHAZE_{loc.upper()}" if loc else "<unknown env var>"
        logger.error("  - missing or invalid: %s (env: %s) -- %s", loc, env_hint, msg)
    logger.debug("phaze.agent_watcher: full pydantic ValidationError follows", exc_info=exc)


async def _post_ready_paths(poster: Poster, ready: list[str]) -> None:
    """POST each settled path; one path's failure must not stop the others.

    The broad handler guards the unattended loop against violations of Poster's
    no-exception contract; narrowing it to documented errors would defeat that guard.
    """
    for path in ready:
        try:
            await poster.post_one(path)
        except Exception:
            logger.exception("watcher: post failed; entry already removed from debouncer path=%s", path)


def _log_evicted_paths(evicted: list[str]) -> None:
    """Log each path the debouncer dropped for exceeding ``max_pending`` uncleared.

    phaze-kw36: Debouncer.sweep only reaches eviction after giving the entry one
    full extra max_pending window to settle (see debouncer.py), so by the time we
    get here the path has failed to go quiet across two consecutive cap windows --
    do not assert "mtime still changing" as the cause, since sweep cannot actually
    distinguish continued churn from an unusually long single stall.
    """
    for path in evicted:
        logger.warning(
            "watcher: dropping path=%s; did not settle within max_pending cap even after one grace extension",
            path,
        )


async def _run_sweep_iteration(
    debouncer: Debouncer,
    poster: Poster,
    settle_period: float,
    max_pending: float,
) -> None:
    """Run one sweep: drain ready/evicted entries, post/log them, never raise.

    Entries leave the debouncer before posting. The broad outer guard keeps an
    unattended watcher alive after any unexpected failure in one sweep tick.
    """
    try:
        ready, evicted = debouncer.sweep(settle_period=settle_period, max_pending=max_pending)
        await _post_ready_paths(poster, ready)
        _log_evicted_paths(evicted)
    except Exception:
        logger.exception("watcher: sweep iteration failed")


async def _sweep_loop(
    debouncer: Debouncer,
    poster: Poster,
    sweep_interval: float,
    settle_period: float,
    max_pending: float,
    shutdown_event: asyncio.Event,
) -> None:
    """Drain settled and stuck entries until the shutdown event fires."""
    while not shutdown_event.is_set():
        await _run_sweep_iteration(debouncer, poster, settle_period, max_pending)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown_event.wait(), timeout=sweep_interval)


def _configure_logging() -> None:
    """Configure the central structlog pipeline for the watcher process.

    The watcher runs via ``asyncio.run(main())`` and never goes through uvicorn, so
    without configuration EVERY ``logger.info/error/...`` call is swallowed and
    operators see an empty ``docker logs`` stream even when the process is alive and
    posting files.

    Called bare (env-driven: PHAZE_LOG_LEVEL / PHAZE_LOG_JSON) and FIRST in ``main()``,
    BEFORE ``get_settings()`` -- so a pydantic ``ValidationError`` raised by
    ``AgentSettings`` (the very misconfig the watcher is trying to report) is still
    logged through the pipeline rather than crashing on settings construction
    ``configure_logging`` is itself idempotent.
    """
    configure_logging()


async def main() -> None:
    """Bootstrap the watcher with readable config failures and visible process logging."""
    _configure_logging()
    # This independent OS process installs its own opt-in telemetry provider.
    configure_telemetry("watcher")
    try:
        cfg = get_settings()
    except ValidationError as exc:
        _log_settings_validation_error(exc)
        # `sys.exit(1)` from inside `asyncio.run(main())` propagates as
        # SystemExit -- the runtime exits non-zero so docker compose restarts.
        sys.exit(1)
    if not isinstance(cfg, AgentSettings):
        msg = f"agent_watcher requires PHAZE_ROLE=agent; got {type(cfg).__name__}"
        raise RuntimeError(msg)

    # D-13 invariant: token preview is FIRST 12 CHARS + "..." -- never the full bearer.
    # The format-string key is "auth_id_prefix" (no secret keywords) so static
    # analyzers do not flag the format literal itself as a leak.
    token_preview = cfg.agent_token.get_secret_value()[:12] + "..."
    logger.info(
        "phaze.agent_watcher startup role=agent api=%s auth_id_prefix=%s",
        cfg.agent_api_url,
        token_preview,
    )

    client = construct_agent_client(cfg)
    # Own the client inside one try/finally so every startup failure closes it.
    try:
        identity = await whoami_with_retry(client)

        # The watcher loads no models; worker startup validates the provisioned set.

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

        # Polling mode supports bind mounts that do not propagate inotify; native observers
        # remain the production default.
        handler = WatcherEventHandler(loop=loop, debouncer_touch=debouncer.touch)

        # One observer per root isolates missing mounts and exhausted watch limits; fail hard only
        # when no root can start.
        if cfg.watcher_polling_mode:
            logger.info("watcher: using PollingObserver (PHAZE_WATCHER_POLLING_MODE=true)")

        observers: list[tuple[str, BaseObserver]] = []
        failed_roots: list[str] = []
        for root in identity.scan_roots:
            root_observer: BaseObserver = PollingObserver(timeout=cfg.watcher_sweep_interval_seconds) if cfg.watcher_polling_mode else Observer()
            root_observer.schedule(handler, path=root, recursive=True)
            try:
                root_observer.start()
            except OSError as exc:
                failed_roots.append(root)
                logger.warning(
                    "watcher: failed to start observer for scan root %s; skipping it and continuing with the remaining root(s): %s",
                    root,
                    exc,
                )
                continue
            observers.append((root, root_observer))

        if not observers:
            msg = f"watcher: no scan root could be watched (all {len(identity.scan_roots)} failed to start); aborting"
            raise RuntimeError(msg)
        if failed_roots:
            logger.warning(
                "watcher: started with %d/%d scan root(s); failed: %s",
                len(observers),
                len(identity.scan_roots),
                failed_roots,
            )

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
            # Bound each join to 10 s so one wedged filesystem cannot hold container shutdown.
            for _root, root_observer in observers:
                root_observer.stop()
            for root, root_observer in observers:
                root_observer.join(timeout=10.0)
                if root_observer.is_alive():
                    logger.warning("watcher: observer thread for scan root %s did not stop within 10s; abandoning", root)
    finally:
        await client.close()
        # LAST, after every resource that could still emit. Bounded by
        # PHAZE_TELEMETRY_FLUSH_TIMEOUT_MS (default 3,000 ms) and never raises, so a
        # collector that is down cannot hold the watcher's shutdown open.
        shutdown_telemetry()


if __name__ == "__main__":
    asyncio.run(main())
