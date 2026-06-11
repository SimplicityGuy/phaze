"""Testable mount helper for the SAQ monitoring dashboard (Phase 33).

This isolates the single ``saq_web`` call behind a pure function so the Wave 2
lifespan body stays thin and the mount can be unit-tested over in-memory queue
fakes (``tests/_queue_fakes.py::FakeQueue``) without booting the app, the DB, or
Redis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from saq.web.starlette import saq_web


if TYPE_CHECKING:
    from saq import Queue
    from starlette.applications import Starlette


# The mount path and the ``root_path`` passed to ``saq_web`` MUST be identical:
# the rendered HTML bakes asset URLs as ``{root_path}/static/...`` and the outer
# mount strips this same prefix, so any mismatch breaks every CSS/JS asset link.
_MOUNT_PATH = "/saq"


def build_saq_app(queues: list[Queue]) -> Starlette:
    """Wrap the single ``saq_web("/saq", queues)`` call in a pure factory.

    The returned Starlette app exposes the SAQ dashboard routes (``/``,
    ``/api/queues``, per-queue/job views, retry/abort POSTs, ``/static``,
    ``/health``) and reads all queue/job state via the PASSED queue instances'
    ``.info()`` — it never opens a second Redis pool (the LOCKED no-second-pool
    decision). This helper constructs no ``Queue``, never calls
    ``Queue.from_url``, and never builds a Redis client; it only wraps the
    instances it is handed.

    WARNING — call exactly once per process. ``saq_web`` stores its queue
    registry in the module-level globals ``saq.web.starlette.QUEUES`` /
    ``ROOT_PATH`` and CLEARS ``QUEUES`` on every call
    (``saq/web/starlette.py:135``). A second call in the same process wipes the
    first call's registry, so production MUST mount the dashboard a single time.
    """
    return saq_web(_MOUNT_PATH, queues=queues)
