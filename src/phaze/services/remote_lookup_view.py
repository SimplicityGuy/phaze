"""Read model for the recording page's remote look-up panel: what is STORED, shaped for the operator to review.

Pure reads. Building the panel never issues a lookup, a load or any provider call: candidates, loaded versions and
rejections come from the tables ``services.remote_lookup`` wrote, and the provider list from the ledger. Provider
names are the descriptor's ``display_name`` (carried by :class:`ProviderStatus`); nothing here knows a provider id.

Evidence is shown as recorded. The matcher's assessment is parsed back out of the candidate's evidence strings
(``services.remote_lookup._assessment_evidence`` writes them) and is never turned into a score or a verdict:
eligibility stays ``review_required``, a contradiction is shown as a contradiction, and an incomplete scope is
stated as "not evidence that no tracklist exists".
"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
import uuid

from pydantic import ValidationError
from sqlalchemy import func, select

from phaze.models.file import FileRecord
from phaze.models.provider_source import (
    ProviderRecordingCandidate,
    ProviderRecordingSelection,
    ProviderSelectionEvent,
    ProviderSourceObject,
    ProviderSourceObservation,
)
from phaze.services.local_source_import import selection_token
from phaze.services.provider_persistence import DISCOVERED, source_availability
from phaze.tracklist_providers.domain import ProviderTrack, Snapshot


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.schemas.remote_lookup import ProviderStatus

MAX_GROUPS = 50
MAX_TRACKS_SHOWN = 200
_ROWS = 300

# What each recorded relation means to the operator. Only the words differ: nothing is scored or thresholded.
_RELATION_TEXT = {
    "exact": "matches exactly",
    "normalized": "matches after normalising case and spacing",
    "fuzzy": "similar spelling (fuzzy match)",
    "missing": "not stated by one side",
    "uncertain": "uncertain",
    "contradictory": "CONFLICTS with this recording",
    "compatible_alternative": "matches one of the recording's possible dates",
    "kind_mismatch": "same date but a different kind of date (for example broadcast rather than performance)",
}
_CONFLICTING_RELATIONS = frozenset({"contradictory", "kind_mismatch"})
_BAND_TEXT = {
    "corroborated": "Corroborated: several independent facts agree",
    "plausible": "Plausible: some facts agree, none conflict",
    "insufficient": "Insufficient: too little to compare",
    "conflicting": "Conflicting: at least one fact disagrees",
}


@dataclass(frozen=True)
class FieldView:
    label: str
    relation: str
    text: str
    conflicting: bool


@dataclass(frozen=True)
class AssessmentView:
    band: str
    band_text: str
    fields: tuple[FieldView, ...]
    date_alternatives: tuple[str, ...]
    scope_state: str | None
    scope_reason: str | None
    tied_with: int
    reasons: tuple[str, ...]
    conflicting: bool


@dataclass(frozen=True)
class TrackView:
    position: int
    artist: str | None
    title: str | None
    timing: str | None
    timing_approximate: bool
    unresolved: bool
    section_ordinal: int | None = None


@dataclass(frozen=True)
class SectionView:
    """One section of a sectioned snapshot, a candidate for the recording the operator must pick explicitly."""

    ordinal: int
    title: str | None
    track_count: int
    complete: bool
    warnings: tuple[str, ...]
    selectable: bool


@dataclass(frozen=True)
class VersionView:
    observation_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_status: str
    status: str
    revision: str | None
    retrieved_at: datetime
    url: str | None
    completeness_state: str | None
    completeness_reason: str | None
    tracks: tuple[TrackView, ...]
    track_count: int
    rights: tuple[str, ...]
    assessment: AssessmentView | None
    conflicting_source: bool
    selected: bool
    can_select: bool
    can_reject: bool
    unreadable: bool
    decision_id: uuid.UUID
    sections: tuple[SectionView, ...] = ()
    section_scan_complete: bool = False
    # True when the snapshot has several sections: the operator must choose which one belongs to this recording.
    section_choice_required: bool = False
    sections_have_warnings: bool = False


@dataclass(frozen=True)
class CandidateGroup:
    """One remote provider object for this recording: what discovery said, and every version loaded from it."""

    provider_id: str
    provider_name: str
    native_id: str
    display_name: str | None
    url: str | None
    discovered_observation_id: uuid.UUID | None
    discovered_candidate_id: uuid.UUID | None
    discovered_revision: str | None
    assessment: AssessmentView | None
    versions: tuple[VersionView, ...]
    rejected: bool
    rejected_at: datetime | None
    # The rejected candidate row a Reopen decision names (any one of the object's rejected rows lifts them all).
    reopen_observation_id: uuid.UUID | None
    reopen_revision: str | None
    can_load: bool
    can_reject_discovered: bool
    decision_id: uuid.UUID


@dataclass(frozen=True)
class RemotePanel:
    media_id: uuid.UUID
    media_sha256: str
    expected_selection_token: str
    providers: tuple["ProviderStatus", ...]
    groups: tuple[CandidateGroup, ...]
    rejected: tuple[CandidateGroup, ...]
    configured: bool = True


def safe_attribution_url(url: str | None) -> str | None:
    """Only an http(s) URL with a host becomes a link: a stored value must never become a ``javascript:`` href."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme in {"http", "https"} and parts.netloc else None


