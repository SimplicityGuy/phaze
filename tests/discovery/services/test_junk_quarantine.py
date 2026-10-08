"""Control side of the junk quarantine against real Postgres: dispatch, the agent's report, the dry-run plan (phaze-lwuf6).

The agent's move is covered on a real filesystem in ``test_quarantine_move.py`` and end to end in
``tests/discovery/test_junk_quarantine_e2e.py``. Here the enqueuer is a recording fake: these tests are
about which rows are claimed, what is committed before a job leaves, and what a report changes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select

from phaze.enums.junk_review import JunkReviewStatus
from phaze.models.agent import Agent
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.schemas.agent_junk_quarantine import JunkQuarantineResultPayload
from phaze.schemas.agent_tasks import QuarantineCompanionPayload
from phaze.services import junk_quarantine
from phaze.services.agent_task_router import AmbiguousEnqueueError
from phaze.services.companion_junk_review import transition_review
from phaze.services.junk_quarantine import (
    QUARANTINE_TASK,
    QuarantineReportRefused,
    enqueue_quarantine,
    plan_quarantine,
    quarantine_destination,
    record_quarantine_result,
)


if TYPE_CHECKING:
    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncSession


_AGENT = "junk-agent"
_OTHER = "other-agent"
_ROOT = "/archive/music"
_JUNK = b"Downloaded from www.example-release-site.test\r\n"
_SHA = hashlib.sha256(_JUNK).hexdigest()


class _Router:
    """Records every enqueue, and the row's committed status as the job leaves; fails on request."""

    def __init__(self, session: AsyncSession, *, fail: dict[uuid.UUID, Exception] | None = None) -> None:
        self.session = session
        self.fail = fail or {}
        self.enqueued: list[tuple[str, str, dict[str, Any]]] = []
        self.status_at_enqueue: list[str] = []
        self.closed = False

    async def enqueue_for_agent(self, *, agent_id: str, task_name: str, payload: BaseModel) -> None:
        dumped = payload.model_dump(mode="json")
        review_id = uuid.UUID(dumped["review_id"])
        assert not self.session.in_transaction(), "the claim must be committed before a job leaves"
        self.status_at_enqueue.append(
            (await self.session.execute(select(CompanionJunkReview.status).where(CompanionJunkReview.id == review_id))).scalar_one()
        )
        await self.session.commit()  # end this probe's own read, so the next enqueue's check means something
        if review_id in self.fail:
            raise self.fail[review_id]
        self.enqueued.append((agent_id, task_name, dumped))

    async def close(self) -> None:
        self.closed = True


async def _agents(session: AsyncSession) -> None:
    session.add_all(
        [
            Agent(id=_AGENT, name="Junk Agent", token_hash="a" * 64, scan_roots=[_ROOT, "/archive/video"]),
            Agent(id=_OTHER, name="Other Agent", token_hash="b" * 64, scan_roots=["/other"]),
        ]
    )
    await session.flush()


async def _review(session: AsyncSession, path: str, status: JunkReviewStatus, *, agent_id: str = _AGENT, sha: str = _SHA) -> CompanionJunkReview:
    now = datetime.now(UTC)
    row = CompanionJunkReview(
        agent_id=agent_id,
        original_path=path,
        sha256_hash=sha,
        file_type=path.rsplit(".", 1)[-1],
        file_size=len(_JUNK),
        reason="known_stamp",
        content_group=sha,
        status=status.value,
        decided_at=None if status is JunkReviewStatus.PENDING else now,
        executed_at=now if status in (JunkReviewStatus.QUARANTINED, JunkReviewStatus.FAILED) else None,
    )
    session.add(row)
    await session.flush()
    return row


async def _file(session: AsyncSession, path: str, *, agent_id: str = _AGENT, sha: str = _SHA) -> FileRecord:
    row = FileRecord(
        agent_id=agent_id,
        sha256_hash=sha,
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type=path.rsplit(".", 1)[-1],
        file_size=len(_JUNK),
    )
    session.add(row)
    await session.flush()
    return row


