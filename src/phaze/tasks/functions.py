"""SAQ task: process_file -- essentia analysis of a music file, posted via HTTP (Phase 26 D-05).

Replaces the prior ORM-bound body. Now reads the file from local disk via
payload.original_path, runs essentia in the process pool, and posts the
result via ctx["api_client"].put_analysis (PUT /api/internal/agent/analysis/{file_id}).

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/test_task_split.py (Plan 10).

Wire-format conversion (D-26):
- ``analyze_file`` returns ``mood``/``style`` as strings (dominant label).
- ``AnalysisWritePayload`` requires ``mood``/``style`` as ``dict[str, float]``.
- We rebuild the dicts from ``analysis["features"]`` so the wire contract is
  honored end-to-end: ``mood`` averages each ``mood_*`` set's positive-class
  prediction across the 3 variants; ``style`` takes the top-10 genre
  predictions from the discogs effnet model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from phaze.schemas.agent_analysis import AnalysisWritePayload
from phaze.schemas.agent_tasks import ProcessFilePayload
from phaze.services.analysis import analyze_file
from phaze.tasks.pool import run_in_process_pool


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


_MUSIC_FILE_TYPES = frozenset({"mp3", "flac", "ogg", "m4a", "wav", "aiff", "wma", "aac", "opus"})

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


def _features_to_mood_dict(features: dict[str, Any]) -> dict[str, float] | None:
    """Average each ``mood_*`` set's positive-class predictions across variants.

    Returns the wire-format ``dict[str, float]`` mapping (e.g., ``{"happy": 0.82, "sad": 0.10}``)
    suitable for ``AnalysisWritePayload.mood``. Keys are stripped of the ``mood_`` prefix
    so downstream consumers see clean labels.
    """
    out: dict[str, float] = {}
    for set_name in _MOOD_SET_NAMES:
        set_data = features.get(set_name)
        if not isinstance(set_data, dict):
            continue
        variant_scores: list[float] = []
        for predictions in set_data.values():
            if isinstance(predictions, list) and predictions and isinstance(predictions[0], dict):
                # First class = positive class (binary classifier)
                try:
                    variant_scores.append(float(predictions[0]["prediction"]))
                except (KeyError, TypeError, ValueError):
                    continue
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


async def process_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Run essentia analysis on a local file and post results via HTTP."""
    payload = ProcessFilePayload.model_validate(kwargs)

    # Skip non-music files (parity with prior body)
    if payload.file_type not in _MUSIC_FILE_TYPES:
        return {"file_id": str(payload.file_id), "status": "skipped", "reason": "not_music"}

    api: PhazeAgentClient = ctx["api_client"]

    # CPU-bound analysis in process pool (D-23: original_path is in the payload, no read-back)
    analysis = await run_in_process_pool(
        ctx,
        analyze_file,
        payload.original_path,
        payload.models_path,
    )

    features = analysis.get("features", {}) if isinstance(analysis, dict) else {}
    mood_dict = _features_to_mood_dict(features) if isinstance(features, dict) else None
    style_dict = _features_to_style_dict(features) if isinstance(features, dict) else None

    # PUT result via HTTP (D-26 idempotent upsert; CR-01 partial-PUT semantics preserved by exclude_unset)
    await api.put_analysis(
        payload.file_id,
        AnalysisWritePayload(
            bpm=analysis.get("bpm"),
            musical_key=analysis.get("musical_key"),
            mood=mood_dict,
            style=style_dict,
            danceability=analysis.get("danceability"),
            energy=analysis.get("energy"),
        ),
    )
    return {"file_id": str(payload.file_id), "status": "analyzed"}