def parse_assessment(evidence: list[str] | tuple[str, ...]) -> AssessmentView | None:
    """The matcher's recorded assessment, back from the candidate's evidence strings. None if there is none."""
    values: dict[str, str] = {}
    alternatives: list[str] = []
    reasons: list[str] = []
    for item in evidence:
        if not item.startswith("assessment:"):
            continue
        _, _, rest = item.partition(":")
        key, _, value = rest.partition(":")
        if key == "date_alternative":
            alternatives.append(value)
        elif key == "reason":
            reasons.append(value)
        else:
            values.setdefault(key, value)
    band = values.get("band")
    if band is None:
        return None
    date_relation, _, date_precision = values.get("date", "missing:unknown").partition(":")
    fields = [
        FieldView(label, relation, _RELATION_TEXT.get(relation, relation), relation in _CONFLICTING_RELATIONS)
        for label, relation in (("Artist", values.get("artist", "missing")), ("Event", values.get("event", "missing")))
    ]
    date_text = _RELATION_TEXT.get(date_relation, date_relation)
    if date_precision and date_precision != "unknown":
        date_text = f"{date_text} (to the {date_precision})"
    fields.append(FieldView("Date", date_relation, date_text, date_relation in _CONFLICTING_RELATIONS))
    scope_state, _, scope_reason = values.get("scope", "").partition(":")
    try:
        tied = int(values.get("tied_with", "0"))
    except ValueError:
        tied = 0
    return AssessmentView(
        band=band,
        band_text=_BAND_TEXT.get(band, band),
        fields=tuple(fields),
        date_alternatives=tuple(alternatives),
        scope_state=scope_state or None,
        scope_reason=scope_reason or None,
        tied_with=tied,
        reasons=tuple(reasons),
        conflicting=band == "conflicting" or any(field.conflicting for field in fields),
    )


def _track_view(track: ProviderTrack) -> TrackView:
    timing: str | None = None
    approximate = False
    if track.timestamp is not None:
        stamp = track.timestamp
        if stamp.offset_usability == "qualified":
            timing = stamp.original
        elif stamp.offset_usability == "approximate":
            timing, approximate = stamp.original, True
        else:
            timing = f"no usable offset (the source says {stamp.original!r})"
    return TrackView(
        position=track.position,
        artist=track.artist,
        title=track.title,
        timing=timing,
        timing_approximate=approximate,
        unresolved=not (track.artist and track.artist.strip()) and not (track.title and track.title.strip()),
        section_ordinal=track.section_ordinal,
    )


