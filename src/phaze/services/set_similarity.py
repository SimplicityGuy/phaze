""" "More like this set" -- deterministic similarity over `set_profile` rows (phaze-x1qr3.11).

The baseline arm `docs/design/0001-audiomuse-ai-no-go.md` asked for before any embedding
question can be reopened: no ANN index, no CLAP, no learned similarity space -- one file's
stored classifier outputs, projected shape and modal key, scored against every other file's
the same way, in Python, over one query's worth of rows.

**What is compared, and why it is exactly these four terms.** `SetProfile.mean_vector` is
positional in `MOOD_ORDER` (`phaze.services.set_projection`) and already carries BOTH the
7 mood probabilities and the 4 style-adjacent ones (danceability, gender, tonality,
voice_instrumental) in one 11-d vector -- so cosine similarity over that vector alone IS
"style and dominant-mood agreement": there is no separate discrete term for it, because the
continuous cosine term already subsumes an argmax comparison at finer resolution than a
match/no-match bucket would. The other three terms are read from data no single vector
carries: `arc` (Euclidean, the set's energy shape over time), `AnalysisResult.bpm` (the
file's own aggregate tempo, already computed once by `aggregate_bpm` at analysis time --
this module reads it rather than re-deriving a median over stored fine windows, which would
turn one candidate row into an N+1 window read), and `SetProfile.camelot_modal` (wheel
adjacency via `services.set_projection.wheel_adjacent`, the same predicate the harmonic
journey wheel colours edges with).

**Determinism.** Every term is a closed-form function of stored columns -- no randomness, no
model call, no external I/O. Ties are broken by `file_id` so two candidates scoring
identically always order the same way across runs.

**Complexity.** One `SELECT ... JOIN ... LEFT JOIN ...` loads every candidate row with a
usable profile (`O(1)` round trips); scoring one candidate is `O(len(MOOD_ORDER) +
ARC_POINTS)`, a fixed constant (11 + 64 = 75), so the whole call is `O(n)` in the number of
candidate rows, never `O(n * m)` and never a query per candidate. See
`tests/shared/services/test_set_similarity.py`'s corpus-scale test for a synthetic
measurement of `n`.

**Gaps stay gaps, never a manufactured zero.** A `mean_vector` or `arc` position with no
overlapping evidence between the query and a candidate (either side's value is `NaN`) is
dropped from that pairwise comparison rather than treated as agreement or disagreement; a
missing BPM or Camelot code on either side contributes no credit for that term rather than
raising or silently defaulting to "similar". A file with no usable profile of its own (no
`SetProfile` row, or one with no `mean_vector`/`arc` at all -- nothing to compare) has
nothing to rank candidates by, so it gets an empty list, the same honest gap the rest of the
projection renders elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Final

from sqlalchemy import func, select

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.set_profile import SetProfile
from phaze.services.set_projection import wheel_adjacent


if TYPE_CHECKING:
    from collections.abc import Sequence
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


# How many neighbours the sidebar slot renders. Public so the router and a test can both cite
# it instead of a bare literal "3" drifting between them.
TOP_N: Final[int] = 3

# The four scored terms and their weights, in ONE table for the same reason
# `set_projection.ENERGY_WEIGHTS` is one table: a test greps this module for literal weight
# floats outside it, so a weight can never quietly reappear as an inline constant elsewhere.
# These magnitudes are the implementer's own pick -- plausible relative weight, not yet
# reviewed by the operator; nothing here should be read as though that review has happened.
# `mood_style` and `arc_shape` are weighted equal and heaviest (the two continuous, richest
# signals); `bpm` and `key` are equal and lighter (each a single scalar / categorical fact).
SIMILARITY_WEIGHTS: Final[dict[str, float]] = {
    "mood_style": 0.35,
    "arc_shape": 0.35,
    "bpm": 0.15,
    "key": 0.15,
}

# The BPM term's decay scale: at a 2 % difference the term is already down to half credit,
# matching the bead's own "BPM within 2 %" framing rather than a hard cutoff at that value --
# a set 2.1 % off is still very close, not suddenly worthless.
BPM_DECAY_SCALE: Final[float] = 0.02

# Camelot-key term values: exact match, wheel-adjacent (`wheel_adjacent`, shared with the
# harmonic journey wheel), or neither -- including a tritone, which is deliberately in the
# "neither" bucket alongside every other non-adjacent relation.
KEY_EXACT: Final[float] = 1.0
KEY_ADJACENT: Final[float] = 0.6
KEY_OTHER: Final[float] = 0.0


@dataclass(frozen=True)
class SimilarSet:
    """One "more like this set" row -- a scored candidate plus everything the sidebar renders.

    ``set_profile`` is the candidate's own row, carried through unchanged so the sidebar can
    hand it straight to the existing ``ui.set_glyph`` macro exactly as the record page and the
    Files table already do, with no second read.
    """

    file_id: uuid.UUID
    title: str
    score: float
    scoring_line: str
    set_profile: SetProfile


def _paired_finite(a: Sequence[float], b: Sequence[float]) -> list[tuple[float, float]]:
    """The ``(a[i], b[i])`` pairs where BOTH sides are finite (neither ``NaN`` nor missing).

    Stops at the shorter sequence rather than raising on a length mismatch -- ``mean_vector``
    and ``arc`` carry no DDL length constraint (writer contracts only, per
    ``models/set_profile.py``), so a malformed row degrades to fewer comparable positions
    instead of crashing a whole candidate scan over one bad row.
    """
    return [(x, y) for x, y in zip(a, b) if math.isfinite(x) and math.isfinite(y)]  # noqa: B905 -- deliberately not strict, see docstring


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity over the positions both vectors carry real (finite) data for.

    ``0.0`` -- the honest "no evidence" floor, never negative and never an exception -- when
    there is no overlapping position at all, or either side is the zero vector at the
    positions that do overlap. Every stored component is a probability in ``[0, 1]``, so a
    negative cosine is not expected in practice, but the result is still clamped to ``[0, 1]``
    so a pathological input cannot pull the weighted sum below zero from this term alone.
    """
    pairs = _paired_finite(a, b)
    if not pairs:
        return 0.0
    dot = sum(x * y for x, y in pairs)
    norm_a = math.sqrt(sum(x * x for x, _ in pairs))
    norm_b = math.sqrt(sum(y * y for _, y in pairs))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def arc_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Euclidean distance over the positions both arcs carry real (finite) data for.

    Symmetric by construction: the pairing (`_paired_finite`) and the sum of squared
    differences treat ``a`` and ``b`` identically, so ``arc_distance(a, b) ==
    arc_distance(b, a)`` for any two arcs -- asserted directly by
    ``tests/shared/services/test_set_similarity.py``.

    ``math.inf`` when there is no overlapping position at all -- maximally dissimilar, the
    honest reading of "nothing in common to compare" -- rather than ``0.0``, which would read
    as a perfect match.
    """
    pairs = _paired_finite(a, b)
    if not pairs:
        return math.inf
    return math.sqrt(sum((x - y) ** 2 for x, y in pairs))


def _bpm_pct_diff(a: float | None, b: float | None) -> float | None:
    """Symmetric relative BPM difference, or ``None`` when either side has no usable BPM.

    ``abs(a - b) / max(a, b)`` is symmetric under swapping ``a`` and ``b`` -- both the
    numerator and the denominator are -- matching the arc term's symmetry even though only the
    arc term's is a named acceptance criterion.
    """
    if a is None or b is None or not math.isfinite(a) or not math.isfinite(b) or a <= 0 or b <= 0:
        return None
    return abs(a - b) / max(a, b)


def _key_term(a: str | None, b: str | None) -> float:
    if not a or not b:
        return KEY_OTHER
    if a == b:
        return KEY_EXACT
    if wheel_adjacent(a, b):
        return KEY_ADJACENT
    return KEY_OTHER


def _scoring_line(distance: float, bpm_pct: float | None, camelot_modal: str | None) -> str:
    """The explainable per-neighbour line, e.g. ``"arc 0.09 · 1.2 % BPM · 8A"``.

    Each segment is present only when there was evidence for it: an infinite arc distance (no
    overlapping arc data) or a missing BPM/Camelot code drops that segment rather than
    printing a placeholder, so the line never claims a measurement that was not made.
    """
    parts: list[str] = []
    if math.isfinite(distance):
        parts.append(f"arc {distance:.2f}")
    if bpm_pct is not None:
        parts.append(f"{bpm_pct * 100:.1f} % BPM")
    if camelot_modal:
        parts.append(camelot_modal)
    return " · ".join(parts)


def _score_candidate(
    *,
    query_mean_vector: list[float],
    query_arc: list[float],
    query_camelot_modal: str | None,
    query_bpm: float | None,
    candidate: SetProfile,
    candidate_bpm: float | None,
    title: str,
) -> SimilarSet:
    cosine = cosine_similarity(query_mean_vector, candidate.mean_vector or [])
    distance = arc_distance(query_arc, candidate.arc or [])
    arc_sim = 1.0 / (1.0 + distance) if math.isfinite(distance) else 0.0
    bpm_pct = _bpm_pct_diff(query_bpm, candidate_bpm)
    bpm_sim = 1.0 / (1.0 + bpm_pct / BPM_DECAY_SCALE) if bpm_pct is not None else 0.0
    key_sim = _key_term(query_camelot_modal, candidate.camelot_modal)

    score = (
        SIMILARITY_WEIGHTS["mood_style"] * cosine
        + SIMILARITY_WEIGHTS["arc_shape"] * arc_sim
        + SIMILARITY_WEIGHTS["bpm"] * bpm_sim
        + SIMILARITY_WEIGHTS["key"] * key_sim
    )
    return SimilarSet(
        file_id=candidate.file_id,
        title=title,
        score=score,
        scoring_line=_scoring_line(distance, bpm_pct, candidate.camelot_modal),
        set_profile=candidate,
    )


async def find_similar_sets(
    session: AsyncSession,
    file_id: uuid.UUID,
    query_profile: SetProfile | None,
    query_bpm: float | None,
    *,
    limit: int = TOP_N,
) -> list[SimilarSet]:
    """The top ``limit`` sets most like ``file_id``'s, scored deterministically, or ``[]``.

    ``query_profile`` and ``query_bpm`` are the CALLER's already-loaded reads (the record
    router already fetches both to build the facts panel) -- this function takes no further
    read of ``file_id``'s own data, so the only query it issues is the ONE candidate scan
    below. Returns ``[]`` immediately, with no query at all, when the query file has no usable
    profile of its own (no row, or one with no ``mean_vector``/``arc``) -- there is nothing to
    rank candidates against.
    """
    if query_profile is None or query_profile.mean_vector is None or query_profile.arc is None:
        return []

    title_column = func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename)
    statement = (
        select(SetProfile, title_column.label("title"), AnalysisResult.bpm)
        .join(FileRecord, FileRecord.id == SetProfile.file_id)
        .outerjoin(AnalysisResult, AnalysisResult.file_id == SetProfile.file_id)
        .where(SetProfile.file_id != file_id)
        .where(SetProfile.mean_vector.is_not(None))
        .where(SetProfile.arc.is_not(None))
    )
    rows = (await session.execute(statement)).all()

    scored = [
        _score_candidate(
            query_mean_vector=query_profile.mean_vector,
            query_arc=query_profile.arc,
            query_camelot_modal=query_profile.camelot_modal,
            query_bpm=query_bpm,
            candidate=candidate,
            candidate_bpm=candidate_bpm,
            title=title,
        )
        for candidate, title, candidate_bpm in rows
    ]
    scored.sort(key=lambda similar: (-similar.score, str(similar.file_id)))
    return scored[:limit]


__all__ = [
    "BPM_DECAY_SCALE",
    "KEY_ADJACENT",
    "KEY_EXACT",
    "KEY_OTHER",
    "SIMILARITY_WEIGHTS",
    "TOP_N",
    "SimilarSet",
    "arc_distance",
    "cosine_similarity",
    "find_similar_sets",
]
