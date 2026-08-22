"""Mood / style / danceability derivation from a coarse window's model predictions.

Extracted VERBATIM from ``services/analysis.py`` (phaze-bk9el.15) -- pure reductions over
the feature dicts ``_run_model_sets_over_windows`` builds, with no essentia import and no
dependency on the analysis pipeline. Nothing here touches D-07, D-08 or D-09.

``services/analysis.py`` re-exports every name below; ``_analyze_coarse_windows`` therefore
still resolves ``derive_mood`` / ``derive_style`` / ``derive_danceability`` through that
module's globals, and ``patch.object(analysis, "derive_mood", ...)`` still reaches it.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Mood / style derivation
# ---------------------------------------------------------------------------

_MOOD_SET_NAMES = frozenset(
    {
        "mood_acoustic",
        "mood_electronic",
        "mood_aggressive",
        "mood_relaxed",
        "mood_happy",
        "mood_sad",
        "mood_party",
    }
)


def _positive_class_prediction(predictions: list[dict[str, Any]]) -> float:
    """Return the POSITIVE-class probability from a binary classifier's prediction list.

    essentia's binary-classifier metadata orders classes ALPHABETICALLY, not
    positive-first, so ``predictions[0]`` is the positive class for only SOME model
    sets. e.g. ``mood_relaxed`` = ``['non_relaxed', 'relaxed']`` and ``mood_sad`` /
    ``mood_party`` put the NEGATIVE class first — indexing ``[0]`` there scored the
    mood with P(non_relaxed) and systematically inverted relaxed/sad/party.

    Select the positive class by LABEL: it is the entry whose label does NOT start
    with a negation prefix (``non_`` / ``not_``). Falls back to the first entry when
    no label qualifies (defensive — preserves behavior for unexpected label shapes).
    Callers guard ``if predictions`` so the list is non-empty here.
    """
    positive: dict[str, Any] | None = None
    for entry in predictions:
        if not str(entry.get("label", "")).startswith(("non_", "not_")):
            positive = entry
            break
    if positive is None:
        positive = predictions[0]
    return float(positive["prediction"])


def derive_mood(features: dict[str, Any]) -> str:
    """Derive dominant mood from feature predictions.

    For each mood model set, average the positive-class prediction (selected by
    label, not list position) across the 3 variants. Return the mood name (without
    'mood_' prefix) with the highest averaged confidence.
    """
    best_mood = ""
    best_score = -1.0

    for set_name in _MOOD_SET_NAMES:
        if set_name not in features:
            continue

        variant_scores: list[float] = []
        for _variant_name, predictions in features[set_name].items():
            if predictions:
                variant_scores.append(_positive_class_prediction(predictions))

        if variant_scores:
            avg_score = sum(variant_scores) / len(variant_scores)
            if avg_score > best_score:
                best_score = avg_score
                best_mood = set_name

    # Strip "mood_" prefix
    return best_mood.removeprefix("mood_")


def derive_style(genre_features: dict[str, Any]) -> str:
    """Derive top style/genre from genre model predictions.

    Returns the label of the highest-confidence genre prediction.
    Defensively replaces '---' with '/' in labels.
    """
    predictions = genre_features.get("predictions", [])
    if not predictions:
        return "unknown"

    top = max(predictions, key=lambda p: p["confidence"])
    return str(top["label"]).replace("---", "/")


def derive_danceability(features: dict[str, Any]) -> float | None:
    """Derive a scalar danceability from the danceability model set.

    Averages the positive-class ('danceable') prediction across the 3 variants,
    selected by label (robust to class order) rather than list position. Returns
    None if the danceability set is absent/empty.
    """
    set_data = features.get("danceability")
    if not set_data:
        return None

    scores: list[float] = []
    for _variant_name, predictions in set_data.items():
        if predictions:
            scores.append(_positive_class_prediction(predictions))

    return sum(scores) / len(scores) if scores else None
