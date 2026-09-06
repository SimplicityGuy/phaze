"""``services/set_projection.MOOD_ORDER`` -- the projection's fixed archive-wide vocabulary.

phaze-x1qr3.1 declares the constant; phaze-x1qr3.2 adds the projection math to the same module
and will extend this file. What is asserted here is the acceptance criterion "``mood_scores`` keys
are the 11 fixed names in the fixed archive-wide order declared in
``services/set_projection.MOOD_ORDER``", split into the two independent ways it can break.
"""

from phaze.services.analysis_models import MODEL_SETS
from phaze.services.set_projection import MOOD_ORDER


def test_mood_order_is_the_eleven_fixed_names_in_a_fixed_order() -> None:
    """The exact tuple is pinned, so a reorder or a rename is a deliberate edit to this test.

    Order is not cosmetic here. It is the stacking and hue assignment of the mood river, the order
    of its legend and of the tracklist's mood dots, and -- because ``SetProfile.mean_vector`` is
    positional -- it is baked into every stored projection row. Changing it re-colours every
    rendered set and invalidates every stored vector, so it is a ``projection_version`` bump and a
    re-backfill, never an edit in place.
    """
    assert MOOD_ORDER == (
        "mood_acoustic",
        "mood_electronic",
        "mood_aggressive",
        "mood_relaxed",
        "mood_happy",
        "mood_sad",
        "mood_party",
        "danceability",
        "gender",
        "tonality",
        "voice_instrumental",
    )
    assert len(MOOD_ORDER) == 11
    assert len(set(MOOD_ORDER)) == len(MOOD_ORDER), "a duplicate name would silently shorten the vector"


def test_mood_order_names_exactly_the_model_sets_that_produce_them() -> None:
    """Every name is a real ``MODEL_SETS`` entry, and no model set is left out of the projection.

    This is the half a pinned literal cannot catch on its own. The names double as the keys those
    model sets occupy in ``analysis_window.features``, so a rename or an added/removed model set
    on the analysis side would leave the projection reading a key that is no longer written -- and
    a missing-key read yields no error, just a silently absent score. Compared as SETS, not
    sequences, precisely because the ORDER above must be free to differ from ``MODEL_SETS``'
    declaration order without failing.
    """
    assert set(MOOD_ORDER) == {model_set.name for model_set in MODEL_SETS}


def test_seven_of_the_eleven_are_the_binary_mood_classifiers() -> None:
    """The ``mood_`` prefix is kept, so the 7 moods stay distinguishable from the 4 non-mood sets.

    The mood river stacks the 7; the other 4 (danceability, gender, tonality, voice_instrumental)
    are facts and similarity terms, never river bands. Keeping the prefix in the stored key means
    that split needs no second list to fall out of sync with this one.
    """
    moods = [name for name in MOOD_ORDER if name.startswith("mood_")]
    assert len(moods) == 7
    assert len(MOOD_ORDER) - len(moods) == 4
    # The 7 lead, so the river's bands are a prefix of the vector rather than a scatter through it.
    assert MOOD_ORDER[:7] == tuple(moods)
