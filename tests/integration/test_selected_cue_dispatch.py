"""Real import/selection → reviewed CUE route → authenticated worker preflight → versioned bytes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
import pytest
from sqlalchemy import select, update

from phaze.config import AgentSettings
from phaze.models.discogs_link import DiscogsLink
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.provider_source import ProviderRecordingSelection, ProviderSourceObservation
from phaze.schemas.agent_tasks import WriteCueSheetPayload
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.agent_client import AgentApiClientError, PhazeAgentClient
from phaze.services.local_source_import import decide_local_source, get_selected_recording_source, import_local_source
from phaze.services.selected_cue import agent_supports_selected_cue, build_selected_cue_artifact
from phaze.services.selected_discogs import decide_recording_discogs_link
from phaze.tasks.cue_write import write_cue_sheet
from phaze.tracklist_providers.local_parser import PARSER_VERSION
from tests._queue_fakes import install_fake_queues
from tests.integration.test_local_source_import import decision, inventory
from tests.integration.test_selected_source_consumers import CUE


async def test_real_selected_cue_dispatch_authenticated_worker_and_stale_refusal(client, session, seed_test_agent, tmp_path):
    client.headers["Origin"] = "http://test"
    media, companion, raw = await inventory(session, CUE, "cue", agent_id=seed_test_agent[0].id)
    audio = tmp_path / "recording.mp3"
    audio.write_bytes(b"synthetic media")
    media.current_path = str(audio)
    session.add(RenameProposal(file_id=media.id, proposed_filename=media.original_filename, status=ProposalStatus.EXECUTED))
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    selected = await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, ordinal=1), actor="reviewer")
    accepted = DiscogsLink(
        source_observation_id=selected.observation_id,
        source_track_position=1,
        discogs_release_id="synthetic",
        confidence=95,
        discogs_label="Accepted Label",
        discogs_year=2025,
    )
    session.add(accepted)
    await session.flush()
    await decide_recording_discogs_link(
        session, media_id=media.id, observation_id=selected.observation_id, selected_token=selected.selection_token, link_id=accepted.id, accept=True
    )
    await session.commit()
    artifact = await build_selected_cue_artifact(session, media.id)
    with pytest.raises(ValidationError):
        WriteCueSheetPayload(
            file_id=media.id,
            tracklist_id=uuid.uuid4(),
            source_binding=artifact.binding,
            agent_id=media.agent_id,
            audio_path=str(audio),
            content=artifact.content,
        )
    _, router = install_fake_queues(client)
    form = {"binding": artifact.binding.model_dump_json(), "audio_path": str(audio)}
    # Old workers report no capability: no new payload is silently sent to them.
    unsupported = await client.post(f"/cue/files/{media.id}/generate", data=form)
    assert "has not reported selected-source CUE support" in unsupported.text and not router.captures
    agent, token = seed_test_agent
    async with AsyncClient(
        transport=ASGITransport(app=client._transport.app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    ) as agent_http:
        beat = await agent_http.post(
            "/api/internal/agent/heartbeat",
            json={"agent_version": "test", "worker_pid": 1, "queue_depth": 0, "lane": "meta", "selected_cue_v1": True},
        )
        assert beat.status_code == 204 and await agent_supports_selected_cue(session, agent.id)
        preview = await client.get(f"/cue/files/{media.id}/preview")
        assert "INDEX 01 00:01:01" in preview.text and "Generate reviewed CUE artifact" in preview.text
        queued = await client.post(f"/cue/files/{media.id}/generate", data=form)
        assert queued.status_code == 200 and "artifact queued" in queued.text and len(router.captures) == 1
        _, task_name, kwargs = router.captures[0]
        assert task_name == "write_cue_sheet" and kwargs["tracklist_id"] is None
        assert kwargs["source_binding"]["observation_id"] == str(artifact.source.observation_id)
        assert not audio.with_suffix(".cue").exists()
        api = PhazeAgentClient(base_url="http://test", token=token, _client=agent_http)
        cfg = MagicMock(spec=AgentSettings)
        cfg.scan_roots = [str(tmp_path)]
        with patch("phaze.tasks.cue_write.get_settings", return_value=cfg):
            result = await write_cue_sheet({"api_client": api}, **kwargs)
        assert result["cue_version"] == 1
        written = audio.with_suffix(".cue").read_text(encoding="utf-8-sig")
        assert "INDEX 01 00:01:01" in written and "INDEX 01 00:02:02" in written
        assert 'REM LABEL "Accepted Label"' in written and 'REM YEAR "2025"' in written
        # A changed accepted overlay changes the actual artifact digest even with the same selected token.
        accepted.discogs_year = 2026
        await session.commit()
        assert (await build_selected_cue_artifact(session, media.id)).binding.content_sha256 != artifact.binding.content_sha256
        with patch("phaze.tasks.cue_write.get_settings", return_value=cfg), pytest.raises(AgentApiClientError):
            await write_cue_sheet({"api_client": api}, **kwargs)
        assert not audio.with_name("recording.v2.cue").exists()
        # Selection changed while the same payload waited in the queue: actual HTTP409 refuses disk I/O.
        await session.execute(
            update(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id).values(selection_token=uuid.uuid4())
        )
        await session.commit()
        with patch("phaze.tasks.cue_write.get_settings", return_value=cfg), pytest.raises(AgentApiClientError):
            await write_cue_sheet({"api_client": api}, **kwargs)
        assert not audio.with_name("recording.v2.cue").exists()


async def test_capability_requires_current_owning_meta_heartbeat(session, seed_test_agent):
    agent, _ = seed_test_agent
    fresh = datetime.now(UTC).isoformat()
    agent.last_status = {"selected_cue_v1": True, "selected_cue_v1_received_at": fresh, "lanes": {"analyze": {"selected_cue_v1": True}}}
    await session.flush()
    assert not await agent_supports_selected_cue(session, agent.id)
    agent.last_status = {"lanes": {"meta": {"selected_cue_v1": True, "selected_cue_v1_received_at": fresh}}}
    await session.flush()
    assert await agent_supports_selected_cue(session, agent.id)
    agent.last_status = {
        "lanes": {"meta": {"selected_cue_v1": True, "selected_cue_v1_received_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()}}
    }
    await session.flush()
    assert not await agent_supports_selected_cue(session, agent.id)
    agent.last_status = {"lanes": {"meta": {"agent_version": "old"}}}
    await session.flush()
    assert not await agent_supports_selected_cue(session, agent.id)


def test_legacy_cue_wire_bytes_survive_restart_and_authority_xor():
    legacy = {
        "file_id": str(uuid.uuid4()),
        "tracklist_id": str(uuid.uuid4()),
        "agent_id": "test-fileserver",
        "audio_path": "/synthetic/a.mp3",
        "content": "cue",
    }
    assert WriteCueSheetPayload.model_validate_json(WriteCueSheetPayload.model_validate(legacy).model_dump_json()).model_dump() == legacy
    with pytest.raises(ValidationError):
        WriteCueSheetPayload.model_validate({key: value for key, value in legacy.items() if key != "tracklist_id"})


@pytest.mark.parametrize("frame", [75, 77])
async def test_invalid_frame_capture_metadata_review_requires_target_bound_exclusion_ack(client, session, seed_test_agent, tmp_path, frame):
    client.headers["Origin"] = "http://test"
    text = (
        'FILE "recording.mp3" MP3\nPERFORMER "Artist"\n'
        'TRACK 01 AUDIO\nTITLE "Opening"\nINDEX 01 00:01:01\n'
        f'TRACK 02 AUDIO\nTITLE "Untimed"\nINDEX 01 01:02:{frame}\n'
        'TRACK 03 AUDIO\nTITLE "Closing"\nINDEX 01 02:03:74\n'
    )
    agent, token = seed_test_agent
    media, companion, raw = await inventory(session, text, "cue", agent_id=agent.id)
    audio = tmp_path / "recording.mp3"
    audio.write_bytes(b"synthetic media")
    media.current_path = str(audio)
    session.add(RenameProposal(file_id=media.id, proposed_filename=media.original_filename, status=ProposalStatus.EXECUTED))
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert imported.tracklist_status == "found"
    observation = await session.scalar(select(ProviderSourceObservation).where(ProviderSourceObservation.id == imported.tracklist_observation_id))
    assert observation.payload["text"] == text and observation.parser_version == PARSER_VERSION
    assert observation.payload["tracks"][1]["timestamp"]["original"] == f"01:02:{frame}"
    assert observation.payload["tracks"][1]["timestamp"]["offset"] is None
    command = decision(media, companion, imported.tracklist_observation_id, ordinal=1)
    selected = await decide_local_source(session, command, actor="reviewer")
    assert len(selected.tracks) == 3 and selected.tracks[1].timestamp.offset_usability == "unusable"
    assert (await decide_local_source(session, command, actor="reviewer")).selection_token == selected.selection_token
    replay = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert replay.tracklist_observation_id == imported.tracklist_observation_id
    await session.commit()
    assert (await get_selected_recording_source(session, media.id)).tracks[1].timestamp.original == f"01:02:{frame}"
    detail = await client.get(f"/files/{media.id}/companion-observations/{imported.tracklist_observation_id}", headers={"HX-Request": "true"})
    assert "Invalid source timing" in detail.text and f"01:02:{frame}" in detail.text
    artifact = await build_selected_cue_artifact(session, media.id)
    assert artifact.omitted_track_count == 1 and 'TITLE "Untimed"' not in artifact.content
    _, router = install_fake_queues(client)
    async with AsyncClient(
        transport=ASGITransport(app=client._transport.app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    ) as agent_http:
        await agent_http.post(
            "/api/internal/agent/heartbeat",
            json={"agent_version": "test", "worker_pid": 1, "queue_depth": 0, "lane": "meta", "selected_cue_v1": True},
        )
        preview = await client.get(f"/cue/files/{media.id}/preview")
        assert 'name="accept_omitted_tracks"' in preview.text and "1 tracks lack qualified offsets" in preview.text
        form = {"binding": artifact.binding.model_dump_json(), "audio_path": str(audio)}
        refused = await client.post(f"/cue/files/{media.id}/generate", data=form)
        assert "Acknowledge the tracks" in refused.text and not router.captures
        queued = await client.post(f"/cue/files/{media.id}/generate", data={**form, "accept_omitted_tracks": "true"})
        assert "artifact queued" in queued.text and len(router.captures) == 1
        _, _, kwargs = router.captures[0]
        api = PhazeAgentClient(base_url="http://test", token=token, _client=agent_http)
        cfg = MagicMock(spec=AgentSettings)
        cfg.scan_roots = [str(tmp_path)]
        with patch("phaze.tasks.cue_write.get_settings", return_value=cfg):
            result = await write_cue_sheet({"api_client": api}, **kwargs)
        assert result["cue_version"] == 1
        written = audio.with_suffix(".cue").read_text(encoding="utf-8-sig")
        assert 'TITLE "Untimed"' not in written and "INDEX 01 00:01:01" in written and "INDEX 01 02:03:74" in written
        await session.execute(
            update(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id).values(selection_token=uuid.uuid4())
        )
        await session.commit()
        stale = await client.post(f"/cue/files/{media.id}/generate", data={**form, "accept_omitted_tracks": "true"})
        assert "selection or inventory changed" in stale.text and len(router.captures) == 1
        with patch("phaze.tasks.cue_write.get_settings", return_value=cfg), pytest.raises(AgentApiClientError):
            await write_cue_sheet({"api_client": api}, **kwargs)
        assert not audio.with_name("recording.v2.cue").exists()


async def test_only_invalid_timing_is_reviewable_metadata_without_generated_artifact(session):
    media, companion, raw = await inventory(session, 'FILE "recording.mp3" MP3\nTRACK 01 AUDIO\nTITLE "Untimed"\nINDEX 01 00:00:77', "cue")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    selected = await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, ordinal=1), actor="reviewer")
    assert selected.tracks[0].title == "Untimed" and selected.tracks[0].timestamp.offset is None
    with pytest.raises(ValueError, match="no qualified recording offsets"):
        await build_selected_cue_artifact(session, media.id)
