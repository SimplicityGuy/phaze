"""Tests for the HTTP-rewritten process_file task (Phase 26 Plan 11)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pydantic import ValidationError
import pytest

from phaze.tasks.functions import (
    _features_to_mood_dict,
    _features_to_style_dict,
    process_file,
)


# Mock essentia analyze_file return value matching analysis.py contract.
MOCK_ANALYSIS: dict[str, Any] = {
    "bpm": 128.0,
    "musical_key": "C minor",
    "mood": "happy",
    "style": "Electronic/House",
    "features": {
        "mood_happy": {
            "musicnn_msd": [{"label": "happy", "prediction": 0.8}, {"label": "not_happy", "prediction": 0.2}],
            "musicnn_mtt": [{"label": "happy", "prediction": 0.7}, {"label": "not_happy", "prediction": 0.3}],
            "vggish": [{"label": "happy", "prediction": 0.9}, {"label": "not_happy", "prediction": 0.1}],
        },
        "mood_sad": {
            "musicnn_msd": [{"label": "sad", "prediction": 0.1}, {"label": "not_sad", "prediction": 0.9}],
            "musicnn_mtt": [{"label": "sad", "prediction": 0.2}, {"label": "not_sad", "prediction": 0.8}],
            "vggish": [{"label": "sad", "prediction": 0.1}, {"label": "not_sad", "prediction": 0.9}],
        },
        "genre": {
            "predictions": [
                {"label": "Electronic---House", "confidence": 0.9},
                {"label": "Electronic---Techno", "confidence": 0.5},
            ],
        },
    },
}


def _make_ctx(api_client: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with an api_client mock."""
    if api_client is None:
        api_client = AsyncMock()
        api_client.put_analysis = AsyncMock(return_value=MagicMock())
    return {"process_pool": MagicMock(), "api_client": api_client}


def _make_payload_kwargs(file_id: uuid.UUID | None = None, file_type: str = "mp3") -> dict[str, Any]:
    return {
        "file_id": str(file_id or uuid.uuid4()),
        "original_path": "/music/track.mp3",
        "file_type": file_type,
        "agent_id": "test-agent",
        "models_path": "/models",
    }


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_features_to_mood_dict_returns_averaged_dict() -> None:
    """_features_to_mood_dict averages positive-class predictions across variants."""
    out = _features_to_mood_dict(MOCK_ANALYSIS["features"])
    assert out is not None
    # mood_happy: (0.8+0.7+0.9)/3 = 0.8
    assert out["happy"] == pytest.approx(0.8, rel=1e-3)
    # mood_sad: (0.1+0.2+0.1)/3 = 0.133
    assert out["sad"] == pytest.approx(0.1333, rel=1e-2)


def test_features_to_mood_dict_returns_none_for_empty() -> None:
    """No mood sets -> None."""
    assert _features_to_mood_dict({}) is None
    assert _features_to_mood_dict({"genre": {"predictions": []}}) is None


def test_features_to_style_dict_returns_normalized_labels() -> None:
    """_features_to_style_dict replaces ``---`` with ``/`` in labels."""
    out = _features_to_style_dict(MOCK_ANALYSIS["features"])
    assert out is not None
    assert out["Electronic/House"] == pytest.approx(0.9, rel=1e-3)
    assert out["Electronic/Techno"] == pytest.approx(0.5, rel=1e-3)


def test_features_to_style_dict_returns_none_for_empty() -> None:
    """Missing/empty genre -> None."""
    assert _features_to_style_dict({}) is None
    assert _features_to_style_dict({"genre": {"predictions": []}}) is None
    assert _features_to_style_dict({"genre": {}}) is None


def test_features_to_mood_dict_skips_malformed_prediction_entries() -> None:
    """Mood loop must continue past KeyError / TypeError / ValueError on bad prediction shapes."""
    features = {
        "mood_happy": {
            "v1": [{"label": "happy"}],  # KeyError on "prediction"
            "v2": [{"label": "happy", "prediction": "not-a-number"}],  # ValueError on float()
            "v3": [{"label": "happy", "prediction": 0.7}],  # OK
        },
    }
    out = _features_to_mood_dict(features)
    assert out is not None
    # Only the OK variant contributes -> 0.7
    assert out["happy"] == pytest.approx(0.7, rel=1e-3)


def test_features_to_style_dict_skips_non_dict_entries() -> None:
    """Style loop must skip predictions list entries that are not dicts."""
    features = {
        "genre": {
            "predictions": [
                "not-a-dict",  # non-dict entry -> continue
                {"label": "Rock", "confidence": 0.5},
            ],
        },
    }
    out = _features_to_style_dict(features)
    assert out is not None
    assert list(out.keys()) == ["Rock"]
    assert out["Rock"] == pytest.approx(0.5, rel=1e-3)


def test_features_to_style_dict_skips_entries_missing_label_or_confidence() -> None:
    """Style loop must skip entries with missing label or missing confidence."""
    features = {
        "genre": {
            "predictions": [
                {"label": "MissingConfidence"},  # no confidence -> skip
                {"confidence": 0.5},  # no label -> skip
                {"label": "Good", "confidence": 0.9},
            ],
        },
    }
    out = _features_to_style_dict(features)
    assert out is not None
    assert list(out.keys()) == ["Good"]
    assert out["Good"] == pytest.approx(0.9, rel=1e-3)


