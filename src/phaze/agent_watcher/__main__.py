"""Always-on watcher entry point: 'uv run python -m phaze.agent_watcher' (Phase 27 D-15, D-16).

Standalone asyncio process -- NOT a SAQ worker. Boots with ``asyncio.run(main())``,
calls ``/whoami`` with bounded retry to resolve the calling agent's identity,
schedules one watchdog Observer per entry in ``identity.scan_roots`` (phaze-jzid: a
dedicated observer instance per root so one root's start() failure cannot abort the
others), sweeps the :class:`Debouncer` every ``watcher_sweep_interval_seconds`` and
POSTs each settled path via :class:`Poster`. SIGINT / SIGTERM trigger graceful
shutdown: sweep loop exits, every root observer's stop() + join() drain its watchdog
thread, and the HTTP client is closed.

Import-graph invariant (Pitfall 5 / D-22):
    This module MUST NOT import ``phaze.tasks.agent_worker``,
    ``phaze.database``, ``phaze.tasks.session``, or ``sqlalchemy.ext.asyncio``.
    Verified by ``tests/shared/core/test_task_split.py::test_agent_watcher_does_not_import_phaze_database``
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
    5. One :class:`watchdog.observers.Observer` PER ``identity.scan_roots`` entry;
       each is scheduled against exactly one root and started independently. A
       root whose ``start()`` raises ``OSError`` (missing/unmounted path,
       inotify watch-limit exhaustion) is logged as a WARNING and skipped
       WITHOUT aborting the remaining roots (phaze-jzid); the watcher only
       fails hard if every root failed to start. The watcher does NOT walk the
       existing tree on start (D-04) -- only post-startup events flow through
       the bridge.
    6. ``_sweep_loop`` blocks until ``shutdown_event`` fires.
    7. ``finally``: ``observer.stop()`` + ``observer.join()`` + ``client.close()``.
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


async def _post_ready_paths(poster: Poster, ready: list[str]) -> None:
    """POST each settled path; one path's failure must not stop the others.

    DECISION (phaze-bk9el.13, implementer): ``except Exception`` here is
    intentionally broad, not narrowed to :class:`Poster`'s documented error
    types. ``Poster.post_one`` already contracts that "no exception escapes
    this method" (poster.py), so this handler exists as a guard against a
    *violation* of that contract, not against its normal failure modes --
    narrowing it to Poster's own exception types would defeat the purpose.
    The watcher is unattended (runs on fileserver hosts with no operator
    watching stdout), so keeping the sweep loop alive across a bug in the
    poster is worth more than a stack trace surfacing at the loop boundary;
    the ``logger.exception`` call preserves the trace either way.
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

    Pitfall 1: a single post raising MUST NOT crash the loop; the entry has
    already been removed from the debouncer (sweep returns by-value).

    DECISION (phaze-bk9el.13, implementer): the outer ``except Exception`` is
    intentionally broad. This is the top-level guard for one unattended sweep
    tick -- ``debouncer.sweep`` itself is not documented as exception-free the
    way ``Poster.post_one`` is, so an unanticipated failure here (a future
    Debouncer bug, a logging backend error, anything) must not take the whole
    watcher process down; the container has no supervisor watching for "still
    posting files" versus "crashed", only for "process exited". Losing one
    sweep tick and retrying on the next is always preferable to that.
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
    """Drain settled / stuck entries from the debouncer until shutdown.

    Pattern (RESEARCH §Pattern 2):
        - Sweep, post readies, log evictions (:func:`_run_sweep_iteration`).
        - ``await asyncio.wait_for(shutdown_event.wait(), timeout=sweep_interval)``
          either returns early (shutdown) or raises TimeoutError (regular tick).
    """
    while not shutdown_event.is_set():
        await _run_sweep_iteration(debouncer, poster, settle_period, max_pending)
        with contextlib.suppress(TimeoutError):
            # Regular tick: TimeoutError means shutdown_event not yet set; loop again.
            await asyncio.wait_for(shutdown_event.wait(), timeout=sweep_interval)


def _configure_logging() -> None:
    """Configure the central structlog pipeline for the watcher process.

    PR3 observability: delegates to :func:`phaze.logging_config.configure_logging`.
    The watcher runs via ``asyncio.run(main())`` and never goes through uvicorn, so
    without configuration EVERY ``logger.info/error/...`` call is swallowed and
    operators see an empty ``docker logs`` stream even when the process is alive and
    posting files (Phase 27 UAT Gap 7: a healthy watcher was indistinguishable from a
    hung one).

    Called bare (env-driven: PHAZE_LOG_LEVEL / PHAZE_LOG_JSON) and FIRST in ``main()``,
    BEFORE ``get_settings()`` -- so a pydantic ``ValidationError`` raised by
    ``AgentSettings`` (the very misconfig the watcher is trying to report) is still
    logged through the pipeline rather than crashing on settings construction
    (Gap-5/Gap-7). ``configure_logging`` is itself idempotent.
    """
    configure_logging()


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
    # phaze-m1drf.1: the watcher is its own OS process (asyncio.run, never uvicorn, never
    # SAQ), so it installs its own SDK. Off unless an OTLP endpoint is configured. It emits
    # no metrics of its own today -- what it contributes is a `phaze-watcher` service on the
    # traces, so a file's journey from settled-on-disk to analyzed is one trace rather than
    # starting at the API.
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
    # WR-02: wrap EVERYTHING after client construction in a try/finally so
    # ``client.close()`` runs even if ``whoami_with_retry`` raises (auth fail or
    # exhausted retry budget). Previously the client was constructed before the
    # try/finally and the underlying httpx.AsyncClient would leak (ResourceWarning)
    # on the startup-failure path -- a violation of the module-docstring's
    # deterministic-close contract.
    try:
        identity = await whoami_with_retry(client)

        # Phase 29 WARNING-7 (kept after phaze-ynv6w): the watcher intentionally
        # does NOT call ensure_models_present. The worker (phaze.tasks.agent_worker
        # .startup) validates the operator-provisioned /models set (never downloads
        # it); the watcher loads no model and cannot dispatch analysis jobs
        # without a worker anyway.

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
        handler = WatcherEventHandler(loop=loop, debouncer_touch=debouncer.touch)

        # phaze-jzid: ONE watchdog.BaseObserver PER scan root, started independently,
        # rather than a single shared Observer scheduled against every root. watchdog's
        # BaseObserver.start() iterates all of its emitters and re-raises the first
        # OSError it hits (e.g. FileNotFoundError for a root that does not exist inside
        # this container -- registration/mount drift -- or ENOSPC/EMFILE from exceeding
        # fs.inotify.max_user_watches) -- so scheduling every root on ONE observer means a
        # single bad root aborts start() for ALL of them, taking down realtime ingestion
        # on otherwise-healthy roots and crash-looping the whole watcher under compose's
        # restart policy. Isolating each root behind its own observer instance means a
        # per-root start() failure is caught and skipped without touching the others.
        # Fail hard only if every single root failed to start (nothing left to watch).
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
            # WR-07: bound the join with a timeout so a wedged watchdog thread
            # (NFS stall, FUSE deadlock) cannot block ``docker compose down``
            # indefinitely. ``threading.Thread.join()`` is blocking-by-default;
            # 10s matches the typical container-shutdown grace period and is
            # long enough for a healthy thread to drain. If the thread is still
            # alive after the timeout we log a warning and proceed -- the
            # container's process supervisor handles the final SIGKILL. Every
            # per-root observer gets its own bounded stop/join so one wedged
            # root cannot delay shutdown of the others.
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
