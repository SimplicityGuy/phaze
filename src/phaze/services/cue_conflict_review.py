"""Resolve recordings whose CUE is ambiguous, one recording at a time (phaze-drote).

The bulk CUE link review (:mod:`phaze.services.cue_link_review`) refuses two kinds of candidate as
AMBIGUOUS: a recording with more than one current CUE file, and a CUE whose FILE entries do not say
which one is the recording. This module turns each such recording into one card the operator decides.

**What is proposed** (operator decision, 2026-11-11, phaze-drote): when every CUE file of a recording
parses to the same tracks and exact offsets, the choice cannot change what a consumer reads, so the
strongest link is preselected -- link method in the companion chain's order (reference, stem, folder,
twin, close name), then CUE file name. When the CUEs differ, nothing is preselected and the positions
that differ are shown. A FILE entry the CUE does not determine is always the operator's pick. Nothing
is ever applied without the operator pressing the card's button.

**What confirming does** (operator decision, 2026-11-11, phaze-drote): in ONE transaction the chosen CUE is selected through
:func:`~phaze.services.local_source_import.decide_local_source` and every other CUE file shown on the
card is rejected through the same function -- one audited ``provider_selection_events`` row each, every
rejection naming the selected observation in ``replaced_by_observation_id``. Older parses of a file
(``older_parse``, a stopgap until phaze-og7sn) are never shown here, selected or rejected.

**Nothing unseen.** A card carries a token over every candidate it showed; a confirmation for a card
whose candidates changed (a CUE appeared, went stale, or was decided elsewhere) applies nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import zip_longest
from typing import TYPE_CHECKING, Final, Literal
import uuid

import structlog

from phaze.schemas.local_source_import import SourceDecision
from phaze.services.cue_link_review import METHOD_ORDER, CueLinkRow, _key, _ordinal_for, _replace, build_rows, cue_files, load_population
from phaze.services.local_source_import import decide_local_source


if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

ConflictReason = Literal["multiple_cues", "file_ordinal"]
CONFLICT_ORDER: Final = ("multiple_cues", "file_ordinal")


@dataclass(frozen=True)
class CueConflict:
    """One recording whose CUE needs the operator: its candidate CUE files, compared."""

    media_id: uuid.UUID
    media_filename: str
    reason: ConflictReason
    candidates: tuple[CueLinkRow, ...]
    """One per CUE file (its newest parse), with ``cue_file_ordinal`` set where the CUE determines it."""
    identical: bool
    """Every candidate's FILE entry is determined and they parse to the same tracks and exact offsets."""
    differing: tuple[int, ...] = ()
    """1-based track numbers (within the recording) where determined candidates disagree."""
    proposed_id: uuid.UUID | None = None

    @property
    def token(self) -> str:
        shown = "\0".join((str(self.media_id), *sorted(candidate.token for candidate in self.candidates)))
        return hashlib.sha256(shown.encode()).hexdigest()[:32]

    @property
    def undetermined(self) -> tuple[CueLinkRow, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.cue_file_ordinal is None)


@dataclass(frozen=True)
class ConflictOutcome:
    conflict: CueConflict | None = None
    winner: CueLinkRow | None = None
    rejected: tuple[CueLinkRow, ...] = ()
    stale: bool = False
    error: str | None = None

    @property
    def applied(self) -> bool:
        return self.winner is not None


def _strongest(candidates: Iterable[CueLinkRow]) -> CueLinkRow:
    return min(candidates, key=lambda row: (METHOD_ORDER.index(row.method), _key(row.source_filename or ""), str(row.candidate_id)))


def _compare(candidates: tuple[CueLinkRow, ...]) -> tuple[bool, tuple[int, ...]]:
    """(identical, differing track numbers). Undetermined FILE entries cannot be compared: neither."""
    if len(candidates) < 2 or any(candidate.cue_file_ordinal is None for candidate in candidates):
        return False, ()
    prints = [candidate.fingerprint(candidate.cue_file_ordinal or 0) for candidate in candidates]
    if len(set(prints)) == 1:
        return True, ()
    differing = tuple(number for number, column in enumerate(zip_longest(*prints), start=1) if len(set(column)) > 1)
    return False, differing


