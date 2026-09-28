"""Reload triggers for ``phaze.runtime_config`` (``phaze-mvq8z.5``): SIGHUP + a watched directory.

Two triggers, wired into every process that holds a :class:`~phaze.runtime_config.RuntimeConfigStore`
(the api lifespan, the control SAQ worker's ``startup`` hook, and the agent SAQ worker's ``startup``
hook -- see each module for the exact call site):

1. :func:`install_sighup_handler` -- a phaze-owned ``SIGHUP`` handler
   (``loop.add_signal_handler``, matching ``phaze.agent_watcher.__main__``'s idiom). Today's default
   disposition kills the process on SIGHUP (neither uvicorn nor SAQ's ``Worker`` -- whose
   ``SIGNALS = [SIGINT, SIGTERM]`` -- touches it), which is exactly why this exists. Installed on a
   DIFFERENT signal number from SAQ's own SIGINT/SIGTERM handlers
   (``asyncio.AbstractEventLoop.add_signal_handler`` dispatches per-signal-number, so the two never
   collide), and is a plain reload trigger -- it never itself stops the worker.

2. :class:`RuntimeConfigWatcher` -- a watchdog directory watch on ``runtime_config_dir`` (never a
   single file: see ``BaseSettings.runtime_config_dir``'s docstring and
   ``docs/design/0019-runtime-config-hot-reload.md`` §12 for why a
   single-file bind mount or a k8s subPath ConfigMap mount never observes an edit at all). Debounces
   a burst of filesystem events into one settle window, then content-hash compares before calling
   ``reload("file")`` -- so a rewrite that lands the SAME bytes (a no-op deploy, an editor's
   save-with-no-changes) never even attempts a reload.

Both a real container's SIGHUP delivery and its watched-directory event shapes were measured, not
assumed, in ``phaze-mvq8z.3``: SIGHUP already reaches the Python process unmodified in every
container shape (``uv`` forks and forwards it); a rename-into-place edit -- the safe-write pattern
this project's own tooling should use for ``runtime.toml`` -- surfaces as a DELETE+CREATE pair under
BOTH the native backend and ``PollingObserver``, never a single paired ``moved`` event, on the
Colima/virtiofs setup that measurement ran against. This module therefore never subscribes to
``on_moved`` at all -- unlike ``phaze.agent_watcher.observer.WatcherEventHandler``, which watches a
different, rsync-fed tree where a genuine paired rename IS the common case.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from pathlib import Path
import signal
from typing import TYPE_CHECKING

import structlog
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from phaze.config import get_settings
from phaze.config_control import ControlSettings, backends_config_path
from phaze.runtime_config import RUNTIME_TOML_NAME


if TYPE_CHECKING:
    from collections.abc import Callable

    from watchdog.events import DirCreatedEvent, DirDeletedEvent, DirModifiedEvent, FileCreatedEvent, FileDeletedEvent, FileModifiedEvent
    from watchdog.observers.api import BaseObserver

    from phaze.runtime_config import RuntimeConfigStore


logger = structlog.get_logger(__name__)


def install_sighup_handler(loop: asyncio.AbstractEventLoop, store: RuntimeConfigStore) -> bool:
    """Install the phaze-owned SIGHUP -> ``reload("sighup")`` handler on ``loop``.

    Returns whether it was actually installed. ``add_signal_handler`` can fail for reasons that
    have nothing to do with SIGHUP support and must not abort process startup over: no platform
    support (``NotImplementedError`` -- Windows, or some asyncio policies), and off the main
    thread of the main interpreter (``RuntimeError`` -- e.g. ``TestClient`` drives the api
    lifespan on an anyio portal thread with its own loop; ``asyncio.run`` for a SAQ worker or a
    real deployment always runs on the main thread, so production is unaffected). Both are
    logged at debug and treated as "no SIGHUP trigger for this process", never as a startup
    failure -- the file/API/DB reload layers still work.

    ``store.reload`` never raises (see its docstring), so the scheduled task is fire-and-forget:
    nothing here can turn a delivered signal into an unhandled exception on the loop.
    """

    def _on_sighup() -> None:
        loop.create_task(store.reload("sighup"), name="runtime-config-sighup-reload")

    try:
        loop.add_signal_handler(signal.SIGHUP, _on_sighup)
    except (NotImplementedError, RuntimeError) as exc:
        logger.debug("phaze.runtime_config_triggers: SIGHUP handler not installed", error=str(exc))
        return False
    return True


def _content_digest(path: Path) -> str | None:
    """SHA-256 of ``path``'s bytes, or ``None`` if it does not exist right now.

    ``None`` is a real, distinct digest value (not "unknown") -- a target that does not exist
    compares equal to a prior "does not exist" reading and different from any prior content.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("phaze.runtime_config_triggers: could not read watch target for hashing", path=str(path), error=str(exc))
        return None


