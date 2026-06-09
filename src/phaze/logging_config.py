"""Central structlog configuration for every Phaze process.

A single :func:`configure_logging` entry point routes structlog-native logs AND
foreign stdlib / uvicorn / SAQ logs through one consistent pipeline -- JSON when
stdout is not a TTY (production / Docker), human-friendly console otherwise. It is
called once per OS process: the FastAPI lifespan, each SAQ worker ``startup`` hook,
the watcher ``main()``, and the CLI / script entry points.

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
import os
import sys

import structlog


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


def configure_logging(*, level: str | None = None, json_logs: bool | None = None) -> None:
    """Configure structlog + the stdlib root logger for the current process.

    Keyword-only and env-fallback so it is decoupled from full settings
    construction: the watcher calls it bare (env-driven) before ``get_settings()``,
    while settings-holding entry points pass ``level=cfg.log_level`` /
    ``json_logs=cfg.log_json`` through.

    Idempotent: re-calling clears existing root handlers first, so exactly one
    handler is ever attached and a second call simply re-applies the (possibly
    changed) level / renderer.
    """
    level_int = _resolve_level(level)
    json_output = _resolve_json(json_logs)

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
        cache_logger_on_first_use=True,
    )

    renderer: structlog.types.Processor = structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    # Idempotent root-handler reset: clear first so re-calling never stacks handlers.
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level_int)

    # Tame noisy libraries: keep them at WARNING unless we are explicitly at DEBUG.
    noisy_level = level_int if level_int <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(noisy_level)

    # Route uvicorn's own loggers through the root pipeline (drop their handlers,
    # let records propagate) so api access/error logs render identically.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
