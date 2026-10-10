"""Discogs review identity bound to a real immutable selected provider track."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from phaze.models.discogs_link import DiscogsLink
from phaze.models.provider_source import ProviderSourceObservation
from phaze.services.local_source_import import get_selected_recording_source
from phaze.services.selected_tag_sources import SelectedTagSource, overlay_selected_tag_sources, validate_selected_tag_review


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.schemas.local_source_import import SelectedRecordingSource


@dataclass(frozen=True)
class RecordingDiscogsPin:
    source: SelectedRecordingSource
    evidence: list[dict[str, Any]]


async def read_recording_discogs_pin(
    session: AsyncSession, media_id: uuid.UUID, observation_id: uuid.UUID, selected_token: uuid.UUID
) -> RecordingDiscogsPin:
    """Read current target projection and its bounded inventory evidence before external lookup."""
    selected = await get_selected_recording_source(session, media_id)
    facts = (await overlay_selected_tag_sources(session, [media_id], {})).get(media_id)
    if (
        selected is None
        or selected.observation_id != observation_id
        or selected.selection_token != selected_token
        or selected.availability != "current"
        or not isinstance(facts, SelectedTagSource)
    ):
        raise ValueError("Selected source or recording inventory changed; refresh the recording")
    evidence = [item for item in facts.evidence if item["kind"] == "tracklist"]
    if not evidence or any(item["availability"] != "current" for item in evidence):
        raise ValueError("Selected tracklist inventory is unavailable")
    return RecordingDiscogsPin(selected, evidence)


async def lock_recording_discogs_pin(session: AsyncSession, pin: RecordingDiscogsPin) -> None:
    """Revalidate under globally ordered inventory locks, then serialize this source's link writes."""
    await validate_selected_tag_review(session, pin.source.media_id, pin.evidence, kind="tracklist")
    current = await read_recording_discogs_pin(session, pin.source.media_id, pin.source.observation_id, pin.source.selection_token)
    if current.source.tracks != pin.source.tracks or current.source.target_mapping != pin.source.target_mapping:
        raise ValueError("Selected track projection changed during Discogs lookup")
    await session.execute(select(ProviderSourceObservation.id).where(ProviderSourceObservation.id == pin.source.observation_id).with_for_update())


async def decide_recording_discogs_link(
    session: AsyncSession,
    *,
    media_id: uuid.UUID,
    observation_id: uuid.UUID,
    selected_token: uuid.UUID,
    link_id: uuid.UUID,
    accept: bool,
) -> DiscogsLink:
    """Accept/dismiss only a track in this recording's current explicit target projection."""
    pin = await read_recording_discogs_pin(session, media_id, observation_id, selected_token)
    await lock_recording_discogs_pin(session, pin)
    link = await session.scalar(select(DiscogsLink).where(DiscogsLink.id == link_id).execution_options(populate_existing=True))
    if link is None or link.source_observation_id != observation_id or link.source_track_position not in {t.position for t in pin.source.tracks}:
        raise ValueError("Discogs link does not belong to this selected recording track")
    if accept:
        await session.execute(
            update(DiscogsLink)
            .where(
                DiscogsLink.source_observation_id == observation_id,
                DiscogsLink.source_track_position == link.source_track_position,
                DiscogsLink.id != link.id,
                DiscogsLink.status == "accepted",
            )
            .values(status="dismissed")
        )
    link.status = "accepted" if accept else "dismissed"
    await session.flush()
    return link


async def accepted_recording_discogs_links(session: AsyncSession, selected: SelectedRecordingSource) -> dict[int, DiscogsLink]:
    """Accepted links are intrinsic observation+position identities, limited to mapped tracks."""
    if selected.availability != "current" or not selected.tracks:
        return {}
    links = (
        (
            await session.execute(
                select(DiscogsLink).where(
                    DiscogsLink.source_observation_id == selected.observation_id,
                    DiscogsLink.source_track_position.in_([track.position for track in selected.tracks]),
                    DiscogsLink.status == "accepted",
                )
            )
        )
        .scalars()
        .all()
    )
    return {link.source_track_position: link for link in links if link.source_track_position is not None}