async def _status(session: AsyncSession, review_id: uuid.UUID) -> CompanionJunkReview:
    statement = select(CompanionJunkReview).where(CompanionJunkReview.id == review_id).execution_options(populate_existing=True)
    return (await session.execute(statement)).scalar_one()


# enqueue_quarantine


async def test_only_approved_rows_are_claimed_committed_and_enqueued_once(session: AsyncSession) -> None:
    await _agents(session)
    approved = await _review(session, f"{_ROOT}/a/site.nfo", JunkReviewStatus.APPROVED)
    other_agent = await _review(session, "/other/b/site.nfo", JunkReviewStatus.APPROVED, agent_id=_OTHER)
    refused = [
        await _review(session, f"{_ROOT}/c/{status.value}.nfo", status)
        for status in (JunkReviewStatus.PENDING, JunkReviewStatus.REJECTED, JunkReviewStatus.QUARANTINED, JunkReviewStatus.FAILED)
    ]
    await session.commit()
    router = _Router(session)

    count = await enqueue_quarantine(session, [approved.id, other_agent.id, *(row.id for row in refused), approved.id], task_router=router)  # type: ignore[arg-type]

    assert count == 2
    assert router.status_at_enqueue == ["executing", "executing"]
    assert [(agent, task) for agent, task, _payload in router.enqueued] == [(_AGENT, QUARANTINE_TASK), (_OTHER, QUARANTINE_TASK)]
    payload = QuarantineCompanionPayload.model_validate(router.enqueued[0][2])
    assert payload == QuarantineCompanionPayload(
        review_id=approved.id, agent_id=_AGENT, source_path=f"{_ROOT}/a/site.nfo", sha256=_SHA, size=len(_JUNK)
    )
    assert [(await _status(session, row.id)).status for row in refused] == ["pending", "rejected", "quarantined", "failed"]
    assert (await _status(session, approved.id)).status == "executing"

    # Idempotent: a second call finds nothing approved and enqueues nothing.
    again = _Router(session)
    assert await enqueue_quarantine(session, [approved.id, other_agent.id], task_router=again) == 0  # type: ignore[arg-type]
    assert again.enqueued == []


async def test_no_ids_and_unknown_ids_enqueue_nothing(session: AsyncSession) -> None:
    router = _Router(session)

    assert await enqueue_quarantine(session, [], task_router=router) == 0  # type: ignore[arg-type]
    assert await enqueue_quarantine(session, [uuid.uuid4()], task_router=router) == 0  # type: ignore[arg-type]
    assert router.enqueued == []


class _DyingRouter(_Router):
    """Enqueues the first ``healthy`` jobs, then every later call raises ``failure`` (the broker died mid-batch)."""

    def __init__(self, session: AsyncSession, *, healthy: int, failure: BaseException) -> None:
        super().__init__(session)
        self.healthy = healthy
        self.failure = failure
        self.calls = 0

    async def enqueue_for_agent(self, *, agent_id: str, task_name: str, payload: BaseModel) -> None:
        self.calls += 1
        if self.calls > self.healthy:
            raise self.failure
        await super().enqueue_for_agent(agent_id=agent_id, task_name=task_name, payload=payload)


async def _three_approved(session: AsyncSession) -> list[CompanionJunkReview]:
    await _agents(session)
    rows = [await _review(session, f"{_ROOT}/{name}/site.nfo", JunkReviewStatus.APPROVED) for name in ("a", "b", "c")]
    await session.commit()
    return rows


async def test_a_broker_failing_partway_releases_the_rest_to_approved_and_a_retry_sends_them(session: AsyncSession) -> None:
    """No row is stranded in executing: the unsent ones keep their approval, and the next call dispatches exactly them."""
    rows = await _three_approved(session)
    dying = _DyingRouter(session, healthy=1, failure=ConnectionRefusedError("broker down"))

    assert await enqueue_quarantine(session, [row.id for row in rows], task_router=dying) == 1  # type: ignore[arg-type]

    after = [await _status(session, row.id) for row in rows]
    assert [row.status for row in after] == ["executing", "approved", "approved"]
    assert all(row.decided_at is not None and row.error_message is None for row in after)  # the approval stands

    retry = _Router(session)
    assert await enqueue_quarantine(session, [row.id for row in rows], task_router=retry) == 2  # type: ignore[arg-type]
    assert [payload["review_id"] for _agent, _task, payload in retry.enqueued] == [str(rows[1].id), str(rows[2].id)]
    assert [(await _status(session, row.id)).status for row in rows] == ["executing"] * 3