async def load_cue_conflicts(session: AsyncSession, *, media_ids: Iterable[uuid.UUID] | None = None) -> list[CueConflict]:
    """Every recording whose CUE is ambiguous and still undecided (or only those of ``media_ids``). Reads only.

    A recording is listed when it has no selected tracklist, at least one of its CUE candidates is
    pending, and either more than one current CUE file stands for it or its only one does not
    determine the FILE entry.
    """
    built = build_rows(await load_population(session, media_ids=media_ids))
    files = cue_files(built)
    by_media: dict[uuid.UUID, list[CueLinkRow]] = {}
    pending: set[uuid.UUID] = set()
    selected: set[uuid.UUID] = set()
    for item in built:
        if item.selected_observation_id is not None:
            selected.add(item.cue.media_id)
        if item.stands_for_its_file:
            ordinal, basis = _ordinal_for(item.cue.file_names, item.cue.media_filename)
            by_media.setdefault(item.cue.media_id, []).append(_replace(item.cue, cue_file_ordinal=ordinal, ordinal_basis=basis))
            if item.pending:
                pending.add(item.cue.media_id)
    conflicts: list[CueConflict] = []
    for media_id, rows in by_media.items():
        if media_id in selected or media_id not in pending:
            continue
        candidates = tuple(sorted(rows, key=lambda row: (METHOD_ORDER.index(row.method), _key(row.source_filename or ""), str(row.candidate_id))))
        if len(files[media_id]) > 1:
            reason: ConflictReason = "multiple_cues"
        elif candidates[0].cue_file_ordinal is None:
            reason = "file_ordinal"
        else:
            continue
        identical, differing = _compare(candidates)
        conflicts.append(
            CueConflict(
                media_id=media_id,
                media_filename=candidates[0].media_filename,
                reason=reason,
                candidates=candidates,
                identical=identical,
                differing=differing,
                proposed_id=_strongest(candidates).candidate_id if identical else None,
            )
        )
    conflicts.sort(key=lambda conflict: (CONFLICT_ORDER.index(conflict.reason), _key(conflict.media_filename), str(conflict.media_id)))
    return conflicts


def _decision(row: CueLinkRow, **changes: object) -> SourceDecision:
    return SourceDecision.model_validate(
        {
            "media_id": row.media_id,
            "observation_id": row.observation_id,
            "kind": "tracklist",
            "decision_id": uuid.uuid4(),
            "expected_revision": row.revision,
            "expected_source_sha256": str(row.source_sha256),
            "expected_media_sha256": row.media_sha256,
            **changes,
        }
    )


async def resolve_cue_conflict(
    session: AsyncSession, *, media_id: uuid.UUID, card_token: str, winner_token: str, cue_file_ordinal: int | None, actor: str
) -> ConflictOutcome:
    """Select the operator's CUE for one recording and reject its other CUE files, or apply nothing.

    ``cue_file_ordinal`` is required only when the chosen CUE does not determine its FILE entry, and must
    then be one of that CUE's FILE entries; a determined entry cannot be overridden here. The caller owns
    the commit.
    """
    conflict = next(iter(await load_cue_conflicts(session, media_ids={media_id})), None)
    if conflict is None or conflict.token != card_token:
        return ConflictOutcome(conflict=conflict, stale=True)
    winner = next((candidate for candidate in conflict.candidates if candidate.token == winner_token), None)
    if winner is None:
        return ConflictOutcome(conflict=conflict, stale=True)
    if winner.cue_file_ordinal is not None:
        if cue_file_ordinal not in {None, winner.cue_file_ordinal}:
            return ConflictOutcome(conflict=conflict, error="That CUE already names its FILE entry for this recording.")
        ordinal = winner.cue_file_ordinal
    elif cue_file_ordinal in {number for number, _name in winner.file_names}:
        ordinal = cue_file_ordinal
    else:
        return ConflictOutcome(conflict=conflict, error="Choose which FILE entry of the chosen CUE is this recording.")
    siblings = tuple(candidate for candidate in conflict.candidates if candidate.candidate_id != winner.candidate_id)
    selection = uuid.uuid4()
    try:
        async with session.begin_nested():
            await decide_local_source(session, _decision(winner, decision_id=selection, action="select", cue_file_ordinal=ordinal), actor=actor)
            for sibling in siblings:
                await decide_local_source(
                    session,
                    _decision(sibling, action="reject", expected_selection_token=selection, replaced_by_observation_id=winner.observation_id),
                    actor=actor,
                )
    except ValueError as exc:
        logger.info("cue_conflict_refused", media_id=str(media_id), reason=str(exc))
        return ConflictOutcome(conflict=conflict, error=str(exc))
    chosen = _replace(winner, cue_file_ordinal=ordinal)
    logger.info(
        "cue_conflict_resolved",
        media_id=str(media_id),
        candidate_id=str(winner.candidate_id),
        rejected=[str(sibling.candidate_id) for sibling in siblings],
        proposed=conflict.proposed_id == winner.candidate_id,
    )
    return ConflictOutcome(conflict=conflict, winner=chosen, rejected=siblings)


__all__ = ["CONFLICT_ORDER", "ConflictOutcome", "CueConflict", "load_cue_conflicts", "resolve_cue_conflict"]
