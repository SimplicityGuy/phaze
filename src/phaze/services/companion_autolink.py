"""Automatic companion association: what triggers a run, how runs coalesce, and the per-agent unit (phaze-spd83).

THE ROOT CAUSE THIS CLOSES (phaze-bniuk). :func:`phaze.services.companion.associate_companions` had
exactly one caller, the operator's ``POST /api/v1/associate``. Nothing ran it after a scan or a
watcher ingest, so production holds only the links operator clicks wrote: 19,980 of its 19,983 were
created in the minute after the first root's scan completed, and the three roots scanned later hold
companions and no links at all (the measurements are in a bead comment on phaze-spd83).

WHEN A RUN IS REQUESTED. A companion can only be decided once its content features are stored, and
those arrive asynchronously, after the upsert that created its row. A decision can also change when
media appears on its agent (a reference, a stem, a folder, a twin or a close name may name a file
that was not there before) or when a file moves. So the triggers are the events that make or change
a decision, not "a scan finished" alone:

- ``POST /api/internal/agent/companion-features`` stored at least one row (scan, watcher, or the
  ``extract_companion_features`` backfill task);
- ``POST /api/internal/agent/files`` inserted at least one MEDIA row (a scan chunk or a watcher
  file; a re-upsert of a known path changes nothing association reads, which is paths only);
- ``POST /api/internal/agent/files/move`` (a moved file is a new path);
- a scan batch reaching a terminal state (``PATCH /api/internal/agent/scan-batches/{id}``).

Each calls :func:`request_association` AFTER its own commit, so the run can never read a state older
than the event. The request is best-effort: a failed enqueue is logged and never fails the ingest,
and the next event, or ``phaze backfill companion-links``, covers it.

COALESCING, PER AGENT. A request names a WINDOW, ``floor(now / ASSOCIATION_WINDOW_SECONDS)``, and
enqueues ``associate_agent_companions(agent_id, window)`` keyed on both and scheduled for the
window's END. Every request in one window lands on one key, and SAQ drops a repeat of a queued key,
so a scan posting thousands of chunks enqueues at most one run per agent per window. Nothing is lost
to the drop: a job is dequeued no earlier than its window's end (SAQ compares ``scheduled`` with
the worker's clock), so any event that happens after the run has started falls in a LATER window and
gets a key of its own. That holds while the api and the worker read the same clock, as they do on the
one compose host; it is the property ``tests/integration/test_companion_association_coalescing.py``
pins against a real broker.

DEFERRED WHILE A SCAN RUNS, SERIAL PER AGENT. A run for an agent with a RUNNING scan batch would
re-derive the whole agent once per window for the length of the scan (hours on a full archive), so
it re-requests itself for a later window and returns; the scan's terminal PATCH requests the run
that counts. Two runs for one agent never overlap: each holds a session-level advisory lock for the
agent, and a run that cannot take it re-requests itself the same way. A deferral is a single
enqueue, so at most one deferred run per agent waits at a time.

THE UNIT (:func:`run_agent_association`): re-derive the agent's links, then re-run the phaze-bk5jp
junk-review detector over the same agent. "Duplicate" is a fact about the links stored when it is
judged, so a pending duplicate row whose link has gone, or a new copy of a newly linked companion,
is brought up to date by the same run that changed the links. The detector only ever creates or
withdraws PENDING rows; nothing is approved or moved.

DURING THE FEATURES BACKFILL. phaze-rmhfr's deploy order says to associate after ``phaze backfill
companion-features --apply`` has finished, because a run over PARTIAL features judges duplicates on
the links of the companions it can decide. Automatic runs fire while that backfill lands, so they do
run over partial features. Every run is a full re-derive of the stored state, and its result does not
depend on the links a previous run left (``tests/discovery/services/test_companion.py::
test_the_duplicate_rule_is_stable_across_a_full_re_derive_from_any_prior_state``). The last features
chunk requests one more run, and that run sees every feature. So the end state is the one the
deploy order describes. Before it, a copy may be linked and then unlinked, and a pending duplicate
review row may appear and then be withdrawn.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection
import structlog

from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.companion import associate_companions
from phaze.services.companion_junk_review import detect_junk_reviews
from phaze.services.enqueue_router import resolve_queue_for_task


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.services.companion import AssociationOutcome
    from phaze.services.companion_junk_review import DetectionOutcome


logger = structlog.get_logger(__name__)

ASSOCIATION_TASK = "associate_agent_companions"
"""The controller task one window's coalesced run executes (``tasks/companion_association.py``)."""

