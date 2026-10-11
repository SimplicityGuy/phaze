"""Physical local capture, served review, and owning-worker output as one workflow."""

import asyncio
from collections.abc import AsyncIterator
import contextlib
from datetime import UTC, datetime
from fractions import Fraction
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

from bs4 import BeautifulSoup
import httpx
from httpx import AsyncClient
import pytest
from saq import Job, Status, Worker
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.config import AgentSettings
from phaze.models.agent import Agent
from phaze.models.companion_import import ProviderAcquisitionAttempt
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderSourceObject, ProviderSourceObservation
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.companion_capture import enqueue_companion_capture
from phaze.services.cue_generator import generate_cue_content
from phaze.services.cue_review import build_cue_tracks_for_file
from phaze.services.local_source_import import get_selected_recording_source
from phaze.services.proposal_parsing import BatchProposalResponse, FileProposalResponse
from phaze.services.search_queries import search
from phaze.services.selected_cue import build_selected_cue_artifact
from phaze.services.tag_comparison import _get_accepted_discogs_link, _get_tracklist_for_file
from phaze.services.tag_proposal import compute_proposed_tags
from phaze.services.track_segments import build_track_segments
from phaze.services.tracklist_review import get_file_tracklist_review
from phaze.tasks.companion_capture import capture_companion_source
from phaze.tasks.cue_write import write_cue_sheet
from phaze.tasks.discogs import match_recording_source_to_discogs
from phaze.tasks.proposal import generate_proposals
from phaze.tracklist_providers.domain import SourceRead
from phaze.tracklist_providers.local_release import RELEASE_PARSER_VERSION
from tests.db_guard import integration_dsns
from tests.discovery.routers.test_agent_companion_capture import report, seed_pair
from tests.integration.test_companion_viewing import HX, TEXT


pytestmark = pytest.mark.integration


async def complete_owned_job(worker: Worker, job: Job, session: AsyncSession, *, timeout: float = 60) -> Job:
    """Process a bounded real broker step; cancellation/retry is never completion."""

    stopping = asyncio.Event()

    async def process_until_terminal() -> Job:
        while not stopping.is_set():
            current = await worker.queue.job(job.key)
            assert current is not None, f"Owned broker job disappeared: {job.key}"
            if current.status == Status.COMPLETE:
                return current
            assert current.status not in {Status.FAILED, Status.ABORTED}, current.error
            await worker.process()
            # SAQ swallows cancellation and retries; do not start another attempt.
            if stopping.is_set():
                raise asyncio.CancelledError
        raise asyncio.CancelledError

    runner = asyncio.create_task(process_until_terminal())
    try:
        done, _ = await asyncio.wait({runner}, timeout=timeout)
        if not done:
            raise TimeoutError(f"Owned job did not complete: {job.key}")
        completed = await runner
        assert completed.status == Status.COMPLETE and completed.result is not None
        return completed
    except BaseException:
        stopping.set()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        children = [context["task"] for context in worker.job_task_contexts.values()]
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        # The real callback shares this fixture's savepoint connection. A cancelled
        # flush can invalidate it; rollback before any later read or fixture cleanup.
        if session.bind is not None and session.bind.invalidated:
            # Clear the invalid outer/savepoint transaction before Session can
            # reconnect. A callback cancellation may have invalidated both.
            await session.bind.rollback()
        await session.rollback()
        # Reconnect through the async API before the fixture's synchronous info
        # inspection; no test or callback may read an invalidated transaction.
        await session.connection()
        raise


