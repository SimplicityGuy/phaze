"""Control side of the junk quarantine: dispatch approved rows, record what the agent did (phaze-lwuf6).

Nothing reaches the filesystem from here. :func:`enqueue_quarantine` moves APPROVED
``companion_junk_review`` rows to ``executing`` and hands each to its owning agent as one meta-lane
``quarantine_companion`` job; the agent re-checks and moves the file
(``phaze.services.quarantine_move``) and reports through :func:`record_quarantine_result`, which marks
the row ``quarantined`` and retires the file's ``files`` row with ``delete_file_cascade`` -- or marks
it ``failed`` with the agent's reason. The review row is never deleted: it is the audit record of what
was moved and when (it survives every cascade, phaze-bk5jp).

Only APPROVED rows are ever dispatched, from an approval on the junk-review page (phaze-l1j35) or
``phaze junk quarantine --apply``. :func:`plan_quarantine` is the read-only view of what the next
dispatch would move, for the dry run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, func, select, update
import structlog

from phaze.constants import QUARANTINE_DIRNAME
from phaze.enums.junk_review import JunkReviewStatus, allowed_from
from phaze.models.agent import Agent
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.schemas.agent_tasks import QuarantineCompanionPayload
from phaze.services.agent_task_router import AgentTaskRouter, AmbiguousEnqueueError
from phaze.services.companion_junk_review import transition_review
from phaze.services.pg_text import sanitize_pg_text
from phaze.services.scan_deletion import delete_file_cascade
from phaze.services.scheduling_ledger import clear_ledger_entry


if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.schemas.agent_junk_quarantine import JunkQuarantineResultPayload


logger = structlog.get_logger(__name__)

QUARANTINE_TASK = "quarantine_companion"
"""The agent task name (meta lane, keyed on ``review_id`` in ``deterministic_key``)."""

_ID_PAGE = 1_000
"""Review ids per ``IN`` list: one bind each, far under asyncpg's 32,767 cap."""

_ERROR_MESSAGE_MAX = 2000


def _ledger_key(review_id: uuid.UUID) -> str:
    return f"{QUARANTINE_TASK}:{review_id}"


async def _claim_approved(session: AsyncSession, review_ids: Sequence[uuid.UUID]) -> list[Any]:
    """Move every APPROVED row among ``review_ids`` to ``executing``; returns the moved rows. Does NOT commit.

    One conditional ``UPDATE`` per page: the status guard sits in the ``WHERE``, so a row in any other
    status is skipped and two concurrent callers can never both claim one row.
    """
    ids = list(dict.fromkeys(review_ids))
    claimed: list[Any] = []
    for start in range(0, len(ids), _ID_PAGE):
        result = await session.execute(
            update(CompanionJunkReview)
            .where(
                CompanionJunkReview.id.in_(ids[start : start + _ID_PAGE]),
                CompanionJunkReview.status.in_(allowed_from(JunkReviewStatus.EXECUTING)),
            )
            .values(status=JunkReviewStatus.EXECUTING.value, updated_at=func.now())
            .returning(
                CompanionJunkReview.id,
                CompanionJunkReview.agent_id,
                CompanionJunkReview.original_path,
                CompanionJunkReview.sha256_hash,
                CompanionJunkReview.file_size,
            )
        )
        claimed.extend(result.all())
    return sorted(claimed, key=lambda row: (row.agent_id, row.original_path))


async def _release_unsent(session: AsyncSession, review_ids: Collection[uuid.UUID]) -> int:
    """Undo the claim of rows whose job provably never left: ``executing -> approved``, COMMITTED. Returns how many moved.

    This is the dispatcher withdrawing its own claim, not a review transition, which is why it is not
    an edge in :data:`~phaze.enums.junk_review.TRANSITIONS`: there, an ``executing -> approved`` edge
    would let an operator's group approval (``decide_content_group``) pull a row back out from under a
    LIVE job and dispatch it twice. Here the guard is the caller's knowledge that no job exists, and
    the ``WHERE status = 'executing'`` keeps a row an agent already reported on untouched.
    """
    ids = list(review_ids)
    released = 0
    for start in range(0, len(ids), _ID_PAGE):
        result = await session.execute(
            update(CompanionJunkReview)
            .where(CompanionJunkReview.id.in_(ids[start : start + _ID_PAGE]), CompanionJunkReview.status == JunkReviewStatus.EXECUTING)
            .values(status=JunkReviewStatus.APPROVED.value, updated_at=func.now())
        )
        # An UPDATE's rowcount is the rows it changed (asyncpg reports the command tag), so nothing is fetched.
        released += cast("CursorResult[Any]", result).rowcount
    await session.commit()
    return released


