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
import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from phaze.agent_watcher.debouncer import Debouncer
from phaze.agent_watcher.observer import WatcherEventHandler
from phaze.agent_watcher.poster import Poster
from phaze.config import AgentSettings, get_settings
from phaze.tasks._shared.agent_bootstrap import construct_agent_client, whoami_with_retry


if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver


logger = logging.getLogger(__name__)


def _log_settings_validation_error(exc: ValidationError) -> None:
    """Log a readable summary of which AgentSettings fields failed validation.

    Phase 27 UAT Gap 5: when PHAZE_AGENT_API_URL (or similarly required env)
    is missing, the raw pydantic ValidationError stack trace buries the
    operator-actionable hint behind a wall of pydantic internals. This
    helper extracts just the field name + reason from each error in the
    pydantic.ValidationError and emits one ERROR line per failed field --
    the operator-facing format. The original exception is still logged at
    DEBUG for troubleshooting.

    Designed to be the FIRST handler called when get_settings() raises;
    `main_entrypoint` below routes ValidationError here and exits 1.
    """
    logger.error("phaze.agent_watcher: agent settings failed validation (%d issue(s))", len(exc.errors()))
    for err in exc.errors():
        # Pydantic error shape: {"loc": ("field",), "msg": "...", "type": "..."}
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = err.get("msg", "<no message>")
        # Map back to the documented env-var name (best-effort: pydantic-settings
        # uses the field name in `loc`, e.g. `agent_api_url`).
        env_hint = f"PHAZE_{loc.upper()}" if loc else "<unknown env var>"
        logger.error("  - missing or invalid: %s (env: %s) -- %s", loc, env_hint, msg)
    logger.debug("phaze.agent_watcher: full pydantic ValidationError follows", exc_info=exc)


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


def _configure_logging() -> None:
    """Attach a stdout StreamHandler to the root logger.

    The watcher runs via ``asyncio.run(main())`` and never goes through
    uvicorn, so without an explicit handler EVERY ``logger.info/error/...``
    call is swallowed and operators see an empty ``docker logs`` stream
    even when the process is alive and posting files. Phase 27 UAT Gap 7
    surfaced this: a healthy watcher was indistinguishable from a hung one.

    Idempotent: re-running adds no duplicate handler.
    """
    root = logging.getLogger()
    if any(isinstance(h, logging.StreamHandler) and h.stream is sys.stdout for h in root.handlers):
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


async def main() -> None:
    """Bootstrap the watcher process (D-16 startup sequence).

    Phase 27 UAT Gap 5: the config read is wrapped so a pydantic
    ``ValidationError`` (raised by ``AgentSettings`` when a required env var
    is missing) is translated into a readable ERROR log + non-zero exit
    BEFORE we reach the `whoami_with_retry` code path. Previously the
    operator saw only a pydantic stack trace and the Pitfall-7
    "auth invalid; check PHAZE_AGENT_TOKEN" hint never surfaced.

    Phase 27 UAT Gap 7: ``_configure_logging`` attaches a stdout handler so
    every subsequent log line actually reaches ``docker logs``.
    """
    _configure_logging()
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

        # Phase 29 D-21 + WARNING-7: the watcher intentionally does NOT call
        # ensure_models_present. The worker (phaze.tasks.agent_worker.startup)
        # owns the download on a fresh /models volume; the watcher cannot
        # dispatch analysis jobs without a worker anyway, and having both
        # entry points race on .part files in /models would be wasteful.
        # If a future plan needs models on the watcher side, gate via a
        # filelock and only one entry point downloads.

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

        # Phase 27 UAT Gap 8: macOS docker bind mounts (rancher-desktop /
        # Docker Desktop) do not propagate inotify events through 9p/virtiofs
        # — the native Observer never fires. PollingObserver works on any
        # filesystem at a modest CPU cost. Native Observer remains the default
        # for production Linux file servers where inotify is fully functional.
        observer: BaseObserver
        if cfg.watcher_polling_mode:
            logger.info("watcher: using PollingObserver (PHAZE_WATCHER_POLLING_MODE=true)")
            observer = PollingObserver(timeout=cfg.watcher_sweep_interval_seconds)
        else:
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
            # WR-07: bound the join with a timeout so a wedged watchdog thread
            # (NFS stall, FUSE deadlock) cannot block ``docker compose down``
            # indefinitely. ``threading.Thread.join()`` is blocking-by-default;
            # 10s matches the typical container-shutdown grace period and is
            # long enough for a healthy thread to drain. If the thread is still
            # alive after the timeout we log a warning and proceed -- the
            # container's process supervisor handles the final SIGKILL.
            observer.stop()
            observer.join(timeout=10.0)
            if observer.is_alive():
                logger.warning("watcher: observer thread did not stop within 10s; abandoning")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
