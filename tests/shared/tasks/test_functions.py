"""Tests for the HTTP-rewritten process_file task (Phase 26 Plan 11)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pydantic import ValidationError
import pytest
from saq import Status

from phaze.services.video_audio import AudioSource
from phaze.tasks.functions import (
    _features_to_mood_dict,
    _features_to_style_dict,
    process_file,
)


def _extracted(path: str) -> AudioSource:
    """An ``extract_audio_track`` result for the EXTRACTION branch (phaze-l832u).

    ``analysis_path`` and ``cleanup_path`` are the same newly-created scratch file, which is
    what the real function returns whenever it actually ran ffmpeg. The SKIP branch is the
    other shape -- ``AudioSource(<the input>, None)`` -- and it is spelled out at each of the
    few tests that are about it, because "nothing to clean up" is the whole point there.
    """
    return AudioSource(analysis_path=path, cleanup_path=path)


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
        # phaze-c8zi: REAL essentia metadata lists mood_sad ALPHABETICALLY, i.e. the
        # NEGATIVE class 'non_sad' FIRST. Positive-class selection must still pick 'sad'
        # (0.1/0.2/0.1); the old ``predictions[0]`` code would score P(non_sad)=~0.87.
        "mood_sad": {
            "musicnn_msd": [{"label": "non_sad", "prediction": 0.9}, {"label": "sad", "prediction": 0.1}],
            "musicnn_mtt": [{"label": "non_sad", "prediction": 0.8}, {"label": "sad", "prediction": 0.2}],
            "vggish": [{"label": "non_sad", "prediction": 0.9}, {"label": "sad", "prediction": 0.1}],
        },
        "genre": {
            "predictions": [
                {"label": "Electronic---House", "confidence": 0.9},
                {"label": "Electronic---Techno", "confidence": 0.5},
            ],
        },
    },
}


@pytest.fixture(autouse=True)
def _patch_agent_settings() -> Any:
    """Patch ``get_settings`` so ``process_file`` resolves AgentSettings-shaped config.

    ``process_file`` reads the agent-only ``analysis_*`` fields via ``get_settings()``
    (the module-level ``settings`` singleton is ControlSettings-typed and lacks them).
    Tests run with the default control role, so every ``process_file`` call would
    otherwise trip the agent-role guard. This default stub supplies the knobs
    ``process_file`` reads; tests that assert specific threading override the return value's
    attrs. phaze-w55w1 replaced the three Phase-43 knobs (inner timeout + the two window caps)
    with the single stall threshold.
    """
    from phaze.config import AgentSettings

    stub = MagicMock(spec=AgentSettings)
    stub.analysis_stall_timeout_sec = 1800
    # Derived on the real settings class (a property), so a spec'd MagicMock does NOT compute it --
    # it must be stamped or every throttle comparison hits a MagicMock (phaze-w55w1).
    stub.analysis_job_heartbeat_sec = 3600
    stub.analysis_progress_interval_sec = 5.0
    # phaze-3ea41: process_file now reads this to thread extraction's scratch_dir; stamped
    # None (unconfigured -- extract_audio_track falls back to tempfile.gettempdir()) so a
    # spec'd mock does not raise AttributeError the moment ANY test reaches extraction.
    stub.cloud_scratch_dir = None
    with patch("phaze.tasks.functions.get_settings", return_value=stub) as m:
        yield m


@pytest.fixture(autouse=True)
def _patch_extract_audio_track() -> Any:
    """Default extraction stub: fabricates a DISTINCT synthetic extracted path.

    phaze-3ea41, IMPLEMENTER's decision (format scope): extraction is offered EVERY file,
    not just recognized video containers, so every ``process_file`` call reaches
    ``extract_audio_track``. The operator's answer covered VIDEO containers only
    ("Probe-based, any container", 2026-08-12) -- see services/video_audio.py's D-09
    "operator decisions of record".

    Most tests in this module exercise process_file's OTHER behavior and have no interest in
    extraction at all -- patching it here keeps them oblivious to it, exactly as they were
    before this bead.

    Deliberately NOT a passthrough (returning ``read_path`` unchanged): production
    ``extract_audio_track`` ALWAYS returns a NEW scratch file distinct from its input, and
    ``process_file``'s outer ``finally`` unconditionally unlinks whatever path this returns.
    A literal passthrough would make ``extracted_audio_path == payload.scratch_path`` for any
    test that sets ``scratch_path``, so that unconditional unlink would delete the SAME file
    the scratch-copy-preservation tests below assert survives a retryable cancellation --
    correct in production (extraction never aliases its input) but a test-fixture artifact
    that would falsely break those tests, not a issue in the real code path.

    Tests that DO care about extraction itself (track selection, no-audio failure, cleanup,
    heartbeat relay -- see the phaze-3ea41 section below) override this with their own
    ``@patch("phaze.tasks.functions.extract_audio_track", ...)``, which shadows this fixture's
    patch for the duration of that test only.
    """

    async def _fake_extract(read_path: str, **_kwargs: Any) -> AudioSource:
        return _extracted(f"{read_path}.extracted.mka")

    with patch("phaze.tasks.functions.extract_audio_track", side_effect=_fake_extract) as m:
        yield m


def _make_ctx(api_client: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with an api_client mock."""
    if api_client is None:
        api_client = AsyncMock()
        api_client.put_analysis = AsyncMock(return_value=MagicMock())
    return {"api_client": api_client}


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
    # mood_sad (negative-class-first real order): positive 'sad' = (0.1+0.2+0.1)/3 = 0.133
    # NOT P(non_sad) ~ 0.867, which the old predictions[0] code would have stored.
    assert out["sad"] == pytest.approx(0.1333, rel=1e-2)


def test_features_to_mood_dict_selects_positive_class_by_label() -> None:
    """phaze-c8zi: negative-class-first sets (relaxed/sad/party) must store the POSITIVE prob.

    Mirrors the real essentia alphabetical class order where 'non_<mood>' is listed
    first. The persisted wire dict drives AI filename/path proposals, so an inverted
    value (P(non_party) stored as 'party') silently corrupts that metadata. This asserts
    the label-based selection stores each mood's true positive-class probability.
    """
    features = {
        "mood_relaxed": {
            "musicnn_msd": [{"label": "non_relaxed", "prediction": 0.05}, {"label": "relaxed", "prediction": 0.95}],
            "musicnn_mtt": [{"label": "non_relaxed", "prediction": 0.05}, {"label": "relaxed", "prediction": 0.95}],
            "vggish": [{"label": "non_relaxed", "prediction": 0.05}, {"label": "relaxed", "prediction": 0.95}],
        },
        "mood_party": {
            "musicnn_msd": [{"label": "non_party", "prediction": 0.9}, {"label": "party", "prediction": 0.1}],
            "musicnn_mtt": [{"label": "non_party", "prediction": 0.9}, {"label": "party", "prediction": 0.1}],
            "vggish": [{"label": "non_party", "prediction": 0.9}, {"label": "party", "prediction": 0.1}],
        },
    }
    out = _features_to_mood_dict(features)
    assert out is not None
    assert out["relaxed"] == pytest.approx(0.95, rel=1e-3), "must store P(relaxed), not P(non_relaxed)"
    assert out["party"] == pytest.approx(0.1, rel=1e-3), "must store P(party), not P(non_party)"


