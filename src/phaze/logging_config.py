"""Central structlog configuration for every Phaze process.

A single :func:`configure_logging` entry point routes structlog-native logs AND
foreign stdlib / uvicorn / SAQ logs through one consistent pipeline -- JSON when
stdout is not a TTY (production / Docker), human-friendly console otherwise. It is
called once per OS process: the FastAPI lifespan, each SAQ worker ``startup`` hook,
the watcher ``main()``, and the CLI / script entry points.

Opt-in dual rendering (``PHAZE_LOG_FRIENDLY``): the one-shot job_runner pod wants a
human-readable line alongside the machine JSON line so an operator tailing pod logs
can follow progress without a JSON pretty-printer. Setting ``PHAZE_LOG_FRIENDLY``
truthy attaches a SECOND stdlib handler with its own :class:`structlog.dev.ConsoleRenderer`
formatter next to the primary handler -- both formatters render from the SAME event
dict (structlog's ``ProcessorFormatter.format`` takes a fresh ``.copy()`` of it per
formatter, see the stdlib source), so the two renderings can never drift apart or
require a second log call site. Default is off, and with it off every process's
output is byte-identical to before this existed.

Import-boundary invariant (Phase 26 D-25 / tests/test_task_split.py):
    This module is imported transitively by the agent worker, the watcher, and the
    ``tasks/_shared`` bootstrap modules, all of which MUST run on a host with no
    Postgres reachable. It therefore imports ONLY the standard library + structlog --
    never ``phaze.*`` (especially not ``phaze.config`` / ``phaze.database``) and never
    SQLAlchemy. The env-fallback resolution below is what lets the watcher configure
    logging BEFORE constructing settings, so a pydantic ``ValidationError`` can still
    be logged through the same pipeline.
"""

from __future__ import annotations

import logging
from logging import getLogger as _stdlib_get_logger
import os
import sys

import structlog


# This module is the ONE place that manipulates stdlib loggers directly (the root
# logger plus the uvicorn / noisy-library loggers), via the ``_stdlib_get_logger``
# accessor imported above. Every *module* logger uses ``structlog.get_logger``; these
# calls are logging infrastructure, so the project-wide stdlib-getLogger migration
# gate stays empty (the literal accessor is imported, never dotted off ``logging``).

_NOISY_LIBRARIES = ("httpx", "httpcore", "asyncio")
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _parse_bool(value: str) -> bool:
    """Parse a human-written boolean env value (``true``/``1``/``yes`` -> True)."""
    return value.strip().lower() in _TRUE_VALUES


def _resolve_level(level: str | None) -> int:
    """Resolve an explicit/env/default level name to its stdlib integer.

    Precedence: explicit ``level`` arg, then ``PHAZE_LOG_LEVEL``, then ``INFO``.
    Unknown names fall back to ``INFO`` rather than raising so a typo never
    silences logging entirely.
    """
    name = (level or os.environ.get("PHAZE_LOG_LEVEL") or "INFO").upper()
    return logging.getLevelNamesMapping().get(name, logging.INFO)


def _resolve_friendly(friendly: bool | None) -> bool:
    """Decide whether to attach a second, human-friendly console handler.

    Precedence: explicit ``friendly`` arg, then ``PHAZE_LOG_FRIENDLY`` env, then
    default False -- today's single-rendering behavior is unchanged unless a
    caller (or the pod's environment) opts in explicitly.
    """
    if friendly is not None:
        return friendly
    env_value = os.environ.get("PHAZE_LOG_FRIENDLY")
    if env_value is None or env_value.strip() == "":
        return False
    return _parse_bool(env_value)


def _resolve_json(json_logs: bool | None) -> bool:
    """Decide JSON vs console rendering.

    Precedence: explicit ``json_logs`` arg, then ``PHAZE_LOG_JSON`` env, then the
    auto rule -- JSON when stdout is not a TTY (the Docker / production case),
    console when attached to an interactive terminal.
    """
    if json_logs is not None:
        return json_logs
    env_value = os.environ.get("PHAZE_LOG_JSON")
    if env_value is not None and env_value.strip() != "":
        return _parse_bool(env_value)
    return not sys.stdout.isatty()


def configure_logging(*, level: str | None = None, json_logs: bool | None = None, friendly: bool | None = None) -> None:
    """Configure structlog + the stdlib root logger for the current process.

    Keyword-only and env-fallback so it is decoupled from full settings
    construction: the watcher calls it bare (env-driven) before ``get_settings()``,
    while settings-holding entry points pass ``level=cfg.log_level`` /
    ``json_logs=cfg.log_json`` through.

    ``friendly`` (env fallback ``PHAZE_LOG_FRIENDLY``) opts a process into dual
    rendering: the primary handler (JSON or console, exactly as resolved without
    this parameter) is unchanged, and a second handler renders the SAME event
    through :class:`structlog.dev.ConsoleRenderer` -- see the module docstring for
    why the two can't drift. Colors are forced off on the friendly renderer so
    output stays deterministic (no ANSI escapes) regardless of terminal detection.

    Idempotent: re-calling clears existing root handlers first, so exactly one
    handler set is ever attached -- one handler normally, two when ``friendly`` is
    enabled -- and a second call simply re-applies the (possibly changed) level /
    renderer(s) rather than accumulating handlers.
    """
    level_int = _resolve_level(level)
    json_output = _resolve_json(json_logs)
    friendly_enabled = _resolve_friendly(friendly)

    # Shared chain -- order matters. PositionalArgumentsFormatter is CRITICAL: it
    # keeps every legacy ``logger.info("text %s", value)`` call interpolating after
    # the mechanical get_logger swap. Dropping it would render literal "%s".
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        # cache_logger_on_first_use=False (NOT the structlog default of True): a True
        # cache freezes each module-level ``structlog.get_logger(__name__)`` proxy at
        # the level active on its FIRST log call -- a later configure_logging() with a
        # different level is then silently ignored for that already-used logger (the
        # cached bound logger survives even structlog.reset_defaults()). Production
        # configures logging exactly once per process before any logging, so True would
        # be safe there; but False makes this module's documented idempotent-reconfigure
        # guarantee actually hold (level changes take effect for every logger) at a
        # negligible per-call rebuild cost for this app's log volume.
        cache_logger_on_first_use=False,
    )

    renderer: structlog.types.Processor = structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    # Idempotent root-handler reset: clear first so re-calling never stacks handlers.
    root = _stdlib_get_logger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    if friendly_enabled:
        # Second sink over the SAME event dict -- ProcessorFormatter.format() takes a
        # fresh `.copy()` of the stored event dict per formatter call (see structlog's
        # stdlib source), so this handler's remove_processors_meta/renderer mutating
        # its copy can never affect the primary handler's rendering (or vice versa),
        # and there is no second log call site to drift out of sync. colors=False
        # keeps the friendly line deterministic (no ANSI escapes) whether or not
        # stdout is attached to a real terminal.
        friendly_formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, structlog.dev.ConsoleRenderer(colors=False)],
        )
        friendly_handler = logging.StreamHandler(sys.stdout)
        friendly_handler.setFormatter(friendly_formatter)
        root.addHandler(friendly_handler)

    root.setLevel(level_int)

    # Tame noisy libraries: keep them at WARNING unless we are explicitly at DEBUG.
    noisy_level = level_int if level_int <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LIBRARIES:
        _stdlib_get_logger(name).setLevel(noisy_level)

    # Route uvicorn's own loggers through the root pipeline (drop their handlers,
    # let records propagate) so api access/error logs render identically.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = _stdlib_get_logger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
