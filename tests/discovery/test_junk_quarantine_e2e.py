"""End to end: an approved junk companion is moved into quarantine and never comes back (phaze-lwuf6).

Real producers throughout. Synthetic files in ``tmp_path`` are ingested by the scan's
``scan_directory`` through the production agent client and the authenticated routes, the detector
proposes the junk, the operator's group approval is :func:`decide_content_group`, and
:func:`enqueue_quarantine` dispatches it. The only stand-in is the broker: the recorded job's kwargs
are handed to the real ``quarantine_companion`` task, which moves the real file and reports through
the real callback route. A second scan and the watcher's event handler then prove the quarantined file
is never re-ingested, and the detector that it is never proposed again.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent

from phaze.agent_watcher.observer import WatcherEventHandler
from phaze.config import AgentSettings
from phaze.constants import QUARANTINE_DIRNAME
from phaze.enums.junk_review import JunkReviewStatus
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.companion_junk_review import decide_content_group, detect_junk_reviews
from phaze.services.junk_quarantine import QUARANTINE_TASK, enqueue_quarantine
from phaze.tasks.junk_quarantine import quarantine_companion
from phaze.tasks.scan import scan_directory


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


class _Broker:
    """Records each enqueued job's kwargs, as SAQ would hand them to the agent's worker."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, str, dict[str, Any]]] = []

    async def enqueue_for_agent(self, *, agent_id: str, task_name: str, payload: BaseModel) -> None:
        self.jobs.append((agent_id, task_name, payload.model_dump(mode="json")))


def _agent_api(client: AsyncClient) -> PhazeAgentClient:
    return PhazeAgentClient(base_url=str(client.base_url), token="unused-test-token", _client=client)  # noqa: S106 -- the client carries the fixture token


def _agent_settings(scan_roots: list[str]) -> AgentSettings:
    cfg = MagicMock(spec=AgentSettings)
    cfg.scan_roots = scan_roots
    return cfg


async def _scan(client: AsyncClient, session: AsyncSession, agent: Agent, root: Path) -> dict[str, Any]:
    batch = ScanBatch(agent_id=agent.id, scan_path=str(root), status=ScanStatus.RUNNING.value, total_files=0, processed_files=0)
    session.add(batch)
    await session.commit()
    return await scan_directory({"api_client": _agent_api(client)}, scan_path=str(root), batch_id=str(batch.id), agent_id=agent.id)


async def _paths(session: AsyncSession) -> set[str]:
    return set((await session.execute(select(FileRecord.original_path).execution_options(populate_existing=True))).scalars())


async def test_an_approved_junk_companion_is_quarantined_and_never_re_ingested(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    agent, _token = seed_test_agent
    root = tmp_path / "music"
    release = root / "Example Artist - Live @ Example Festival - 2024-04-12"
    release.mkdir(parents=True)
    (release / "Example Artist - Live @ Example Festival - 2024-04-12.mp3").write_bytes(b"audio-bytes")
    (release / "Example Artist - Live @ Example Festival - 2024-04-12.cue").write_bytes(
        b'FILE "Example Artist - Live @ Example Festival - 2024-04-12.mp3" MP3\r\n  TRACK 01 AUDIO\r\n    INDEX 01 00:00:00\r\n'
    )
    junk = release / "site.nfo"
    junk.write_bytes(b"")

    assert (await _scan(authenticated_client, session, agent, root))["status"] == "completed"
    assert str(junk) in await _paths(session)

    # The detector proposes the empty file; the operator approves its content group.
    await detect_junk_reviews(session, agent.id, apply=True)
    await session.commit()
    (review,) = (await session.execute(select(CompanionJunkReview))).scalars()
    assert (review.original_path, review.reason, review.status) == (str(junk), "empty", "pending")
    assert await decide_content_group(session, _EMPTY_SHA, JunkReviewStatus.APPROVED) == 1

    broker = _Broker()
    assert await enqueue_quarantine(session, [review.id], task_router=broker) == 1  # type: ignore[arg-type]
    ((job_agent, task_name, kwargs),) = broker.jobs
    assert (job_agent, task_name) == (agent.id, QUARANTINE_TASK)

    with patch("phaze.tasks.junk_quarantine.get_settings", return_value=_agent_settings([str(root)])):
        result = await quarantine_companion({"api_client": _agent_api(authenticated_client)}, **kwargs)
        # A SAQ retry after the move: the quarantine copy corroborates it, and the report is a no-op.
        replay = await quarantine_companion({"api_client": _agent_api(authenticated_client)}, **kwargs)

    destination = root / QUARANTINE_DIRNAME / release.name / "site.nfo"
    assert result == {"review_id": str(review.id), "status": "quarantined", "replayed": False}
    assert replay == {"review_id": str(review.id), "status": "quarantined", "replayed": True}
    assert not junk.exists()
    assert destination.is_file()
    row = (await session.execute(select(CompanionJunkReview).execution_options(populate_existing=True))).scalar_one()
    assert (row.status, row.executed_at is not None, row.error_message) == ("quarantined", True, None)  # the audit record survives
    after_move = await _paths(session)
    assert str(junk) not in after_move  # the files row is retired

    # A second scan of the same root never ingests anything under the quarantine directory.
    assert (await _scan(authenticated_client, session, agent, root))["status"] == "completed"
    assert await _paths(session) == after_move
    assert not any(QUARANTINE_DIRNAME in path for path in await _paths(session))

    # Nor does the watcher, for the events the rename and later touches produce.
    loop = MagicMock()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=MagicMock(), debouncer_move=MagicMock())
    handler.on_moved(FileMovedEvent(src_path=str(junk), dest_path=str(destination)))
    handler.on_created(FileCreatedEvent(src_path=str(destination)))
    handler.on_modified(FileModifiedEvent(src_path=str(destination)))
    loop.call_soon_threadsafe.assert_not_called()

    # And the detector never proposes it again: no file row, and the path is under quarantine.
    await detect_junk_reviews(session, agent.id, apply=True)
    await session.commit()
    statuses = (await session.execute(select(CompanionJunkReview.status).execution_options(populate_existing=True))).scalars().all()
    assert statuses == ["quarantined"]


async def test_a_refused_move_fails_the_row_and_leaves_file_and_row_in_place(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """The bytes changed after approval: the agent refuses, the row fails with the reason, nothing moves."""
    agent, _token = seed_test_agent
    root = tmp_path / "music"
    (root / "rel").mkdir(parents=True)
    junk = root / "rel" / "site.nfo"
    junk.write_bytes(b"")
    await _scan(authenticated_client, session, agent, root)
    await detect_junk_reviews(session, agent.id, apply=True)
    await decide_content_group(session, _EMPTY_SHA, JunkReviewStatus.APPROVED)
    (review,) = (await session.execute(select(CompanionJunkReview))).scalars()
    broker = _Broker()
    await enqueue_quarantine(session, [review.id], task_router=broker)  # type: ignore[arg-type]
    junk.write_bytes(b"now it has content")

    with patch("phaze.tasks.junk_quarantine.get_settings", return_value=_agent_settings([str(root)])):
        result = await quarantine_companion({"api_client": _agent_api(authenticated_client)}, **broker.jobs[0][2])

    assert result["status"] == "failed"
    row = (await session.execute(select(CompanionJunkReview).execution_options(populate_existing=True))).scalar_one()
    assert row.status == "failed"
    assert row.error_message is not None
    assert "the approved file was 0" in row.error_message
    assert junk.read_bytes() == b"now it has content"
    assert not (root / QUARANTINE_DIRNAME / "rel" / "site.nfo").exists()
    assert str(junk) in await _paths(session)