def test_positive_class_prediction_defensive_branches() -> None:
    """phaze-c8zi: _positive_class_prediction tolerates malformed prediction lists.

    Covers the never-raise contract: non-dict entries are skipped; an all-negative
    list falls back to the first well-formed entry; a list with no usable dict
    returns None.
    """
    from phaze.services.analysis_wire import _positive_class_prediction

    # Non-dict entries are skipped; the positive-labeled dict is selected.
    assert _positive_class_prediction([123, {"label": "party", "prediction": 0.7}]) == pytest.approx(0.7)
    # All labels negative -> fall back to the first well-formed entry.
    assert _positive_class_prediction([{"label": "non_a", "prediction": 0.3}, {"label": "not_b", "prediction": 0.4}]) == pytest.approx(0.3)
    # No usable dict at all -> None (never raises).
    assert _positive_class_prediction(["x", "y"]) is None


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


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
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


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
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


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
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


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_skips_non_analyzable(mock_pool: AsyncMock) -> None:
    """Non-analyzable (companion) file_types short-circuit before pool + HTTP call."""
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_type="jpg"))

    assert result["status"] == "skipped"
    assert result["reason"] == "not_analyzable"
    mock_pool.assert_not_awaited()
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_preserves_completed_non_dict_analysis(mock_pool: AsyncMock) -> None:
    """A decoded non-object result is still PUT instead of retried after completion."""
    mock_pool.return_value = ["unexpected", "analysis", "shape"]
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())

    result = await process_file(_make_ctx(api_client=api), **_make_payload_kwargs())

    assert result["status"] == "analyzed"
    api.put_analysis.assert_awaited_once()
    body = api.put_analysis.await_args.args[1]
    assert body.model_dump(exclude_unset=True) == {
        "bpm": None,
        "musical_key": None,
        "mood": None,
        "style": None,
        "danceability": None,
        "energy": None,
        "fine_windows_analyzed": None,
        "fine_windows_total": None,
        "coarse_windows_analyzed": None,
        "coarse_windows_total": None,
        "windows": [],
    }


@pytest.mark.parametrize("video_type", ["mp4", "mkv", "webm", "mov"])
@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_analyzes_video(mock_pool: AsyncMock, mock_extract: AsyncMock, video_type: str) -> None:
    """phaze-p0l9: video file_types are analyzed (not skipped), so they cross the HTTP boundary.

    A skipped video crossed no callback, so its scheduling-ledger row never cleared -- the analyze
    stage never converged and recovery re-enqueued it forever. Videos must reach put_analysis (or a
    terminal failure ack) exactly like audio.
    """
    mock_extract.return_value = _extracted("/scratch/extracted-audio.mka")
    mock_pool.return_value = {"bpm": 120.0, "musical_key": "A minor", "features": {}, "windows": []}
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_type=video_type))

    assert result["status"] == "analyzed"  # NOT skipped
    mock_extract.assert_awaited_once()
    mock_pool.assert_awaited_once()
    api.put_analysis.assert_awaited_once()


