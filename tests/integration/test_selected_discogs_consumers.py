"""Real selected-source Discogs requests, identity, review and downstream outputs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.provider_source import ProviderSelectionEvent, ProviderSourceObject, ProviderSourceObservation
from phaze.models.tag_write_log import TagWriteLog
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.cue_review import build_cue_tracks_for_file
from phaze.services.local_source_import import decide_local_source, get_selected_recording_source, import_local_source
from phaze.services.proposal_parsing import BatchProposalResponse, FileProposalResponse
from phaze.services.scan_deletion import delete_file_cascade
from phaze.services.selected_discogs import decide_recording_discogs_link
from phaze.services.tag_comparison import (
    _get_accepted_discogs_link,
    _get_accepted_discogs_links_for_files,
    _get_tracklists_for_files,
    _tag_review_payload,
)
from phaze.services.tag_proposal import compute_proposed_tags
from phaze.services.tag_writer import enqueue_tag_write
from phaze.tasks.discogs import match_recording_source_to_discogs
from phaze.tasks.proposal import generate_proposals
from tests._queue_fakes import install_fake_queues
from tests.integration.test_local_source_import import decision, inventory
from tests.integration.test_selected_source_consumers import CUE


async def selected_inventory(session):
    media, companion, raw = await inventory(session, CUE, "cue")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    selected = await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, ordinal=1), actor="reviewer")
    await decide_local_source(session, decision(media, companion, imported.release_observation_id, kind="release_metadata"), actor="reviewer")
    session.info.setdefault("owned_files", []).extend([media.id, companion.id])
    await session.commit()
    return media, companion, selected


@pytest_asyncio.fixture
async def committed_session(async_engine):
    """Real independent transactions for HTTP phase separation and row-lock race evidence."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as db:
        try:
            yield db
        finally:
            owned = db.info.get("owned_files", [])
            await db.rollback()
            async with factory.begin() as cleanup:
                source_ids = list(
                    (await cleanup.execute(select(ProviderSourceObject.id).where(ProviderSourceObject.source_file_id.in_(owned)))).scalars()
                )
                observation_ids = select(ProviderSourceObservation.id).where(ProviderSourceObservation.object_id.in_(source_ids))
                await cleanup.execute(delete(DiscogsLink).where(DiscogsLink.source_observation_id.in_(observation_ids)))
                await cleanup.execute(delete(ProviderSelectionEvent).where(ProviderSelectionEvent.media_id.in_(owned)))
                for file_id in owned:
                    await delete_file_cascade(cleanup, file_id)
                await cleanup.execute(delete(ProviderSourceObservation).where(ProviderSourceObservation.object_id.in_(source_ids)))
                await cleanup.execute(delete(ProviderSourceObject).where(ProviderSourceObject.id.in_(source_ids)))


def external_discogs(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "phaze.services.discogs_matcher.httpx.AsyncClient", lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handler))
    )


async def test_selected_match_accept_and_real_tag_cue_consumers(committed_session, monkeypatch):
    session = committed_session
    media, companion, selected = await selected_inventory(session)
    requests = []

    def search(request):
        requests.append(request)
        assert request.url.path == "/api/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "synthetic-release",
                        "name": "Reviewed Artist - Discogs Title",
                        "relevance": 1,
                        "metadata": {"label": "Reviewed Label", "year": 2025},
                    }
                ]
            },
        )

    external_discogs(monkeypatch, search)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    kwargs = {
        "media_id": str(media.id),
        "expected_observation_id": str(selected.observation_id),
        "expected_selection_token": str(selected.selection_token),
    }
    result = await match_recording_source_to_discogs({"async_session": factory}, **kwargs)
    assert result["status"] == "matched" and result["candidates_created"] == 2
    assert len(requests) == 2 and all("Reviewed Artist" in request.url.params["q"] for request in requests)
    links = (await session.execute(select(DiscogsLink).order_by(DiscogsLink.source_track_position))).scalars().all()
    assert all(link.track_id is None and link.source_observation_id == selected.observation_id for link in links)
    accepted = await decide_recording_discogs_link(
        session, media_id=media.id, observation_id=selected.observation_id, selected_token=selected.selection_token, link_id=links[0].id, accept=True
    )
    accepted_id = accepted.id
    await session.commit()
    resolved = await _get_tracklists_for_files(session, [media.id])
    assert (await _get_accepted_discogs_link(session, media.id)).id == accepted_id
    batch = await _get_accepted_discogs_links_for_files(session, resolved)
    assert batch[media.id].id == accepted_id
    tags = compute_proposed_tags(None, resolved[media.id], media.original_filename, batch[media.id])
    assert tags["artist"] == "Reviewed Artist" and tags["title"] == "Discogs Title" and tags["year"] == 2025
    cue = await build_cue_tracks_for_file(session, media.id)
    assert cue[0].label == "Reviewed Label" and cue[0].year == 2025
    await session.commit()
    # Re-match replaces candidates but keeps accepted intrinsic identity and UUID.
    assert (await match_recording_source_to_discogs({"async_session": factory}, **kwargs))["status"] == "matched"
    assert (await session.get(DiscogsLink, accepted_id, populate_existing=True)).status == "accepted"
    companion.sha256_hash = "b" * 64
    await session.commit()
    assert await _get_accepted_discogs_link(session, media.id) is None
    assert all(track.timestamp_seconds is None for track in await build_cue_tracks_for_file(session, media.id))


