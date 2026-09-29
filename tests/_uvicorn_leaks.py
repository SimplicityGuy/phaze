"""Undo the process-global logging state that constructing a ``uvicorn.Config`` leaves behind.

``uvicorn.Config.__init__`` runs uvicorn's own ``configure_logging()``, which does two things to
the WHOLE pytest process and never undoes either:

* ``logging.config.dictConfig(LOGGING_CONFIG)`` sets the levels of the ``uvicorn`` /
  ``uvicorn.error`` / ``uvicorn.access`` loggers (phaze-pv3kk: a later test asserting on an INFO
  record from ``uvicorn.error`` then sees nothing);
* ``logging.addLevelName(5, "TRACE")`` registers a TRACE level. After that,
  ``logging.getLevelNamesMapping()`` knows ``TRACE``, so
  ``tests/shared/config/test_runtime_config.py::test_an_unknown_start_time_log_level_reports_the_info_configure_logging_falls_back_to``
  sees ``TRACE`` accepted as a real level (phaze-mvq8z.11: red in a full-suite gate lane where a
  uvicorn fixture ran first; ``test_agent_client_tls.py`` followed by that test reproduces it).

Every test that constructs a ``uvicorn.Config`` wraps the construction AND the server's lifetime
in :func:`contained_uvicorn_logging`, so the collection order stops mattering.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterator


_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@contextlib.contextmanager
def contained_uvicorn_logging() -> Iterator[None]:
    """Snapshot the uvicorn logger levels and the level-name registry; restore both on exit."""
    saved_levels = {name: logging.getLogger(name).level for name in _UVICORN_LOGGERS}
    saved_names = logging.getLevelNamesMapping()
    try:
        yield
    finally:
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
        # The stdlib has no public way to unregister a level name. These two dicts are what
        # addLevelName writes, under the module lock it takes.
        with logging._lock:  # type: ignore[attr-defined]
            for name in set(logging.getLevelNamesMapping()) - set(saved_names):
                level = logging._nameToLevel.pop(name)  # type: ignore[attr-defined]
                if logging._levelToName.get(level) == name:  # type: ignore[attr-defined]
                    del logging._levelToName[level]  # type: ignore[attr-defined]
