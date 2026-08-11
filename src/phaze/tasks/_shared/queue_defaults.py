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

Per-function job policy (phaze-plpnf, re-shaped by phaze-w55w1)
--------------------------------------------------------------
Live logs on 2026-08-11 showed a steady stream of ``process_file`` jobs dying at exactly 600s
with a SAQ ``TimeoutError``, carrying ``timeout=600`` / ``retries=4`` -- the ROLE defaults above
-- while ``process_file``'s sole producer (:func:`phaze.services.analysis_enqueue.enqueue_process_file`)
pinned an explicit policy (``timeout=7200`` / ``retries=2`` at the time; ``timeout=0`` /
``retries=2`` since phaze-w55w1). Root cause: recovery's ledger replay
(:func:`phaze.tasks.reenqueue._replay_row`) replays a ``scheduling_ledger`` row's STORED
``timeout``/``retries`` when present, but those columns are nullable and rows written before the
Phase-45 capture columns existed carry NULL -- so the replay omits both kwargs entirely and this
hook's generic "still at the SAQ default" fill runs instead, landing on the ROLE default
(600s/4 retries) rather than ``process_file``'s own policy. A long analysis then burns its whole
retry budget at 1/12th of the timeout it needs and goes terminal ``ANALYSIS_FAILED``.

The generic fill above is deliberately GENERIC -- it exists to give every job a sane role-level
default, and call sites that want something else pass explicit values, which the "still at SAQ
default" check preserves. It has no way to know that ONE function (``process_file``) has its own
policy that is not the role default. :data:`_FUNCTION_JOB_POLICY` closes that gap: after the
generic fill runs, a function listed here has its ``timeout`` PINNED to the policy value and its
``retries`` capped to at most the policy's maximum (never run with MORE retry churn than its
policy budgets -- each retry can itself run a full analysis, so extra retries are not "safer",
they are extra hours). This is enforced regardless of producer or replay path: the SOLE current
``process_file`` producer already emits exactly that policy, so this step is a no-op on that path
and only bites a job that reached this hook WITHOUT it -- exactly the replay-with-NULL-bounds
path above.
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

# phaze-plpnf: per-function policy -- ``{function: (timeout, max_retries)}``.
# Enforced AFTER the generic "still at SAQ default" fill above, unconditionally on every
# enqueue of that function (independent of producer/replay path). ``timeout`` is PINNED
# exactly; ``retries`` is capped to at most the value. Grep-able single source of truth --
# add a new pinned-policy function here, never as a one-off clobber-guard scattered at a
# call site.
#
# phaze-w55w1 changed ``timeout`` from a FLOOR ("raise to at least") to a PIN ("set to
# exactly"). A floor cannot express ``process_file``'s policy any more, because that policy is
# now ``timeout=0`` -- SAQ's "disabled" -- and ``0`` is below every value a floor could raise
# from, so a floor would silently leave a replayed job on the 600s role default and reintroduce
# the exact phaze-plpnf defect (a long analysis dying at 1/12th the bound it needs) that this
# table exists to prevent. Pinning is also strictly more faithful to what the table means: a
# function listed here has ONE timeout policy, not a minimum.
_FUNCTION_JOB_POLICY: dict[str, tuple[int, int]] = {
    # analyze: phaze.services.analysis_enqueue.enqueue_process_file pins timeout=0/retries=2
    # (no wall-clock net -- exhaustive analysis runs for hours by design, ADR-0007 §7 -- plus the
    # locked 1-2 retry band that kills long-file re-analysis churn).
    "process_file": (0, 2),
}

# Functions whose ``heartbeat`` this hook must also pin, and from which setting.
#
# ``heartbeat`` is pinned here and not left to the producer alone because ``timeout=0`` makes it
# the ONLY liveness signal SAQ has: ``Job.stuck`` is
# ``(timeout and ...) or (heartbeat and ...)``, so a ``process_file`` row carrying 0 for BOTH is
# permanently un-stuck -- invisible to SAQ's sweep, excluded from both key reapers (they skip
# ``timeout: 0`` rows by design), and forever "in_flight" to
# ``classify_process_file_collision``. One worker crash would then hold that file's
# deterministic key until a human noticed.
#
# That is not hypothetical: ``reenqueue._replay_row`` replays a ``scheduling_ledger`` row's
# STORED bounds, the ledger captures ``timeout``/``retries`` and NOT ``heartbeat``, so every
# recovery replay reaches this hook with ``heartbeat`` unset. Pinning it here -- the one
# chokepoint every producer and every replay path passes through -- is what makes a replayed
# job as supervised as a fresh one.
_FUNCTION_HEARTBEAT_POLICY: dict[str, str] = {
    "process_file": "analysis_job_heartbeat_sec",
}


async def apply_project_job_defaults(job: Job) -> None:
    """SAQ ``before_enqueue`` hook -- apply Phaze's policy defaults to ``job``.

    Reads :func:`phaze.config.get_settings` for the running role's policy values
    (``worker_job_timeout``, ``worker_max_retries``, ``worker_keep_result``) and
    overrides the job's ``timeout`` / ``retries`` / ``ttl`` ONLY when the job
    still carries the SAQ default. Call sites that pass explicit values to
    :func:`saq.Queue.enqueue` are left alone.

    Then enforces :data:`_FUNCTION_JOB_POLICY` and :data:`_FUNCTION_HEARTBEAT_POLICY`
    for any function listed there (phaze-plpnf, phaze-w55w1): ``job.timeout`` and
    ``job.heartbeat`` are PINNED to the policy values and ``job.retries`` is capped
    to at most the policy maximum, UNCONDITIONALLY -- this step does not care whether
    the generic fill above just ran or the job already carried an explicit policy.
    That makes it the single chokepoint guaranteeing ``process_file`` runs under its
    current policy (``timeout=0`` + a derived progress ``heartbeat``, ``retries=2``)
    no matter which producer or replay path enqueued it -- including a recovery replay
    of a legacy ``scheduling_ledger`` row with NULL captured bounds, or one carrying
    the pre-phaze-w55w1 7200s wall clock that must NOT be honoured.

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

    policy = _FUNCTION_JOB_POLICY.get(job.function)
    if policy is not None:
        pinned_timeout, max_retries = policy
        job.timeout = pinned_timeout
        if job.retries > max_retries:
            job.retries = max_retries

    heartbeat_field = _FUNCTION_HEARTBEAT_POLICY.get(job.function)
    if heartbeat_field is not None:
        # Pinned, not filled-if-absent: a replayed row can carry a STALE heartbeat from an older
        # deployment's configuration just as easily as none at all, and both must land on the
        # current policy. See _FUNCTION_HEARTBEAT_POLICY for why an unpinned heartbeat on a
        # timeout=0 job is a permanently un-sweepable row.
        job.heartbeat = getattr(cfg, heartbeat_field)


__all__ = ["apply_project_job_defaults"]
