"""Bulk review of unambiguous CUE link candidates (phaze-2dxhd).

A CUE link candidate is a pending ``provider_recording_candidates`` row whose observation is a parsed
CUE tracklist for one recording. Reviewing one by hand (``/api/local-sources/decision``) asks the
operator to type the CUE ``FILE`` ordinal that belongs to the recording; this module proposes that
ordinal where it is not a judgement call, so a page of such candidates can be approved or rejected
in one operator action.

Nothing here selects anything by itself. :func:`load_cue_link_review` only CLASSIFIES; the only
writer is :func:`decide_cue_links`, which runs the ordinary per-candidate
:func:`phaze.services.local_source_import.decide_local_source` for every candidate the operator
checked -- so each approval or rejection is its own audited ``provider_selection_events`` row with
its own decision id, exactly as a single review would write.

**Unambiguous** (the implementer's reading of the bead's "reference-method links and single-FILE
cues"): the candidate's source is current, completely parsed and selectable; it is the ONLY current,
selectable CUE source for its recording; the recording has no selected tracklist yet; and its FILE
ordinal is determined -- the CUE has one FILE entry, or exactly one of its FILE entries names this
recording (same file name, else same stem with another extension). Everything else is EXCLUDED and
listed with its reason. Two reasons are AMBIGUITY, listed first and separately: more than one current
CUE FILE for one recording, and a FILE ordinal the CUE does not determine. Choosing between ambiguous
CUEs is not this module's job -- :mod:`phaze.services.cue_conflict_review` resolves them one recording
at a time (phaze-drote); this module only refuses to guess.

**One CUE file is one CUE** (phaze-drote). A file parsed by two parser versions (``local-tracklist-v1``
and ``-v4``) has two observations and two candidates for the same recording. Counting candidates made
every such recording "ambiguous"; the population is therefore grouped by source object (the CUE file),
the NEWEST parser version's candidate stands for the file, and an older parse of the same file and
revision is excluded as ``older_parse`` -- never proposed, rejected or selected here.

**Nothing unseen.** Every proposed row carries a review token: its candidate id plus a digest of the
exact values it was shown with (observation, revision, both content hashes, FILE ordinal). A decision
re-classifies the selected rows' recordings from the database and refuses the WHOLE batch, applying
nothing, if any selected row is no longer proposed or no longer matches its token.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
from pathlib import PurePosixPath
import re
from typing import TYPE_CHECKING, Any, Final, Literal
import unicodedata
import uuid

from sqlalchemy import and_, case, exists, literal_column, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased
import structlog

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderRecordingSelection
from phaze.schemas.local_source_import import SourceDecision
from phaze.services.companion_content import MEDIA_FILE_TYPES
from phaze.services.companion_details import Observation, Source, _observation_query, _summary
from phaze.services.local_source_import import decide_local_source
from phaze.services.selected_tag_sources import CurrentMediaBinding
from phaze.tracklist_providers.local_parser import PARSER_VERSIONS
from phaze.tracklist_providers.local_release import RELEASE_PARSER_VERSIONS


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

METHOD_ORDER: Final = ("reference", "stem", "folder", "twin", "close_name", "unknown")
"""Group order: the companion linking chain's own step order (``services/companion_linking.py``),
strongest evidence first. ``unknown`` collects links made before derivations were recorded."""

METHOD_LABELS: Final = {
    "reference": "The CUE names this recording",
    "stem": "Same file name as the recording",
    "folder": "Only media in the CUE's folder",
    "twin": "Release-folder twin",
    "close_name": "Close name match",
    "unknown": "Link method not recorded",
}

AMBIGUITY_REASONS: Final = ("multiple_cues", "file_ordinal")
"""Excluded as AMBIGUOUS -- resolved in the CUE conflicts queue, never by a bulk proposal."""

OTHER_REASONS: Final = ("not_current", "incomplete", "older_parse", "already_selected", "conflict")
"""Excluded because the candidate cannot be selected as shown, not because it is ambiguous."""

REASON_LABELS: Final = {
    "multiple_cues": "More than one current CUE file links to this recording",
    "file_ordinal": "The CUE has several FILE entries and does not say which one is this recording",
    "not_current": "The CUE source is not current for this recording",
    "incomplete": "The CUE was not completely parsed",
    # Stopgap: older-parser observations stay current until phaze-og7sn supersedes them at import (and
    # backfills the existing ones). Until then they are listed here, and left pending.
    "older_parse": "An older parse of a CUE file that a newer parser version also parsed",
    "already_selected": "This recording already has a selected tracklist",
    "conflict": "A conflicting source revision needs single review",
}

MAX_DECISIONS: Final = 200
"""Bound on one batch: a page is 50 rows; this leaves room without letting a request grow unbounded."""

_FILE_EVIDENCE = re.compile(r"^FILE:(\d+):(.*)$", re.S)

ReasonCode = Literal["multiple_cues", "file_ordinal", "not_current", "incomplete", "older_parse", "already_selected", "conflict"]

_TRACKLIST_PARSER_ORDER: Final = tuple(sorted(PARSER_VERSIONS, key=lambda version: int(version.rsplit("v", 1)[1])))
"""Local tracklist parser versions, oldest first (``local-tracklist-v1`` ... the current one)."""

TrackPrint = tuple[int, int, str, str | None]
"""One parsed track: (FILE ordinal, position, fingerprint of artist/title/exact offset, title)."""


@dataclass(frozen=True)
class CueLinkRow:
    """One pending CUE link candidate, as the operator sees it."""

    candidate_id: uuid.UUID
    media_id: uuid.UUID
    media_filename: str
    observation_id: uuid.UUID
    source_file_id: uuid.UUID | None
    source_filename: str | None
    method: str
    revision: str | None
    media_sha256: str
    source_sha256: str | None
    set_artist: str | None
    set_event: str | None
    file_names: tuple[tuple[int, str], ...]
    tracks_by_file: Mapping[int, tuple[int, int]]
    cue_file_ordinal: int | None = None
    ordinal_basis: Literal["single_file", "file_name", "file_stem"] | None = None
    reason: ReasonCode | None = None
    detail: str | None = None
    source_object_id: uuid.UUID | None = None
    tracks: tuple[TrackPrint, ...] = ()
    conflict: bool = False
    """The observation conflicts with other retained source data (``conflict_with_id``): selecting it
    needs the operator's explicit acknowledgement, exactly as single review asks for it."""

    def fingerprint(self, ordinal: int) -> tuple[str, ...]:
        """The tracks under FILE ``ordinal``, in order, as content fingerprints -- equal iff same tracks and offsets."""
        return tuple(print_ for file, _position, print_, _title in sorted(self.tracks, key=lambda t: t[1]) if file == ordinal)

    def titles(self, ordinal: int) -> tuple[str | None, ...]:
        return tuple(title for file, _position, _print, title in sorted(self.tracks, key=lambda t: t[1]) if file == ordinal)

    @property
    def track_count(self) -> int:
        return self.tracks_by_file.get(self.cue_file_ordinal, (0, 0))[0] if self.cue_file_ordinal else sum(t for t, _ in self.tracks_by_file.values())

    @property
    def timed_count(self) -> int:
        return self.tracks_by_file.get(self.cue_file_ordinal, (0, 0))[1] if self.cue_file_ordinal else sum(q for _, q in self.tracks_by_file.values())

    @property
    def file_name(self) -> str | None:
        return dict(self.file_names).get(self.cue_file_ordinal) if self.cue_file_ordinal else None

    @property
    def token(self) -> str:
        """``<candidate id>.<digest of every value the proposal was shown with>``."""
        shown = "\0".join(
            str(value) for value in (self.observation_id, self.revision, self.media_sha256, self.source_sha256, self.cue_file_ordinal, self.media_id)
        )
        return f"{self.candidate_id}.{hashlib.sha256(shown.encode()).hexdigest()[:32]}"