async def enqueue_quarantine(session: AsyncSession, review_ids: Sequence[uuid.UUID], *, task_router: AgentTaskRouter | None = None) -> int:
    """Dispatch every APPROVED row among ``review_ids`` to its owning agent; returns how many were enqueued. COMMITS.

    Rows in any other status are skipped, so a second call with the same ids enqueues nothing. Each
    claimed row moves ``approved -> executing`` and that is COMMITTED before any job is handed to an
    agent: the agent's report must find the row in ``executing`` (the tag-write rule, phaze-ysnp).
    Whatever the caller changed in ``session`` before this call is committed with it.

    A row whose job provably never reached the broker -- its enqueue raised before the broker
    connection existed, or the loop never got to it because something escaped (a cancellation, a
    payload that failed validation) -- goes BACK to ``approved`` (:func:`_release_unsent`), so the
    operator's approval stands and the next call dispatches it. Nothing is ever left ``executing``
    without a job that may exist. An AMBIGUOUS enqueue (:class:`AmbiguousEnqueueError`: the job may
    exist) is the one case that stays ``executing``: releasing it could dispatch the file twice. Its
    ``quarantine_companion:<review_id>`` scheduling-ledger row, written before the broker insert,
    is what ledger recovery re-drives if the job never landed. It is not counted.

    ``task_router`` is the per-agent enqueuer (``app.state.task_router`` in a route). Without one, a
    router is built from settings for this call and closed afterwards.
    """
    claimed = await _claim_approved(session, review_ids)
    await session.commit()
    if not claimed:
        return 0

    router = task_router
    if router is None:
        from phaze.config import get_settings  # noqa: PLC0415 -- only the router-less path needs settings and the engine
        from phaze.database import async_session  # noqa: PLC0415

        settings = get_settings()
        router = AgentTaskRouter(queue_url=settings.queue_url, cache_redis_url=settings.redis_url, ledger_sessionmaker=async_session)
    unsent = {row.id for row in claimed}
    enqueued = 0
    try:
        for row in claimed:
            payload = QuarantineCompanionPayload(
                review_id=row.id, agent_id=row.agent_id, source_path=row.original_path, sha256=row.sha256_hash, size=row.file_size
            )
            try:
                await router.enqueue_for_agent(agent_id=row.agent_id, task_name=QUARANTINE_TASK, payload=payload)
            except AmbiguousEnqueueError:
                unsent.discard(row.id)
                logger.error("junk quarantine enqueue ambiguous -- row left executing", review_id=str(row.id), agent_id=row.agent_id, exc_info=True)
                continue
            except Exception:
                logger.warning(
                    "junk quarantine enqueue failed -- row goes back to approved", review_id=str(row.id), agent_id=row.agent_id, exc_info=True
                )
                continue
            unsent.discard(row.id)
            enqueued += 1
    finally:
        try:
            if unsent:
                await session.rollback()
                released = await _release_unsent(session, unsent)
                logger.warning("junk quarantine: undispatched rows released back to approved", released=released)
        finally:
            if task_router is None:
                await router.close()
    logger.info("junk quarantine dispatched", requested=len(review_ids), claimed=len(claimed), enqueued=enqueued)
    return enqueued


class QuarantineReportRefused(Exception):
    """A report for a row this agent does not own, or that does not exist (answered as 404, never revealing which)."""


@dataclass(frozen=True)
class QuarantineRecordOutcome:
    """What a report changed: the row's status after it, whether it moved, and how many ``files`` rows were retired."""

    status: str
    applied: bool
    retired_files: int


