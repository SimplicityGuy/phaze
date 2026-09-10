""" "More like this set" -- deterministic similarity over `set_profile` rows (phaze-x1qr3.11).

The baseline arm `docs/design/0001-audiomuse-ai-no-go.md` asked for before any embedding
question can be reopened: no ANN index, no CLAP, no learned similarity space -- one file's
stored classifier outputs, projected shape and modal key, scored against every other file's
the same way, in Python, over one query's worth of rows.

**What is compared, and why it is exactly these six terms.** `SetProfile.mean_vector` is
positional in `MOOD_ORDER` (`phaze.services.set_projection`) and carries 11 CONTINUOUS
positive-class probabilities -- the 7 mood ones and 4 style-ADJACENT ones (danceability,
gender, tonality, voice_instrumental) -- so cosine similarity over it is a real, finer-grained
signal, but it is **not** the same claim as the brief's "style and dominant-mood agreement":
`MOOD_ORDER` carries no Discogs-style genre at all, so a `mean_vector`-only score would narrow
that criterion rather than discharge it (review finding, phaze-x1qr3.11 changes-requested
round 1). The fix is two DISCRETE agreement terms read off `AnalysisResult.style` and
`AnalysisResult.mood` -- the same duration-weighted dominant labels
`services.record_facts.dominant_label` derives for the sidebar's own "Mood · style" fact row,
here read pre-computed off the file's `AnalysisResult` row (`aggregate_dominant` already wrote
them once, at analysis completion) rather than re-derived from windows. `AnalysisResult` is
already joined for `bpm` below, so `style`/`mood` cost no extra query. The remaining three
terms are read from data no single vector or label carries: `arc` (Euclidean, the set's energy
shape over time), `AnalysisResult.bpm` (the file's own aggregate tempo, already computed once
by `aggregate_bpm` at analysis time -- this module reads it rather than re-deriving a median
over stored fine windows, which would turn one candidate row into an N+1 window read), and
`SetProfile.camelot_modal` (wheel adjacency via `services.set_projection.wheel_adjacent`, the
same predicate the harmonic journey wheel colours edges with).

The six terms, in `SIMILARITY_WEIGHTS`: `vector` (cosine over `mean_vector`), `arc_shape`
(Euclidean over `arc`), `style` and `mood` (binary agreement over `AnalysisResult.style` /
`.mood` -- no "adjacent" bucket, since neither label carries an ordering relation the way
Camelot positions do), `bpm`, and `key`.

The scoring line stays the brief's literal three-part shape (`"arc 0.09 · 1.2 % BPM · 8A"`)
rather than growing a fourth/fifth segment for style/mood -- appending a style mark broke the
line's own worked example and every test asserting it byte-for-byte, for a term two candidates
either share completely or not at all (nothing gradual to report the way "1.2 % BPM" reports a
measured gap). The style/mood contribution is real in the score and documented here, in
`SIMILARITY_WEIGHTS`'s own comment, and in `_categorical_agreement`'s docstring, rather than
rendered.

**Determinism.** Every term is a closed-form function of stored columns -- no randomness, no
model call, no external I/O. Ties are broken by `file_id` so two candidates scoring
identically always order the same way across runs.

**Complexity, and what the candidate scan is allowed to carry (phaze-zb5y9).** Two queries,
both `O(1)` round trips. The first loads every candidate row with a usable profile, selecting
**only the four scored `SetProfile` columns** (`file_id`, `mean_vector`, `arc`,
`camelot_modal`) plus the joined title/BPM/style/mood -- explicitly NOT `glyph`, the per-window
JSONB the sidebar renders, which is ~240 cells for a 12 h set and which nothing in the scoring
path reads. Hydrating it for every profiled file was the whole cost of this call: the scan is
`O(n)` either way, but `n` glyphs is `O(n)` JSONB decodes for a result that keeps three. The
second query loads the `SetProfile` entities for the `limit` WINNERS only, in one
`WHERE file_id IN (...)`, so the sidebar still gets a real row to hand the glyph macro.

Scoring one candidate is `O(len(MOOD_ORDER) + ARC_POINTS)`, a fixed constant (11 + 64 = 75,
plus two O(1) string comparisons for style and mood). The top `limit` are kept with
`heapq.nlargest` -- `O(n log limit)` rather than the `O(n log n)` of a full sort that then
discards all but three rows. Its key is `(score, -file_id.int)`, which reproduces the previous
`sort(key=(-score, str(file_id)))` ordering EXACTLY: `uuid.UUID.int` orders identically to the
hyphenated lowercase-hex `str`, so negating it turns "smallest id first" into "largest key
first" without changing which candidate wins a tie. See
`tests/shared/services/test_set_similarity.py`'s corpus-scale test for a synthetic measurement
of `n`, and `tests/integration/test_record_similarity_queries.py` for the SQL-level proof that
the scan carries no `glyph` and runs on the full page only.

**Gaps stay gaps, never a manufactured zero.** A `mean_vector` or `arc` position with no
overlapping evidence between the query and a candidate (either side's value is `NaN`) is
dropped from that pairwise comparison rather than treated as agreement or disagreement; a
missing BPM, Camelot code, style, or mood on either side contributes no credit for that term
rather than raising or silently defaulting to "similar". A file with no usable profile of its
own (no `SetProfile` row, or one with no `mean_vector`/`arc` at all -- nothing to compare) has
nothing to rank candidates by, so it gets an empty list, the same honest gap the rest of the
projection renders elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
import heapq
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

# The six scored terms and their weights, in ONE table for the same reason
# `set_projection.ENERGY_WEIGHTS` is one table: a test greps this module for literal weight
# floats outside it, so a weight can never quietly reappear as an inline constant elsewhere.
# These magnitudes are the implementer's own pick -- plausible relative weight, not yet
# reviewed by the operator; nothing here should be read as though that review has happened.
# `vector` (cosine over `mean_vector`) and `arc_shape` are weighted equal and heaviest (the two
# continuous, richest signals); `style`, `mood`, `bpm` and `key` are equal and lighter (each a
# single scalar or categorical fact) -- `style` and `mood` are discrete AnalysisResult labels,
# not folded into `vector`, per the review finding recorded in the module docstring.
SIMILARITY_WEIGHTS: Final[dict[str, float]] = {
    "vector": 0.30,
    "arc_shape": 0.30,
    "style": 0.10,
    "mood": 0.10,
    "bpm": 0.10,
    "key": 0.10,
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

# Style/mood term values: exact label agreement or none. Unlike Camelot codes, an
# `AnalysisResult.style` / `.mood` label ("techno", "energetic") carries no ordering relation
# to another label, so there is no "adjacent" middle bucket -- only match or no match (which
# includes either side missing).
CATEGORICAL_MATCH: Final[float] = 1.0
CATEGORICAL_NO_MATCH: Final[float] = 0.0


@dataclass(frozen=True)
class SimilarSet:
    """One "more like this set" row -- a scored candidate plus everything the sidebar renders.

    ``set_profile`` is the candidate's own row, carried through so the sidebar can hand it
    straight to the existing ``ui.set_glyph`` macro exactly as the record page and the Files
    table already do, with no second read *in the caller*. It is loaded by ``find_similar_sets``
    for the WINNERS only (phaze-zb5y9) -- the candidate scan itself never carries ``glyph``.
    """

    file_id: uuid.UUID
    title: str
    score: float
    scoring_line: str
    set_profile: SetProfile


@dataclass(frozen=True)
class _ScoredCandidate:
    """One scored candidate BEFORE its ``SetProfile`` entity is loaded (phaze-zb5y9).

    Everything the ranking needs and nothing the ranking does not: the scan produces one of
    these per candidate row, `heapq.nlargest` keeps ``limit`` of them, and only those get a
    second query for the entity the sidebar renders.
    """

    file_id: uuid.UUID
    title: str
    score: float
    scoring_line: str


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


def _categorical_agreement(a: str | None, b: str | None) -> float:
    """``CATEGORICAL_MATCH`` when both sides carry the same non-empty label, else
    ``CATEGORICAL_NO_MATCH`` -- including when either side is missing (no credit for "I don't
    know" agreeing with anything). Shared by the ``style`` and ``mood`` terms, which read
    ``AnalysisResult.style`` / ``.mood`` -- the same duration-weighted dominant labels
    ``services.record_facts.dominant_label`` derives for the sidebar's "Mood · style" fact row,
    here read pre-computed rather than re-derived from windows.
    """
    if not a or not b:
        return CATEGORICAL_NO_MATCH
    return CATEGORICAL_MATCH if a == b else CATEGORICAL_NO_MATCH


def _scoring_line(distance: float, bpm_pct: float | None, camelot_modal: str | None) -> str:
    """The explainable per-neighbour line, e.g. ``"arc 0.09 · 1.2 % BPM · 8A"``.

    Each segment is present only when there was evidence for it: an infinite arc distance (no
    overlapping arc data) or a missing BPM/Camelot code drops that segment rather than
    printing a placeholder, so the line never claims a measurement that was not made.

    Deliberately carries no style/mood segment: the brief's own worked example is exactly this
    three-part shape, and style/mood is a binary match-or-not with nothing gradual to report the
    way "1.2 % BPM" reports a measured gap. The term still contributes to ``score`` (see
    ``SIMILARITY_WEIGHTS``, ``_categorical_agreement``) -- it is scored and documented, just not
    rendered.
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
    query_style: str | None,
    query_mood: str | None,
    candidate_file_id: uuid.UUID,
    candidate_mean_vector: list[float] | None,
    candidate_arc: list[float] | None,
    candidate_camelot_modal: str | None,
    candidate_bpm: float | None,
    candidate_style: str | None,
    candidate_mood: str | None,
    title: str,
) -> _ScoredCandidate:
    cosine = cosine_similarity(query_mean_vector, candidate_mean_vector or [])
    distance = arc_distance(query_arc, candidate_arc or [])
    arc_sim = 1.0 / (1.0 + distance) if math.isfinite(distance) else 0.0
    bpm_pct = _bpm_pct_diff(query_bpm, candidate_bpm)
    bpm_sim = 1.0 / (1.0 + bpm_pct / BPM_DECAY_SCALE) if bpm_pct is not None else 0.0
    key_sim = _key_term(query_camelot_modal, candidate_camelot_modal)
    style_sim = _categorical_agreement(query_style, candidate_style)
    mood_sim = _categorical_agreement(query_mood, candidate_mood)

    score = (
        SIMILARITY_WEIGHTS["vector"] * cosine
        + SIMILARITY_WEIGHTS["arc_shape"] * arc_sim
        + SIMILARITY_WEIGHTS["style"] * style_sim
        + SIMILARITY_WEIGHTS["mood"] * mood_sim
        + SIMILARITY_WEIGHTS["bpm"] * bpm_sim
        + SIMILARITY_WEIGHTS["key"] * key_sim
    )
    return _ScoredCandidate(
        file_id=candidate_file_id,
        title=title,
        score=score,
        scoring_line=_scoring_line(distance, bpm_pct, candidate_camelot_modal),
    )


