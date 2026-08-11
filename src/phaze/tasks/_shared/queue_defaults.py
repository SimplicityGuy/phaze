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

Per-function policy floor (phaze-plpnf)
----------------------------------------
Live logs on 2026-08-11 showed a steady stream of ``process_file`` jobs dying at exactly 600s
with a SAQ ``TimeoutError``, carrying ``timeout=600`` / ``retries=4`` -- the ROLE defaults above
-- while ``process_file``'s sole producer (:func:`phaze.services.analysis_enqueue.enqueue_process_file`)
always pins ``timeout=7200`` / ``retries=2``. Root cause: recovery's ledger replay
(:func:`phaze.tasks.reenqueue._replay_row`) replays a ``scheduling_ledger`` row's STORED
``timeout``/``retries`` when present, but those columns are nullable and rows written before the
Phase-45 capture columns existed carry NULL -- so the replay omits both kwargs entirely and this
hook's generic "still at the SAQ default" fill runs instead, landing on the ROLE default
(600s/4 retries) rather than ``process_file``'s own policy. A long analysis then burns its whole
retry budget at 1/12th of the timeout it needs and goes terminal ``ANALYSIS_FAILED``.

The generic fill above is deliberately GENERIC -- it exists to give every job a sane role-level
default, and call sites that want something else pass explicit values, which the "still at SAQ
default" check preserves. It has no way to know that ONE function (``process_file``) has its own
policy that is not the role default. :data:`_FUNCTION_POLICY_FLOOR` closes that gap: after the
generic fill runs, a function listed here has its ``timeout`` raised to at least the floor (never
run with LESS protection than its policy affords) and its ``retries`` capped to at most the floor
(never run with MORE retry churn than its policy budgets -- each retry can itself run up to the
full floor timeout, so extra retries are not "safer", they are extra hours). This is enforced
regardless of producer or replay path: the SOLE current ``process_file`` producer already emits
exactly ``timeout=7200``/``retries=2``, so the floor is a no-op on that path (7200 is not < 7200;
2 is not > 2) and only bites a job that reached this hook WITHOUT that explicit policy -- exactly
the replay-with-NULL-bounds path above.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from phaze import config as _config


if TYPE_CHECKING:
    from saq import Job


logger = structlog.get_logger(__name__)


# SAQ 0.26.3 Job dataclass defaults -- pinned here so the "still at default"
# predicate is explicit and grep-able. If SAQ bumps these, this module is the
# single source of truth that needs updating.
_SAQ_DEFAULT_TIMEOUT = 10
_SAQ_DEFAULT_RETRIES = 1
_SAQ_DEFAULT_TTL = 600

# phaze-plpnf: per-function policy floor -- ``{function: (min_timeout, max_retries)}``.
# Enforced AFTER the generic "still at SAQ default" fill above, unconditionally on every
# enqueue of that function (independent of producer/replay path). A job's ``timeout`` is
# raised to at least the floor; its ``retries`` is capped to at most the floor. Grep-able
# single source of truth -- add a new pinned-policy function here, never as a one-off
# clobber-guard scattered at a call site.
_FUNCTION_POLICY_FLOOR: dict[str, tuple[int, int]] = {
    # analyze: phaze.services.analysis_enqueue.enqueue_process_file pins timeout=7200/retries=2
    # (Phase 43 outer safety net + the locked 1-2 retry band that kills long-file re-analysis
    # churn). Never let process_file run under that protection, regardless of how it was
    # enqueued -- see the module docstring's "Per-function policy floor" section.
    "process_file": (7200, 2),
}


async def apply_project_job_defaults(job: Job) -> None:
    """SAQ ``before_enqueue`` hook -- apply Phaze's policy defaults to ``job``.

    Reads :func:`phaze.config.get_settings` for the running role's policy values
    (``worker_job_timeout``, ``worker_max_retries``, ``worker_keep_result``) and
    overrides the job's ``timeout`` / ``retries`` / ``ttl`` ONLY when the job
    still carries the SAQ default. Call sites that pass explicit values to
    :func:`saq.Queue.enqueue` are left alone.

    Then enforces :data:`_FUNCTION_POLICY_FLOOR` for any function listed there
    (phaze-plpnf): ``job.timeout`` is raised to at least the floor's minimum and
    ``job.retries`` is capped to at most the floor's maximum, UNCONDITIONALLY --
    this step does not care whether the generic fill above just ran or the job
    already carried an explicit policy. That makes it the single chokepoint that
    guarantees ``process_file`` can never run under its 7200s/retries=2 policy no
    matter which producer or replay path (including a recovery replay of a
    legacy ``scheduling_ledger`` row with NULL captured bounds) enqueued it.

    The hook is registered via ``Queue.register_before_enqueue(...)`` from each
    role's settings module (``controller.py`` + ``agent_worker.py``). SAQ awaits
    the callback before persisting the job to the ``PostgresQueue`` broker (Phase
    36), so attribute mutations here are seen by the worker that later dequeues
    the job.
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

    floor = _FUNCTION_POLICY_FLOOR.get(job.function)
    if floor is not None:
        min_timeout, max_retries = floor
        if job.timeout < min_timeout:
            job.timeout = min_timeout
        if job.retries > max_retries:
            job.retries = max_retries


__all__ = ["apply_project_job_defaults"]