# ---------------------------------------------------------------------------
# phaze-3ea41: video-container pre-analysis audio extraction
# ---------------------------------------------------------------------------


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_video_analyzes_the_extracted_audio_path_not_the_original(mock_pool: AsyncMock, mock_extract: AsyncMock) -> None:
    """The essentia analysis driver must receive the EXTRACTED audio path, not the original
    container -- extraction is a pre-step in front of the SAME analysis call, unchanged."""
    mock_extract.return_value = _extracted("/scratch/extracted-audio.mka")
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    mock_extract.assert_awaited_once()
    assert mock_extract.await_args.args[0] == "/music/track.mp3"  # the container's read_path
    mock_pool.assert_awaited_once()
    assert mock_pool.await_args.args[0] == "/scratch/extracted-audio.mka"


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_audio_also_goes_through_extraction(mock_pool: AsyncMock, mock_extract: AsyncMock) -> None:
    """phaze-3ea41, IMPLEMENTER's decision (format scope): extraction is offered EVERY file,
    audio included -- there is no ``payload.file_type`` gate any more. The operator was asked
    which VIDEO containers to accept and answered "Probe-based, any container" (2026-08-12);
    see services/video_audio.py's D-09 "operator decisions of record". ``ffprobe`` is the sole
    authority on whether a file has an audio stream, so plain audio types reach
    ``extract_audio_track`` exactly like video ones (a lossless stream copy for an
    already-bare-audio file, per the module's decision record)."""
    mock_extract.return_value = _extracted("/scratch/extracted-audio.mka")
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs(file_type="mp3"))

    mock_extract.assert_awaited_once()
    assert mock_extract.await_args.args[0] == "/music/track.mp3"
    mock_pool.assert_awaited_once()
    assert mock_pool.await_args.args[0] == "/scratch/extracted-audio.mka"


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_video_with_no_audio_track_fails_cleanly(mock_pool: AsyncMock, mock_extract: AsyncMock) -> None:
    """Acceptance criterion: a video with NO audio track fails cleanly with a stored
    error_message -- never a jam/perpetual in-flight, and never a blind SAQ retry (the failure
    is deterministic: re-probing the same bytes reports the same absence every time)."""
    from phaze.services.video_audio import NoAudioTrackError

    mock_extract.side_effect = NoAudioTrackError("no audio stream found in '/music/track.mp3'")
    api = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    api.put_analysis = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    assert result["status"] == "analysis_failed"
    mock_pool.assert_not_awaited()  # never reaches essentia at all
    api.put_analysis.assert_not_awaited()
    api.report_analysis_failed.assert_awaited_once()
    failure = api.report_analysis_failed.await_args.args[1]
    assert failure.reason == "error"
    assert "no audio stream" in failure.error


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_corrupt_container_is_terminal_no_blind_retry(mock_pool: AsyncMock, mock_extract: AsyncMock) -> None:
    """Review correction #4: AudioExtractionError (dominantly a corrupt/truncated container --
    deterministic, same T-43-08 reasoning as AnalysisSubprocessError below) gets the SAME
    immediate-terminal treatment as NoAudioTrackError, NOT the generic retryable-aware handler
    it used to fall through to. A non-retryable job.retryable=False attempt still only reports
    ONCE (not twice) and does not re-raise for SAQ to retry."""
    from phaze.services.video_audio import AudioExtractionError

    mock_extract.side_effect = AudioExtractionError("ffmpeg audio extraction failed (exit 1): Invalid data found when processing input")
    api = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    api.put_analysis = AsyncMock()
    ctx = _make_ctx(api_client=api)
    ctx["job"] = MagicMock(retryable=True)  # even a RETRYABLE attempt must not blind-retry this

    result = await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    assert result["status"] == "analysis_failed"
    mock_pool.assert_not_awaited()  # never reaches essentia at all
    api.put_analysis.assert_not_awaited()
    api.report_analysis_failed.assert_awaited_once()
    failure = api.report_analysis_failed.await_args.args[1]
    assert failure.reason == "error"
    assert "Invalid data" in failure.error


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_threads_configured_scratch_dir_to_extraction(
    mock_pool: AsyncMock, mock_extract: AsyncMock, _patch_agent_settings: MagicMock
) -> None:
    """Review correction #5: the agent's configured ``cloud_scratch_dir`` -- a directory
    already provisioned for large landed files -- is threaded to extraction rather than
    silently falling back to bare /tmp for a multi-hour set's audio track."""
    stub = _patch_agent_settings.return_value
    stub.cloud_scratch_dir = "/data/phaze-scratch"
    mock_extract.return_value = _extracted("/data/phaze-scratch/extracted-audio.mka")
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    mock_extract.assert_awaited_once()
    assert mock_extract.await_args.kwargs["scratch_dir"] == "/data/phaze-scratch"


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_scratch_dir_none_when_unconfigured(
    mock_pool: AsyncMock, mock_extract: AsyncMock, _patch_agent_settings: MagicMock
) -> None:
    """An agent with no configured cloud_scratch_dir (a pure local fileserver agent) passes
    None through -- extract_audio_track itself falls back to tempfile.gettempdir()."""
    stub = _patch_agent_settings.return_value
    stub.cloud_scratch_dir = None
    mock_extract.return_value = _extracted("/scratch/extracted-audio.mka")
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    assert mock_extract.await_args.kwargs["scratch_dir"] is None


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_extraction_scratch_cleaned_up_on_success(mock_pool: AsyncMock, mock_extract: AsyncMock, tmp_path: Any) -> None:
    """The extracted-audio scratch file is deleted in the outer ``finally`` on SUCCESS."""
    extracted = tmp_path / "extracted-audio.mka"
    extracted.write_bytes(b"fake-audio")
    mock_extract.return_value = _extracted(str(extracted))
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    assert result["status"] == "analyzed"
    assert not extracted.exists()


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_extraction_scratch_cleaned_up_on_analysis_failure(mock_pool: AsyncMock, mock_extract: AsyncMock, tmp_path: Any) -> None:
    """The extracted-audio scratch file is ALSO deleted when the analysis step AFTER
    extraction fails (crash, stall, or a generic error) -- cleanup is unconditional."""
    extracted = tmp_path / "extracted-audio.mka"
    extracted.write_bytes(b"fake-audio")
    mock_extract.return_value = _extracted(str(extracted))
    mock_pool.side_effect = RuntimeError("essentia segfaulted")
    api = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    ctx["job"] = MagicMock(retryable=False)  # non-retryable (terminal) attempt -> report + reraise

    with pytest.raises(RuntimeError, match="essentia segfaulted"):
        await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    assert not extracted.exists()
    api.report_analysis_failed.assert_awaited_once()


@pytest.mark.parametrize("analysis_fails", [False, True], ids=["on_success", "on_failure"])
@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_never_deletes_the_archive_original_on_the_skip_branch(
    mock_pool: AsyncMock, mock_extract: AsyncMock, tmp_path: Any, analysis_fails: bool
) -> None:
    """phaze-l832u, THE archive-safety test: the input file survives the SKIP branch.

    On the local lane with no pushed copy, ``read_path`` is ``payload.original_path`` -- the
    operator's REAL ARCHIVE FILE, not a staged copy -- and the outer ``finally`` unlinks
    ``extracted_audio_path`` unconditionally. So the skip branch reporting its input as "the
    scratch path you own and delete" would silently delete the archive, one file per analysis.
    ``AudioSource.cleanup_path`` is None here, which is what makes that impossible. Asserted on
    BOTH exits, because the failure path runs the same ``finally``.
    """
    original = tmp_path / "archive-original.mp3"
    original.write_bytes(b"the operator's only copy")
    # The skip branch: analyze the input where it lies, nothing created, nothing to clean up.
    mock_extract.return_value = AudioSource(analysis_path=str(original), cleanup_path=None)
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    payload = _make_payload_kwargs()
    payload["original_path"] = str(original)

    if analysis_fails:
        mock_pool.side_effect = RuntimeError("essentia segfaulted")
        ctx["job"] = MagicMock(retryable=False)
        with pytest.raises(RuntimeError, match="essentia segfaulted"):
            await process_file(ctx, **payload)
    else:
        mock_pool.return_value = MOCK_ANALYSIS
        assert (await process_file(ctx, **payload))["status"] == "analyzed"

    assert original.exists(), "process_file deleted the file it was asked to analyze"
    assert original.read_bytes() == b"the operator's only copy"
    assert mock_pool.await_args.args[0] == str(original), "the analyzer must read the original directly on the skip branch"