async def _version_view(
    session: "AsyncSession",
    candidate: ProviderRecordingCandidate,
    observation: ProviderSourceObservation,
    *,
    media_id: uuid.UUID,
    selected_observation_id: uuid.UUID | None,
) -> VersionView:
    selected = selected_observation_id == observation.id
    tracks: tuple[TrackView, ...] = ()
    track_count = 0
    rights: tuple[str, ...] = ()
    state: str | None = None
    reason: str | None = None
    unreadable = False
    snapshot: Snapshot | None = None
    try:
        snapshot = Snapshot.model_validate({**observation.payload, "retrieved_at": observation.retrieved_at, "url": observation.source_url})
    except ValidationError:
        unreadable = True
    if snapshot is not None:
        tracks = tuple(_track_view(track) for track in snapshot.tracks[:MAX_TRACKS_SHOWN])
        track_count = len(snapshot.tracks)
        rights = tuple(snapshot.rights_evidence)
        state, reason = snapshot.completeness.state, snapshot.completeness.reason
    availability = await source_availability(session, observation, media_id)
    sections: tuple[SectionView, ...] = ()
    if snapshot is not None and snapshot.sections:
        per_section: dict[int, int] = {}
        for track in snapshot.tracks:
            if track.section_ordinal is not None:
                per_section[track.section_ordinal] = per_section.get(track.section_ordinal, 0) + 1
        sections = tuple(
            SectionView(
                ordinal=section.ordinal,
                title=section.title,
                track_count=per_section.get(section.ordinal, 0),
                complete=section.completeness.state == "complete",
                warnings=tuple(section.warnings),
                selectable=section.completeness.state == "complete" and per_section.get(section.ordinal, 0) > 0,
            )
            for section in snapshot.sections
        )
    if sections:
        # A sectioned snapshot is selectable through ONE complete section of a fully enumerated scan.
        complete = (
            snapshot is not None and snapshot.section_scan_complete and any(item.selectable for item in sections) and observation.status == "found"
        )
    else:
        complete = snapshot is not None and state == "complete" and track_count > 0 and observation.status == "found"
    return VersionView(
        observation_id=observation.id,
        candidate_id=candidate.id,
        candidate_status=candidate.status,
        status=observation.status,
        revision=observation.revision,
        retrieved_at=observation.retrieved_at,
        url=safe_attribution_url(observation.source_url),
        completeness_state=state,
        completeness_reason=reason,
        tracks=tracks,
        track_count=track_count,
        rights=rights,
        assessment=parse_assessment(candidate.evidence),
        conflicting_source=observation.conflict_with_id is not None,
        selected=selected,
        can_select=complete and candidate.status == "pending" and availability == "current" and not selected,
        can_reject=candidate.status in {"pending", "accepted"} and not selected,
        unreadable=unreadable,
        decision_id=uuid.uuid4(),
        sections=sections,
        section_scan_complete=snapshot.section_scan_complete if snapshot is not None else False,
        section_choice_required=len(sections) > 1,
        sections_have_warnings=any(item.warnings for item in sections),
    )