async def test_selection_changes_during_actual_http_lookup_prevent_link_write(committed_session, monkeypatch):
    session = committed_session
    media, companion, selected = await selected_inventory(session)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    changed = False

    async def search(request):
        nonlocal changed
        if not changed:
            changed = True
            async with factory() as concurrent:
                current_media = await concurrent.get(FileRecord, media.id)
                current_companion = await concurrent.get(FileRecord, companion.id)
                command = decision(current_media, current_companion, selected.observation_id, ordinal=1).model_copy(
                    update={"expected_selection_token": selected.selection_token, "decision_id": uuid.uuid4()}
                )
                await decide_local_source(concurrent, command, actor="concurrent-reviewer")
                await concurrent.commit()
        return httpx.Response(200, json={"results": [{"id": "synthetic-release", "name": "Reviewed Artist - First", "relevance": 1}]})

    external_discogs(monkeypatch, search)
    result = await match_recording_source_to_discogs(
        {"async_session": factory},
        media_id=str(media.id),
        expected_observation_id=str(selected.observation_id),
        expected_selection_token=str(selected.selection_token),
    )
    assert result["status"] == "stale_source" and result["candidates_created"] == 0
    assert await session.scalar(select(func.count(DiscogsLink.id))) == 0
    assert (await get_selected_recording_source(session, media.id)).selection_token != selected.selection_token


async def test_provider_link_binding_checks_and_concurrent_accept_preserve_one_accepted(committed_session):
    session = committed_session
    media, _, selected = await selected_inventory(session)
    common = {"discogs_release_id": "synthetic", "confidence": 90}
    for binding in (
        {},
        {"source_observation_id": selected.observation_id, "source_track_position": 0},
        {"source_observation_id": selected.observation_id},
    ):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(DiscogsLink(**common, **binding))
                await session.flush()
    links = [DiscogsLink(**common, source_observation_id=selected.observation_id, source_track_position=1) for _ in range(2)]
    session.add_all(links)
    await session.commit()
    ids = [link.id for link in links]
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def accept(link_id):
        async with factory() as concurrent:
            await decide_recording_discogs_link(
                concurrent,
                media_id=media.id,
                observation_id=selected.observation_id,
                selected_token=selected.selection_token,
                link_id=link_id,
                accept=True,
            )
            await concurrent.commit()

    await asyncio.gather(*(accept(link_id) for link_id in ids))
    assert await session.scalar(select(func.count(DiscogsLink.id)).where(DiscogsLink.status == "accepted")) == 1
    assert set((await session.execute(select(DiscogsLink.id))).scalars()) == set(ids)


async def test_real_rename_task_uses_reviewed_facts_and_rechecks_after_llm(committed_session):
    session = committed_session
    media, companion, selected = await selected_inventory(session)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    calls = []
    change_selection = False

    async def llm(contexts):
        calls.append(contexts)
        assert contexts[0]["reviewed_source_tags"]["artist"] == "Reviewed Artist"
        assert contexts[0]["reviewed_source_tags"]["title"] == "Reviewed Set"
        assert contexts[0]["companions"] == []
        if change_selection:
            async with factory() as concurrent:
                current_media = await concurrent.get(FileRecord, media.id)
                current_companion = await concurrent.get(FileRecord, companion.id)
                command = decision(current_media, current_companion, selected.observation_id, ordinal=1).model_copy(
                    update={"expected_selection_token": selected.selection_token, "decision_id": uuid.uuid4()}
                )
                await decide_local_source(concurrent, command, actor="concurrent-reviewer")
                await concurrent.commit()
        return BatchProposalResponse(
            proposals=[FileProposalResponse(file_index=0, proposed_filename="Reviewed Artist - Set.mp3", confidence=95, reasoning="Reviewed facts")]
        )

    context = {"async_session": factory, "redis": AsyncMock(eval=AsyncMock(return_value=1)), "proposal_service": SimpleNamespace(generate_batch=llm)}
    result = await generate_proposals(context, file_ids=[str(media.id)], batch_index=0)
    assert result["status"] == "ok" and result["count"] == 1
    proposal = await session.scalar(select(RenameProposal).where(RenameProposal.file_id == media.id))
    assert proposal.proposed_filename == "Reviewed Artist - Set.mp3"
    assert proposal.context_used["input_context"]["reviewed_source_tags"]["artist"] == "Reviewed Artist"
    await session.commit()
    change_selection = True
    rejected = await generate_proposals(context, file_ids=[str(media.id)], batch_index=1)
    assert rejected["status"] == "stale_source" and rejected["count"] == 0
    assert len(calls) == 2
    assert await session.scalar(select(func.count(RenameProposal.id)).where(RenameProposal.file_id == media.id)) == 1