async def find_similar_sets(
    session: AsyncSession,
    file_id: uuid.UUID,
    query_profile: SetProfile | None,
    query_bpm: float | None,
    query_style: str | None,
    query_mood: str | None,
    *,
    limit: int = TOP_N,
) -> list[SimilarSet]:
    """The top ``limit`` sets most like ``file_id``'s, scored deterministically, or ``[]``.

    ``query_profile``, ``query_bpm``, ``query_style`` and ``query_mood`` are the CALLER's
    already-loaded reads (the record router already fetches its ``SetProfile`` and
    ``AnalysisResult`` rows to build the facts panel) -- this function takes no further read of
    ``file_id``'s own data. It issues exactly TWO queries (phaze-zb5y9): the candidate scan
    below, which selects only the scored columns and never ``glyph``, and one
    ``WHERE file_id IN (...)`` for the ``limit`` winners' ``SetProfile`` entities -- bounded by
    ``limit``, not by the corpus. Returns ``[]`` immediately, with no query at all, when the
    query file has no usable profile of its own (no row, or one with no ``mean_vector``/``arc``)
    -- there is nothing to rank candidates against.
    """
    if query_profile is None or query_profile.mean_vector is None or query_profile.arc is None:
        return []

    title_column = func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename)
    statement = (
        select(
            SetProfile.file_id,
            SetProfile.mean_vector,
            SetProfile.arc,
            SetProfile.camelot_modal,
            title_column.label("title"),
            AnalysisResult.bpm,
            AnalysisResult.style,
            AnalysisResult.mood,
        )
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
            query_style=query_style,
            query_mood=query_mood,
            candidate_file_id=candidate_file_id,
            candidate_mean_vector=candidate_mean_vector,
            candidate_arc=candidate_arc,
            candidate_camelot_modal=candidate_camelot_modal,
            candidate_bpm=candidate_bpm,
            candidate_style=candidate_style,
            candidate_mood=candidate_mood,
            title=title,
        )
        for (
            candidate_file_id,
            candidate_mean_vector,
            candidate_arc,
            candidate_camelot_modal,
            title,
            candidate_bpm,
            candidate_style,
            candidate_mood,
        ) in rows
    ]
    # Same ordering as the full sort this replaced -- score DESC, then file_id ASC as the
    # deterministic tiebreak -- at O(n log limit). See the module docstring on why negating
    # `UUID.int` is the exact equivalent of the old `str(file_id)` ascending tiebreak.
    winners = heapq.nlargest(limit, scored, key=lambda candidate: (candidate.score, -candidate.file_id.int))
    if not winners:
        return []

    # The winners' full rows -- the ONLY place `glyph` is read, for `limit` files rather than
    # for the whole corpus. One query, `IN (...)`, never a per-winner get.
    profile_statement = select(SetProfile).where(SetProfile.file_id.in_([candidate.file_id for candidate in winners]))
    profiles = {profile.file_id: profile for profile in (await session.execute(profile_statement)).scalars()}
    return [
        SimilarSet(
            file_id=candidate.file_id,
            title=candidate.title,
            score=candidate.score,
            scoring_line=candidate.scoring_line,
            set_profile=profiles[candidate.file_id],
        )
        for candidate in winners
    ]


__all__ = [
    "BPM_DECAY_SCALE",
    "CATEGORICAL_MATCH",
    "CATEGORICAL_NO_MATCH",
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