async def record_quarantine_result(
    session: AsyncSession, agent_id: str, review_id: uuid.UUID, body: JunkQuarantineResultPayload
) -> QuarantineRecordOutcome:
    """Apply the agent's report for one ``executing`` row. Does NOT commit.

    ``quarantined``: the row becomes ``quarantined`` and every ``files`` row of that identity
    (agent, original path, approved SHA-256) is retired with ``delete_file_cascade`` -- the file is no
    longer at that path. ``failed``: the row becomes ``failed`` with the agent's reason, and the
    ``files`` row stays (nothing moved). A row that already left ``executing`` is left alone: a
    replayed report is a no-op. The row is locked first, so two concurrent reports serialize.
    """
    row = (await session.execute(select(CompanionJunkReview).where(CompanionJunkReview.id == review_id).with_for_update())).scalar_one_or_none()
    if row is None or row.agent_id != agent_id:
        raise QuarantineReportRefused(str(review_id))
    if row.status != JunkReviewStatus.EXECUTING:
        return QuarantineRecordOutcome(status=row.status, applied=False, retired_files=0)

    retired = 0
    if body.status == "quarantined":
        await transition_review(session, review_id, JunkReviewStatus.QUARANTINED)
        file_ids = (
            await session.execute(
                select(FileRecord.id).where(
                    FileRecord.agent_id == row.agent_id,
                    FileRecord.original_path == row.original_path,
                    FileRecord.sha256_hash == row.sha256_hash,
                )
            )
        ).scalars()
        for file_id in list(file_ids):
            await delete_file_cascade(session, file_id)
            retired += 1
        logger.info(
            "junk companion quarantined",
            review_id=str(review_id),
            agent_id=agent_id,
            destination=body.destination_path,
            replayed=body.replayed,
            retired_files=retired,
        )
    else:
        reason = sanitize_pg_text(body.error_message or "")[:_ERROR_MESSAGE_MAX]
        await transition_review(session, review_id, JunkReviewStatus.FAILED, error_message=reason)
        logger.warning("junk quarantine failed", review_id=str(review_id), agent_id=agent_id, error=reason)
    await clear_ledger_entry(session, _ledger_key(review_id), from_running_job=True)
    return QuarantineRecordOutcome(status=body.status, applied=True, retired_files=retired)


@dataclass(frozen=True)
class QuarantinePlanItem:
    """One approved row and where the agent would move it."""

    review_id: uuid.UUID
    agent_id: str
    reason: str
    size: int
    source_path: str
    destination_path: str | None
    """``None`` when no configured scan root of the agent contains the path: the agent would refuse it."""


def quarantine_destination(source_path: str, scan_roots: Sequence[str]) -> str | None:
    """``<root>/.phaze-quarantine/<relative path>`` for the first configured root lexically containing ``source_path``.

    A lexical preview for the dry run. The agent decides for real, on the resolved on-disk path.
    """
    source = PurePosixPath(source_path)
    for root in scan_roots:
        root_path = PurePosixPath(root)
        if source != root_path and source.is_relative_to(root_path):
            return str(root_path / QUARANTINE_DIRNAME / source.relative_to(root_path))
    return None


async def plan_quarantine(session: AsyncSession) -> list[QuarantinePlanItem]:
    """Every APPROVED row, ordered by agent and path, with its previewed destination. Read-only."""
    rows = (
        await session.execute(
            select(CompanionJunkReview, Agent.scan_roots)
            .join(Agent, Agent.id == CompanionJunkReview.agent_id, isouter=True)
            .where(CompanionJunkReview.status == JunkReviewStatus.APPROVED)
            .order_by(CompanionJunkReview.agent_id, CompanionJunkReview.original_path)
        )
    ).all()
    return [
        QuarantinePlanItem(
            review_id=review.id,
            agent_id=review.agent_id,
            reason=review.reason,
            size=review.file_size,
            source_path=review.original_path,
            destination_path=quarantine_destination(review.original_path, scan_roots or []),
        )
        for review, scan_roots in rows
    ]