@dataclass
class CueLinkReview:
    """The classified population: proposals grouped by link method, exclusions by reason."""

    proposed: list[CueLinkRow] = field(default_factory=list)
    excluded: list[CueLinkRow] = field(default_factory=list)

    def proposed_counts(self) -> dict[str, int]:
        counts = Counter(row.method for row in self.proposed)
        return {method: counts[method] for method in METHOD_ORDER if counts[method]}

    def excluded_counts(self) -> dict[str, int]:
        counts = Counter(row.reason for row in self.excluded)
        return {reason: counts[reason] for reason in (*AMBIGUITY_REASONS, *OTHER_REASONS) if counts[reason]}


@dataclass(frozen=True)
class DecisionOutcome:
    action: Literal["select", "reject"]
    applied: tuple[CueLinkRow, ...] = ()
    stale: tuple[str, ...] = ()
    error: str | None = None


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _basename(name: str) -> str:
    """A CUE FILE value may carry a directory, in either separator style."""
    return PurePosixPath(name.replace("\\", "/")).name


def _ordinal_for(file_names: Sequence[tuple[int, str]], media_filename: str) -> tuple[int | None, str | None]:
    """The FILE ordinal this recording maps to, and the basis -- or ``(None, None)`` when undetermined."""
    ordinals = sorted({ordinal for ordinal, _name in file_names})
    if len(ordinals) == 1:
        return ordinals[0], "single_file"
    media = PurePosixPath(_key(media_filename))
    by_name = {ordinal for ordinal, name in file_names if _key(_basename(name)) == media.name}
    if len(by_name) == 1:
        return next(iter(by_name)), "file_name"
    if by_name:
        return None, None
    by_stem = {ordinal for ordinal, name in file_names if PurePosixPath(_key(_basename(name))).stem == media.stem}
    if len(by_stem) == 1:
        return next(iter(by_stem)), "file_stem"
    return None, None