@pytest.mark.parametrize("analysis_fails", [False, True], ids=["on_success", "on_failure"])
@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_never_deletes_the_archive_original_on_the_extraction_branch(
    mock_pool: AsyncMock, mock_extract: AsyncMock, tmp_path: Any, analysis_fails: bool
) -> None:
    """The same invariant on the EXTRACTION branch: the scratch file goes, the input stays."""
    original = tmp_path / "archive-original.mkv"
    original.write_bytes(b"the operator's only copy")
    extracted = tmp_path / "extracted-audio.mka"
    extracted.write_bytes(b"fake-audio")
    mock_extract.return_value = _extracted(str(extracted))
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    payload = _make_payload_kwargs(file_type="mkv")
    payload["original_path"] = str(original)

    if analysis_fails:
        mock_pool.side_effect = RuntimeError("essentia segfaulted")
        ctx["job"] = MagicMock(retryable=False)
        with pytest.raises(RuntimeError, match="essentia segfaulted"):
            await process_file(ctx, **payload)
    else:
        mock_pool.return_value = MOCK_ANALYSIS
        assert (await process_file(ctx, **payload))["status"] == "analyzed"

    assert original.exists(), "process_file deleted the file it was asked to analyze"
    assert not extracted.exists(), "the extraction scratch file leaked"


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_extraction_scratch_cleaned_up_on_no_audio_track(mock_pool: AsyncMock, mock_extract: AsyncMock) -> None:
    """extract_audio_track itself never hands back a path on NoAudioTrackError (nothing was
    produced), so the cleanup finally is simply a no-op -- assert it doesn't error either way."""
    from phaze.services.video_audio import NoAudioTrackError

    mock_extract.side_effect = NoAudioTrackError("no audio stream found")
    api = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    assert result["status"] == "analysis_failed"
    mock_pool.assert_not_awaited()


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_extraction_heartbeat_touches_the_saq_job(
    mock_pool: AsyncMock, mock_extract: AsyncMock, _patch_agent_settings: MagicMock
) -> None:
    """A long extraction must touch the SAQ job's OWN heartbeat -- run_analysis_subprocess's
    inner stall watchdog is not armed yet during extraction (the child hasn't spawned), so only
    the outer job-heartbeat deadline is at risk of expiring across a slow extraction."""

    async def _extract_with_heartbeat(_read_path: str, **kwargs: Any) -> AudioSource:
        await kwargs["heartbeat_cb"]()
        return _extracted("/scratch/extracted-audio.mka")

    mock_extract.side_effect = _extract_with_heartbeat
    mock_pool.return_value = MOCK_ANALYSIS
    job = AsyncMock()
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)
    ctx["job"] = job

    await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    job.update.assert_awaited()


@patch("phaze.tasks.functions.extract_audio_track", new_callable=AsyncMock)
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_extraction_without_saq_job_passes_no_heartbeat_cb(mock_pool: AsyncMock, mock_extract: AsyncMock) -> None:
    """A bare test/direct-call ctx (no ``ctx['job']``) must pass ``heartbeat_cb=None`` to
    extraction rather than a callback that would explode touching a nonexistent job."""
    mock_extract.return_value = _extracted("/scratch/extracted-audio.mka")
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)  # no ctx["job"]

    await process_file(ctx, **_make_payload_kwargs(file_type="mkv"))

    assert mock_extract.await_args.kwargs["heartbeat_cb"] is None


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_propagates_pool_failure(mock_pool: AsyncMock) -> None:
    """A generic essentia error with no ``ctx['job']`` re-raises and does NOT report.

    With no job in the context (``ctx.get('job') is None``) the worker cannot decide
    retryability, so it re-raises WITHOUT reporting -- SAQ owns the retry decision.
    """
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    mock_pool.side_effect = RuntimeError("essentia died")
    ctx = _make_ctx(api_client=api)  # no "job" key

    with pytest.raises(RuntimeError, match="essentia died"):
        await process_file(ctx, **_make_payload_kwargs())
    api.put_analysis.assert_not_awaited()
    api.report_analysis_failed.assert_not_awaited()


