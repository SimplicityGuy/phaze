"""The set projection's fixed archive-wide vocabulary (phaze-x1qr3.1).

This bead is the SCHEMA half of section E's "one projection, then everything rides it":
the nullable per-window columns on ``analysis_window`` and the per-file ``set_profile``
table. The only thing this module declares today is :data:`MOOD_ORDER` -- the fixed name
and order of the 11 positive-class scores those columns carry. The projection math (the
Camelot table, the 11-d vector, the energy scalar, the per-file profile) is
``phaze-x1qr3.2`` and lands in this same module.

``MOOD_ORDER`` is declared here rather than on the model because it is one order shared by
several surfaces that must not disagree: the JSONB key set of
``AnalysisWindow.mood_scores``, the element order of ``SetProfile.mean_vector``, the mood
river's stacking order and hue assignment, the legend, and the tracklist's mood dots. A
per-surface ordering would render a different picture per page from identical data.
"""

from __future__ import annotations


# The 11 model sets whose POSITIVE-class prediction the projection stores, in the single
# fixed archive-wide order every surface reads.
#
# WHY THESE NAMES: each entry is the ``name`` of a ``ModelSetConfig`` in
# ``services/analysis_models.MODEL_SETS``, which is also the key that model set's
# predictions occupy in a coarse window's ``analysis_window.features`` JSONB (shape
# ``features[set_name][variant] -> [{label, prediction}]``). Keeping the projection's key
# names identical to the stored feature keys means the backfill in phaze-x1qr3.3 is a
# direct read with no translation table to drift -- and it keeps the ``mood_`` prefix,
# which distinguishes the 7 binary mood classifiers from the 4 non-mood ones below.
# ``tests/shared/services/test_set_projection.py`` fails the build if the name SET ever
# stops matching ``MODEL_SETS``.
#
# WHY THIS ORDER: it is ``MODEL_SETS``' own declaration order, pinned here as a literal
# rather than derived from it. The order is a DISPLAY contract (the mood river's stacking
# and hue assignment, the legend, the tracklist mood dots) and it is baked into stored
# data (``SetProfile.mean_vector`` is positional), so reordering ``MODEL_SETS`` -- a purely
# internal analysis concern -- must not silently re-colour every rendered set or invalidate
# every stored vector. Deriving it would make exactly that possible. Changing this tuple is
# a ``projection_version`` bump and a re-backfill, never an edit in place.
#
# WHY POSITIVE-CLASS, not raw predictions: see
# ``services/analysis_derive._positive_class_prediction`` -- essentia orders a binary
# classifier's classes ALPHABETICALLY, so ``predictions[0]`` is the NEGATIVE class for
# ``mood_relaxed`` / ``mood_sad`` / ``mood_party``. The projection stores the positive class
# selected BY LABEL; a positional read here would systematically invert three of the seven
# moods, which is the defect that function exists to prevent.
MOOD_ORDER: tuple[str, ...] = (
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

__all__ = ["MOOD_ORDER"]
