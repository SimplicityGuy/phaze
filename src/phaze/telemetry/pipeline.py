"""Pipeline instrumentation: scheduling-ledger transitions and the waiting-room gauges.

Two very different shapes, deliberately kept apart:

* :func:`record_transition` is a COUNTER on the scheduling ledger -- the durable record
  recovery reads. A ledger row appearing means a stage was scheduled for a file; the row
  going away means that scheduling resolved. Per-ATTEMPT work is already counted by
  ``phaze.saq.jobs``, so this is not a duplicate of it: a retried job is many attempts and
  one ledger row.
* :func:`record_backlog` is a GAUGE fed from the admin UI's own ``/pipeline/stats`` read.
  **It is poll-driven and goes stale when no admin tab is open.** That is a real
  limitation, stated here rather than discovered later: it makes these series fine for a
  dashboard and wrong for an alert. ``phaze-m1drf.5`` builds its rules on
  ``phaze.analysis.audio.duration``, which the analysis path emits itself, for exactly
  this reason.

Neither function raises. Both are called from paths that are already carrying a file
through the pipeline, and a metric is never worth a lost file.
"""

from __future__ import annotations

from phaze.telemetry.instruments import add, set_gauge


#: Ledger function name -> the Stage the operator calls it. Only two stages route through
#: a SAQ function today (``stage_control.STAGE_TO_FUNCTION``); everything else reports its
#: function name, which is bounded by the registered function lists.
_FUNCTION_TO_STAGE: dict[str, str] = {
    "extract_file_metadata": "metadata",
    "process_file": "analyze",
}


def stage_label(function: str) -> str:
    """The bounded ``stage`` label for a ledger function name."""
    return _FUNCTION_TO_STAGE.get(function, function or "unknown")


def record_transition(function: str, transition: str) -> None:
    """Count one scheduling-ledger transition. ``transition`` is ``scheduled`` or ``resolved``."""
    add("phaze.pipeline.stage.transitions", 1, stage=stage_label(function), transition=transition)


def record_backlog(counts: dict[str, int]) -> None:
    """Publish one sample of the pipeline's waiting-room depths.

    Keys outside the catalogued domain are dropped by the instruments layer's attribute
    check rather than published, so a caller that grows a new counter cannot silently grow
    the label set with it -- it shows up as a missing series and, under
    ``PHAZE_TELEMETRY_STRICT``, as a red test.
    """
    for name, value in counts.items():
        set_gauge("phaze.pipeline.backlog", float(value), backlog=name)
