"""Shared SAQ `before_enqueue` hook -- applies project-wide Job defaults (Phase 27 UAT Gap 1).

Background
----------
SAQ 0.26.3's ``Worker.__init__`` does **not** accept ``timeout``, ``retries``, or
``keep_result``. Those keys are per-Job settings (defaults: 10s timeout, 1 retry,
600s ttl) and must be applied to each :class:`saq.Job` individually -- either at
``Queue.enqueue(...)`` call sites or via a ``before_enqueue`` callback registered
on the :class:`saq.Queue`.

Phaze previously passed the three keys through the ``settings`` dict consumed by
``saq <module>.settings`` (see ``phaze.tasks.controller.settings`` and
``phaze.tasks.agent_worker.settings``). The CLI then handed the dict to
``Worker.__init__`` which rejected the unknown kwargs with ``TypeError`` -- this
prevented the ``worker`` service from starting on a fresh docker compose stack
(Phase 27 UAT Gap 1).

The fix:

1. Drop ``timeout`` / ``retries`` / ``keep_result`` from both ``settings`` dicts.
2. Preserve the project's policy defaults (longer timeouts + retry budget than
   SAQ ships with) by registering :func:`apply_project_job_defaults` as a
   ``before_enqueue`` callback on each Queue. The hook reads
   :func:`phaze.config.get_settings` to obtain the role's
   ``worker_job_timeout`` / ``worker_max_retries`` / ``worker_keep_result``
   values and applies them to every Job whose corresponding attribute is still
   at its SAQ default.

The "still at its SAQ default" check is necessary because enqueue call sites
(e.g., :mod:`phaze.tasks.execution`) deliberately override per-job settings
for specific batches -- we MUST NOT clobber those overrides.

Both :class:`ControlSettings` and :class:`AgentSettings` expose the three knobs
on the shared :class:`BaseSettings` base, so the hook works for both roles
without further dispatch.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from phaze import config as _config


if TYPE_CHECKING:
    from saq import Job


logger = logging.getLogger(__name__)


# SAQ 0.26.3 Job dataclass defaults -- pinned here so the "still at default"
# predicate is explicit and grep-able. If SAQ bumps these, this module is the
# single source of truth that needs updating.
_SAQ_DEFAULT_TIMEOUT = 10
_SAQ_DEFAULT_RETRIES = 1
_SAQ_DEFAULT_TTL = 600


async def apply_project_job_defaults(job: Job) -> None:
    """SAQ ``before_enqueue`` hook -- apply Phaze's policy defaults to ``job``.

    Reads :func:`phaze.config.get_settings` for the running role's policy values
    (``worker_job_timeout``, ``worker_max_retries``, ``worker_keep_result``) and
    overrides the job's ``timeout`` / ``retries`` / ``ttl`` ONLY when the job
    still carries the SAQ default. Call sites that pass explicit values to
    :func:`saq.Queue.enqueue` are left alone.

    The hook is registered via ``Queue.register_before_enqueue(...)`` from each
    role's settings module (``controller.py`` + ``agent_worker.py``). SAQ awaits
    the callback before persisting the job to Redis, so attribute mutations
    here are seen by the worker that later dequeues the job.
    """
    # Resolve via the `phaze.config` module attribute (not a local import) so
    # tests can monkeypatch `phaze.config.get_settings` and see the override.
    cfg = _config.get_settings()

    if job.timeout == _SAQ_DEFAULT_TIMEOUT:
        job.timeout = cfg.worker_job_timeout
    if job.retries == _SAQ_DEFAULT_RETRIES:
        job.retries = cfg.worker_max_retries
    if job.ttl == _SAQ_DEFAULT_TTL:
        job.ttl = cfg.worker_keep_result


__all__ = ["apply_project_job_defaults"]