@contextlib.asynccontextmanager
async def physical_source(
    session: AsyncSession,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    extension: str = "nfo",
    kind: str = "mp3",
    *,
    offline_first: bool = False,
    stem: str = "notes",
) -> AsyncIterator[tuple[FileRecord, FileRecord, ProviderSourceObservation, AgentTaskRouter, Worker]]:
    """Reuse the real broker seam, extending its lifetime through served consumers."""
    agent, token = seed_test_agent
    path = tmp_path / f"{stem}.{extension}"
    path.write_bytes(text.encode())
    source, media = await seed_pair(session, agent, path, text.encode())
    source.file_type, media.file_type = extension, kind
    audio = tmp_path / f"recording.{kind}"
    audio.write_bytes(b"synthetic media")
    media.current_path = str(audio)
    media.original_path = str(tmp_path / f"{stem}-recording.{kind}")
    await session.commit()
    dsn, _ = integration_dsns()
    router = AgentTaskRouter(queue_url=dsn, cache_redis_url=os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0"))
    queue = router.queue_for(agent.id, "meta")
    owned_jobs: list[Job] = []
    original_enqueue = queue.enqueue

    async def enqueue_owned(*args: Any, **kwargs: Any) -> Job | None:
        job = await original_enqueue(*args, **kwargs)
        if job is not None:
            owned_jobs.append(job)
        return job

    monkeypatch.setattr(queue, "enqueue", enqueue_owned)
    api = PhazeAgentClient(base_url="http://test", token=token, _client=authenticated_client)
    worker = Worker(queue=queue, functions=[capture_companion_source, write_cue_sheet], concurrency=1, dequeue_timeout=0.5)
    worker.context.update(api_client=api, agent_identity=SimpleNamespace(agent_id=agent.id), owned_jobs=owned_jobs)
    config = MagicMock(spec=AgentSettings)
    config.scan_roots = [str(tmp_path)]
    monkeypatch.setattr("phaze.tasks.companion_capture.get_settings", lambda: config)
    monkeypatch.setattr("phaze.tasks.cue_write.get_settings", lambda: config)
    factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        if offline_first:
            failure = report(source, media, SourceRead(status="unavailable", code="offline", scope=str(source.id), retrieved_at=datetime.now(UTC)))
            failure = failure.model_copy(update={"attempt_id": uuid.uuid4()})
            response = await authenticated_client.post("/api/internal/agent/companion-captures", json=failure.model_dump(mode="json"))
            assert response.status_code == 200
        await enqueue_companion_capture(factory, router, source.id, media_id=media.id)
        completed = await complete_owned_job(worker, owned_jobs[-1], session)
        raw = (
            await session.execute(
                select(ProviderSourceObservation)
                .join(ProviderSourceObject)
                .where(
                    ProviderSourceObject.source_file_id == source.id,
                    ProviderSourceObservation.parser_version == "source-read-v1",
                    ProviderSourceObservation.status == "found",
                )
            )
        ).scalar_one()
        assert raw is not None and raw.decoded_text == text and raw.status == "found"
        assert completed.result["observation_id"] == str(raw.id)
        yield source, media, raw, router, worker
    finally:
        children = [context["task"] for context in worker.job_task_contexts.values()]
        for child in children:
            child.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        if not session.is_active:
            await session.rollback()
        async with queue.pool.connection() as connection:
            await connection.execute("DELETE FROM saq_jobs WHERE queue = %s AND key = ANY(%s)", (queue.name, [job.key for job in owned_jobs]))
        await router.close()


async def native_import(client: AsyncClient, media: FileRecord, raw: ProviderSourceObservation) -> None:
    detail = await client.get(f"/files/{media.id}/companion-sources/{raw.object_id}/observations", headers=HX)
    soup = BeautifulSoup(detail.text, "html.parser")
    form = soup.find("form", attrs={"hx-post": "/api/local-sources/reimport"})
    if not raw.decoded_text:
        assert form is None and soup.select_one("[data-source-review]") is not None
        response = await client.post("/api/local-sources/reimport", json={"media_id": str(media.id), "raw_observation_id": str(raw.id)}, headers=HX)
        assert response.status_code == 200 and "Content extracted" in response.text
        return
    assert form is not None, detail.text
    values = {field["name"]: field.get("value", "") for field in form.select("input[name]")}
    response = await client.post(form["hx-post"], data=values, headers=HX)
    assert response.status_code == 200 and "Content extracted" in response.text


async def parsed(session: AsyncSession, raw: ProviderSourceObservation, kind: str) -> ProviderSourceObservation | None:
    return await session.scalar(
        select(ProviderSourceObservation).where(
            ProviderSourceObservation.parent_id == raw.id,
            ProviderSourceObservation.parser_version == RELEASE_PARSER_VERSION
            if kind == "release_metadata"
            else ProviderSourceObservation.parser_version != RELEASE_PARSER_VERSION,
        )
    )


async def native_select(client: AsyncClient, media: FileRecord, observation: ProviderSourceObservation, ordinal: int | None = None) -> None:
    detail = await client.get(f"/files/{media.id}/companion-observations/{observation.id}", headers=HX)
    form = BeautifulSoup(detail.text, "html.parser").select_one("[data-source-decision]")
    assert form is not None
    values = {field["name"]: field.get("value", "") for field in form.select('input[type="hidden"]')}
    values.update(action="select", accept_conflict="true")
    if ordinal is not None:
        values["cue_file_ordinal"] = str(ordinal)
    response = await client.post(form["hx-post"], data=values, headers=HX)
    assert "Source selected" in response.text, response.text


@pytest.mark.parametrize("kind,extension", [("mp3", "nfo"), ("mp4", "txt")])
async def test_physical_capture_native_import_music_video_stored_page_drawer(
    client: AsyncClient,
    session: AsyncSession,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    extension: str,
) -> None:
    text = TEXT.replace("\n", "\r\n")
    async with physical_source(session, authenticated_client, seed_test_agent, tmp_path, monkeypatch, text, extension, kind) as (
        source,
        media,
        raw,
        _router,
        _worker,
    ):
        await native_import(client, media, raw)
        observation = await parsed(session, raw, "tracklist")
        assert observation is not None

        def forbidden(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("Viewing cannot acquire companion data")

        monkeypatch.setattr("phaze.services.companion_capture.enqueue_companion_capture", forbidden)
        monkeypatch.setattr("phaze.routers.record.enqueue_companion_capture", forbidden)
        texts = []
        for path in (f"/files/{media.id}", f"/record/{media.id}"):
            response = await client.get(path)
            assert response.status_code == 200
            soup = BeautifulSoup(response.text, "html.parser")
            assert "Example Festival" in soup.select_one("[data-visible-companion-release]").get_text()
            assert "Opening" in soup.select_one("[data-visible-track]").get_text()
            assert f"/files/{source.id}#companion-sources" in response.text
            texts.append(soup.select_one("[data-record-content]").get_text())
        assert texts[0] == texts[1]
        detail = await client.get(f"/files/{media.id}/companion-observations/{observation.id}", headers=HX)
        original = detail
        assert BeautifulSoup(original.text, "html.parser").select_one("[data-companion-text]") is not None
        if text:
            assert BeautifulSoup(original.text, "html.parser").select_one("[data-companion-text]").get_text() == text
        else:
            assert "empty" in original.text.lower()
        assert "&lt;script&gt;" in original.text and "<script>alert(" not in original.text
        reverse = await client.get(f"/files/{source.id}/linked-media", headers=HX)
        assert media.original_filename in reverse.text
        assert (await get_file_tracklist_review(session, media.id)).selected_source is None


@pytest.mark.parametrize("multi", [False, True])
async def test_captured_cue_native_mapping_real_broker_authenticated_write(
    client: AsyncClient,
    session: AsyncSession,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi: bool,
) -> None:
    text = 'FILE "one.mp3" MP3\n TRACK 01 AUDIO\n TITLE "First target"\n INDEX 01 00:01:01\n'
    if multi:
        text += 'FILE "two.mp3" MP3\n TRACK 01 AUDIO\n TITLE "Second target"\n INDEX 01 00:02:02\n'
    async with physical_source(session, authenticated_client, seed_test_agent, tmp_path, monkeypatch, text, "cue") as (
        _source,
        media,
        raw,
        router,
        worker,
    ):
        await native_import(client, media, raw)
        observation = await parsed(session, raw, "tracklist")
        await native_select(client, media, observation, 1)
        review = await get_file_tracklist_review(session, media.id)
        assert [row.title for row in review.tracks] == ["First target"]
        assert build_track_segments(review.tracks, [], 30)[0].start_sec == float(Fraction(76, 75))
        assert (await build_cue_tracks_for_file(session, media.id))[0].timestamp_seconds == Fraction(76, 75)
        session.add(RenameProposal(file_id=media.id, proposed_filename=media.original_filename, status=ProposalStatus.EXECUTED))
        await session.commit()
        if multi:
            audio = tmp_path / "second.mp3"
            audio.write_bytes(b"synthetic second target")
            target = FileRecord(
                agent_id=media.agent_id,
                original_path=str(audio),
                current_path=str(audio),
                original_filename=audio.name,
                file_type="mp3",
                sha256_hash="b" * 64,
                file_size=audio.stat().st_size,
            )
            session.add(target)
            await session.flush()
            session.add(FileCompanion(media_id=target.id, companion_id=_source.id))
            await session.commit()
            await native_import(client, target, raw)
            await native_select(client, target, observation, 2)
            other = await build_selected_cue_artifact(session, target.id)
            assert "Second target" in other.content and "First target" not in other.content
            assert "INDEX 01 00:02:02" in other.content
        beat = await authenticated_client.post(
            "/api/internal/agent/heartbeat",
            json={"agent_version": "test", "worker_pid": 1, "queue_depth": 0, "lane": "meta", "selected_cue_v1": True},
        )
        assert beat.status_code == 204
        artifact = await build_selected_cue_artifact(session, media.id)
        preview = await client.get(f"/cue/files/{media.id}/preview")
        form = BeautifulSoup(preview.text, "html.parser").find("form")
        assert form is not None
        values = {field["name"]: field.get("value", "") for field in form.select("input[name]")}
        client._transport.app.state.task_router = router
        queued = await client.post(f"/cue/files/{media.id}/generate", data=values, headers={"Origin": "http://test"})
        assert "artifact queued" in queued.text, queued.text
        completed = await complete_owned_job(worker, worker.context["owned_jobs"][-1], session)
        assert completed.function == "write_cue_sheet"
        written = Path(media.current_path).with_suffix(".cue").read_text(encoding="utf-8-sig")
        assert "INDEX 01 00:01:01" in written and "First target" in written and "Second target" not in written
        assert artifact.binding.observation_id == observation.id


async def test_authenticated_attempt_chronology_retains_physical_text(
    client: AsyncClient,
    session: AsyncSession,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with physical_source(
        session, authenticated_client, seed_test_agent, tmp_path, monkeypatch, TEXT.split("<script>")[0], offline_first=True
    ) as (
        source,
        media,
        raw,
        _router,
        _worker,
    ):
        await native_import(client, media, raw)
        failure = report(source, media, SourceRead(status="unavailable", code="offline", scope=str(source.id), retrieved_at=datetime.now(UTC)))
        failure = failure.model_copy(update={"attempt_id": uuid.uuid4()})
        first = await authenticated_client.post("/api/internal/agent/companion-captures", json=failure.model_dump(mode="json"))
        replay = await authenticated_client.post("/api/internal/agent/companion-captures", json=failure.model_dump(mode="json"))
        again = await authenticated_client.post(
            "/api/internal/agent/companion-captures", json=failure.model_copy(update={"attempt_id": uuid.uuid4()}).model_dump(mode="json")
        )
        assert first.status_code == replay.status_code == again.status_code == 200
        assert first.json()["observation_id"] == again.json()["observation_id"]
        assert await session.scalar(select(func.count()).select_from(ProviderAcquisitionAttempt)) == 4
        attempts = (await session.execute(select(ProviderAcquisitionAttempt).order_by(ProviderAcquisitionAttempt.ordinal))).scalars().all()
        assert attempts[0].observation_id == attempts[-1].observation_id
        assert attempts[1].observation_id == raw.id
        for path in (f"/files/{media.id}", f"/record/{media.id}"):
            page = await client.get(path)
            assert "offline" in page.text and "Example Festival" in page.text
        original = await client.get(f"/files/{media.id}/companion-observations/{raw.id}/text", headers=HX)
        assert "Example Festival" in original.text
        observation = await parsed(session, raw, "tracklist")
        await native_select(client, media, observation)
        await session.execute(delete(FileCompanion).where(FileCompanion.media_id == media.id, FileCompanion.companion_id == source.id))
        await session.commit()
        assert (await get_selected_recording_source(session, media.id)).availability == "unlinked"
        for path in (f"/files/{media.id}", f"/record/{media.id}"):
            page = await client.get(path)
            assert "unlinked" in page.text and "Example Festival" in page.text
        assert "Example Festival" in (await client.get(f"/files/{media.id}/companion-observations/{raw.id}/text", headers=HX)).text
        # History grants the exact reviewed raw parent, not a sibling failed read.
        sibling = first.json()["observation_id"]
        assert (await client.get(f"/files/{media.id}/companion-observations/{sibling}/text", headers=HX)).status_code == 404
        assert (await client.get(f"/files/{media.id}/companion-observations/{uuid.uuid4()}/text", headers=HX)).status_code == 404
        unrelated_object = ProviderSourceObject(provider_id="local", native_id="synthetic-unrelated", native_digest="e" * 64)
        session.add(unrelated_object)
        await session.flush()

        def retained_row(object_id: uuid.UUID, parser_version: str, parent_id: uuid.UUID | None = None) -> ProviderSourceObservation:
            return ProviderSourceObservation(
                object_id=object_id,
                status="found",
                code="synthetic",
                revision="e" * 64,
                revision_scope="full",
                parser_version=parser_version,
                content_digest="e" * 64,
                payload=raw.payload,
                decoded_text="unrelated retained text",
                retrieved_at=datetime.now(UTC),
                parent_id=parent_id,
            )

        unrelated_raw = retained_row(unrelated_object.id, "source-read-v1")
        later_raw = retained_row(raw.object_id, "source-read-v1")
        session.add_all([unrelated_raw, later_raw])
        await session.flush()
        wrong_object_child = retained_row(raw.object_id, "synthetic-parser", unrelated_raw.id)
        foreign_media_child = retained_row(raw.object_id, "synthetic-parser", uuid.UUID(sibling))
        session.add_all([wrong_object_child, foreign_media_child])
        await session.flush()
        session.add_all(
            [
                ProviderRecordingCandidate(media_id=media.id, observation_id=wrong_object_child.id),
                ProviderRecordingCandidate(media_id=source.id, observation_id=foreign_media_child.id),
            ]
        )
        await session.commit()
        for denied in (unrelated_raw.id, later_raw.id, uuid.UUID(sibling)):
            for suffix in ("", "/text"):
                assert (await client.get(f"/files/{media.id}/companion-observations/{denied}{suffix}", headers=HX)).status_code == 404
        history = await client.get(f"/files/{media.id}/companion-sources/{raw.object_id}/observations?history_only=true", headers=HX)
        assert str(raw.id) in history.text and str(later_raw.id) not in history.text and sibling not in history.text
        session.add(Agent(id="foreign-owner", name="Foreign owner", token_hash="c" * 64))
        source.agent_id = "foreign-owner"
        await session.commit()
        assert (await client.get(f"/files/{media.id}/companion-observations/{raw.id}/text", headers=HX)).status_code == 404
        assert (await client.get(f"/files/{media.id}/companion-observations/{raw.id}", headers=HX)).status_code == 404
        assert all(row.timestamp_seconds is None for row in await build_cue_tracks_for_file(session, media.id))


@pytest.mark.parametrize("text", ["", "Unsupported playlist prose without known fields\n"])
async def test_physical_text_only_and_native_exact_embedded_channel_remain_inspectable(
    client: AsyncClient,
    session: AsyncSession,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    async with physical_source(session, authenticated_client, seed_test_agent, tmp_path, monkeypatch, text, "txt") as (
        _source,
        media,
        raw,
        _router,
        _worker,
    ):
        await native_import(client, media, raw)
        tags = FileMetadata(file_id=media.id, raw_tags={"Comments": "01. Embedded - Untimed\n", "comments": "Different exact channel"})
        session.add(tags)
        await session.commit()
        page = await client.get(f"/files/{media.id}")
        form = BeautifulSoup(page.text, "html.parser").find("input", attrs={"name": "embedded_channel", "value": "Comments"}).find_parent("form")
        values = {field["name"]: field.get("value", "") for field in form.select("input[name]")}
        assert "Content extracted" in (await client.post(form["hx-post"], data=values, headers=HX)).text
        for path in (f"/files/{media.id}", f"/record/{media.id}"):
            response = await client.get(path)
            assert "Untimed" in response.text
        rows = (await session.execute(select(ProviderSourceObject).where(ProviderSourceObject.source_file_id == media.id))).scalars().all()
        assert [row.native_id for row in rows] == [f"embedded:{media.id}:Comments"]
        assert await get_selected_recording_source(session, media.id) is None
        assert not await build_cue_tracks_for_file(session, media.id)
        original = await client.get(f"/files/{media.id}/companion-observations/{raw.id}/text", headers=HX)
        assert original.status_code == 200
        if text:
            assert BeautifulSoup(original.text, "html.parser").select_one("[data-companion-text]").get_text() == text
        else:
            assert "Stored text is empty" in original.text


async def test_physical_competing_sources_native_independent_choices_preserve_manual_authority(
    client: AsyncClient,
    session: AsyncSession,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_text = "Artist: Source Artist\nAlbum: First Album\n01. Track Artist - Opening\n"
    second_text = "Artist: Release Artist\nAlbum: Alternate Album\nGenre: House\n01. Alternate - Finale\n"
    async with physical_source(session, authenticated_client, seed_test_agent, tmp_path, monkeypatch, first_text, stem="first") as (
        _source,
        media,
        raw,
        _router,
        _worker,
    ):
        legacy = Tracklist(file_id=media.id, source="manual", external_id="synthetic-reviewed", source_url="", artist="Manual Artist")
        session.add(legacy)
        await session.flush()
        version = TracklistVersion(tracklist_id=legacy.id, version_number=7)
        session.add(version)
        await session.flush()
        legacy.latest_version_id = version.id
        track = TracklistTrack(version_id=version.id, position=1, artist="Manual Artist", title="Manual Title", timestamp="00:01:01")
        session.add(track)
        await session.flush()
        accepted = DiscogsLink(
            track_id=track.id, discogs_release_id="synthetic-manual", confidence=99, status="accepted", discogs_artist="Manual Accepted"
        )
        tags = FileMetadata(file_id=media.id, artist="Embedded Artist", title="Embedded Title", raw_tags={"Comments": "untimed original comment"})
        session.add_all([accepted, tags])
        await session.commit()
        saved = (legacy.id, version.id, track.id, accepted.id)
        await native_import(client, media, raw)
        async with physical_source(session, authenticated_client, seed_test_agent, tmp_path, monkeypatch, second_text, stem="second") as (
            alternate,
            _other,
            raw_b,
            _router_b,
            _worker_b,
        ):
            session.add(FileCompanion(media_id=media.id, companion_id=alternate.id))
            await session.commit()
            await native_import(client, media, raw_b)
            assert (await get_file_tracklist_review(session, media.id)).tracklist.id == legacy.id
            assert (await _get_accepted_discogs_link(session, media.id)).id == accepted.id
            assert not build_track_segments((await get_file_tracklist_review(session, media.id)).tracks, [], 30, media_id=media.id)
            legacy_cue = await build_cue_tracks_for_file(session, media.id)
            assert all(row.timestamp_seconds is None for row in legacy_cue)
            assert "INDEX 01" not in generate_cue_content("recording.mp3", "mp3", legacy_cue)
            assert not (await search(session, "Alternate Album"))[0]
            a_tracks, b_release = await parsed(session, raw, "tracklist"), await parsed(session, raw_b, "release_metadata")
            await native_select(client, media, a_tracks)
            await native_select(client, media, b_release)
            selected = await get_selected_recording_source(session, media.id)
            release = await get_selected_recording_source(session, media.id, kind="release_metadata")
            assert selected.observation_id == a_tracks.id and release.observation_id == b_release.id
            resolved = await _get_tracklist_for_file(session, media.id)
            proposed = compute_proposed_tags(tags, resolved, media.original_filename, None)
            assert proposed["artist"] == "Release Artist" and proposed["album"] == "Alternate Album"
            assert (await search(session, "Alternate Album"))[0][0].id == str(media.id)
            requests = []

            def external(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "synthetic-new",
                                "name": "Matched Artist - Matched Title",
                                "relevance": 1,
                                "metadata": {"label": "Matched Label", "year": 2025},
                            }
                        ]
                    },
                )

            http_type = httpx.AsyncClient
            monkeypatch.setattr(
                "phaze.services.discogs_matcher.httpx.AsyncClient", lambda **kwargs: http_type(**kwargs, transport=httpx.MockTransport(external))
            )
            factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
            result = await match_recording_source_to_discogs(
                {"async_session": factory},
                media_id=str(media.id),
                expected_observation_id=str(selected.observation_id),
                expected_selection_token=str(selected.selection_token),
            )
            assert result["status"] == "matched" and len(requests) == 1
            workspace = await client.get(f"/recording-discogs/{media.id}")
            form = BeautifulSoup(workspace.text, "html.parser").find("form", attrs={"action": lambda value: value and "/links/" in value})
            assert form is not None
            values = {field["name"]: field.get("value", "") for field in form.select("input[name]")}
            values["action"] = "accept"
            reviewed = await client.post(form["action"], data=values, headers={"Origin": "http://test"})
            assert reviewed.status_code == 200
            actual_link = await _get_accepted_discogs_link(session, media.id)
            assert actual_link.source_observation_id == a_tracks.id and actual_link.track_id is None
            assert (
                compute_proposed_tags(tags, await _get_tracklist_for_file(session, media.id), media.original_filename, actual_link)["title"]
                == "Matched Title"
            )
            llm_contexts = []

            async def llm(contexts: list[dict[str, Any]]) -> BatchProposalResponse:
                llm_contexts.extend(contexts)
                assert contexts[0]["reviewed_source_tags"]["artist"] == "Matched Artist"
                assert contexts[0]["reviewed_source_tags"]["album"] == "Alternate Album"
                return BatchProposalResponse(
                    proposals=[
                        FileProposalResponse(
                            file_index=0,
                            proposed_filename="Matched Artist - Alternate Album.mp3",
                            confidence=95,
                            reasoning="Explicit reviewed sources",
                        )
                    ]
                )

            proposal_result = await generate_proposals(
                {
                    "async_session": factory,
                    "redis": AsyncMock(eval=AsyncMock(return_value=1)),
                    "proposal_service": SimpleNamespace(generate_batch=llm),
                },
                file_ids=[str(media.id)],
                batch_index=0,
            )
            assert proposal_result["status"] == "ok" and proposal_result["count"] == 1 and len(llm_contexts) == 1
            stored_proposal = await session.scalar(select(RenameProposal).where(RenameProposal.file_id == media.id))
            assert stored_proposal.proposed_filename == "Matched Artist - Alternate Album.mp3"
            assert stored_proposal.context_used["input_context"]["reviewed_source_tags"]["album"] == "Alternate Album"
            await native_import(client, media, raw_b)
            pending_tracks = await parsed(session, raw_b, "tracklist")
            detail = await client.get(f"/files/{media.id}/companion-observations/{pending_tracks.id}", headers=HX)
            reject = BeautifulSoup(detail.text, "html.parser").select_one("[data-source-decision]")
            values = {field["name"]: field.get("value", "") for field in reject.select('input[type="hidden"]')}
            values["action"] = "reject"
            assert "Candidate skipped" in (await client.post(reject["hx-post"], data=values, headers=HX)).text
            assert (await get_selected_recording_source(session, media.id)).selection_token == selected.selection_token
            assert (await get_selected_recording_source(session, media.id, kind="release_metadata")).selection_token == release.selection_token
            assert (legacy.id, version.id, track.id, accepted.id) == saved and version.version_number == 7
            assert track.timestamp == "00:01:01" and track.timestamp_evidence is None and accepted.status == "accepted"
            assert tags.artist == "Embedded Artist" and tags.raw_tags == {"Comments": "untimed original comment"}


async def test_actual_callback_cancellation_drains_worker_and_rolls_back_flush(session, authenticated_client, seed_test_agent, tmp_path, monkeypatch):
    from sqlalchemy import text

    async with physical_source(session, authenticated_client, seed_test_agent, tmp_path, monkeypatch, TEXT) as (source, media, _raw, router, worker):
        backend_pid = await session.scalar(select(func.pg_backend_pid()))
        original_flush = session.flush

        async def hold_callback_flush(*args, **kwargs):
            # A real callback storage await on the actual fixture connection, not
            # a mocked broker/read/report. Cancel only after PG confirms it active.
            await session.execute(text("SELECT pg_sleep(60) /* jwals_callback_flush */"))
            return await original_flush(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(session, "flush", hold_callback_flush)
            factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
            await enqueue_companion_capture(factory, router, source.id, media_id=media.id)
            job = worker.context["owned_jobs"][-1]
            completion = asyncio.create_task(complete_owned_job(worker, job, session))
            observer = async_sessionmaker(session.bind.engine, expire_on_commit=False)

            async def wait_for_callback_query():
                async with observer() as db:
                    while not await db.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid=:pid "
                            "AND state='active' AND query LIKE '%jwals_callback_flush%')"
                        ),
                        {"pid": backend_pid},
                    ):
                        # PG caches statistics per transaction. Each poll must
                        # observe current activity rather than the first snapshot.
                        await db.commit()
                        await asyncio.sleep(0.01)

            try:
                await asyncio.wait_for(wait_for_callback_query(), 10)
                completion.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await completion
            finally:
                if not completion.done():
                    completion.cancel()
                    await asyncio.gather(completion, return_exceptions=True)
        assert not worker.job_task_contexts
        retried = await worker.queue.job(job.key)
        assert retried is not None and retried.status == Status.QUEUED and retried.error == "cancelled"
        assert session.is_active and await session.scalar(select(1)) == 1
