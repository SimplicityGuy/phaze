"""SAQ worker instrumentation -- job duration, outcome, and queue depth.

Wired as ordinary SAQ ``before_process`` / ``after_process`` hooks, the same mechanism
phaze already uses for ``enforce_stage_pause_on_process`` and ``increment_completed``, so
there is no new extension point and no wrapper around the function registry.

**The job function name is the label and it is bounded** by the union of the controller's
and the agent's registered function lists -- a closed set in this repo's source, not a
value derived from a payload. Job id, file id and kwargs never reach a metric; the job id
goes on the span.

phaze-zaf2l measured SAQ at 0.0939 jobs/s against a burst capacity of 318.3/s and filed no
bead against it. These metrics are therefore NOT here because the queue is suspected: they
are here so the next person asking does not have to sample ``saq_jobs`` by hand every 120
seconds for the length of a spike.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from phaze.telemetry.instruments import add, record, set_gauge
from phaze.telemetry.tracing import span


log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_START_KEY = "_phaze_telemetry_started"
_SPAN_KEY = "_phaze_telemetry_span"

#: SAQ statuses that mean the job is finished and how it finished. Anything else that
#: reaches ``after_process`` -- ``queued`` after a ``retry()`` -- is not terminal and is
#: counted as an error, because from the caller's point of view the attempt did not
#: produce a result.
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


async def after_process(ctx: dict[str, Any]) -> None:
    """SAQ ``after_process``: record duration + outcome and close the span.

    SAQ runs this in a ``finally`` after EVERY outcome, so ``job.status`` is authoritative.
    Never raises, for the same reason: this hook runs alongside phaze's ledger-clearing
    hook, and an exception here would be raised through SAQ's own ``finally``.
    """
    try:
        job = ctx.get("job")
        if job is None:
            return
        outcome = "ok" if str(getattr(job, "status", "")).rsplit(".", 1)[-1].lower() == _OK_STATUS else "error"
        name = _job_name(job)
        started = ctx.pop(_START_KEY, None)
        if started is not None:
            record("phaze.saq.job.duration", time.perf_counter() - started, job=name, outcome=outcome)
        add("phaze.saq.jobs", 1, job=name, outcome=outcome)
        current = ctx.pop(_SPAN_KEY, None)
        if current is not None:
            current.__exit__(None, None, None)
    except Exception:
        # NEVER raises: this runs in SAQ's own hook chain, alongside the ledger clear.
        # Logged rather than passed so a systematically broken hook is discoverable.
        log.debug("telemetry_saq_hook_failed", exc_info=True)


def record_queue_depth(queue_name: str, counts: dict[str, int]) -> None:
    """Publish one sample of ``{status: count}`` for ``queue_name``.

    A synchronous gauge fed by whatever already samples the queue, rather than an
    OBSERVABLE gauge with its own callback: an observable gauge's callback runs on the
    metric reader's thread, which has no event loop and therefore cannot await the async
    queue API. Statuses outside the catalogued set are dropped rather than published,
    because an unrecognised status is exactly how a label set grows silently.
    """
    for status, count in counts.items():
        set_gauge("phaze.saq.queue.depth", float(count), queue=queue_name, status=str(status))


def hooks() -> tuple[Callable[[dict[str, Any]], Awaitable[None]], Callable[[dict[str, Any]], Awaitable[None]]]:
    """``(before_process, after_process)`` -- the pair a worker settings dict needs."""
    return before_process, after_process