def _file_shape(tracks: Any) -> tuple[tuple[tuple[int, str], ...], dict[int, tuple[int, int]], tuple[TrackPrint, ...]]:
    """FILE names, per-FILE (tracks, qualified offsets) and per-track prints from the stored track evidence.

    Prints come back empty unless ``tracks`` carries them (:func:`_track_prints`, conflicts only)."""
    names: dict[int, str] = {}
    counts: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    prints: list[TrackPrint] = []
    for track in tracks or ():
        evidence = track.get("file") if isinstance(track, dict) else None
        match = _FILE_EVIDENCE.match(evidence) if isinstance(evidence, str) else None
        if match is None:
            continue
        ordinal = int(match.group(1))
        names.setdefault(ordinal, match.group(2))
        counts[ordinal][0] += 1
        counts[ordinal][1] += track.get("q") == "qualified"
        if "h" not in track:
            continue
        position = track.get("p")
        title = track.get("n")
        prints.append((ordinal, position if isinstance(position, int) else 0, str(track.get("h")), title if isinstance(title, str) else None))
    return tuple(sorted(names.items())), {ordinal: (value[0], value[1]) for ordinal, value in counts.items()}, tuple(prints)


def _track_shape() -> Any:
    """Per track: the FILE evidence it was parsed under and its offset usability -- all classification needs.

    Fixed SQL over this row's own payload; nothing here is interpolated from input. The per-track PRINTS
    (position, a short title and a content fingerprint) cost about three times as much and only the conflicts
    queue reads them, so :func:`load_track_prints` fetches them for its candidates alone (phaze-2al7q.3).
    """
    return literal_column(
        "(SELECT jsonb_agg(jsonb_build_object("
        "'file', (SELECT e FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(t->'evidence') = 'array' THEN t->'evidence' ELSE '[]'::jsonb END) e"
        " WHERE left(e, 5) = 'FILE:' LIMIT 1),"
        " 'q', t->'timestamp'->>'offset_usability'))"
        " FROM jsonb_array_elements(CASE WHEN jsonb_typeof(provider_source_observations.payload->'tracks') = 'array'"
        " THEN provider_source_observations.payload->'tracks' ELSE '[]'::jsonb END) t)",
        JSONB,
    )


def _track_prints() -> Any:
    """Per track: FILE evidence, position, a short title and a fingerprint of what a consumer reads (artist, title,
    exact offset, usability) -- a few bytes per track instead of the whole stored payload."""
    return literal_column(
        "(SELECT jsonb_agg(jsonb_build_object("
        "'file', (SELECT e FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(t->'evidence') = 'array' THEN t->'evidence' ELSE '[]'::jsonb END) e"
        " WHERE left(e, 5) = 'FILE:' LIMIT 1),"
        " 'p', t->'position',"
        " 'n', left(t->>'title', 200),"
        " 'h', md5(jsonb_build_object('a', t->'artist', 't', t->'title', 'o', t->'timestamp'->'offset',"
        " 'u', t->'timestamp'->'offset_usability')::text)))"
        " FROM jsonb_array_elements(CASE WHEN jsonb_typeof(provider_source_observations.payload->'tracks') = 'array'"
        " THEN provider_source_observations.payload->'tracks' ELSE '[]'::jsonb END) t)",
        JSONB,
    )


