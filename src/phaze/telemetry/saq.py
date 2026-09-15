"""SAQ worker instrumentation -- per-job duration and outcome.

Queue DEPTH is not here. The only sampler phaze has is the admin UI's stage-activity
snapshot, which groups by pipeline stage rather than by SAQ queue; it is published as
``phaze.pipeline.stage.inflight`` from ``telemetry/pipeline.py`` under a label that says
what it actually is.

Wired as ordinary SAQ ``before_process`` / ``after_process`` hooks, the same mechanism
phaze already uses for ``enforce_stage_pause_on_process`` and ``increment_completed``, so
there is no new extension point and no wrapper around the function registry.

The ``after_process`` side is registered through :func:`after_process_chain` rather than as
the last element of SAQ's hook list, because SAQ's list is a bare loop that the first
raising hook abandons -- see that function. The ``before_process`` side needs no equivalent:
a hook ahead of it that raises means the job never started, and the worker says so with
:func:`mark_bounced` so this module reports nothing rather than an error.

**The job function name is the label -- spelled ``saq_function``, never ``job`` -- and it
is bounded** by the union of the controller's
and the agent's registered function lists -- a closed set in this repo's source, not a
value derived from a payload. Job id, file id and kwargs never reach a metric; the job id
goes on the span. ``job`` is reserved by Prometheus for the target derived from
``service.name``, and a metric that collides with it is DROPPED ENTIRELY by the collector
rather than renamed; ``catalogue.RESERVED_LABEL_NAMES`` and its guard test are what keep
that from happening again.

phaze-zaf2l measured SAQ at 0.0939 jobs/s against a burst capacity of 318.3/s and filed no
bead against it. These metrics are therefore NOT here because the queue is suspected: they
are here so the next person asking does not have to sample ``saq_jobs`` by hand every 120
seconds for the length of a spike.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

from phaze.telemetry.instruments import add, record
from phaze.telemetry.tracing import span


log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Protocol

    #: The shape SAQ's ``before_process`` / ``after_process`` hooks take.
    SaqHook = Callable[[dict[str, Any]], Awaitable[None]]

    class ChainedHook(Protocol):
        """An :func:`after_process_chain` result -- callable, plus the order it will run in.

        ``chain`` exists for the wiring tests: collapsing the worker's ``after_process``
        list into one callable would otherwise make "is this hook registered, and where"
        unassertable, and those assertions are the ones that caught two ordering defects.
        """

        chain: tuple[SaqHook, ...]

        def __call__(self, ctx: dict[str, Any]) -> Awaitable[None]: ...


_START_KEY = "_phaze_telemetry_started"
_SPAN_KEY = "_phaze_telemetry_span"
#: Set by the worker (via :func:`mark_bounced`) when a ``before_process`` hook aborted the
#: attempt before :func:`before_process` ever ran, so :func:`after_process` can tell a
#: never-started attempt from a job that ran and failed.
_BOUNCE_KEY = "_phaze_telemetry_bounced"

#: SAQ statuses that mean the job is finished and how it finished. Anything else that
#: reaches ``after_process`` -- ``queued`` after a ``retry()`` -- is not terminal and is
#: counted as an error, because from the caller's point of view the attempt did not
#: produce a result. The ONE exception is a marked bounce (:func:`mark_bounced`), which
#: never ran a function at all.
_OK_STATUS = "complete"


def _job_name(job: Any) -> str:
    name = getattr(job, "function", None)
    return str(name) if name else "unknown"


async def before_process(ctx: dict[str, Any]) -> None:
    """SAQ ``before_process``: start the clock and open the job's span.

    ``ctx`` is a FRESH dict per job (``saq.worker.Worker.process`` builds
    ``{**self.context, "job": job}``), verified against the installed SAQ rather than
    assumed, so stashing per-job state on it cannot collide between concurrent jobs.

    Never raises. A ``before_process`` that raises kills the job before it starts.
    """
    try:
        job = ctx.get("job")
        if job is None:
            return
        ctx[_START_KEY] = time.perf_counter()
        current = span(
            "saq.job",
            {
                "phaze.saq.job": _job_name(job),
                "phaze.saq.job_id": str(getattr(job, "key", "") or ""),
                "phaze.saq.attempt": getattr(job, "attempts", 0),
            },
        )
        current.__enter__()
        ctx[_SPAN_KEY] = current
    except Exception:
        # NEVER raises: this runs in SAQ's own hook chain, alongside the ledger clear.
        # Logged rather than passed so a systematically broken hook is discoverable.
        log.debug("telemetry_saq_hook_failed", exc_info=True)


def mark_bounced(ctx: dict[str, Any]) -> None:
    """Tell :func:`after_process` that this attempt was BOUNCED, not run.

    Called by the worker's composed ``before_process`` when one of the hooks ahead of
    :func:`before_process` aborted the attempt -- in this repo, exactly one thing does that:
    ``enforce_stage_pause_on_process`` raising ``StagePausedRetry`` because the operator
    paused the stage. The job's function never ran, its retry budget is restored and both
    neighbouring ``after_process`` hooks are deliberate no-ops for it (Phase 35 D-02
    bounce-neutrality), so there is no outcome to report and no duration to report it with.

    The signal is EXPLICIT rather than inferred from the absence of :data:`_START_KEY`: a
    missing start time also describes a telemetry hook that failed internally, and those two
    must not be conflated -- one is a healthy paused system, the other is a defect.

    Never raises. It is called from a hook that is already unwinding.
    """
    try:
        ctx[_BOUNCE_KEY] = True
    except Exception:
        log.debug("telemetry_saq_hook_failed", exc_info=True)


async def after_process(ctx: dict[str, Any]) -> None:
    """SAQ ``after_process``: record duration + outcome and close the span.

    SAQ runs this in a ``finally`` after EVERY outcome, so ``job.status`` is authoritative.
    Never raises, for the same reason: this hook runs alongside phaze's ledger-clearing
    hook, and an exception here would be raised through SAQ's own ``finally``.

    It is NOT reached by SAQ's own dispatch when an earlier ``after_process`` hook raises --
    ``saq.worker.Worker._after_process`` is one bare loop and the first exception abandons
    the rest of it -- which is why the workers register it through
    :func:`after_process_chain` instead of as the last element of that list.

    Contains NO await points, deliberately. Cancellation is delivered only at a suspension
    point, so this hook cannot be interrupted part-way and cannot leak the span it closes --
    which is what makes it safe to await from a ``finally`` that is already unwinding a
    ``CancelledError``. Adding an ``await`` here would quietly remove that property.
    """
    try:
        job = ctx.get("job")
        if job is None:
            return
        if ctx.pop(_BOUNCE_KEY, False):
            # A bounce is not an outcome: nothing ran. Recording it as `error` turned an
            # operator pause into an error storm on the service-health panel (phaze-35eiv);
            # recording it as a third `outcome` value would widen a label shared with four
            # other metrics. Close any span defensively -- there should be none, because
            # `before_process` never ran -- and report nothing.
            leaked = ctx.pop(_SPAN_KEY, None)
            if leaked is not None:
                leaked.__exit__(None, None, None)
            ctx.pop(_START_KEY, None)
            return
        outcome = "ok" if str(getattr(job, "status", "")).rsplit(".", 1)[-1].lower() == _OK_STATUS else "error"
        name = _job_name(job)
        started = ctx.pop(_START_KEY, None)
        if started is not None:
            record("phaze.saq.job.duration", time.perf_counter() - started, saq_function=name, outcome=outcome)
        add("phaze.saq.jobs", 1, saq_function=name, outcome=outcome)
        current = ctx.pop(_SPAN_KEY, None)
        if current is not None:
            current.__exit__(None, None, None)
    except Exception:
        # NEVER raises: this runs in SAQ's own hook chain, alongside the ledger clear.
        # Logged rather than passed so a systematically broken hook is discoverable.
        log.debug("telemetry_saq_hook_failed", exc_info=True)


def after_process_chain(*hooks: SaqHook) -> ChainedHook:
    """The ONE ``after_process`` entry a worker registers: ``hooks`` in order, then
    :func:`after_process` in a ``finally``.

    ``saq.worker.Worker._after_process`` runs the registered hooks in a single bare loop
    (``for ap in self.after_process: await ap(ctx)``), and ``Worker.process`` wraps the whole
    loop in one ``try`` that merely logs. So a hook that raises -- a transient DB error in
    ``repark_if_stage_paused``, a raise from ``record_drain_slice_completion``, or a
    ``CancelledError`` during shutdown -- abandons every hook after it. With telemetry
    registered LAST in that list, the job's span was never ``__exit__``'d (never ended, never
    exported) and its duration and outcome were never recorded: precisely the abnormally-ended
    jobs the metrics exist to show were the ones missing from the panels (phaze-24dl8).

    This changes nothing about the neighbouring hooks' ORDER or their behaviour, including the
    abort-on-first-raise between them: ``repark_if_stage_paused`` raising must still skip
    ``increment_completed``, because repark is what stops a bounce being read as a genuine
    terminal outcome. The exception still propagates to SAQ's own handler, so a systematically
    broken hook still shows up as ``Failed to run after process hook``.

    Returns an ``async def`` closure, NOT a callable object: SAQ gates on
    ``asyncio.iscoroutinefunction(func)``, which is FALSE for an instance with an async
    ``__call__`` -- such a hook would be handed to a thread pool, which would return the
    coroutine without awaiting it and drop the telemetry silently.
    """

    async def run_after_process_chain(ctx: dict[str, Any]) -> None:
        try:
            for hook in hooks:
                await hook(ctx)
        finally:
            await after_process(ctx)

    chained = cast("ChainedHook", run_after_process_chain)
    chained.chain = (*hooks, after_process)
    return chained