async def test_an_escaping_cancellation_releases_every_unsent_row_and_still_propagates(session: AsyncSession) -> None:
    rows = await _three_approved(session)
    dying = _DyingRouter(session, healthy=1, failure=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await enqueue_quarantine(session, [row.id for row in rows], task_router=dying)  # type: ignore[arg-type]

    assert [(await _status(session, row.id)).status for row in rows] == ["executing", "approved", "approved"]
    retry = _Router(session)
    assert await enqueue_quarantine(session, [row.id for row in rows], task_router=retry) == 2  # type: ignore[arg-type]


async def test_a_release_never_touches_a_row_an_agent_already_reported(session: AsyncSession) -> None:
    """The release is guarded on executing: a row already terminal is not pulled back to approved."""
    rows = await _three_approved(session)
    await _claim_and_finish(session, rows[0].id)

    assert await junk_quarantine._release_unsent(session, {rows[0].id, rows[1].id}) == 0
    assert [(await _status(session, row.id)).status for row in rows[:2]] == ["quarantined", "approved"]


async def test_a_release_counts_exactly_the_rows_it_moved_across_pages(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The count is the UPDATE's rowcount, never fetched rows: two executing rows move, an approved one does not count."""
    monkeypatch.setattr(junk_quarantine, "_ID_PAGE", 1)
    rows = await _three_approved(session)
    for row in rows[:2]:
        await transition_review(session, row.id, JunkReviewStatus.EXECUTING)
    await session.commit()

    assert await junk_quarantine._release_unsent(session, [row.id for row in rows]) == 2
    assert [(await _status(session, row.id)).status for row in rows] == ["approved"] * 3


async def _claim_and_finish(session: AsyncSession, review_id: uuid.UUID) -> None:
    await enqueue_quarantine(session, [review_id], task_router=_Router(session))  # type: ignore[arg-type]
    await record_quarantine_result(session, _AGENT, review_id, _moved())
    await session.commit()


async def test_an_ambiguous_dispatch_leaves_the_row_executing_and_uncounted(session: AsyncSession) -> None:
    """The job may exist: failing the row would let the file be approved and dispatched a second time."""
    await _agents(session)
    row = await _review(session, f"{_ROOT}/a/site.nfo", JunkReviewStatus.APPROVED)
    await session.commit()
    router = _Router(session, fail={row.id: AmbiguousEnqueueError("ack lost")})

    assert await enqueue_quarantine(session, [row.id], task_router=router) == 0  # type: ignore[arg-type]
    assert (await _status(session, row.id)).status == "executing"


async def test_without_a_router_one_is_built_for_the_call_and_closed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await _agents(session)
    row = await _review(session, f"{_ROOT}/a/site.nfo", JunkReviewStatus.APPROVED)
    await session.commit()
    built: list[_Router] = []

    def _build(**kwargs: Any) -> _Router:
        assert set(kwargs) == {"queue_url", "cache_redis_url", "ledger_sessionmaker"}
        built.append(_Router(session))
        return built[-1]

    monkeypatch.setattr(junk_quarantine, "AgentTaskRouter", _build)

    assert await enqueue_quarantine(session, [row.id]) == 1
    assert len(built) == 1
    assert built[0].closed
    assert len(built[0].enqueued) == 1


async def test_without_a_router_nothing_is_built_when_nothing_is_approved(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(junk_quarantine, "AgentTaskRouter", lambda **_kwargs: pytest.fail("no router is needed"))

    assert await enqueue_quarantine(session, [uuid.uuid4()]) == 0


# record_quarantine_result


def _moved(path: str = f"{_ROOT}/.phaze-quarantine/a/site.nfo", *, replayed: bool = False) -> JunkQuarantineResultPayload:
    return JunkQuarantineResultPayload(status="quarantined", destination_path=path, replayed=replayed)


async def test_a_confirmed_move_quarantines_the_row_and_retires_only_that_file_row(session: AsyncSession) -> None:
    await _agents(session)
    path = f"{_ROOT}/a/site.nfo"
    review = await _review(session, path, JunkReviewStatus.EXECUTING)
    moved = await _file(session, path)
    same_path_other_bytes = await _file(session, path, agent_id=_OTHER)
    same_bytes_elsewhere = await _file(session, f"{_ROOT}/b/site.nfo")
    await session.commit()

    outcome = await record_quarantine_result(session, _AGENT, review.id, _moved())
    await session.commit()

    assert (outcome.status, outcome.applied, outcome.retired_files) == ("quarantined", True, 1)
    row = await _status(session, review.id)
    assert (row.status, row.executed_at is not None, row.error_message) == ("quarantined", True, None)
    remaining = set((await session.execute(select(FileRecord.id))).scalars())
    assert moved.id not in remaining
    assert {same_path_other_bytes.id, same_bytes_elsewhere.id} <= remaining

    # A replayed report changes nothing and retires nothing.
    replay = await record_quarantine_result(session, _AGENT, review.id, _moved(replayed=True))
    assert (replay.status, replay.applied, replay.retired_files) == ("quarantined", False, 0)


async def test_a_file_row_with_other_bytes_at_the_path_is_not_retired(session: AsyncSession) -> None:
    """The row describes different content than the one approved and moved: it is not the moved file's record."""
    await _agents(session)
    path = f"{_ROOT}/a/site.nfo"
    review = await _review(session, path, JunkReviewStatus.EXECUTING)
    newer = await _file(session, path, sha="f" * 64)
    await session.commit()

    outcome = await record_quarantine_result(session, _AGENT, review.id, _moved())

    assert outcome.retired_files == 0
    assert (await session.execute(select(FileRecord.id).where(FileRecord.id == newer.id))).scalar_one() == newer.id


async def test_a_refused_move_fails_the_row_with_the_reason_and_keeps_the_file_row(session: AsyncSession) -> None:
    await _agents(session)
    path = f"{_ROOT}/a/site.nfo"
    review = await _review(session, path, JunkReviewStatus.EXECUTING)
    record = await _file(session, path)
    await session.commit()

    outcome = await record_quarantine_result(
        session, _AGENT, review.id, JunkQuarantineResultPayload(status="failed", error_message="has SHA-256 x\x00; the approved file was y")
    )

    assert (outcome.status, outcome.applied, outcome.retired_files) == ("failed", True, 0)
    row = await _status(session, review.id)
    assert row.status == "failed"
    assert row.error_message == "has SHA-256 x; the approved file was y"  # a NUL never reaches Postgres
    assert (await session.execute(select(FileRecord.id).where(FileRecord.id == record.id))).scalar_one() == record.id


async def test_a_report_for_a_row_not_executing_changes_nothing(session: AsyncSession) -> None:
    await _agents(session)
    approved = await _review(session, f"{_ROOT}/a/site.nfo", JunkReviewStatus.APPROVED)
    await session.commit()

    outcome = await record_quarantine_result(session, _AGENT, approved.id, _moved())

    assert (outcome.status, outcome.applied) == ("approved", False)
    assert (await _status(session, approved.id)).status == "approved"


async def test_a_report_from_another_agent_or_for_an_unknown_row_is_refused(session: AsyncSession) -> None:
    await _agents(session)
    review = await _review(session, f"{_ROOT}/a/site.nfo", JunkReviewStatus.EXECUTING)
    await session.commit()

    with pytest.raises(QuarantineReportRefused):
        await record_quarantine_result(session, _OTHER, review.id, _moved())
    with pytest.raises(QuarantineReportRefused):
        await record_quarantine_result(session, _AGENT, uuid.uuid4(), _moved())
    assert (await _status(session, review.id)).status == "executing"


# The dry-run plan


@pytest.mark.parametrize(
    ("source", "roots", "expected"),
    [
        (f"{_ROOT}/a/b/site.nfo", [_ROOT], f"{_ROOT}/.phaze-quarantine/a/b/site.nfo"),
        ("/archive/video/x.txt", [_ROOT, "/archive/video"], "/archive/video/.phaze-quarantine/x.txt"),
        ("/archive/musical/x.txt", [_ROOT], None),  # a name prefix is not containment
        (_ROOT, [_ROOT], None),
        ("/elsewhere/x.txt", [], None),
    ],
)
def test_the_previewed_destination_mirrors_the_path_under_its_root(source: str, roots: list[str], expected: str | None) -> None:
    assert quarantine_destination(source, roots) == expected


async def test_the_plan_lists_every_approved_row_and_nothing_else(session: AsyncSession) -> None:
    await _agents(session)
    first = await _review(session, f"{_ROOT}/b/site.nfo", JunkReviewStatus.APPROVED)
    second = await _review(session, f"{_ROOT}/a/site.nfo", JunkReviewStatus.APPROVED)
    unrooted = await _review(session, "/not-a-root/site.nfo", JunkReviewStatus.APPROVED, agent_id=_OTHER)
    await _review(session, f"{_ROOT}/c/site.nfo", JunkReviewStatus.PENDING)
    await _review(session, f"{_ROOT}/d/site.nfo", JunkReviewStatus.EXECUTING)
    await session.commit()

    plan = await plan_quarantine(session)

    assert [(item.review_id, item.agent_id, item.destination_path) for item in plan] == [
        (second.id, _AGENT, f"{_ROOT}/.phaze-quarantine/a/site.nfo"),
        (first.id, _AGENT, f"{_ROOT}/.phaze-quarantine/b/site.nfo"),
        (unrooted.id, _OTHER, None),
    ]
    assert {(item.reason, item.size) for item in plan} == {("known_stamp", len(_JUNK))}


# A job lost AFTER a successful enqueue


def test_a_lost_quarantine_job_is_left_to_ledger_recovery_which_replays_it_to_its_owner() -> None:
    """The ``quarantine_companion:<review_id>`` ledger row outlives a lost job, and recovery classifies it as owed agent work.

    The control-side router writes that row before the broker insert; only this module's report
    clears it (the agent worker carries no ledger session, so its ``after_process`` clears nothing).
    A job that vanishes, aborts or exhausts its retries therefore leaves the row behind with no live
    ``saq_jobs`` key, which is exactly what ``recover_orphaned_work`` re-drives -- verbatim, to the
    payload's ``agent_id`` -- at controller start and from the Recover button. There is no periodic
    sweep: between those two moments the review row stays ``executing``.
    """
    from phaze.models.scheduling_ledger import SchedulingLedger
    from phaze.services.scheduling_ledger import routing_for_function
    from phaze.tasks.recovery_policy import _DoneSets, _is_orphaned, _plan_owner_groups, _plan_replay

    review_id = uuid.uuid4()
    payload = QuarantineCompanionPayload(review_id=review_id, agent_id=_AGENT, source_path=f"{_ROOT}/a/site.nfo", sha256=_SHA, size=len(_JUNK))
    row = SchedulingLedger(
        key=f"{QUARANTINE_TASK}:{review_id}", function=QUARANTINE_TASK, routing=routing_for_function(QUARANTINE_TASK), payload=payload.model_dump()
    )
    nothing_done = _DoneSets(set(), set(), {}, set(), set())

    assert row.routing == "agent"
    assert _is_orphaned(row, live=set(), done_sets=nothing_done, in_flight=set(), awaiting_cloud=set())
    assert not _is_orphaned(row, live={row.key}, done_sets=nothing_done, in_flight=set(), awaiting_cloud=set())
    assert _plan_replay([row]).other_agent_rows == (row,)
    assert [(group.owner_id, group.rows) for group in _plan_owner_groups([row]).groups] == [(_AGENT, (row,))]