# ---------------------------------------------------------------------------
# Phase 43: terminal timeout/crash classification + retry policy + coverage
# ---------------------------------------------------------------------------


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_stall_is_terminal_and_stores_a_real_error(mock_pool: AsyncMock) -> None:
    """A stalled child is terminal: report reason='timeout' WITH the stall detail, return normally.

    The task returns ``status='analysis_failed'`` (normal return -> SAQ marks the job COMPLETE ->
    NO blind re-run of a file that wedged). ``put_analysis`` is never called.

    phaze-w55w1 adds the ``error`` half, and it is the half that matters operationally: the
    reason vocabulary stays ``"timeout"`` so every existing consumer is unchanged, but the file
    now carries a stored message saying it STOPPED MAKING PROGRESS rather than merely that it
    ran long -- the difference between "this file is too big" (no longer a thing that can
    happen) and "this file wedged" (the only thing that can).
    """
    from phaze.services.analysis_exec import AnalysisStalledError

    file_id = uuid.uuid4()
    mock_pool.side_effect = AnalysisStalledError(
        "analysis child stalled: no progress for 1800s (last stage: coarse_model)", stall_timeout=1800, last_stage="coarse_model"
    )
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result == {"file_id": str(file_id), "status": "analysis_failed"}
    api.report_analysis_failed.assert_awaited_once()
    awaited = api.report_analysis_failed.await_args
    assert awaited.args[0] == file_id
    assert awaited.args[1].reason == "timeout"
    assert "stalled" in awaited.args[1].error
    assert "coarse_model" in awaited.args[1].error, "the stored error must name the stage the child died in"
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_subprocess_crash_is_terminal(mock_pool: AsyncMock) -> None:
    """An AnalysisSubprocessError (essentia OOM/crash in the child) is terminal: report reason='crashed'.

    Phase 101: the ProcessExpired mapping, carried over to the exec-child model.
    """
    from phaze.services.analysis_exec import AnalysisSubprocessError

    file_id = uuid.uuid4()
    mock_pool.side_effect = AnalysisSubprocessError("analysis child failed (exit 1): essentia died", exit_code=1)
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result == {"file_id": str(file_id), "status": "analysis_failed"}
    api.report_analysis_failed.assert_awaited_once()
    failure = api.report_analysis_failed.await_args.args[1]
    assert failure.reason == "crashed"
    # phaze-zibn: the child's terminal error line rides along as detail so the durable
    # failure marker names the actual cause (e.g. AnalysisDecodeError vs a segfault).
    assert failure.error is not None
    assert "essentia died" in failure.error
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_zero_natural_windows_is_terminal(mock_pool: AsyncMock) -> None:
    """phaze-by30: a SUCCESSFUL analyze_file return with 0 natural windows in BOTH passes is
    still a terminal failure, not a completed analysis.

    phaze-zibn's floor inside analyze_file (services/analysis.py) only raises when at least
    one natural window existed; it is a no-op when the duration probe itself reads 0 seconds
    (an undecodable file, or a container whose header nonetheless yields zero-length audio
    properties). Without this guard, that 0/0 result would reach ``put_analysis`` and be
    recorded as a completed analysis (``windows=[]``, all-None aggregates).
    """
    file_id = uuid.uuid4()
    mock_pool.return_value = {
        **MOCK_ANALYSIS,
        "bpm": None,
        "musical_key": None,
        "mood": None,
        "style": None,
        "danceability": None,
        "features": {},
        "windows": [],
        "fine_windows_analyzed": 0,
        "fine_windows_total": 0,
        "coarse_windows_analyzed": 0,
        "coarse_windows_total": 0,
    }
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result == {"file_id": str(file_id), "status": "analysis_failed"}
    api.report_analysis_failed.assert_awaited_once()
    failure = api.report_analysis_failed.await_args.args[1]
    assert failure.reason == "crashed"
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_partial_windows_still_completes(mock_pool: AsyncMock) -> None:
    """A genuine partial result (>=1 natural window in either pass) is NOT the zero-window
    failure -- it still completes normally, matching analyze_file's own "partial success" rule.
    """
    mock_pool.return_value = {
        **MOCK_ANALYSIS,
        "fine_windows_analyzed": 1,
        "fine_windows_total": 1,
        "coarse_windows_analyzed": 0,
        "coarse_windows_total": 0,
    }
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs())

    assert result["status"] == "analyzed"
    api.put_analysis.assert_awaited_once()
    api.report_analysis_failed.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_missing_coverage_fields_not_treated_as_zero(mock_pool: AsyncMock) -> None:
    """Absent coverage fields (older/mocked analyzers) mean "unknown", not "zero" -- must NOT
    trip the zero-window terminal-failure guard (regression guard for the phaze-by30 fix).
    """
    mock_pool.return_value = MOCK_ANALYSIS  # no coverage keys at all
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs())

    assert result["status"] == "analyzed"
    api.put_analysis.assert_awaited_once()
    api.report_analysis_failed.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_timeout_report_post_failure_stays_terminal(mock_pool: AsyncMock) -> None:
    """A failed terminal-failure report POST must NOT convert the timeout outcome into a retry.

    phaze-x3dg: the report used to run unprotected inside the outer try, so a transient
    control-plane outage escaped into the generic handler and SAQ blindly re-ran a
    deterministically-doomed multi-hour analysis. The task must still return the terminal
    status dict (SAQ marks the job COMPLETE) with reason='timeout' attempted exactly once —
    no reason='error' fallback report, no re-raise.
    """
    file_id = uuid.uuid4()
    mock_pool.side_effect = TimeoutError("inner timeout")
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock(side_effect=RuntimeError("app server mid-restart"))
    ctx = _make_ctx(api_client=api)
    ctx["job"] = MagicMock(retryable=True)  # retries left — the escape path SAQ would have taken

    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result == {"file_id": str(file_id), "status": "analysis_failed"}
    api.report_analysis_failed.assert_awaited_once()
    assert api.report_analysis_failed.await_args.args[1].reason == "timeout"
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_crash_report_post_failure_stays_terminal(mock_pool: AsyncMock) -> None:
    """Same delivery guard for the child-crash branch: a failed reason='crashed' report
    still ends terminally instead of re-raising into the generic retry path (phaze-x3dg)."""
    from phaze.services.analysis_exec import AnalysisSubprocessError

    file_id = uuid.uuid4()
    mock_pool.side_effect = AnalysisSubprocessError("analysis child failed (exit 1)", exit_code=1)
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock(side_effect=RuntimeError("app server mid-restart"))
    ctx = _make_ctx(api_client=api)
    ctx["job"] = MagicMock(retryable=True)

    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result == {"file_id": str(file_id), "status": "analysis_failed"}
    api.report_analysis_failed.assert_awaited_once()
    assert api.report_analysis_failed.await_args.args[1].reason == "crashed"
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_non_retryable_generic_error_reports_then_raises(mock_pool: AsyncMock) -> None:
    """A generic error on the LAST attempt (``job.retryable is False``) reports then re-raises.

    ``report_analysis_failed(reason='error')`` is called with a truncated detail, then the
    exception propagates so SAQ records the failed (final) attempt.
    """
    mock_pool.side_effect = RuntimeError("boom")
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)
    ctx["job"] = MagicMock(retryable=False)

    with pytest.raises(RuntimeError, match="boom"):
        await process_file(ctx, **_make_payload_kwargs())

    api.report_analysis_failed.assert_awaited_once()
    payload = api.report_analysis_failed.await_args.args[1]
    assert payload.reason == "error"
    assert payload.error is not None and "boom" in payload.error
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_terminal_ack_failure_reraises_original_error(mock_pool: AsyncMock) -> None:
    """WR-01 (phaze-ys4d): on the TERMINAL attempt, if report_analysis_failed ALSO raises (E2),
    the ORIGINAL task error (E1) must propagate -- not the ack error. Mirrors the already-guarded
    siblings (fingerprint.py, scan.py, metadata_extraction.py). The ack is awaited once, failure
    swallowed by ``_report_terminal_failure``."""
    mock_pool.side_effect = RuntimeError("boom")
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock(side_effect=RuntimeError("ack POST failed"))
    ctx = _make_ctx(api_client=api)
    ctx["job"] = MagicMock(retryable=False)

    # E1 (the analysis RuntimeError) propagates -- NOT E2 (the ack RuntimeError).
    with pytest.raises(RuntimeError, match="boom"):
        await process_file(ctx, **_make_payload_kwargs())

    api.report_analysis_failed.assert_awaited_once()
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_retryable_generic_error_raises_without_reporting(mock_pool: AsyncMock) -> None:
    """A generic error with retries left (``job.retryable is True``) re-raises WITHOUT reporting.

    Transient errors must retry (retries=2 -> one real retry), so the worker stays silent and
    lets SAQ re-run; reporting only happens on the terminal attempt.
    """
    mock_pool.side_effect = RuntimeError("transient")
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    ctx["job"] = MagicMock(retryable=True)

    with pytest.raises(RuntimeError, match="transient"):
        await process_file(ctx, **_make_payload_kwargs())

    api.report_analysis_failed.assert_not_awaited()
    api.put_analysis.assert_not_awaited()


