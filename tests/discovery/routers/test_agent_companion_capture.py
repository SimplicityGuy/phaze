"""Real authenticated report -> immutable database text, retained while agents are offline."""

from datetime import UTC, datetime
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.provider_source import ProviderSourceObservation
from phaze.schemas.agent_companion_capture import CaptureBudget, CaptureReport, CaptureTarget
from phaze.services.companion_capture import enqueue_companion_capture
from phaze.services.companion_capture_worker import read_capture
from phaze.tracklist_providers.domain import SourceRead


ROUTE = "/api/internal/agent/companion-captures"


async def seed_pair(session: AsyncSession, agent: Agent, path: Path, raw: bytes) -> tuple[FileRecord, FileRecord]:
    media = FileRecord(
        agent_id=agent.id,
        original_path="/test/music/set.mp3",
        current_path="/test/music/set.mp3",
        original_filename="set.mp3",
        file_type="music",
        file_size=100,
        sha256_hash="a" * 64,
    )
    source = FileRecord(
        agent_id=agent.id,
        original_path=str(path),
        current_path=str(path),
        original_filename=path.name,
        file_type="txt",
        file_size=len(raw),
        sha256_hash=hashlib.sha256(raw).hexdigest(),
    )
    session.add_all([media, source])
    await session.flush()
    session.add(FileCompanion(media_id=media.id, companion_id=source.id))
    await session.commit()
    return source, media


def report(source: FileRecord, media: FileRecord, read: SourceRead) -> CaptureReport:
    return CaptureReport(
        target=CaptureTarget(
            file_id=source.id, media_id=media.id, path=source.current_path, expected_sha256=source.sha256_hash, expected_size=source.file_size
        ),
        read=read,
    )


async def test_real_report_idempotently_retains_text_errors_and_stale_inventory(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession, tmp_path: Path
) -> None:
    agent, _token = seed_test_agent
    path = tmp_path / "notes.txt"
    raw = b"Artist: Example\r\n01. Intro\n"
    path.write_bytes(raw)
    source, media = await seed_pair(session, agent, path, raw)
    request = CaptureTarget(file_id=source.id, media_id=media.id, path=str(path), expected_sha256=source.sha256_hash, expected_size=len(raw))
    read = read_capture(request, CaptureBudget(), [str(tmp_path)])
    body = report(source, media, read).model_dump(mode="json")
    first = await authenticated_client.post(ROUTE, json=body)
    repeat = await authenticated_client.post(ROUTE, json=body)
    assert first.status_code == repeat.status_code == 200, first.text
    assert first.json()["observation_id"] == repeat.json()["observation_id"] and repeat.json()["reused"]
    stored = await session.get(ProviderSourceObservation, uuid.UUID(first.json()["observation_id"]))
    assert stored is not None and stored.decoded_text == raw.decode() and "tracks" not in stored.payload
    unavailable = SourceRead(status="unavailable", code="offline", scope=str(source.id), retrieved_at=datetime.now(UTC))
    assert (await authenticated_client.post(ROUTE, json=report(source, media, unavailable).model_dump(mode="json"))).status_code == 200
    # A changed inventory never upgrades an old successful callback to current authority.
    source.sha256_hash = "b" * 64
    await session.commit()
    stale = await authenticated_client.post(ROUTE, json=body)
    assert stale.status_code == 200 and stale.json()["freshness"] == "stale"
    latest = await session.get(ProviderSourceObservation, uuid.UUID(stale.json()["observation_id"]))
    assert latest is not None and latest.status == "retry" and latest.code == "inventory_changed"
    assert stored.decoded_text == raw.decode()
    assert (await session.scalar(select(func.count()).select_from(ProviderSourceObservation))) == 3


async def test_cross_agent_anonymous_unlinked_and_malformed_reports_refused(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession, tmp_path: Path
) -> None:
    agent, _token = seed_test_agent
    source, media = await seed_pair(session, agent, tmp_path / "notes.txt", b"")
    read = SourceRead(status="unavailable", code="offline", scope=str(source.id), retrieved_at=datetime.now(UTC))
    body = report(source, media, read).model_dump(mode="json")
    async with AsyncClient(transport=authenticated_client._transport, base_url="http://test") as anonymous:
        assert (await anonymous.post(ROUTE, json=body)).status_code == 401
    alien = dict(body)
    alien["target"] = dict(body["target"], file_id=str(uuid.uuid4()))
    alien["read"] = dict(body["read"], scope=alien["target"]["file_id"])
    assert (await authenticated_client.post(ROUTE, json=alien)).status_code == 403
    malformed = dict(body)
    malformed["read"] = dict(body["read"], text="x" * 262145)
    assert (await authenticated_client.post(ROUTE, json=malformed)).status_code == 422
    link = await session.scalar(select(FileCompanion).where(FileCompanion.companion_id == source.id))
    await session.delete(link)
    await session.commit()
    assert (await authenticated_client.post(ROUTE, json=body)).status_code == 409


async def test_stale_maximum_evidence_is_bounded_and_revalidated(
    authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession, tmp_path: Path
) -> None:
    source, media = await seed_pair(session, seed_test_agent[0], tmp_path / "notes.txt", b"")
    read = SourceRead(
        status="unavailable", code="offline", scope=str(source.id), evidence=tuple(str(i) for i in range(64)), retrieved_at=datetime.now(UTC)
    )
    body = report(source, media, read).model_dump(mode="json")
    source.file_size = 1
    await session.commit()
    response = await authenticated_client.post(ROUTE, json=body)
    assert response.status_code == 200, response.text
    row = await session.get(ProviderSourceObservation, uuid.UUID(response.json()["observation_id"]))
    assert row is not None and len(row.payload["evidence"]) == 64


async def test_dispatch_network_runs_after_database_session_closes(
    seed_test_agent: tuple[Agent, str], session: AsyncSession, async_engine: Any, tmp_path: Path
) -> None:
    source, media = await seed_pair(session, seed_test_agent[0], tmp_path / "notes.txt", b"")
    # The factory's checked-out connection count is observed by the actual network seam.
    sessions = []
    factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")

    def tracked_factory():
        created = factory()
        sessions.append(created)
        return created

    class Queue:
        async def connect(self) -> None:
            assert not sessions[0].in_transaction()

        async def enqueue(self, name: str, **kwargs: Any) -> None:
            assert name == "capture_companion_source" and kwargs["agent_id"] == source.agent_id
            assert kwargs["target"]["file_id"] == str(source.id)

    router = SimpleNamespace(queue_for=lambda agent_id, lane: Queue() if agent_id == source.agent_id and lane == "meta" else None)
    payload = await enqueue_companion_capture(tracked_factory, router, source.id, media_id=media.id)
    assert payload.target.expected_sha256 == source.sha256_hash
    with pytest.raises(ValueError, match="inventoried companion"):
        await enqueue_companion_capture(tracked_factory, router, uuid.uuid4())
    with pytest.raises(ValueError, match="not linked"):
        await enqueue_companion_capture(tracked_factory, router, source.id, media_id=uuid.uuid4())
    assert (await enqueue_companion_capture(tracked_factory, router, source.id)).target.media_id is None
