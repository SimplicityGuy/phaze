"""Tests for the HTTP-rewritten process_file task (Phase 26 Plan 11)."""

from __future__ import annotations

import asyncio
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
    otherwise trip the agent-role guard. This default stub supplies the three Phase-43
    knobs; tests that assert specific threading override the return value's attrs.
    """
    from phaze.config import AgentSettings

    stub = MagicMock(spec=AgentSettings)
    stub.analysis_inner_timeout_sec = 6600
    stub.analysis_fine_cap = 60
    stub.analysis_coarse_cap = 30
    stub.analysis_progress_interval_sec = 5.0
    with patch("phaze.tasks.functions.get_settings", return_value=stub) as m:
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


@pytest.mark.parametrize("video_type", ["mp4", "mkv", "webm", "mov"])
@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_analyzes_video(mock_pool: AsyncMock, video_type: str) -> None:
    """phaze-p0l9: video file_types are analyzed (not skipped), so they cross the HTTP boundary.

    A skipped video crossed no callback, so its scheduling-ledger row never cleared -- the analyze
    stage never converged and recovery re-enqueued it forever. Videos must reach put_analysis (or a
    terminal failure ack) exactly like audio.
    """
    mock_pool.return_value = {"bpm": 120.0, "musical_key": "A minor", "features": {}, "windows": []}
    api = AsyncMock()
    api.put_analysis = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_type=video_type))

    assert result["status"] == "analyzed"  # NOT skipped
    mock_pool.assert_awaited_once()
    api.put_analysis.assert_awaited_once()


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
async def test_process_file_timeout_is_terminal(mock_pool: AsyncMock) -> None:
    """An inner pebble TimeoutError is terminal: report reason='timeout', return normally.

    The task returns ``status='analysis_failed'`` (normal return -> SAQ marks the job
    COMPLETE -> NO retry of a deterministically-too-long file). ``put_analysis`` is
    never called.
    """
    file_id = uuid.uuid4()
    mock_pool.side_effect = TimeoutError("inner pebble timeout")
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


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_threads_inner_timeout_and_caps(mock_pool: AsyncMock, _patch_agent_settings: MagicMock) -> None:
    """The success path passes the inner timeout + 60/30 caps from AgentSettings to the pool."""
    stub = _patch_agent_settings.return_value
    stub.analysis_inner_timeout_sec = 7100
    stub.analysis_fine_cap = 50
    stub.analysis_coarse_cap = 25
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs())

    mock_pool.assert_awaited_once()
    call = mock_pool.await_args
    assert call.kwargs["timeout"] == 7100
    assert call.kwargs["fine_cap"] == 50
    assert call.kwargs["coarse_cap"] == 25


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_payload_caps_override_agent_settings(mock_pool: AsyncMock, _patch_agent_settings: MagicMock) -> None:
    """Phase 44: payload fine_cap/coarse_cap (incl. 0) override the AgentSettings defaults."""
    stub = _patch_agent_settings.return_value
    stub.analysis_fine_cap = 60
    stub.analysis_coarse_cap = 30
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    kwargs = _make_payload_kwargs()
    kwargs["fine_cap"] = 0
    kwargs["coarse_cap"] = 0
    await process_file(ctx, **kwargs)

    call = mock_pool.await_args
    # 0 is a meaningful override (analyze-ALL no-op), NOT the 60/30 AgentSettings default.
    assert call.kwargs["fine_cap"] == 0
    assert call.kwargs["coarse_cap"] == 0


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_caps_fall_back_to_agent_settings_when_none(mock_pool: AsyncMock, _patch_agent_settings: MagicMock) -> None:
    """Phase 44: absent payload caps (None) fall back to the AgentSettings 60/30 defaults exactly as before."""
    stub = _patch_agent_settings.return_value
    stub.analysis_fine_cap = 55
    stub.analysis_coarse_cap = 22
    mock_pool.return_value = MOCK_ANALYSIS
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    # _make_payload_kwargs omits fine_cap/coarse_cap -> ProcessFilePayload defaults them None.
    await process_file(ctx, **_make_payload_kwargs())

    call = mock_pool.await_args
    assert call.kwargs["fine_cap"] == 55
    assert call.kwargs["coarse_cap"] == 22


@patch("phaze.tasks.functions.run_analysis_subprocess", new_callable=AsyncMock)
async def test_process_file_forwards_coverage_fields(mock_pool: AsyncMock) -> None:
    """The five coverage fields from analyze_file are forwarded into AnalysisWritePayload."""
    analysis = {
        **MOCK_ANALYSIS,
        "fine_windows_analyzed": 42,
        "fine_windows_total": 60,
        "coarse_windows_analyzed": 18,
        "coarse_windows_total": 30,
        "sampled": True,
    }
    mock_pool.return_value = analysis
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    await process_file(ctx, **_make_payload_kwargs())

    body = api.put_analysis.await_args.args[1]
    assert body.fine_windows_analyzed == 42
    assert body.fine_windows_total == 60
    assert body.coarse_windows_analyzed == 18
    assert body.coarse_windows_total == 30
    assert body.sampled is True


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
    assert body.sampled is None


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


def _fake_pool_emitting(counts: list[tuple[int, int]], result: dict[str, Any] | None = None, raise_exc: BaseException | None = None):  # type: ignore[no-untyped-def]
    """Build a fake ``run_analysis_subprocess`` that invokes ``progress_cb`` then returns/raises.

    Mirrors the driver call shape ``run_analysis_subprocess(file_path, models_dir, *,
    progress_cb=..., **kwargs)``; the emitted counts go through the REAL parent-side
    throttle + final-flush bridge in ``_run_analysis_with_progress``.
    """

    async def _fake(file_path, models_dir, *, progress_cb=None, **kwargs):  # type: ignore[no-untyped-def]
        assert progress_cb is not None, "the bridge must thread a progress_cb into the driver"
        for analyzed, total in counts:
            progress_cb(analyzed, total)
        if raise_exc is not None:
            raise raise_exc
        return result if result is not None else MOCK_ANALYSIS

    return _fake


@patch("phaze.tasks.functions.run_analysis_subprocess")
async def test_process_file_posts_advancing_progress_and_final_flush(mock_pool: MagicMock, _patch_agent_settings: MagicMock) -> None:
    """The drainer POSTs advancing (analyzed,total) counts and always flushes the final count."""
    _patch_agent_settings.return_value.analysis_progress_interval_sec = 0.0  # no throttle: every count posts
    mock_pool.side_effect = _fake_pool_emitting([(0, 3), (1, 3), (2, 3), (3, 3)])

    file_id = uuid.uuid4()
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.post_analysis_progress = AsyncMock(return_value=None)
    ctx = _make_ctx(api_client=api)

    result = await process_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "analyzed"
    api.put_analysis.assert_awaited_once()  # completion path unchanged
    # Extract the (analyzed, total) of every progress POST.
    posted = [(call.args[1].fine_windows_analyzed, call.args[1].fine_windows_total) for call in api.post_analysis_progress.await_args_list]
    assert posted, "at least one mid-flight progress POST must land"
    analyzed_seq = [a for a, _t in posted]
    assert analyzed_seq == sorted(analyzed_seq), "progress counts must be non-decreasing"
    assert all(total == 3 for _a, total in posted), "denominator must be the fine_windows_total"
    assert posted[0][0] == 0, "the START count (0, N) is posted first"
    assert posted[-1] == (3, 3), "the final count is flushed"


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
    """Only an (int,int) count crosses into the child: analysis.py imports no httpx/agent_client."""
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