# ---------------------------------------------------------------------------
# phaze-2cqx: SAQ cancellation (job-net timeout / worker shutdown) must not bypass
# the retryable-preserve guard and delete the pushed scratch copy a retry needs.
# ---------------------------------------------------------------------------


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_cancelled_retryable_job_preserves_scratch_copy(mock_pool: AsyncMock, tmp_path: Any) -> None:
    """CancelledError on a retryable job must preserve the pushed scratch copy.

    ``asyncio.CancelledError`` is a ``BaseException`` and is never caught by the generic
    ``except Exception`` that normally flips ``cleanup_scratch`` False for a retryable
    attempt. Without a dedicated guard the outer ``finally`` still unlinks the scratch
    copy, stranding the SAQ in-place retry with a FileNotFoundError -> push-mismatch
    round-trip (CR-01).
    """
    scratch_file = tmp_path / "scratch-copy.mp3"
    scratch_file.write_bytes(b"synthetic-audio-bytes")
    mock_pool.side_effect = asyncio.CancelledError()
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    ctx["job"] = MagicMock(retryable=True, status=Status.ACTIVE)

    kwargs = _make_payload_kwargs()
    kwargs["scratch_path"] = str(scratch_file)

    with pytest.raises(asyncio.CancelledError):
        await process_file(ctx, **kwargs)

    assert scratch_file.exists(), "scratch copy must survive so the in-place SAQ retry can re-verify and analyze it"
    api.report_analysis_failed.assert_not_awaited()
    api.put_analysis.assert_not_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_cancelled_aborting_job_cleans_scratch_copy(mock_pool: AsyncMock, tmp_path: Any) -> None:
    """CancelledError on an ABORTING job (saq Worker.abort(), terminal) still cleans up.

    ``Worker.abort()`` cancels the job task too, but the outcome is terminal (``finish_abort``,
    no retry) -- preserving the scratch copy there would leak disk (T-50-scratch-dos), the
    threat the ``finally`` cleanup exists to bound.
    """
    scratch_file = tmp_path / "scratch-copy.mp3"
    scratch_file.write_bytes(b"synthetic-audio-bytes")
    mock_pool.side_effect = asyncio.CancelledError()
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    ctx["job"] = MagicMock(retryable=True, status=Status.ABORTING)

    kwargs = _make_payload_kwargs()
    kwargs["scratch_path"] = str(scratch_file)

    with pytest.raises(asyncio.CancelledError):
        await process_file(ctx, **kwargs)

    assert not scratch_file.exists(), "an aborting (terminal) job must still reclaim scratch disk"


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_cancelled_no_job_in_ctx_cleans_scratch_copy(mock_pool: AsyncMock, tmp_path: Any) -> None:
    """CancelledError with no ``ctx['job']`` (bare test ctx) defaults to cleanup — nothing will retry it."""
    scratch_file = tmp_path / "scratch-copy.mp3"
    scratch_file.write_bytes(b"synthetic-audio-bytes")
    mock_pool.side_effect = asyncio.CancelledError()
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)  # no "job" key

    kwargs = _make_payload_kwargs()
    kwargs["scratch_path"] = str(scratch_file)

    with pytest.raises(asyncio.CancelledError):
        await process_file(ctx, **kwargs)

    assert not scratch_file.exists()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_threads_the_stall_threshold_and_no_caps(mock_pool: AsyncMock, _patch_agent_settings: MagicMock) -> None:
    """The success path passes ``stall_timeout`` from AgentSettings -- and no wall clock, no caps.

    The negative half is the load-bearing one (phaze-w55w1): a ``timeout=`` kwarg reaching the
    driver would silently restore the elapsed-time kill this bead removed, and it would not fail
    any other test in this file.
    """
    stub = _patch_agent_settings.return_value
    stub.analysis_stall_timeout_sec = 1234
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs())

    mock_pool.assert_awaited_once()
    call = mock_pool.await_args
    assert call.kwargs["stall_timeout"] == 1234
    for gone in ("timeout", "fine_cap", "coarse_cap"):
        assert gone not in call.kwargs


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_touches_the_saq_job_heartbeat_on_analysis_progress(mock_pool: AsyncMock, _patch_agent_settings: MagicMock) -> None:
    """Each liveness heartbeat from the child touches the SAQ job (phaze-w55w1).

    ``process_file`` runs ``timeout=0`` + a job ``heartbeat``, so ``Job.stuck`` reads ``touched``
    -- and nothing else refreshes it during an analysis that can legitimately run for hours.
    Without this relay the sweep would abort every long file at the stall threshold, which is
    the same class of failure as the wall clock it replaced, just arriving from the broker.
    """
    stub = _patch_agent_settings.return_value
    stub.analysis_progress_interval_sec = 0.0
    mock_pool.return_value = MOCK_ANALYSIS
    job = AsyncMock()
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)
    ctx["job"] = job

    async def _beat_then_return(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["heartbeat_cb"]("fine_decode", 0, 9)
        kwargs["heartbeat_cb"]("fine", 1, 9)
        kwargs["heartbeat_cb"]("coarse_model", 1, 9)
        return MOCK_ANALYSIS

    mock_pool.side_effect = _beat_then_return

    await process_file(ctx, **_make_payload_kwargs())

    # The FIRST beat always touches (last_touch starts None), which is what proves the relay is
    # wired to the heartbeat channel at all. The three beats here land in the same instant, so the
    # cadence cap collapses the rest -- that collapsing is the subject of the throttle tests below,
    # not of this one.
    assert job.update.await_count >= 1
    job.update.assert_awaited()


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_saq_heartbeat_touch_is_throttled(mock_pool: AsyncMock, _patch_agent_settings: MagicMock) -> None:
    """A burst of heartbeats collapses to ONE broker write (phaze-w55w1).

    Exhaustive analysis emits far more liveness events than the capped version did -- a 12-hour
    file completes thousands of windows and hundreds of model sweeps -- and each untouched
    ``job.update()`` is a Postgres write on the shared broker. The relay therefore rides the same
    throttle gate as the progress POST: enough to keep ``touched`` fresh against a 30-minute
    heartbeat, nowhere near enough to be a write storm.
    """
    stub = _patch_agent_settings.return_value
    stub.analysis_progress_interval_sec = 5.0
    stub.analysis_job_heartbeat_sec = 3600
    job = AsyncMock()

    async def _burst_then_return(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        for i in range(25):
            kwargs["heartbeat_cb"]("fine", i, 25)
        return MOCK_ANALYSIS

    mock_pool.side_effect = _burst_then_return
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)
    ctx["job"] = job

    await process_file(ctx, **_make_payload_kwargs())

    assert job.update.await_count == 1, "25 heartbeats inside one throttle window must collapse to a single touch"


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_saq_touch_cadence_is_capped_by_the_heartbeat_deadline_not_the_display_knob(
    mock_pool: AsyncMock, _patch_agent_settings: MagicMock
) -> None:
    """Raising the UI progress interval must NOT starve the broker liveness touch (phaze-w55w1).

    `analysis_progress_interval_sec` is a DISPLAY knob. An operator raising it to quieten the
    progress bar has no reason to expect it to affect whether SAQ considers the job alive -- but
    under a shared throttle it would, and setting it above the job's heartbeat deadline would get
    every healthy multi-hour analysis swept. So the touch cadence is capped independently at a
    third of the deadline: the display knob can only tighten it, never loosen it past safety.
    """
    stub = _patch_agent_settings.return_value
    stub.analysis_progress_interval_sec = 86400.0  # absurd, and it must not matter
    stub.analysis_job_heartbeat_sec = 3600
    job = AsyncMock()
    beats = 12
    fake_now = iter(float(i * 601) for i in range(beats + 4))  # 601s apart: past 3600/3, under 86400

    async def _spaced_beats(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        for i in range(beats):
            kwargs["heartbeat_cb"]("fine", i, beats)
        return MOCK_ANALYSIS

    mock_pool.side_effect = _spaced_beats
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)
    ctx["job"] = job

    with patch("phaze.tasks.functions.time.monotonic", lambda: next(fake_now)):
        await process_file(ctx, **_make_payload_kwargs())

    # Under the old shared throttle this would be exactly 1 touch in 2 hours of simulated beats,
    # against a 3600s deadline -- i.e. swept. The cap makes every beat past 1200s eligible.
    assert job.update.await_count > 1, "the display knob must not be able to starve the liveness touch"


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_heartbeat_relay_is_inert_without_a_saq_job(mock_pool: AsyncMock, _patch_agent_settings: MagicMock) -> None:
    """A bare ctx (direct call / test harness) has no ``job`` -- the relay must no-op, not raise."""
    stub = _patch_agent_settings.return_value
    stub.analysis_progress_interval_sec = 0.0

    async def _beat_then_return(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["heartbeat_cb"]("fine", 1, 2)
        return MOCK_ANALYSIS

    mock_pool.side_effect = _beat_then_return
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())

    result = await process_file(_make_ctx(api_client=api), **_make_payload_kwargs())

    assert result["status"] == "analyzed"


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_saq_heartbeat_failure_never_fails_the_job(mock_pool: AsyncMock, _patch_agent_settings: MagicMock) -> None:
    """A broker hiccup on ``job.update()`` is swallowed: liveness reporting never kills an analysis."""
    stub = _patch_agent_settings.return_value
    stub.analysis_progress_interval_sec = 0.0
    job = AsyncMock()
    job.update.side_effect = RuntimeError("broker pool hiccup")

    async def _beat_then_return(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["heartbeat_cb"]("fine", 1, 2)
        return MOCK_ANALYSIS

    mock_pool.side_effect = _beat_then_return
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)
    ctx["job"] = job

    result = await process_file(ctx, **_make_payload_kwargs())

    assert result["status"] == "analyzed"


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_forwards_coverage_fields(mock_pool: AsyncMock) -> None:
    """The four progress counts from analyze_file are forwarded into AnalysisWritePayload.

    The counts are equal here because exhaustive analysis makes them equal on a healthy file
    (phaze-w55w1); a shortfall now means individual windows failed to decode, not that coverage
    was deliberately skipped.
    """
    analysis = {
        **MOCK_ANALYSIS,
        "fine_windows_analyzed": 60,
        "fine_windows_total": 60,
        "coarse_windows_analyzed": 30,
        "coarse_windows_total": 30,
    }
    mock_pool.return_value = analysis
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs())

    body = api.put_analysis.await_args.args[1]
    assert body.fine_windows_analyzed == 60
    assert body.fine_windows_total == 60
    assert body.coarse_windows_analyzed == 30
    assert body.coarse_windows_total == 30


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_coverage_fields_default_none_when_absent(mock_pool: AsyncMock) -> None:
    """No coverage keys in the analyze_file dict -> coverage fields stay None (partial-PUT)."""
    mock_pool.return_value = MOCK_ANALYSIS  # no coverage keys
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs())

    body = api.put_analysis.await_args.args[1]
    assert body.fine_windows_analyzed is None
    assert body.fine_windows_total is None
    assert body.coarse_windows_analyzed is None
    assert body.coarse_windows_total is None


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_propagates_http_failure(mock_pool: AsyncMock) -> None:
    """If put_analysis raises (5xx after retries), process_file re-raises."""
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(side_effect=RuntimeError("server is down"))
    ctx = _make_ctx(api_client=api)

    with pytest.raises(RuntimeError, match="server is down"):
        await process_file(ctx, **_make_payload_kwargs())


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
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


