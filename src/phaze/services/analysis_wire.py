"""Shared wire-format converters for essentia analysis features (Phase 52, KJOB-02).

Relocated verbatim from ``phaze.tasks.functions`` so BOTH the SAQ ``process_file``
task path AND the new one-shot ``job_runner`` (Plan 02) import the mood/style
feature-to-dict converters from one place instead of re-deriving them.

This module MUST remain stdlib + typing only -- no database, ORM-model, or
SQLAlchemy imports. Both the SAQ worker and the DB-less one-shot pod load it, so
it must never cross the agent import boundary (mirrors the phaze.tasks.functions
invariant, enforced by tests/shared/core/test_task_split.py).

Wire-format conversion (D-26):
- ``analyze_file`` returns ``mood``/``style`` as strings (dominant label).
- ``AnalysisWritePayload`` requires ``mood``/``style`` as ``dict[str, float]``.
- These converters rebuild the dicts from ``analysis["features"]`` so the wire
  contract is honored end-to-end: ``mood`` averages each ``mood_*`` set's
  positive-class prediction across the 3 variants; ``style`` takes the genre
  predictions returned by the discogs effnet model.
"""

from __future__ import annotations

from typing import Any


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


def _positive_class_prediction(predictions: list[Any]) -> float | None:
    """Return the POSITIVE-class probability from a binary classifier's prediction list.

    essentia's binary-classifier metadata orders classes ALPHABETICALLY, not
    positive-first, so ``predictions[0]`` is the positive class for only SOME model
    sets (e.g. ``mood_relaxed`` = ``['non_relaxed', 'relaxed']`` and ``mood_sad`` /
    ``mood_party`` put the NEGATIVE class first). Indexing ``[0]`` there persisted
    P(non_relaxed)/P(non_sad)/P(non_party) as the mood confidence — a systematic
    inversion of the stored wire dict.

    Select the positive class by LABEL: the entry whose label does NOT start with a
    negation prefix (``non_`` / ``not_``). Falls back to the first well-formed entry
    when no label qualifies. Returns None if nothing usable is present (keeps this
    converter's defensive, never-raise contract for malformed input).
    """
    positive: dict[str, Any] | None = None
    for entry in predictions:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("label", "")).startswith(("non_", "not_")):
            positive = entry
            break
    if positive is None:
        for entry in predictions:
            if isinstance(entry, dict):
                positive = entry
                break
    if positive is None:
        return None
    try:
        return float(positive["prediction"])
    except (KeyError, TypeError, ValueError):
        return None


def _features_to_mood_dict(features: dict[str, Any]) -> dict[str, float] | None:
    """Average each ``mood_*`` set's positive-class predictions across variants.

    Returns the wire-format ``dict[str, float]`` mapping (e.g., ``{"happy": 0.82, "sad": 0.10}``)
    suitable for ``AnalysisWritePayload.mood``. The positive class is selected by label
    (robust to essentia's alphabetical class order), not by list position. Keys are
    stripped of the ``mood_`` prefix so downstream consumers see clean labels.
    """
    out: dict[str, float] = {}
    for set_name in _MOOD_SET_NAMES:
        set_data = features.get(set_name)
        if not isinstance(set_data, dict):
            continue
        variant_scores: list[float] = []
        for predictions in set_data.values():
            if isinstance(predictions, list) and predictions:
                score = _positive_class_prediction(predictions)
                if score is not None:
                    variant_scores.append(score)
        if variant_scores:
            out[set_name.removeprefix("mood_")] = sum(variant_scores) / len(variant_scores)
    return out or None


def _features_to_style_dict(features: dict[str, Any]) -> dict[str, float] | None:
    """Convert ``features["genre"]["predictions"]`` to a wire-format ``dict[str, float]``.

    Returns the top genre predictions as ``{label: confidence}``. Labels have
    ``---`` replaced with ``/`` (consistent with ``derive_style``).
    """
    genre = features.get("genre")
    if not isinstance(genre, dict):
        return None
    predictions = genre.get("predictions")
    if not isinstance(predictions, list):
        return None
    out: dict[str, float] = {}
    for entry in predictions:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        confidence = entry.get("confidence")
        if label is None or confidence is None:
            continue
        try:
            out[str(label).replace("---", "/")] = float(confidence)
        except (TypeError, ValueError):
            continue
    return out or None
