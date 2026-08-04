"""SAQ entry point for the filename-convention learner (phaze-5fta.3).

One job = ONE full refresh of the ``(scope='release_group', convention_kind='date_order')`` rows in
``filename_convention``. There is deliberately **no CronJob** for this task, for the same reason the
epic calls the learner operator-triggered: it sweeps the WHOLE corpus, and its output is the input
to a gate that can change rename proposals. When that recomputation happens is an operator
decision, not a timer's -- and because the table is a pure cache with full-refresh semantics
(``services/filename_convention_learner``), running it late costs staleness and never corruption.

Registered in ``tasks.controller.settings["functions"]`` and in
``enqueue_router.CONTROLLER_TASKS`` so the admin/API layer can enqueue it; keyed in
``tasks._shared.deterministic_key`` so a double-click collapses to one sweep instead of two
concurrent ones racing for the write transaction's advisory lock.
"""

from __future__ import annotations

from typing import Any

import structlog

from phaze.services.filename_convention_learner import (
    CONVENTION_KIND,
    CONVENTION_SCOPE,
    DEFAULT_SCAN_BATCH_SIZE,
    learn_release_group_date_conventions,
)


logger = structlog.get_logger(__name__)


async def learn_filename_conventions(ctx: dict[str, Any], *, batch_size: int = DEFAULT_SCAN_BATCH_SIZE) -> dict[str, Any]:
    """Recompute every release-group date-order convention from the corpus and replace the store.

    Args:
        ctx: SAQ context; ``ctx["async_session"]`` is the short-session factory the learner opens
            and closes around each keyset page and around the single write transaction (never one
            session held across the whole sweep).
        batch_size: corpus rows per keyset page. Exposed so an operator can trade page size against
            memory on a constrained worker; the default is tuned for the ~250,000-file archive.

    Returns:
        The :class:`~phaze.services.filename_convention_learner.LearnerReport` as a JSON-safe
        mapping -- SAQ persists it as the job result, which is how an operator reads what the sweep
        skipped and how many contradictions it found without tailing the worker log.
    """
    report = await learn_release_group_date_conventions(ctx["async_session"], batch_size=batch_size)
    logger.info(
        "learn_filename_conventions complete",
        scope=CONVENTION_SCOPE,
        convention_kind=CONVENTION_KIND,
        groups_written=report.groups_written,
        contradictions=report.contradictions,
    )
    return report.as_dict()