def _population_stmt(media_ids: Iterable[uuid.UUID] | None) -> Any:
    # PARTIAL MITIGATION, still O(population) (phaze-2al7q.3, finding 4): every request still reads and
    # classifies every non-rejected local CUE candidate. Only the per-track prints left this query. The
    # bounded version -- the shape stored at import, and SQL keyset paging -- is phaze-m16w5.
    source_file = aliased(FileRecord)
    # Aliased: the shared observation query's own FileCompanion subqueries would otherwise auto-correlate
    # to a plain outer FileCompanion join and lose their FROM clause.
    link = aliased(FileCompanion)
    track_shape = _track_shape()
    selected = (
        select(ProviderRecordingSelection.observation_id)
        .where(ProviderRecordingSelection.media_id == FileRecord.id, ProviderRecordingSelection.kind == "tracklist")
        .correlate(FileRecord)
        .scalar_subquery()
    )
    # Another candidate of this recording carries a NEWER local parse of the same file and revision --
    # whatever that candidate's status, so rejecting the newer parse never resurrects the older one.
    newer = aliased(Observation)
    newer_candidate = aliased(ProviderRecordingCandidate)
    order = {version: index for index, version in enumerate(_TRACKLIST_PARSER_ORDER)}
    newer_parse = exists(
        select(newer_candidate.id)
        .join(newer, newer.id == newer_candidate.observation_id)
        .where(
            newer_candidate.media_id == FileRecord.id,
            newer.object_id == Observation.object_id,
            newer.revision == Observation.revision,
            newer.id != Observation.id,
            newer.parser_version.in_(_TRACKLIST_PARSER_ORDER),
            case(order, value=newer.parser_version, else_=-1) > case(order, value=Observation.parser_version, else_=-1),
        )
        .correlate(Observation, FileRecord)
    )
    stmt = (
        _observation_query(FileRecord)
        .add_columns(
            FileRecord.id.label("media_id"),
            FileRecord.original_filename.label("media_filename"),
            FileRecord.agent_id.label("media_agent_id"),
            FileRecord.sha256_hash.label("media_sha256"),
            FileRecord.file_type.label("media_file_type"),
            FileRecord.missing_at.label("media_missing_at"),
            ProviderRecordingCandidate.id.label("candidate_id"),
            ProviderRecordingCandidate.status.label("candidate_row_status"),
            link.derivation_method.label("derivation_method"),
            source_file.original_filename.label("source_filename"),
            selected.label("selected_observation_id"),
            Observation.payload["artist"]["value"].astext.label("set_artist"),
            Observation.payload["event"]["value"].astext.label("set_event"),
            track_shape.label("track_shape"),
            newer_parse.label("newer_parse"),
        )
        .join(ProviderRecordingCandidate, ProviderRecordingCandidate.observation_id == Observation.id)
        .join(FileRecord, FileRecord.id == ProviderRecordingCandidate.media_id)
        .outerjoin(source_file, source_file.id == Source.source_file_id)
        .outerjoin(link, and_(link.companion_id == Source.source_file_id, link.media_id == FileRecord.id))
        .where(
            Observation.payload["source_format"].astext == "cue",
            Observation.parser_version.notin_(RELEASE_PARSER_VERSIONS),
            ProviderRecordingCandidate.status != "rejected",
        )
        .order_by(FileRecord.original_filename, ProviderRecordingCandidate.id)
    )
    if media_ids is not None:
        stmt = stmt.where(FileRecord.id.in_(list(media_ids)))
    return stmt


@dataclass(frozen=True)
class Built:
    """One population row with the facts classification needs (shared with the conflicts queue)."""

    cue: CueLinkRow
    availability: str
    selectable: bool
    pending: bool
    older_parse: bool
    conflict: bool
    parser_version: str
    selected_observation_id: uuid.UUID | None

    @property
    def stands_for_its_file(self) -> bool:
        """Current, selectable, and the newest parse of its CUE file: this file's candidate."""
        return self.availability == "current" and self.selectable and not self.older_parse