async def test_actual_discogs_workspace_match_review_forms_and_stale_refusal(client, session):
    media, companion, selected = await selected_inventory(session)
    link = DiscogsLink(
        source_observation_id=selected.observation_id,
        source_track_position=1,
        discogs_release_id="synthetic",
        discogs_artist="Artist",
        discogs_title="Release",
        confidence=95,
    )
    session.add(link)
    await session.commit()
    queue, _ = install_fake_queues(client)
    loaded = await client.get(f"/recording-discogs/{media.id}?page=invalid")
    assert loaded.status_code == 200 and "Release" in loaded.text and "Accept" in loaded.text
    form = {"observation_id": str(selected.observation_id), "selected_token": str(selected.selection_token)}
    queued = await client.post(f"/recording-discogs/{media.id}/match", data=form)
    assert "matching queued" in queued.text and queue.captured[0][0] == "match_recording_source_to_discogs"
    assert queue.captured[0][1]["expected_observation_id"] == str(selected.observation_id)
    accepted = await client.post(f"/recording-discogs/{media.id}/links/{link.id}", data={**form, "action": "accept"})
    assert "decision saved" in accepted.text and (await session.get(DiscogsLink, link.id, populate_existing=True)).status == "accepted"
    dismissed = await client.post(f"/recording-discogs/{media.id}/links/{link.id}", data={**form, "action": "dismiss"})
    assert "dismissed" in dismissed.text
    invalid = await client.post(f"/recording-discogs/{media.id}/links/{link.id}", data={**form, "action": "invalid"})
    assert "Choose accept or dismiss" in invalid.text
    companion = await session.get(FileRecord, companion.id)
    companion.sha256_hash = "b" * 64
    await session.commit()
    refused = await client.post(f"/recording-discogs/{media.id}/match", data=form)
    assert "inventory changed" in refused.text and len(queue.captured) == 1
    assert "No explicit selected" in (await client.get(f"/recording-discogs/{uuid.uuid4()}")).text


async def test_tag_audit_boundary_rejects_changed_accepted_identity_and_values(client, session):
    media, _, selected = await selected_inventory(session)
    session.add(RenameProposal(file_id=media.id, proposed_filename=media.original_filename, status=ProposalStatus.EXECUTED))
    links = [
        DiscogsLink(
            source_observation_id=selected.observation_id,
            source_track_position=1,
            discogs_release_id="synthetic",
            discogs_artist="Accepted Artist",
            discogs_year=2025,
            confidence=95,
        )
        for _ in range(2)
    ]
    session.add_all(links)
    await session.flush()
    _, router = install_fake_queues(client)

    async def accept(link):
        await decide_recording_discogs_link(
            session, media_id=media.id, observation_id=selected.observation_id, selected_token=selected.selection_token, link_id=link.id, accept=True
        )
        await session.commit()

    async def reviewed():
        sources = await _get_tracklists_for_files(session, [media.id])
        accepted = await _get_accepted_discogs_link(session, media.id)
        proposed = compute_proposed_tags(None, sources[media.id], media.original_filename, accepted)
        return proposed, _tag_review_payload(media, sources[media.id], accepted, proposed)["sources"]

    await accept(links[0])
    proposed, versions = await reviewed()
    await accept(links[1])
    with pytest.raises(ValueError, match="Accepted Discogs facts changed"):
        await enqueue_tag_write(session, router, media, proposed, source="reviewed", review_source_versions=versions)
    await session.commit()
    proposed, versions = await reviewed()
    # Even an unchanged timestamp cannot hide a changed used scalar field.
    await session.execute(
        update(DiscogsLink)
        .where(DiscogsLink.id == links[1].id)
        .values(discogs_year=2026, updated_at=DiscogsLink.updated_at)
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(ValueError, match="Accepted Discogs facts changed"):
        await enqueue_tag_write(session, router, media, proposed, source="reviewed", review_source_versions=versions)
    assert not router.captures and await session.scalar(select(func.count(TagWriteLog.id))) == 0
