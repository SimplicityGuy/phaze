"""``services/set_similarity`` -- deterministic "more like this set" similarity (phaze-x1qr3.11).

Every acceptance criterion on the bead, discharged one test at a time: a duplicate rip (same
arc, same keys, same style, same mood) ranks first; a wheel-adjacent key outranks a
tritone-away one at equal arc distance; the arc term is symmetric; a style-agreeing candidate
outranks a style-disagreeing one at equal everything else, and likewise for mood; and a file
with no usable profile of its own returns an empty list. A synthetic corpus-scale measurement
(rows scored, wall clock) closes out the "query over the operator's corpus" acceptance line --
this machine has no route to the real archive, so the measurement here is explicitly synthetic;
the bead comment records it as such and names the real-corpus measurement as an operator item.

Round-1 review (changes-requested): the first version of this module folded "style and
dominant-mood agreement" entirely into the cosine term over ``mean_vector``, which carries no
Discogs-style genre at all -- a narrowing of the criterion, not a discharge of it. This file's
style/mood tests exist because of that finding; see the module docstring for the fix.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.set_profile import SetProfile
from phaze.services.set_projection import ARC_POINTS, MOOD_ORDER
from phaze.services.set_similarity import (
    CATEGORICAL_MATCH,
    CATEGORICAL_NO_MATCH,
    KEY_ADJACENT,
    KEY_OTHER,
    SIMILARITY_WEIGHTS,
    TOP_N,
    SimilarSet,
    _categorical_agreement,
    arc_distance,
    cosine_similarity,
    find_similar_sets,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


# A plausible 11-d positive-class vector and 64-point arc, reused as the "query" shape across
# tests so only the dimension under test (arc distance, key relation, ...) varies between rows.
_BASE_VECTOR = [0.6, 0.4, 0.3, 0.2, 0.7, 0.1, 0.5, 0.6, 0.5, 0.4, 0.8]
_BASE_ARC = [0.3 + 0.2 * math.sin(i / 4) for i in range(ARC_POINTS)]

assert len(_BASE_VECTOR) == len(MOOD_ORDER)


def _shifted_arc(offset: float) -> list[float]:
    """``_BASE_ARC`` with every point shifted by a constant -- a known, fixed Euclidean distance
    from ``_BASE_ARC`` (``offset * sqrt(ARC_POINTS)``), so tests can hold "arc distance" fixed
    across two otherwise-different candidates without depending on floating-point luck."""
    return [value + offset for value in _BASE_ARC]


async def _seed_profile(
    session: AsyncSession,
    make_file,
    *,
    mean_vector: list[float] | None,
    arc: list[float] | None,
    camelot_modal: str | None,
    bpm: float | None,
    style: str | None = None,
    mood: str | None = None,
    original_filename: str = "set.mp3",
) -> FileRecord:
    file = await make_file(original_filename=original_filename)
    session.add(SetProfile(file_id=file.id, mean_vector=mean_vector, arc=arc, camelot_modal=camelot_modal))
    if bpm is not None or style is not None or mood is not None:
        session.add(AnalysisResult(id=uuid.uuid4(), file_id=file.id, bpm=bpm, style=style, mood=mood))
    await session.commit()
    await session.refresh(file)
    return file


async def _find(
    session: AsyncSession,
    file_id: uuid.UUID,
    query_profile: SetProfile | None,
    *,
    bpm: float | None = None,
    style: str | None = None,
    mood: str | None = None,
) -> list[SimilarSet]:
    """Thin wrapper over ``find_similar_sets`` so most call sites need only name the query
    signal(s) the test actually cares about."""
    return await find_similar_sets(session, file_id, query_profile, bpm, style, mood)


# ---------------------------------------------------------------------------
# Pure-function terms
# ---------------------------------------------------------------------------


def test_arc_distance_is_symmetric() -> None:
    """The named acceptance criterion: swapping the two arcs never changes the distance."""
    a = _BASE_ARC
    b = _shifted_arc(0.15)
    assert arc_distance(a, b) == pytest.approx(arc_distance(b, a))
    # Also true with a real gap (NaN) on one side, asymmetric NaN placement included.
    gapped = [*b[:10], math.nan, *b[11:]]
    assert arc_distance(a, gapped) == pytest.approx(arc_distance(gapped, a))


def test_arc_distance_is_zero_for_identical_arcs_and_positive_otherwise() -> None:
    assert arc_distance(_BASE_ARC, _BASE_ARC) == pytest.approx(0.0)
    assert arc_distance(_BASE_ARC, _shifted_arc(0.15)) > 0.0


def test_arc_distance_ignores_positions_where_either_side_is_nan() -> None:
    a = [1.0, 2.0, math.nan]
    b = [1.0, 2.0, 99.0]
    assert arc_distance(a, b) == pytest.approx(0.0)


def test_arc_distance_is_infinite_with_no_overlapping_data() -> None:
    a = [math.nan, math.nan]
    b = [math.nan, math.nan]
    assert arc_distance(a, b) == math.inf


def test_cosine_similarity_is_one_for_identical_vectors() -> None:
    assert cosine_similarity(_BASE_VECTOR, _BASE_VECTOR) == pytest.approx(1.0)


def test_cosine_similarity_is_the_honest_zero_floor_with_no_overlap() -> None:
    assert cosine_similarity([math.nan, math.nan], [math.nan, math.nan]) == 0.0
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# Acceptance: duplicate rip ranks first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_duplicate_rip_ranks_first(session: AsyncSession, make_file) -> None:
    """Same arc, same mean_vector, same key, same BPM, same style, same mood: the closest thing
    to "the same set twice" scores the maximum (every term's weight, at full credit) and sorts
    ahead of two genuinely different candidates."""
    query = await _seed_profile(
        session,
        make_file,
        mean_vector=_BASE_VECTOR,
        arc=_BASE_ARC,
        camelot_modal="8A",
        bpm=128.0,
        style="techno",
        mood="energetic",
        original_filename="query.mp3",
    )
    duplicate = await _seed_profile(
        session,
        make_file,
        mean_vector=list(_BASE_VECTOR),
        arc=list(_BASE_ARC),
        camelot_modal="8A",
        bpm=128.0,
        style="techno",
        mood="energetic",
        original_filename="dup.mp3",
    )
    await _seed_profile(
        session,
        make_file,
        mean_vector=[0.1] * len(MOOD_ORDER),
        arc=_shifted_arc(1.5),
        camelot_modal="2B",
        bpm=95.0,
        style="house",
        mood="dark",
        original_filename="different.mp3",
    )
    await _seed_profile(session, make_file, mean_vector=None, arc=None, camelot_modal=None, bpm=None, original_filename="no-profile.mp3")

    query_profile = await session.get(SetProfile, query.id)
    neighbours = await _find(session, query.id, query_profile, bpm=128.0, style="techno", mood="energetic")

    assert neighbours, "the duplicate and the different candidate must both be scoreable"
    assert neighbours[0].file_id == duplicate.id
    assert neighbours[0].score == pytest.approx(sum(SIMILARITY_WEIGHTS.values()))
    assert neighbours[0].scoring_line == "arc 0.00 · 0.0 % BPM · 8A"


# ---------------------------------------------------------------------------
# Acceptance: wheel-adjacent outranks a tritone away at equal arc distance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wheel_adjacent_key_outranks_a_tritone_at_equal_arc_distance(session: AsyncSession, make_file) -> None:
    """Two candidates share the query's mean_vector, arc distance and BPM exactly -- the only
    difference is the key relation: "9A" is wheel-adjacent to the query's "8A" (same letter,
    one semitone step); "2A" is a tritone away (6 semitones). Only the key term can move the
    score, so the wheel-adjacent candidate must rank first."""
    query = await _seed_profile(
        session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="query.mp3"
    )
    adjacent = await _seed_profile(
        session,
        make_file,
        mean_vector=list(_BASE_VECTOR),
        arc=_shifted_arc(0.2),
        camelot_modal="9A",
        bpm=128.0,
        original_filename="adjacent.mp3",
    )
    tritone = await _seed_profile(
        session,
        make_file,
        mean_vector=list(_BASE_VECTOR),
        arc=_shifted_arc(0.2),
        camelot_modal="2A",
        bpm=128.0,
        original_filename="tritone.mp3",
    )

    query_profile = await session.get(SetProfile, query.id)
    neighbours = await _find(session, query.id, query_profile, bpm=128.0)

    by_id = {neighbour.file_id: neighbour for neighbour in neighbours}
    assert by_id[adjacent.id].score > by_id[tritone.id].score
    # Confirms the ONLY thing that moved is the key term -- arc/cosine/BPM contributions equal.
    assert by_id[adjacent.id].score - by_id[tritone.id].score == pytest.approx(SIMILARITY_WEIGHTS["key"] * (KEY_ADJACENT - KEY_OTHER))


# ---------------------------------------------------------------------------
# Acceptance: style agreement, and mood agreement, each in isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_style_agreement_outranks_disagreement_with_everything_else_equal(session: AsyncSession, make_file) -> None:
    """Two candidates tie on arc, mean_vector, BPM, key and mood -- only ``style`` differs
    ("techno" agrees with the query's "techno"; "house" does not). The style-agreeing candidate
    must rank first, and by exactly the style term's weight."""
    query = await _seed_profile(
        session,
        make_file,
        mean_vector=_BASE_VECTOR,
        arc=_BASE_ARC,
        camelot_modal="8A",
        bpm=128.0,
        style="techno",
        mood="energetic",
        original_filename="query.mp3",
    )
    agrees = await _seed_profile(
        session,
        make_file,
        mean_vector=list(_BASE_VECTOR),
        arc=list(_BASE_ARC),
        camelot_modal="8A",
        bpm=128.0,
        style="techno",
        mood="energetic",
        original_filename="style-agrees.mp3",
    )
    disagrees = await _seed_profile(
        session,
        make_file,
        mean_vector=list(_BASE_VECTOR),
        arc=list(_BASE_ARC),
        camelot_modal="8A",
        bpm=128.0,
        style="house",
        mood="energetic",
        original_filename="style-disagrees.mp3",
    )

    query_profile = await session.get(SetProfile, query.id)
    neighbours = await _find(session, query.id, query_profile, bpm=128.0, style="techno", mood="energetic")

    by_id = {neighbour.file_id: neighbour for neighbour in neighbours}
    assert by_id[agrees.id].score > by_id[disagrees.id].score
    assert by_id[agrees.id].score - by_id[disagrees.id].score == pytest.approx(
        SIMILARITY_WEIGHTS["style"] * (CATEGORICAL_MATCH - CATEGORICAL_NO_MATCH)
    )


@pytest.mark.asyncio
async def test_mood_agreement_outranks_disagreement_with_everything_else_equal(session: AsyncSession, make_file) -> None:
    """The mood mirror of the style test above: only ``mood`` differs between the two
    candidates ("energetic" agrees with the query; "dark" does not)."""
    query = await _seed_profile(
        session,
        make_file,
        mean_vector=_BASE_VECTOR,
        arc=_BASE_ARC,
        camelot_modal="8A",
        bpm=128.0,
        style="techno",
        mood="energetic",
        original_filename="query.mp3",
    )
    agrees = await _seed_profile(
        session,
        make_file,
        mean_vector=list(_BASE_VECTOR),
        arc=list(_BASE_ARC),
        camelot_modal="8A",
        bpm=128.0,
        style="techno",
        mood="energetic",
        original_filename="mood-agrees.mp3",
    )
    disagrees = await _seed_profile(
        session,
        make_file,
        mean_vector=list(_BASE_VECTOR),
        arc=list(_BASE_ARC),
        camelot_modal="8A",
        bpm=128.0,
        style="techno",
        mood="dark",
        original_filename="mood-disagrees.mp3",
    )

    query_profile = await session.get(SetProfile, query.id)
    neighbours = await _find(session, query.id, query_profile, bpm=128.0, style="techno", mood="energetic")

    by_id = {neighbour.file_id: neighbour for neighbour in neighbours}
    assert by_id[agrees.id].score > by_id[disagrees.id].score
    assert by_id[agrees.id].score - by_id[disagrees.id].score == pytest.approx(
        SIMILARITY_WEIGHTS["mood"] * (CATEGORICAL_MATCH - CATEGORICAL_NO_MATCH)
    )


def test_style_and_mood_terms_give_no_credit_when_either_side_is_missing() -> None:
    """A candidate (or the query) with no stored ``AnalysisResult.style``/``.mood`` gets no
    credit for "agreeing" with anything -- missing is not a wildcard match."""
    assert _categorical_agreement(None, "techno") == CATEGORICAL_NO_MATCH
    assert _categorical_agreement("techno", None) == CATEGORICAL_NO_MATCH
    assert _categorical_agreement(None, None) == CATEGORICAL_NO_MATCH
    assert _categorical_agreement("techno", "techno") == CATEGORICAL_MATCH
    assert _categorical_agreement("techno", "house") == CATEGORICAL_NO_MATCH


# ---------------------------------------------------------------------------
# Acceptance: no profile -> empty list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_file_with_no_profile_returns_an_empty_list(session: AsyncSession, make_file) -> None:
    """No ``SetProfile`` row at all for the query file: nothing to rank candidates against."""
    other = await _seed_profile(
        session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="other.mp3"
    )
    file_with_no_row = await make_file(original_filename="query.mp3")

    neighbours = await _find(session, file_with_no_row.id, None)

    assert neighbours == []
    # Sanity: the candidate row above really is scoreable against a DIFFERENT query, so the
    # empty result is really about the missing profile, not an empty corpus.
    other_profile = await session.get(SetProfile, other.id)
    assert other_profile is not None


@pytest.mark.asyncio
async def test_a_file_with_a_profile_row_but_no_mean_vector_or_arc_also_returns_empty(session: AsyncSession, make_file) -> None:
    """A ``SetProfile`` row exists (a fine-only file, per the projection's NULL contract) but
    carries neither ``mean_vector`` nor ``arc`` -- there is still nothing to compare on."""
    fine_only = await _seed_profile(session, make_file, mean_vector=None, arc=None, camelot_modal="8A", bpm=None, original_filename="fine-only.mp3")
    await _seed_profile(session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="other.mp3")

    query_profile = await session.get(SetProfile, fine_only.id)
    neighbours = await _find(session, fine_only.id, query_profile)

    assert neighbours == []


@pytest.mark.asyncio
async def test_no_other_file_has_a_profile_also_returns_empty(session: AsyncSession, make_file) -> None:
    query = await _seed_profile(
        session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="query.mp3"
    )
    query_profile = await session.get(SetProfile, query.id)

    neighbours = await _find(session, query.id, query_profile, bpm=128.0)

    assert neighbours == []


# ---------------------------------------------------------------------------
# Other behaviour: limit, determinism, gaps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_at_most_top_n_neighbours(session: AsyncSession, make_file) -> None:
    query = await _seed_profile(
        session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="query.mp3"
    )
    for i in range(TOP_N + 2):
        await _seed_profile(
            session,
            make_file,
            mean_vector=list(_BASE_VECTOR),
            arc=_shifted_arc(0.1 * (i + 1)),
            camelot_modal="8A",
            bpm=128.0,
            original_filename=f"candidate-{i}.mp3",
        )

    query_profile = await session.get(SetProfile, query.id)
    neighbours = await _find(session, query.id, query_profile, bpm=128.0)

    assert len(neighbours) == TOP_N
    # Closest arc first.
    assert [n.score for n in neighbours] == sorted((n.score for n in neighbours), reverse=True)


@pytest.mark.asyncio
async def test_a_candidate_missing_bpm_still_scores_and_omits_the_bpm_segment(session: AsyncSession, make_file) -> None:
    query = await _seed_profile(
        session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="query.mp3"
    )
    no_bpm = await _seed_profile(
        session, make_file, mean_vector=list(_BASE_VECTOR), arc=_shifted_arc(0.1), camelot_modal="8A", bpm=None, original_filename="no-bpm.mp3"
    )

    query_profile = await session.get(SetProfile, query.id)
    neighbours = await _find(session, query.id, query_profile, bpm=128.0)

    assert neighbours[0].file_id == no_bpm.id
    assert "BPM" not in neighbours[0].scoring_line
    assert neighbours[0].scoring_line.endswith("8A")


@pytest.mark.asyncio
async def test_scoring_line_never_grows_a_style_or_mood_segment(session: AsyncSession, make_file) -> None:
    """The scoring line keeps the brief's literal three-part shape even when style and mood both
    agree -- the term is scored, not rendered (see the module docstring)."""
    query = await _seed_profile(
        session,
        make_file,
        mean_vector=_BASE_VECTOR,
        arc=_BASE_ARC,
        camelot_modal="8A",
        bpm=128.0,
        style="techno",
        mood="energetic",
        original_filename="query.mp3",
    )
    duplicate = await _seed_profile(
        session,
        make_file,
        mean_vector=list(_BASE_VECTOR),
        arc=list(_BASE_ARC),
        camelot_modal="8A",
        bpm=128.0,
        style="techno",
        mood="energetic",
        original_filename="dup.mp3",
    )

    query_profile = await session.get(SetProfile, query.id)
    neighbours = await _find(session, query.id, query_profile, bpm=128.0, style="techno", mood="energetic")

    assert neighbours[0].file_id == duplicate.id
    assert neighbours[0].scoring_line == "arc 0.00 · 0.0 % BPM · 8A"
    assert "techno" not in neighbours[0].scoring_line
    assert "energetic" not in neighbours[0].scoring_line


# ---------------------------------------------------------------------------
# phaze-zb5y9: the top-`limit` selection moved from a full sort to `heapq.nlargest`, and the
# candidate scan stopped hydrating `SetProfile` entities. Neither may change WHICH three
# candidates come back, or in what order -- these two tests are the equivalence proof.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_top_three_are_the_prefix_of_the_full_ranking(session: AsyncSession, make_file) -> None:
    """Asking for every candidate and asking for three must agree.

    The full ranking is also asserted to be exactly ``sorted(key=(-score, str(file_id)))`` --
    the literal key the pre-change implementation sorted by -- so this pins the new selection
    to the old ordering rather than to itself.
    """
    query = await _seed_profile(
        session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="query.mp3"
    )
    # Deliberately mixed: some candidates differ in arc (distinct scores), others are exact
    # duplicates of each other (tied scores, so the file_id tiebreak decides their order).
    for i in range(8):
        await _seed_profile(
            session,
            make_file,
            mean_vector=list(_BASE_VECTOR),
            arc=_shifted_arc(0.1 * (i % 4)),
            camelot_modal="8A",
            bpm=128.0,
            original_filename=f"candidate-{i}.mp3",
        )

    query_profile = await session.get(SetProfile, query.id)
    full = await find_similar_sets(session, query.id, query_profile, 128.0, None, None, limit=8)
    top = await _find(session, query.id, query_profile, bpm=128.0)

    assert len(full) == 8
    assert [(n.file_id, n.score) for n in full] == [
        (n.file_id, n.score) for n in sorted(full, key=lambda similar: (-similar.score, str(similar.file_id)))
    ]
    assert [n.file_id for n in top] == [n.file_id for n in full[:TOP_N]]
    assert [n.scoring_line for n in top] == [n.scoring_line for n in full[:TOP_N]]


@pytest.mark.asyncio
async def test_candidates_tied_on_every_term_are_broken_by_ascending_file_id(session: AsyncSession, make_file) -> None:
    """Six candidates identical on all six terms: only the tiebreak can order them.

    ``heapq.nlargest`` is keyed on ``(score, -file_id.int)`` rather than the old
    ``sort(key=(-score, str(file_id)))``; a tie is the only place those two could disagree, so
    it is where the equivalence is asserted. ``UUID.int`` orders identically to the hyphenated
    lowercase-hex ``str``, which is what makes the negation exact rather than approximate.
    """
    query = await _seed_profile(
        session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="query.mp3"
    )
    tied = [
        await _seed_profile(
            session,
            make_file,
            mean_vector=list(_BASE_VECTOR),
            arc=list(_BASE_ARC),
            camelot_modal="8A",
            bpm=128.0,
            original_filename=f"tied-{i}.mp3",
        )
        for i in range(6)
    ]

    query_profile = await session.get(SetProfile, query.id)
    neighbours = await _find(session, query.id, query_profile, bpm=128.0)

    assert len({round(n.score, 12) for n in neighbours}) == 1, "the fixture must really be a six-way tie"
    assert [n.file_id for n in neighbours] == sorted((file.id for file in tied), key=str)[:TOP_N]


@pytest.mark.asyncio
async def test_the_returned_profiles_are_the_winners_own_rows_with_their_glyphs(session: AsyncSession, make_file) -> None:
    """The candidate scan no longer carries ``glyph``, so the winners' rows are re-read.

    ``SimilarSet.set_profile`` must still be the CANDIDATE's own row -- right ``file_id``, right
    ``camelot_modal``, and a populated ``glyph`` -- because the sidebar hands it straight to the
    shared ``ui.set_glyph`` macro. A hydration keyed on the wrong id would show one neighbour's
    picture under another's title, which no ranking assertion would catch.
    """
    glyph_by_key = {"9A": [{"camelot_number": 9, "energy": 0.9}], "2A": [{"camelot_number": 2, "energy": 0.2}]}
    query = await _seed_profile(
        session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="query.mp3"
    )
    for key, glyph in glyph_by_key.items():
        candidate = await _seed_profile(
            session,
            make_file,
            mean_vector=list(_BASE_VECTOR),
            arc=list(_BASE_ARC),
            camelot_modal=key,
            bpm=128.0,
            original_filename=f"{key}.mp3",
        )
        profile = await session.get(SetProfile, candidate.id)
        assert profile is not None
        profile.glyph = glyph
    await session.commit()

    query_profile = await session.get(SetProfile, query.id)
    neighbours = await _find(session, query.id, query_profile, bpm=128.0)

    assert len(neighbours) == len(glyph_by_key)
    for neighbour in neighbours:
        assert neighbour.set_profile.file_id == neighbour.file_id
        assert neighbour.set_profile.glyph == glyph_by_key[neighbour.set_profile.camelot_modal]


# ---------------------------------------------------------------------------
# SYNTHETIC corpus-scale measurement.
#
# This machine has no route to the operator's production database (CLAUDE.md's seat-isolation
# rules), so the bead's "rows scored, wall clock over the operator's corpus" acceptance line
# cannot be measured here. What CAN be measured, and is: rows scored and wall clock over a
# synthetic corpus of a documented size, generated in-process. The bead comment records these
# numbers labelled SYNTHETIC and names the real-corpus measurement as an operator item.
#
# phaze-zb5y9 re-measured the 20,000-row synthetic case (same shape as this test, every profile
# carrying a 240-cell glyph -- a 12 h set's): 3.17-3.79 s before the change, 0.46-0.56 s after,
# three consecutive calls each. Both numbers are on this seat's own test database and are
# recorded on the bead; the 20,000-row seed stays OUT of the suite for the reason below.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("corpus_size", [2_000])
async def test_synthetic_corpus_scale_measurement(session: AsyncSession, make_file, corpus_size: int) -> None:
    """Seeds ``corpus_size`` synthetic candidate profiles and scores one query against all of
    them, printing rows-scored and wall-clock for the bead comment. Kept at 2,000 in the suite
    (20,000 was also run manually for the bead comment -- seeding 20,000 ORM rows on every CI
    run would make this test itself the slow thing it is trying to measure); this still proves
    the ONE-query-plus-O(n)-scoring shape holds as ``n`` grows.
    """
    query = await _seed_profile(
        session, make_file, mean_vector=_BASE_VECTOR, arc=_BASE_ARC, camelot_modal="8A", bpm=128.0, original_filename="query.mp3"
    )
    rng_vectors = [[(i * 37 + j) % 100 / 100.0 for j in range(len(MOOD_ORDER))] for i in range(corpus_size)]
    rng_arcs = [_shifted_arc((i % 50) / 50.0) for i in range(corpus_size)]
    rng_keys = ["8A", "9A", "2A", "5B", "11A"]
    rng_styles = ["techno", "house", "trance", None]
    rng_moods = ["energetic", "dark", None]

    files = []
    for i in range(corpus_size):
        file = await make_file(original_filename=f"synthetic-{i}.mp3")
        files.append(file)
    session.add_all(
        [
            SetProfile(file_id=files[i].id, mean_vector=rng_vectors[i], arc=rng_arcs[i], camelot_modal=rng_keys[i % len(rng_keys)])
            for i in range(corpus_size)
        ]
    )
    session.add_all(
        [
            AnalysisResult(
                id=uuid.uuid4(),
                file_id=files[i].id,
                bpm=100.0 + (i % 60),
                style=rng_styles[i % len(rng_styles)],
                mood=rng_moods[i % len(rng_moods)],
            )
            for i in range(corpus_size)
        ]
    )
    await session.commit()

    query_profile = await session.get(SetProfile, query.id)
    started = time.perf_counter()
    neighbours = await _find(session, query.id, query_profile, bpm=128.0, style="techno", mood="energetic")
    elapsed_sec = time.perf_counter() - started

    assert len(neighbours) == TOP_N
    print(f"\n[SYNTHETIC set_similarity measurement] rows scored={corpus_size} wall_clock_sec={elapsed_sec:.4f}")
    assert elapsed_sec < 5.0, "generous ceiling for a 2,000-row synthetic corpus on a shared test database"