class _TargetFileEventHandler(FileSystemEventHandler):
    """Bridges watchdog's OS thread to the asyncio loop for edits to ONE target filename.

    Filters by filename, not by the whole directory: ``runtime_config_dir`` may hold other files
    a later bead adds (``backends.toml`` is configured elsewhere, but shares the directory-mount
    requirement), and an edit to one of those must not fire a runtime-config reload. Subscribes
    to created/modified/deleted only -- never ``on_moved`` -- per the module docstring's
    phaze-mvq8z.3 evidence: a rename-into-place is a delete+create pair here, under both backends.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, target_name: str, on_touch: Callable[[], None]) -> None:
        super().__init__()
        self._loop = loop
        self._target_name = target_name
        self._on_touch = on_touch

    def _maybe_touch(self, src_path: bytes | str) -> None:
        path_str = src_path.decode() if isinstance(src_path, bytes) else src_path
        if path_str and Path(path_str).name == self._target_name:
            self._loop.call_soon_threadsafe(self._on_touch)

    def on_created(self, event: DirCreatedEvent | FileCreatedEvent) -> None:
        if not event.is_directory:
            self._maybe_touch(event.src_path)

    def on_modified(self, event: DirModifiedEvent | FileModifiedEvent) -> None:
        if not event.is_directory:
            self._maybe_touch(event.src_path)

    def on_deleted(self, event: DirDeletedEvent | FileDeletedEvent) -> None:
        if not event.is_directory:
            self._maybe_touch(event.src_path)


class RuntimeConfigWatcher:
    """Watches ``directory`` for edits to ``target_name``; debounces, hash-compares, reloads.

    Own the DIRECTORY mount, never a single file -- see the module docstring. ``start`` /
    ``stop`` are the only public lifecycle; both are safe to call from the owning process's
    lifespan/startup and shutdown hooks respectively, mirroring
    ``phaze.agent_watcher.__main__``'s bounded-join observer shutdown.
    """

    def __init__(
        self,
        store: RuntimeConfigStore,
        directory: Path,
        *,
        target_name: str = RUNTIME_TOML_NAME,
        polling: bool,
        poll_interval: float,
        settle_seconds: float,
    ) -> None:
        self._store = store
        self._directory = directory
        self._target_name = target_name
        self._target_path = directory / target_name
        self._polling = polling
        self._poll_interval = poll_interval
        self._settle_seconds = settle_seconds
        self._observer: BaseObserver | None = None
        self._debounce_task: asyncio.Task[None] | None = None
        # Seeded at construction (not at start()) so a change made between construction and
        # start() is still detected as a change on the first post-start event.
        self._last_digest = _content_digest(self._target_path)

    def start(self) -> None:
        """Schedule the observer. Best-effort: a missing/unwatchable directory logs and returns.

        Not fatal -- an operator who has not mounted ``runtime_config_dir`` yet (or a fresh dev
        checkout using none of the hot-reload triggers) still gets a working process; SIGHUP, the
        admin API and DB overrides (once phaze-mvq8z.6 lands) are unaffected. Mirrors
        ``phaze.agent_watcher.__main__``'s per-root ``try/except OSError`` around ``observer.start()``.
        """
        loop = asyncio.get_running_loop()
        handler = _TargetFileEventHandler(loop=loop, target_name=self._target_name, on_touch=self._on_touch)
        observer: BaseObserver = PollingObserver(timeout=self._poll_interval) if self._polling else Observer()
        try:
            observer.schedule(handler, path=str(self._directory), recursive=False)
            observer.start()
        except OSError as exc:
            logger.warning(
                "phaze.runtime_config_triggers: could not watch %s; file-triggered hot-reload is disabled for this "
                "process (SIGHUP, the admin API and DB overrides still work): %s",
                self._directory,
                exc,
            )
            return
        self._observer = observer
        logger.info(
            "phaze.runtime_config_triggers: watching %s for %s (%s)",
            self._directory,
            self._target_name,
            "polling" if self._polling else "native",
        )

    def _on_touch(self) -> None:
        """Runs on the asyncio loop (via ``call_soon_threadsafe``): (re)start the debounce timer."""
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.get_running_loop().create_task(self._settle_and_maybe_reload(), name="runtime-config-watch-debounce")

    async def _settle_and_maybe_reload(self) -> None:
        await asyncio.sleep(self._settle_seconds)
        digest = _content_digest(self._target_path)
        if digest == self._last_digest:
            # Same content (or still/again absent): a no-op rewrite or an editor touch. Do not
            # even attempt reload("file") -- ADR-0019 (runtime config hot-reload) §5 and the bead
            # description both call for the content-hash compare to happen HERE, in the trigger,
            # not just inside reload's own digest-based applier skip.
            return
        self._last_digest = digest
        result = await self._store.reload("file")
        if not result.successful:
            logger.warning("phaze.runtime_config_triggers: file-triggered reload was rejected", error=result.error)

    async def stop(self) -> None:
        """Cancel any pending debounce timer and stop the observer thread. Idempotent."""
        if self._debounce_task is not None:
            self._debounce_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._debounce_task
            self._debounce_task = None
        if self._observer is not None:
            observer, self._observer = self._observer, None
            # Bound the join so one wedged watch thread cannot hold process shutdown open.
            observer.stop()
            await asyncio.to_thread(observer.join, timeout=10.0)
            if observer.is_alive():
                logger.warning("phaze.runtime_config_triggers: observer thread did not stop within 10s; abandoning")


def build_watcher(store: RuntimeConfigStore) -> RuntimeConfigWatcher:
    """The production factory: a watcher over ``get_settings().runtime_config_dir``.

    Deliberately calls :func:`~phaze.config.get_settings` itself rather than accepting a
    ``settings`` argument from the caller: it must resolve to the EXACT SAME singleton
    :func:`~phaze.runtime_config.get_runtime_config_store` already used to build ``store`` (both
    are the same ``lru_cache``d function, so in every real process this is a no-op re-fetch), not
    whatever locally-scoped settings variable a call site happens to be holding -- several startup
    tests patch e.g. ``phaze.tasks.controller.get_settings`` with a bare ``MagicMock()`` that only
    stands in for the handful of attributes that test cares about, and this module's own
    ``get_settings`` binding (imported directly from ``phaze.config``, not re-exported by
    ``phaze.tasks.controller``) is untouched by that patch.
    """
    settings = get_settings()
    return RuntimeConfigWatcher(
        store,
        Path(settings.runtime_config_dir),
        polling=settings.runtime_config_watch_polling,
        poll_interval=settings.runtime_config_watch_poll_interval_seconds,
        settle_seconds=settings.runtime_config_watch_debounce_seconds,
    )


def build_backends_watcher(store: RuntimeConfigStore) -> RuntimeConfigWatcher:
    """A SECOND watcher, over ``backends.toml``'s own directory (``phaze-mvq8z.8``).

    ``PHAZE_BACKENDS_CONFIG_FILE`` (default ``/etc/phaze/backends.toml``) is configured
    independently of ``runtime_config_dir`` and is not guaranteed to live inside it -- the
    default paths are two different directories entirely (``/etc/phaze/backends.toml`` vs.
    ``/etc/phaze/runtime/runtime.toml``), and homelab's actual deployment mounts them as two
    separate volumes. Reusing :class:`RuntimeConfigWatcher` with a DIFFERENT ``directory`` /
    ``target_name`` (rather than teaching the first watcher to watch two filenames across two
    directories) keeps each watcher's debounce/hash-compare state -- and its "the directory
    mount, never a single file" requirement, ADR-0019 (runtime config hot-reload) §12 -- scoped
    to the one file it owns. Control-plane processes only (``api``, the control worker): an
    agent process holds no backend registry to reload (``AgentSettings`` has no ``backends``
    field), so ``src/phaze/tasks/agent_worker.py`` does not call this.

    Reuses ``runtime_config_watch_polling``/``..._poll_interval_seconds``/``..._debounce_seconds``
    rather than adding a second set of knobs for a second directory -- an implementer decision:
    ADR-0019 (runtime config hot-reload) does not call for backends.toml to have its own watch
    cadence, and this file's own D-09/D-08 evidence (native watchdog silently missing every
    Colima/virtiofs host edit) applies identically to whichever directory is being watched.
    """
    settings = get_settings()
    path = backends_config_path(ControlSettings.model_config)
    return RuntimeConfigWatcher(
        store,
        path.parent,
        target_name=path.name,
        polling=settings.runtime_config_watch_polling,
        poll_interval=settings.runtime_config_watch_poll_interval_seconds,
        settle_seconds=settings.runtime_config_watch_debounce_seconds,
    )
