"""One reviewed selected-source CUE artifact and its inventory/version pin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from phaze.constants import AGENT_LIVENESS_ALIVE_SECONDS
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.schemas.agent_tasks import CueSourceBinding
from phaze.services.cue_generator import generate_cue_content
from phaze.services.cue_review import cue_tracks_from_selected
from phaze.services.local_source_import import get_selected_recording_source
from phaze.services.selected_discogs import accepted_recording_discogs_links


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.schemas.local_source_import import SelectedRecordingSource


@dataclass(frozen=True)
class SelectedCueArtifact:
    file: FileRecord
    source: SelectedRecordingSource
    binding: CueSourceBinding
    content: str
    omitted_track_count: int


async def agent_supports_selected_cue(session: AsyncSession, agent_id: str) -> bool:
    """Only a recently received owning meta/all-mode capability permits the new wire shape."""
    agent = await session.scalar(select(Agent).where(Agent.id == agent_id).execution_options(populate_existing=True))
    if agent is None or agent.revoked_at is not None:
        return False
    status = agent.last_status or {}
    if isinstance(status.get("lanes"), dict):
        status = status["lanes"].get("meta", {})
    if not isinstance(status, dict) or status.get("selected_cue_v1") is not True:
        return False
    try:
        received = datetime.fromisoformat(status["selected_cue_v1_received_at"])
        return 0 <= (datetime.now(UTC) - received).total_seconds() < AGENT_LIVENESS_ALIVE_SECONDS
    except (KeyError, TypeError, ValueError):
        return False


async def build_selected_cue_artifact(session: AsyncSession, media_id: uuid.UUID) -> SelectedCueArtifact:
    """DB-only preview; no legacy fallback once an explicit reviewed selection exists."""
    file = await session.scalar(select(FileRecord).where(FileRecord.id == media_id).execution_options(populate_existing=True))
    selected = await get_selected_recording_source(session, media_id)
    if file is None or selected is None:
        raise ValueError("No selected tracklist source for this recording")
    if selected.availability != "current" or selected.target_mapping is None:
        raise ValueError(f"Selected source is {selected.availability}; exact CUE timing is unavailable")
    tracks = cue_tracks_from_selected(selected, await accepted_recording_discogs_links(session, selected))
    timed = sum(track.timestamp_seconds is not None for track in tracks)
    if not timed:
        raise ValueError("Selected source has no qualified recording offsets; original timing remains visible in the recording details")
    content = generate_cue_content(Path(file.current_path).name, file.file_type, tracks)
    mapping = selected.target_mapping
    binding = CueSourceBinding(
        observation_id=selected.observation_id,
        selection_token=selected.selection_token,
        media_sha256=file.sha256_hash,
        source_sha256=str(mapping["source_sha256"]),
        source_revision=selected.snapshot.revision,
        parser_version=selected.snapshot.parser_version,
        target_mapping_digest=hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )
    return SelectedCueArtifact(file, selected, binding, content, len(tracks) - timed)


async def validate_selected_cue_binding(
    session: AsyncSession, media_id: uuid.UUID, binding: CueSourceBinding, *, agent_id: str, audio_path: str
) -> SelectedCueArtifact:
    """Recompute from current inventory and the exact reviewed observation before dispatch/write."""
    artifact = await build_selected_cue_artifact(session, media_id)
    if artifact.file.agent_id != agent_id or artifact.file.current_path != audio_path or artifact.binding != binding:
        raise ValueError("CUE source selection or inventory changed; review the current preview")
    return artifact
