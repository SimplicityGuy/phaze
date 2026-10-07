"""Control side of the junk quarantine against real Postgres: dispatch, the agent's report, the dry-run plan (phaze-lwuf6).

The agent's move is covered on a real filesystem in ``test_quarantine_move.py`` and end to end in
``tests/discovery/test_junk_quarantine_e2e.py``. Here the enqueuer is a recording fake: these tests are
about which rows are claimed, what is committed before a job leaves, and what a report changes.
"""

from __future__ import annotations

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


async def test_a_dispatch_that_never_reached_the_broker_fails_the_row(session: AsyncSession) -> None:
    await _agents(session)
    lost = await _review(session, f"{_ROOT}/a/site.nfo", JunkReviewStatus.APPROVED)
    sent = await _review(session, f"{_ROOT}/b/site.nfo", JunkReviewStatus.APPROVED)
    await session.commit()
    router = _Router(session, fail={lost.id: ConnectionRefusedError("broker down")})

    assert await enqueue_quarantine(session, [lost.id, sent.id], task_router=router) == 1  # type: ignore[arg-type]

    row = await _status(session, lost.id)
    assert (row.status, row.executed_at is not None) == ("failed", True)
    assert row.error_message is not None
    assert "could not dispatch the quarantine" in row.error_message
    assert "broker down" in row.error_message
    assert (await _status(session, sent.id)).status == "executing"


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