def test_features_to_style_dict_skips_non_numeric_confidence() -> None:
    """Style loop must catch TypeError / ValueError when float(confidence) fails."""
    features = {
        "genre": {
            "predictions": [
                {"label": "Bad", "confidence": "not-a-float"},  # ValueError -> skip
                {"label": "Good", "confidence": 0.42},
            ],
        },
    }
    out = _features_to_style_dict(features)
    assert out is not None
    assert list(out.keys()) == ["Good"]
    assert out["Good"] == pytest.approx(0.42, rel=1e-3)


# ---------------------------------------------------------------------------
# process_file behavior
# ---------------------------------------------------------------------------


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_calls_put_analysis(mock_pool: AsyncMock) -> None:
    """process_file calls api.put_analysis with the right schema after running essentia."""
    file_id = uuid.uuid4()
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())

    ctx = _make_ctx(api_client=api)
    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "analyzed"
    assert result["file_id"] == str(file_id)
    mock_pool.assert_awaited_once()
    api.put_analysis.assert_awaited_once()
    # Verify payload shape
    awaited_call = api.put_analysis.await_args
    assert awaited_call.args[0] == file_id
    body = awaited_call.args[1]
    assert body.bpm == 128.0
    assert body.musical_key == "C minor"
    assert isinstance(body.mood, dict)
    assert isinstance(body.style, dict)


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_forwards_windows(mock_pool: AsyncMock) -> None:
    """Phase 31 ANL-01: process_file forwards analyze_file's per-window time-series.

    ``analyze_file`` returns ``{**aggregates, "windows": [fine_dict, coarse_dict]}``
    as plain dicts (Plan 04). process_file must build ``AnalysisWritePayload.windows``
    from those dicts (NOT ORM objects, preserving the D-25 import boundary) and PUT them.
    """
    windows = [
        {"tier": "fine", "window_index": 0, "start_sec": 0.0, "end_sec": 30.0, "bpm": 128.0, "musical_key": "C minor"},
        {"tier": "fine", "window_index": 1, "start_sec": 30.0, "end_sec": 60.0, "bpm": 130.0, "musical_key": "G major"},
        {"tier": "coarse", "window_index": 0, "start_sec": 0.0, "end_sec": 120.0, "mood": "happy", "style": "Electronic/House", "danceability": 0.7},
    ]
    analysis = {**MOCK_ANALYSIS, "windows": windows}
    mock_pool.return_value = analysis
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs())

    body = api.put_analysis.await_args.args[1]
    assert body.windows is not None
    assert len(body.windows) == 3
    # Built from plain dicts and shaped as AnalysisWindowPayload.
    assert [w.tier for w in body.windows] == ["fine", "fine", "coarse"]
    assert body.windows[0].window_index == 0
    assert body.windows[0].bpm == 128.0
    assert body.windows[0].musical_key == "C minor"
    assert body.windows[2].tier == "coarse"
    assert body.windows[2].mood == "happy"
    assert body.windows[2].style == "Electronic/House"
    assert body.windows[2].danceability == pytest.approx(0.7)
    # Aggregate fields still forwarded alongside windows.
    assert body.bpm == 128.0


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_defaults_windows_empty_when_absent(mock_pool: AsyncMock) -> None:
    """No ``windows`` key in the analyze_file dict -> windows defaults to [] (aggregates still sent)."""
    mock_pool.return_value = MOCK_ANALYSIS  # no "windows" key
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs())

    body = api.put_analysis.await_args.args[1]
    assert body.windows == []
    # Existing aggregate fields unaffected.
    assert body.bpm == 128.0
    assert body.musical_key == "C minor"


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_skips_non_music(mock_pool: AsyncMock) -> None:
    """Non-music file_types short-circuit before pool + HTTP call."""
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_type="jpg"))

    assert result["status"] == "skipped"
    assert result["reason"] == "not_music"
    mock_pool.assert_not_awaited()
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_propagates_pool_failure(mock_pool: AsyncMock) -> None:
    """If essentia raises, process_file re-raises (SAQ will retry)."""
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    mock_pool.side_effect = RuntimeError("essentia died")
    ctx = _make_ctx(api_client=api)

    with pytest.raises(RuntimeError, match="essentia died"):
        await process_file(ctx, **_make_payload_kwargs())
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_propagates_http_failure(mock_pool: AsyncMock) -> None:
    """If put_analysis raises (5xx after retries), process_file re-raises."""
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(side_effect=RuntimeError("server is down"))
    ctx = _make_ctx(api_client=api)

    with pytest.raises(RuntimeError, match="server is down"):
        await process_file(ctx, **_make_payload_kwargs())


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_rejects_extra_kwargs(mock_pool: AsyncMock) -> None:
    """ProcessFilePayload.extra='forbid' should reject unknown fields."""
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    ctx = _make_ctx(api_client=api)

    bad_kwargs = _make_payload_kwargs()
    bad_kwargs["bogus_field"] = "x"

    with pytest.raises(ValidationError):
        await process_file(ctx, **bad_kwargs)
    mock_pool.assert_not_awaited()
    api.put_analysis.assert_not_awaited()
