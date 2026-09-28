"""``backends.toml`` hot-reload (``phaze-mvq8z.8``): re-validate + swap the control-plane
backend registry from the same trigger plumbing ``phaze.runtime_config``'s ``RuntimeConfig``
snapshot already uses -- SIGHUP, the runtime-config directory watch, and (once
``phaze-mvq8z.6`` lands) the admin API.

**Why this needs its own hook, not ``register_applier``/``register_validator``.**
``RuntimeConfig`` (``phaze-mvq8z.4``) is the immutable snapshot of the numeric/string
``RELOADABLE_KEYS``; an applier/validator fires only when THAT snapshot's digest changes.
``backends``/``buckets`` are deliberately excluded from it -- they are TOML-only
(``runtime_config._BACKENDS_TOML_KEYS``) and live on the ``ControlSettings`` singleton
``get_settings()`` returns instead -- so a ``backends.toml``-only edit never moves
``RuntimeConfig``'s digest, and a hook gated on it would never run. That gap is exactly what a
dispatcher note on this bead flagged after ``phaze-mvq8z.4`` landed:
``backends.toml is NOT in the RuntimeConfig snapshot, so a backends-only edit changes no
digest and fires no applier/validator``. **Implementer decision (labelled per the bead):**
close it by driving the registry re-validate + swap from the SAME ``store.reload(source)``
call, via :meth:`~phaze.runtime_config.RuntimeConfigStore.register_registry_reload_hook` --
NOT by adding a ``backends_digest`` field to ``RuntimeConfig`` itself. The rejected
alternative would have grown ``RELOADABLE_KEYS``/``RESTART_ONLY_KEYS``, and
``tests/shared/config/test_runtime_config.py::test_the_layer_table_covers_every_reloadable_key``
asserts that set is EXACTLY the core bead's own layer table -- widening it here would either
break that already-landed, already-tested invariant or require touching it out from under a
concurrently-developed sibling bead (``phaze-mvq8z.7``) for a value (a content digest) that
was never actually a *resolvable, layered* setting in ADR-0019 (runtime config hot-reload)'s
sense. A registry hook, by contrast, is pure addition: it changes no existing behavior for a
store with none registered (the common case in every one of ``phaze-mvq8z.4``'s and
``phaze-mvq8z.5``'s own tests).

**Reload semantics** (ADR-0019 (runtime config hot-reload) §9). On a real content change:
fully re-parse and re-validate ``backends.toml`` the SAME way ``ControlSettings`` does at
construction (:func:`phaze.config_control.load_backend_registry`, including eager ``*_file``
secret resolution) -- an invalid file rejects the reload and keeps the settings singleton's
last-good registry, logged and reported, never raised into the SIGHUP handler or the watcher.
A candidate that would REMOVE a backend still referenced by an in-flight ``cloud_job`` row
(the T-82-A1 double-dispatch guard's ``_ACTIVE_CLOUD_STATUSES``) is rejected outright, naming
every blocked backend and its count -- the WHOLE reload fails closed, never a partial
registry, exactly like ``RuntimeConfigStore.reload``'s own validate-then-swap contract.
Otherwise the registry is swapped onto the ``ControlSettings`` singleton IN PLACE (plain
attribute assignment -- ``ControlSettings`` is not ``validate_assignment``/frozen, and every
consumer already reads ``settings.backends`` FRESH per use, per
``phaze.services.backends.registry``'s own docstring), and
``ControlSettings.log_effective_registry()`` logs the resolved registry old -> new (REG-04).
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
from typing import TYPE_CHECKING

from sqlalchemy import func, select
import structlog

from phaze.config_control import ControlSettings, backends_config_path, load_backend_registry
from phaze.models.cloud_job import CloudJob
from phaze.services.pipeline.common import _ACTIVE_CLOUD_STATUSES


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.runtime_config import ReloadSource, RuntimeConfigStore


logger = structlog.get_logger(__name__)

#: A callable returning one ``AsyncSession`` usable as ``async with session_factory() as
#: session:`` -- the shape ``async_sessionmaker()`` itself already satisfies (an
#: ``async_sessionmaker`` instance IS this callable; ``AsyncSession`` implements
#: ``__aenter__``/``__aexit__`` directly, no generator involved). Both
#: ``phaze.database.async_session`` (the api process) and ``ctx["async_session"]`` (the control
#: worker's per-task-engine sessionmaker) satisfy it as-is. Injected rather than imported so this
#: module opens no session and needs no engine at import time.
SessionFactory = Callable[[], "AsyncSession"]


def _content_digest(path: Path) -> str | None:
    """SHA-256 of ``path``'s bytes, or ``None`` if it does not exist right now.

    Mirrors ``runtime_config_triggers._content_digest`` exactly (a real, distinct "absent"
    value, not "unknown") -- deliberately not imported from there, since that module's digest
    is scoped to ``runtime_config_dir``'s watch and this one to a config file that may live in
    an entirely different, separately-mounted directory (``PHAZE_BACKENDS_CONFIG_FILE``).
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("phaze.runtime_config_backends: could not read backends.toml for hashing", path=str(path), error=str(exc))
        return None