ASSOCIATION_WINDOW_SECONDS = 120
"""Requests for one agent within one window share a single run, scheduled for the window's end.

Two minutes is the latency a watcher-ingested companion waits for its links, and the most often a
steady trickle of events can re-derive an agent (rmhfr measured building one agent's media index at
2.8 s plus 8.3 s for close names, on 104,247 media rows)."""

ASSOCIATION_JOB_TIMEOUT_SECONDS = 3600
"""SAQ timeout of one run. Every page commits on its own, so a run cut short leaves earlier pages
re-derived and the next run (association is idempotent) finishes the rest."""


def association_window(at: float) -> int:
    """The coalescing window an event at epoch second ``at`` belongs to."""
    return int(at // ASSOCIATION_WINDOW_SECONDS)


async def enqueue_association(queue: Any, agent_id: str, *, at: float | None = None) -> bool:
    """Enqueue the run for ``agent_id``'s current window on ``queue``; False when that window's run is already queued.

    The key, ``associate_agent_companions:<agent_id>:<window>``, is set by the central
    ``before_enqueue`` hook (``tasks/_shared/deterministic_key.py``).
    """
    window = association_window(time.time() if at is None else at)
    job = await queue.enqueue(
        ASSOCIATION_TASK,
        agent_id=agent_id,
        window=window,
        scheduled=(window + 1) * ASSOCIATION_WINDOW_SECONDS,
        timeout=ASSOCIATION_JOB_TIMEOUT_SECONDS,
    )
    return job is not None


async def request_association(app_state: Any, agent_id: str) -> None:
    """Request a coalesced association run for ``agent_id`` from the api. Best-effort: never raises.

    Called after the triggering request has committed. A failure is logged with its traceback and
    leaves the ingest itself untouched; the next event for the agent requests the run again.
    """
    try:
        routed = await resolve_queue_for_task(ASSOCIATION_TASK, app_state, None)
        await enqueue_association(routed.queue, agent_id)
    except Exception:
        logger.warning("companion association request failed; the next event for this agent requests it again", agent_id=agent_id, exc_info=True)


async def scan_running(session: AsyncSession, agent_id: str) -> bool:
    """Whether ``agent_id`` has a RUNNING scan batch (the watcher's LIVE sentinel is not one)."""
    statement = select(exists().where(ScanBatch.agent_id == agent_id, ScanBatch.status == ScanStatus.RUNNING.value))
    return bool((await session.execute(statement)).scalar())


def _lock_key(agent_id: str) -> Any:
    """The agent's advisory-lock key: a stable 64-bit hash of a namespaced string, computed by Postgres."""
    return func.hashtextextended(f"phaze:companion-association:{agent_id}", 0)


@asynccontextmanager
async def agent_association_lock(session: AsyncSession, agent_id: str) -> AsyncIterator[bool]:
    """Try the agent's session-level advisory lock; yields whether it was taken, and releases it on exit.

    Association commits once per page, and a pooled ``AsyncSession`` hands its connection back on
    every commit, so the lock lives on a connection of its own, held for the whole run -- the
    phaze-yhhy precedent in ``routers/tags.py``. That connection's own transaction is committed right
    away (a session-level lock outlives it) so it never sits idle in a transaction. Under the test
    suite's hermetic session the bind is already a single connection, which is reused as is.
    """
    bind = session.bind
    owns = not isinstance(bind, AsyncConnection)
    connection: AsyncConnection = await bind.connect() if owns else bind  # type: ignore[union-attr,assignment]
    try:
        acquired = bool((await connection.execute(select(func.pg_try_advisory_lock(_lock_key(agent_id))))).scalar())
        if owns:
            await connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                await connection.execute(select(func.pg_advisory_unlock(_lock_key(agent_id))))
                if owns:
                    await connection.commit()
    finally:
        if owns:
            await connection.close()


async def run_agent_association(session: AsyncSession, agent_id: str, *, apply: bool) -> tuple[AssociationOutcome, DetectionOutcome | None]:
    """Re-derive ``agent_id``'s companion links, then refresh its junk-review queue (module docstring).

    With ``apply`` every association page commits and the detector's writes commit at the end. Without
    it nothing is written and the detector is not run: its duplicate verdicts read the STORED links,
    which a dry run leaves as they are, so its counts would describe the links before the run.
    """
    association = await associate_companions(session, agent_id=agent_id, apply=apply)
    if not apply:
        return association, None
    detection = await detect_junk_reviews(session, agent_id, apply=True)
    await session.commit()
    return association, detection