def build_rows(rows: Sequence[Mapping[Any, Any]]) -> list[Built]:
    built: list[Built] = []
    for row in rows:
        media = CurrentMediaBinding(row["media_id"], row["media_agent_id"], row["media_sha256"], row["media_file_type"], row["media_missing_at"])
        availability = _summary(row, media, ()).availability
        if media.missing_at is not None:
            availability = "missing"
        elif media.file_type not in MEDIA_FILE_TYPES:
            availability = "unavailable"
        file_names, tracks_by_file, tracks = _file_shape(row["track_shape"])
        selectable = row["status"] == "found" and row["completeness_state"] == "complete" and bool(row["track_count"]) and bool(file_names)
        method = row["derivation_method"] if row["derivation_method"] in METHOD_ORDER else "unknown"
        cue = CueLinkRow(
            candidate_id=row["candidate_id"],
            media_id=row["media_id"],
            media_filename=row["media_filename"],
            observation_id=row["observation_id"],
            source_file_id=row["source_file_id"],
            source_filename=row["source_filename"],
            method=method,
            revision=row["revision"],
            media_sha256=row["media_sha256"],
            source_sha256=row["sha256_hash"],
            set_artist=row["set_artist"],
            set_event=row["set_event"],
            file_names=file_names,
            tracks_by_file=tracks_by_file,
            source_object_id=row["source_object_id"],
            tracks=tracks,
            conflict=row["conflict_with_id"] is not None,
        )
        built.append(
            Built(
                cue=cue,
                availability=availability,
                selectable=selectable,
                pending=row["candidate_row_status"] == "pending",
                older_parse=bool(row["newer_parse"]),
                conflict=cue.conflict,
                parser_version=row["parser_version"],
                selected_observation_id=row["selected_observation_id"],
            )
        )
    return built


def cue_files(built: Sequence[Built]) -> dict[uuid.UUID, set[uuid.UUID | None]]:
    """Recording -> the CUE files (source objects) that currently stand for a CUE of it."""
    files: dict[uuid.UUID, set[uuid.UUID | None]] = defaultdict(set)
    for item in built:
        if item.stands_for_its_file:
            files[item.cue.media_id].add(item.cue.source_object_id)
    return files


def _classify(rows: Sequence[Mapping[Any, Any]]) -> CueLinkReview:
    built = build_rows(rows)
    files = cue_files(built)
    review = CueLinkReview()
    for item in built:
        cue = item.cue
        if not item.pending:
            continue
        reason: ReasonCode | None = None
        detail: str | None = None
        ordinal: int | None = None
        basis: str | None = None
        if item.availability != "current":
            reason, detail = "not_current", item.availability
        elif not item.selectable:
            reason = "incomplete"
        elif item.older_parse:
            reason, detail = "older_parse", item.parser_version
        elif item.selected_observation_id is not None:
            reason = "already_selected"
        elif len(files[cue.media_id]) > 1:
            reason, detail = "multiple_cues", f"{len(files[cue.media_id])} current CUE files"
        else:
            ordinal, basis = _ordinal_for(cue.file_names, cue.media_filename)
            if ordinal is None:
                reason, detail = "file_ordinal", f"{len(cue.file_names)} FILE entries"
            elif item.conflict:
                reason = "conflict"
        if reason is None:
            review.proposed.append(_replace(cue, cue_file_ordinal=ordinal, ordinal_basis=basis))
        else:
            review.excluded.append(_replace(cue, reason=reason, detail=detail))
    review.proposed.sort(key=lambda row: (METHOD_ORDER.index(row.method), _key(row.media_filename), str(row.candidate_id)))
    order = (*AMBIGUITY_REASONS, *OTHER_REASONS)
    review.excluded.sort(key=lambda row: (order.index(row.reason or ""), _key(row.media_filename), str(row.candidate_id)))
    return review


def _replace(row: CueLinkRow, **changes: Any) -> CueLinkRow:
    return CueLinkRow(**{**row.__dict__, **changes})