async def build_remote_panel(
    session: "AsyncSession", providers: tuple["ProviderStatus", ...], media_id: uuid.UUID, *, configured: bool = True
) -> RemotePanel | None:
    """Everything the panel shows for one recording, or None when the recording does not exist."""
    media = await session.get(FileRecord, media_id)
    if media is None:
        return None
    selection = await session.scalar(
        select(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media_id, ProviderRecordingSelection.kind == "tracklist")
    )
    token = str(selection_token(selection)) if selection is not None else ""
    selected_observation_id = selection.observation_id if selection is not None else None
    names = {status.provider_id: status.display_name for status in providers}
    rows = (
        await session.execute(
            select(ProviderRecordingCandidate, ProviderSourceObservation, ProviderSourceObject)
            .join(ProviderSourceObservation, ProviderSourceObservation.id == ProviderRecordingCandidate.observation_id)
            .join(ProviderSourceObject, ProviderSourceObject.id == ProviderSourceObservation.object_id)
            .where(ProviderRecordingCandidate.media_id == media_id, ProviderSourceObject.channel == "remote")
            .order_by(ProviderRecordingCandidate.created_at.desc(), ProviderRecordingCandidate.id)
            .limit(_ROWS)
        )
    ).all()
    by_object: dict[uuid.UUID, list[tuple[ProviderRecordingCandidate, ProviderSourceObservation, ProviderSourceObject]]] = {}
    for candidate, observation, source in rows:
        by_object.setdefault(source.id, []).append((candidate, observation, source))

    rejected_at: dict[uuid.UUID, datetime | None] = {}
    rejected_observations: list[uuid.UUID] = []
    for group_rows in by_object.values():
        rejected_observations += [observation.id for candidate, observation, _ in group_rows if candidate.status == "rejected"]
    if rejected_observations:
        events = await session.execute(
            select(ProviderSelectionEvent.observation_id, func.max(ProviderSelectionEvent.selected_at))
            .where(
                ProviderSelectionEvent.media_id == media_id,
                ProviderSelectionEvent.action == "reject",
                ProviderSelectionEvent.observation_id.in_(rejected_observations),
            )
            .group_by(ProviderSelectionEvent.observation_id)
        )
        rejected_at = dict(events.tuples().all())

    groups: list[CandidateGroup] = []
    for group_rows in by_object.values():
        source = group_rows[0][2]
        discovered = [(candidate, observation) for candidate, observation, _ in group_rows if observation.status == DISCOVERED]
        versions = [(candidate, observation) for candidate, observation, _ in group_rows if observation.status != DISCOVERED]
        rejected_rows = [(candidate, observation) for candidate, observation, _ in group_rows if candidate.status == "rejected"]
        head_candidate, head_observation = discovered[0] if discovered else (None, None)
        payload = head_observation.payload.get("discovered", {}) if head_observation is not None else {}
        version_views = tuple(
            [
                await _version_view(session, candidate, observation, media_id=media_id, selected_observation_id=selected_observation_id)
                for candidate, observation in versions
            ]
        )
        rejected = bool(rejected_rows)
        url = safe_attribution_url(head_observation.source_url if head_observation is not None else None) or next(
            (version.url for version in version_views if version.url), None
        )
        reopen = rejected_rows[0] if rejected_rows else None
        rejection_times = [when for _, observation in rejected_rows if (when := rejected_at.get(observation.id)) is not None]
        groups.append(
            CandidateGroup(
                provider_id=source.provider_id,
                provider_name=names.get(source.provider_id, source.provider_id),
                native_id=source.native_id,
                display_name=payload.get("display_name") if isinstance(payload, dict) else None,
                url=url,
                discovered_observation_id=head_observation.id if head_observation is not None else None,
                discovered_candidate_id=head_candidate.id if head_candidate is not None else None,
                discovered_revision=head_observation.revision if head_observation is not None else None,
                assessment=parse_assessment(head_candidate.evidence) if head_candidate is not None else None,
                versions=version_views,
                rejected=rejected,
                rejected_at=max(rejection_times, default=None),
                reopen_observation_id=reopen[1].id if reopen else None,
                reopen_revision=reopen[1].revision if reopen else None,
                can_load=head_candidate is not None and not rejected,
                can_reject_discovered=head_candidate is not None and not rejected and not version_views,
                decision_id=uuid.uuid4(),
            )
        )
    active = tuple(group for group in groups if not group.rejected)[:MAX_GROUPS]
    return RemotePanel(
        media_id=media_id,
        media_sha256=media.sha256_hash or "",
        expected_selection_token=token,
        providers=providers,
        groups=active,
        rejected=tuple(group for group in groups if group.rejected)[:MAX_GROUPS],
        configured=configured,
    )