class BackendsRegistryReloader:
    """Validate-then-swap ``ControlSettings.backends``/``.buckets`` in place, on every reload.

    Owns its OWN change detection (a content digest of ``backends.toml``, independent of
    ``RuntimeConfig``'s) so it can be driven by
    :meth:`~phaze.runtime_config.RuntimeConfigStore.register_registry_reload_hook`, which runs
    unconditionally on every ``reload(source)`` attempt rather than only when the RELOADABLE_KEYS
    snapshot changes value.
    """

    def __init__(self, settings: ControlSettings, *, session_factory: SessionFactory, path: Path | None = None) -> None:
        self._settings = settings
        # Deliberately `ControlSettings.model_config`, the real class's, NOT `type(settings)`'s:
        # `model_config` is a class-level constant that never varies by instance, and several
        # startup tests pass a bare `MagicMock()` in place of `settings` (mirroring
        # `runtime_config_triggers.build_watcher`'s own docstring on this exact hazard), which
        # has no real `model_config` to read.
        self._path = path if path is not None else backends_config_path(ControlSettings.model_config)
        self._session_factory = session_factory
        # Seeded at construction (mirrors RuntimeConfigWatcher) so an edit made between
        # construction and the first reload() call is still detected as a change.
        self._last_digest = _content_digest(self._path)

    async def reload(self, source: ReloadSource) -> None:
        """The registry hook body: ``RegistryReloadHook`` (``phaze.runtime_config``)."""
        digest = _content_digest(self._path)
        if digest == self._last_digest:
            return  # No content change since the last APPLIED (or first-seen) registry.
        try:
            backends, buckets = load_backend_registry(ControlSettings.model_config)
        except Exception as exc:  # pydantic ValidationError, ValueError, OSError, TOMLDecodeError
            logger.warning("phaze.runtime_config_backends reload rejected", source=source, error=str(exc))
            return
        removed_ids = {backend.id for backend in self._settings.backends} - {backend.id for backend in backends}
        if removed_ids:
            blocking = await self._in_flight_counts(removed_ids)
            if blocking:
                detail = ", ".join(f"{backend_id} ({count} in-flight)" for backend_id, count in sorted(blocking.items()))
                logger.warning(
                    "phaze.runtime_config_backends reload rejected: backend(s) still referenced by in-flight cloud_job rows",
                    source=source,
                    blocking=blocking,
                    detail=detail,
                )
                return
        old_ids = sorted(backend.id for backend in self._settings.backends)
        self._settings.backends = backends
        self._settings.buckets = buckets
        self._settings.log_effective_registry()
        logger.info(
            "phaze.runtime_config_backends reload applied",
            source=source,
            old_backend_ids=old_ids,
            new_backend_ids=sorted(backend.id for backend in backends),
        )
        self._last_digest = digest

    async def _in_flight_counts(self, backend_ids: set[str]) -> dict[str, int]:
        """``{backend_id: count}`` for every removed id with >=1 ACTIVE (non-terminal) cloud_job row.

        Reuses ``_ACTIVE_CLOUD_STATUSES`` (the T-82-A1 double-dispatch guard's own "in-flight"
        definition), rather than inventing a second status set that could drift from it.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(CloudJob.backend_id, func.count())
                    .where(CloudJob.backend_id.in_(backend_ids), CloudJob.status.in_(_ACTIVE_CLOUD_STATUSES))
                    .group_by(CloudJob.backend_id)
                )
            ).all()
        return {backend_id: count for backend_id, count in rows if backend_id is not None}


def build_backends_registry_reloader(
    store: RuntimeConfigStore, settings: ControlSettings, session_factory: SessionFactory
) -> BackendsRegistryReloader:
    """Build a :class:`BackendsRegistryReloader` and register it on ``store``.

    The one call site every control-plane process (``api``, the control worker) makes once its
    DB session factory is ready -- see ``src/phaze/main.py`` and
    ``src/phaze/tasks/controller.py`` for the exact placement (after migrations/engine
    construction, matching this reloader's need for a live ``cloud_job`` table).
    """
    reloader = BackendsRegistryReloader(settings, session_factory=session_factory)
    store.register_registry_reload_hook("backends_registry", reloader.reload)
    return reloader