# ---------------------------------------------------------------------------
# Phase 57.1 (PROG-01) / Phase 101 (OBS-03): the local + A1 lane progress bridge —
# the driver invokes progress_cb ON the loop; the parent throttles + final-flushes.
# ---------------------------------------------------------------------------


def _fake_pool_emitting(counts: list[tuple[int, ...]], result: dict[str, Any] | None = None, raise_exc: BaseException | None = None):  # type: ignore[no-untyped-def]
    """Build a fake ``run_analysis_subprocess`` that invokes ``progress_cb`` then returns/raises.

    Mirrors the driver call shape ``run_analysis_subprocess(file_path, models_dir, *,
    progress_cb=..., **kwargs)``; the emitted counts go through the REAL parent-side
    throttle + final-flush bridge in ``_run_analysis_with_progress``.

    Each count is a 2-tuple ``(fine_analyzed, fine_total)`` -- coarse pinned at ``(0, 0)``,
    i.e. no coarse-tier work reported, which is what every EXISTING call site below wants --
    or a 4-tuple ``(fine_analyzed, fine_total, coarse_analyzed, coarse_total)`` for a test
    exercising the coarse-tier bridge explicitly (phaze-bp9kz widened ``progress_cb`` from
    fine-only, WORK-04, to both tiers).
    """

    async def _fake(file_path, models_dir, *, progress_cb=None, **kwargs):  # type: ignore[no-untyped-def]
        assert progress_cb is not None, "the bridge must thread a progress_cb into the driver"
        for count in counts:
            if len(count) == 2:
                analyzed, total = count
                progress_cb(analyzed, total, 0, 0)
            else:
                progress_cb(*count)
        if raise_exc is not None:
            raise raise_exc
        return result if result is not None else MOCK_ANALYSIS

    return _fake