async def load_population(session: AsyncSession, *, media_ids: Iterable[uuid.UUID] | None = None) -> Sequence[Mapping[Any, Any]]:
    """Every non-rejected local CUE tracklist candidate (or only those of ``media_ids``). Reads only."""
    return (await session.execute(_population_stmt(media_ids))).mappings().all()


async def load_track_prints(session: AsyncSession, candidate_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, tuple[TrackPrint, ...]]:
    """``candidate id -> its tracks' prints``, for exactly the candidates asked for. Reads only."""
    wanted = list(candidate_ids)
    if not wanted:
        return {}
    rows = await session.execute(
        select(ProviderRecordingCandidate.id, _track_prints())
        .join(Observation, Observation.id == ProviderRecordingCandidate.observation_id)
        .where(ProviderRecordingCandidate.id.in_(wanted))
    )
    return {candidate_id: _file_shape(prints)[2] for candidate_id, prints in rows.all()}


async def load_cue_link_review(session: AsyncSession, *, media_ids: Iterable[uuid.UUID] | None = None) -> CueLinkReview:
    """Classify every pending CUE link candidate (or only those of ``media_ids``). Reads only."""
    return _classify(await load_population(session, media_ids=media_ids))


def parse_tokens(tokens: Sequence[str]) -> dict[uuid.UUID, str]:
    """``candidate id -> token``; a malformed token is a ``ValueError``, never a silent skip."""
    if len(tokens) > MAX_DECISIONS:
        raise ValueError(f"At most {MAX_DECISIONS} candidates can be decided at once")
    parsed: dict[uuid.UUID, str] = {}
    for token in tokens:
        candidate, _, digest = token.partition(".")
        if not re.fullmatch(r"[0-9a-f]{32}", digest):
            raise ValueError("Malformed review token")
        parsed[uuid.UUID(candidate)] = token
    return parsed


async def decide_cue_links(session: AsyncSession, tokens: Sequence[str], action: Literal["select", "reject"], *, actor: str) -> DecisionOutcome:
    """Apply ``action`` to every checked proposal -- all of them, or none if any changed since shown.

    Each candidate goes through :func:`decide_local_source` with a fresh decision id, so the audit is
    the same ``provider_selection_events`` row a single review writes. The caller owns the commit.
    """
    wanted = parse_tokens(tokens)
    if not wanted:
        return DecisionOutcome(action=action, error="Select at least one candidate first.")
    media_ids = (await session.scalars(select(ProviderRecordingCandidate.media_id).where(ProviderRecordingCandidate.id.in_(list(wanted))))).all()
    current = {row.candidate_id: row for row in (await load_cue_link_review(session, media_ids=set(media_ids))).proposed}
    stale = tuple(token for candidate_id, token in wanted.items() if candidate_id not in current or current[candidate_id].token != token)
    if stale:
        return DecisionOutcome(action=action, stale=stale)
    rows = tuple(current[candidate_id] for candidate_id in wanted)
    try:
        async with session.begin_nested():
            for row in rows:
                await decide_local_source(
                    session,
                    SourceDecision(
                        media_id=row.media_id,
                        observation_id=row.observation_id,
                        kind="tracklist",
                        decision_id=uuid.uuid4(),
                        expected_selection_token=None,
                        expected_revision=row.revision,
                        expected_source_sha256=str(row.source_sha256),
                        expected_media_sha256=row.media_sha256,
                        action=action,
                        cue_file_ordinal=row.cue_file_ordinal if action == "select" else None,
                    ),
                    actor=actor,
                )
    except ValueError as exc:
        logger.info("cue_link_bulk_decision_refused", action=action, count=len(rows), reason=str(exc))
        return DecisionOutcome(action=action, error=str(exc))
    logger.info("cue_link_bulk_decision", action=action, count=len(rows), candidate_ids=[str(row.candidate_id) for row in rows])
    return DecisionOutcome(action=action, applied=rows)


__all__ = [
    "AMBIGUITY_REASONS",
    "METHOD_LABELS",
    "METHOD_ORDER",
    "OTHER_REASONS",
    "REASON_LABELS",
    "Built",
    "CueLinkReview",
    "CueLinkRow",
    "DecisionOutcome",
    "TrackPrint",
    "build_rows",
    "cue_files",
    "decide_cue_links",
    "load_cue_link_review",
    "load_population",
    "load_track_prints",
    "parse_tokens",
]
