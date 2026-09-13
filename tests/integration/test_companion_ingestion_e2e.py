"""COMPANION ingestion proof across disk, HTTP, association, and consumers.

The fixture files are the only seeded inputs.  FileRecord and FileCompanion rows must be
produced by the real scanner, authenticated upsert route, and association service; mocking
those database boundaries would let the original dead-producer defect pass unnoticed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.scan_batch import ScanBatch
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.companion import associate_companions
from phaze.services.proposal_context import (
    _date_convention_guidance,
    build_file_context,
    fetch_companion_contents,
    load_companion_targets,
    load_prompt_template,
)
from phaze.services.proposal_parsing import BatchProposalResponse
from phaze.services.proposal_provider import generate_batch
from phaze.services.tracklist_candidate_queue import build_candidate_queue
from phaze.tasks.companion_read import read_companion_files
from phaze.tasks.scan import scan_directory


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


class _InlineAgentQueue:
    """Execute the real agent task behind the same queue round-trip API production uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    async def connect(self) -> None:
        """Mirror the queue connection boundary without opening a second broker."""

    async def apply(self, task_name: str, *, timeout: float, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((task_name, timeout))
        return await read_companion_files({}, **kwargs)


class _InlineAgentTaskRouter:
    """Route the companion read at the control-to-agent boundary, not at Postgres."""

    def __init__(self) -> None:
        self.queue = _InlineAgentQueue()
        self.routes: list[tuple[str, str]] = []

    def queue_for(self, agent_id: str, lane: str) -> _InlineAgentQueue:
        self.routes.append((agent_id, lane))
        return self.queue


async def test_ingested_sibling_cue_reaches_proposal_prompt_and_tracklist_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """A sibling .cue remains useful from its on-disk origin through both consumers."""
    from phaze.config import get_settings

    agent, raw_token = seed_test_agent
    media_path = tmp_path / "Artist - Event - Set (2024).mp3"
    cue_path = tmp_path / "Artist - Event - Set (2024).cue"
    media_path.write_bytes(b"synthetic audio bytes")
    cue_content = 'FILE "Artist - Event - Set (2024).mp3" MP3\n  TRACK 01 AUDIO\n    TITLE "Opening"'
    cue_path.write_text(cue_content, encoding="utf-8")

    batch = ScanBatch(id=uuid.uuid4(), agent_id=agent.id, scan_path=str(tmp_path), status="running")
    session.add(batch)
    await session.commit()

    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", raw_token)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", str(tmp_path))
    monkeypatch.setenv("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@app-server.example:5432/phaze")
    get_settings.cache_clear()

    agent_client = PhazeAgentClient(base_url="http://test", token=raw_token, _client=authenticated_client)
    scan_result = await scan_directory(
        {"api_client": agent_client},
        scan_path=str(tmp_path),
        batch_id=str(batch.id),
        agent_id=agent.id,
    )
    assert scan_result == {"status": "completed", "files_posted": 2}

    records = (await session.execute(select(FileRecord).where(FileRecord.batch_id == batch.id))).scalars().all()
    by_type = {record.file_type: record for record in records}
    assert set(by_type) == {"mp3", "cue"}
    media = by_type["mp3"]
    cue = by_type["cue"]

    assert await associate_companions(session) == 1
    link = (await session.execute(select(FileCompanion))).scalar_one()
    assert (link.companion_id, link.media_id) == (cue.id, media.id)

    task_router = _InlineAgentTaskRouter()
    companion_targets = await load_companion_targets(session, media.id, task_router=task_router)  # type: ignore[arg-type]
    await session.commit()  # release the DB transaction before the agent round trip
    companion_contents = await fetch_companion_contents(companion_targets, 3000, task_router, media.id)  # type: ignore[arg-type]
    assert task_router.routes == [(agent.id, "meta")]
    assert task_router.queue.calls == [("read_companion_files", 30.0)]
    assert companion_contents == [{"filename": cue_path.name, "content": cue_content}]

    file_context = build_file_context(media, None, companion_contents)
    captured: dict[str, Any] = {}

    async def _complete(**kwargs: Any) -> Any:
        captured.update(kwargs)
        content = BatchProposalResponse(proposals=[]).model_dump_json()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")])

    def _parse_completion(content: str | None, *, batch_size: int, finish_reason: str | None) -> BatchProposalResponse:
        assert content is not None
        assert batch_size == 1
        assert finish_reason == "stop"
        return BatchProposalResponse.model_validate_json(content)

    await generate_batch(
        model="test-model",
        prompt_template=load_prompt_template(),
        files_context=[file_context],
        completion_provider=_complete,
        completion_parser=_parse_completion,
        date_guidance=_date_convention_guidance,
    )
    rendered_prompt = captured["messages"][0]["content"]
    assert '"companions": [' in rendered_prompt
    assert json.dumps(cue_content) in rendered_prompt

    candidate_queue = await build_candidate_queue(session)
    assert candidate_queue.stats.media_files == 1
    assert candidate_queue.stats.skipped_cue == 1
    assert candidate_queue.stats.already_tracklisted == 1
    assert candidate_queue.stats.queued == 0