@patch("phaze.tasks.functions.run_analysis_subprocess")
async def test_process_file_posts_advancing_progress_and_final_flush(mock_pool: MagicMock, _patch_agent_settings: MagicMock) -> None:
    """The drainer POSTs advancing counts for BOTH tiers and always flushes the final count.

    phaze-bp9kz widened ``progress_cb`` from fine-only (WORK-04) to both tiers on this lane
    too (the SAQ worker, not just the pod). The fine tier finishing (``3, 3, 0, 1``) must not
    itself look like completion — coarse hasn't started — so the true final is the last
    4-tuple, once coarse also reaches its total.
    """
    _patch_agent_settings.return_value.analysis_progress_interval_sec = 0.0  # no throttle: every count posts
    mock_pool.side_effect = _fake_pool_emitting([(0, 3, 0, 1), (1, 3, 0, 1), (2, 3, 0, 1), (3, 3, 0, 1), (3, 3, 1, 1)])

    file_id = uuid.uuid4()
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.post_analysis_progress = AsyncMock(return_value=None)
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "analyzed"
    api.put_analysis.assert_awaited_once()  # completion path unchanged
    posted = [
        (call.args[1].fine_windows_analyzed, call.args[1].fine_windows_total, call.args[1].coarse_windows_analyzed, call.args[1].coarse_windows_total)
        for call in api.post_analysis_progress.await_args_list
    ]
    assert posted, "at least one mid-flight progress POST must land"
    fine_seq = [fa for fa, _ft, _ca, _ct in posted]
    coarse_seq = [ca for _fa, _ft, ca, _ct in posted]
    assert fine_seq == sorted(fine_seq), "fine progress counts must be non-decreasing"
    assert coarse_seq == sorted(coarse_seq), "coarse progress counts must be non-decreasing"
    assert all(ft == 3 and ct == 1 for _fa, ft, _ca, ct in posted), "both denominators must be the natural totals"
    assert posted[0][0] == 0, "the START count is posted first"
    # 100% (both tiers done) is unreachable before the LAST POST.
    assert not any(fa == 3 and ca == 1 for fa, _ft, ca, _ct in posted[:-1])
    assert posted[-1] == (3, 3, 1, 1), "the final count is flushed"


@patch("phaze.tasks.functions.run_analysis_subprocess")
async def test_process_file_progress_throttle_collapses_bursts(mock_pool: MagicMock, _patch_agent_settings: MagicMock) -> None:
    """A long throttle interval collapses a burst to the first post + the final flush."""
    _patch_agent_settings.return_value.analysis_progress_interval_sec = 10_000.0  # effectively never re-post
    mock_pool.side_effect = _fake_pool_emitting([(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)])

    file_id = uuid.uuid4()
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.post_analysis_progress = AsyncMock(return_value=None)
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "analyzed"
    posted = [(call.args[1].fine_windows_analyzed, call.args[1].fine_windows_total) for call in api.post_analysis_progress.await_args_list]
    # First emission always posts (initial last_post=0); the rest are throttled; the final count is flushed.
    assert posted == [(0, 4), (4, 4)]


@patch("phaze.tasks.functions.run_analysis_subprocess")
async def test_process_file_progress_kill_safe_on_timeout(mock_pool: MagicMock, _patch_agent_settings: MagicMock) -> None:
    """A killed child (TimeoutError from the driver) tears the progress bridge down promptly (no hang)."""
    _patch_agent_settings.return_value.analysis_progress_interval_sec = 0.0
    mock_pool.side_effect = _fake_pool_emitting([(0, 5), (1, 5)], raise_exc=TimeoutError())

    file_id = uuid.uuid4()
    api = AsyncMock()
    api.report_analysis_failed = AsyncMock()
    api.post_analysis_progress = AsyncMock(return_value=None)
    ctx = _make_ctx(api_client=api)

    # The whole call must complete well under the drainer teardown deadline — proving no hang.
    result = await asyncio.wait_for(process_file(ctx, **_make_payload_kwargs(file_id=file_id)), timeout=8.0)

    assert result["status"] == "analysis_failed"
    # Terminal mapping unchanged: a SIGKILL still reports "timeout".
    failure = api.report_analysis_failed.await_args.args[1]
    assert failure.reason == "timeout"


@patch("phaze.tasks.functions.run_analysis_subprocess")
async def test_process_file_progress_post_failure_swallowed(mock_pool: MagicMock, _patch_agent_settings: MagicMock) -> None:
    """A failing progress POST never changes the terminal result (best-effort, D-16)."""
    from phaze.services.agent_client import AgentApiServerError

    _patch_agent_settings.return_value.analysis_progress_interval_sec = 0.0
    mock_pool.side_effect = _fake_pool_emitting([(0, 2), (1, 2), (2, 2)])

    file_id = uuid.uuid4()
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.post_analysis_progress = AsyncMock(side_effect=AgentApiServerError("503 after retries"))
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    # Progress POST raised on every call, yet the file still completes normally.
    assert result["status"] == "analyzed"
    api.put_analysis.assert_awaited_once()
    assert api.post_analysis_progress.await_count >= 1


def test_analysis_child_path_imports_no_agent_client() -> None:
    """Only plain int counts cross into the child: analysis.py imports no httpx/agent_client."""
    from pathlib import Path

    import phaze.services.analysis as analysis_mod

    src = Path(analysis_mod.__file__).read_text(encoding="utf-8")
    assert "agent_client" not in src
    assert "import httpx" not in src


@patch("phaze.tasks.functions.run_analysis_subprocess")
async def test_process_file_respects_ctx_analysis_semaphore(mock_pool: MagicMock, _patch_agent_settings: MagicMock) -> None:
    """Phase 101: a ctx-provided analysis_semaphore is held around the analysis call.

    The semaphore replaces the pebble pool's worker_process_pool_size concurrency
    bound; a zero-permit semaphore must therefore block the analysis from starting.
    """
    _patch_agent_settings.return_value.analysis_progress_interval_sec = 0.0
    mock_pool.side_effect = _fake_pool_emitting([(0, 1), (1, 1)])

    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.post_analysis_progress = AsyncMock(return_value=None)
    ctx = _make_ctx(api_client=api)
    ctx["analysis_semaphore"] = asyncio.Semaphore(0)  # no permits: analysis must not start

    task = asyncio.ensure_future(process_file(ctx, **_make_payload_kwargs()))
    await asyncio.sleep(0.05)
    assert not task.done(), "analysis must wait for a semaphore permit"
    mock_pool.assert_not_called()

    ctx["analysis_semaphore"].release()  # grant the permit; the job completes normally
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result["status"] == "analyzed"
    mock_pool.assert_called_once()
