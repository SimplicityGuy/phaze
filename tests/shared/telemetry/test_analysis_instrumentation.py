"""Analysis instrumentation, against REAL essentia and REAL audio.

ADR-0012 rule 3 is the whole reason this file exists in this shape. The claim under test is
about the analysis pipeline's behaviour, and *"a claim about real essentia is not
discharged by a mocked one"* -- the repo carries
``test_repeated_gated_chunk_decodes_do_not_grow_peak_rss`` and
``test_the_chunk_decode_leaves_no_connected_network_behind`` precisely because a mocked
long-file test once shipped a P0. So ``analyze_file`` is called for real, on a real WAV,
through the real chunk loop.

**What this file does NOT discharge, stated rather than left to be assumed.** The 34-graph
coarse sweep needs 3.26 GB of TensorFlow weights, which are not in CI. Every assertion here
about the model metrics is therefore made in the coarse tier's FAILURE path (no weights ->
per-window failure isolation), which exercises the counters and the graph-build timing but
NOT a successful inference. The successful-inference and per-model-cost claims are
discharged by ``test_analysis_with_real_models`` below, which SKIPS unless
``PHAZE_TEST_MODELS_DIR`` points at a real model set -- and by the measured run recorded in
``docs/telemetry/measurements/``.
"""

from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING
import wave

import numpy as np
import pytest

from phaze.services.analysis import analyze_file
from phaze.telemetry.catalogue import FORBIDDEN_LABEL_SUBSTRINGS


if TYPE_CHECKING:
    from pathlib import Path

    from tests.shared.telemetry.conftest import TelemetrySink

_SOURCE_RATE = 8000
#: Long enough for several fine windows and more than one coarse window, short enough that
#: the whole file decodes in seconds. Two summed sines survive resampling legibly.
_TOTAL_SEC = 200
_FINE_WINDOW_SEC = 30
_COARSE_WINDOW_SEC = 60


@pytest.fixture
def audio(tmp_path: Path) -> str:
    path = str(tmp_path / "sine.wav")
    t = np.arange(_SOURCE_RATE * _TOTAL_SEC) / _SOURCE_RATE
    samples = 0.4 * np.sin(2 * math.pi * 220 * t) + 0.3 * np.sin(2 * math.pi * 331 * t)
    with wave.open(path, "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SOURCE_RATE)
        handle.writeframes((samples * 32767).astype("<i2").tobytes())
    return path


def _run(audio_path: str, models_dir: str) -> dict[str, object]:
    return analyze_file(
        audio_path,
        models_dir,
        fine_window_sec=_FINE_WINDOW_SEC,
        coarse_window_sec=_COARSE_WINDOW_SEC,
    )


def test_the_coarse_tier_is_broken_down_per_chunk(telemetry_sink: TelemetrySink, audio: str, tmp_path: Path) -> None:
    """phaze-m1drf.1 acceptance 1: decode / graph work / inference / derive, per chunk.

    This is the 94.69% of a production analysis that emitted NOTHING outside the process
    before this bead -- ``AnalysisSignals.progress`` is fine-tier-only by design, so the UI
    bar reached 100% at 5.31% of the job and then went silent for 1 h 52 m on a measured
    run (phaze-zaf2l section 3b).
    """
    _run(audio, str(tmp_path / "no-models"))

    span_names = set(telemetry_sink.span_names())
    assert {"analysis.file", "analysis.tier", "analysis.chunk", "analysis.chunk.decode", "analysis.chunk.derive"} <= span_names

    tiers = {frozenset(attrs.items()) for attrs in telemetry_sink.attribute_sets("phaze.analysis.tier.duration")}
    assert tiers == {frozenset({("tier", "fine")}), frozenset({("tier", "coarse")})}, "both tiers must be comparable"

    decode_tiers = {attrs["tier"] for attrs in telemetry_sink.attribute_sets("phaze.analysis.chunk.decode.duration")}
    assert decode_tiers == {"fine", "coarse"}

    assert telemetry_sink.count("phaze.analysis.chunk.derive.duration") >= 1
    assert telemetry_sink.total("phaze.analysis.chunks") >= 2, "at least one chunk per tier"


def test_fine_windows_are_counted_and_measured(telemetry_sink: TelemetrySink, audio: str, tmp_path: Path) -> None:
    """The fine tier gets the treatment the coarse tier gets, so the two can be compared."""
    result = _run(audio, str(tmp_path / "no-models"))

    analyzed = [attrs for attrs in telemetry_sink.attribute_sets("phaze.analysis.windows") if attrs["outcome"] == "analyzed"]
    assert {attrs["tier"] for attrs in analyzed} >= {"fine"}
    fine_points = [
        point for point in telemetry_sink.points("phaze.analysis.windows") if dict(point.attributes) == {"tier": "fine", "outcome": "analyzed"}
    ]
    assert fine_points and fine_points[0].value == result["fine_windows_analyzed"], "the counter and the returned count must agree"
    assert (
        telemetry_sink.count("phaze.analysis.fine_window.duration")
        == result["fine_windows_analyzed"] + result["fine_windows_total"] - result["fine_windows_analyzed"]
    )


def test_the_run_and_the_audio_it_consumed_are_recorded(telemetry_sink: TelemetrySink, audio: str, tmp_path: Path) -> None:
    """``audio_seconds / wall_seconds`` is the throughput phaze-zaf2l derived by joining
    ``analysis_completed_at`` to ``metadata.duration`` over a 7-day window by hand."""
    _run(audio, str(tmp_path / "no-models"))
    assert telemetry_sink.count("phaze.analysis.run.duration") == 1
    audio_seconds = telemetry_sink.total("phaze.analysis.audio.duration")
    assert audio_seconds == pytest.approx(_TOTAL_SEC, rel=0.05)


def test_peak_rss_is_recorded_at_every_chunk_boundary(telemetry_sink: TelemetrySink, audio: str, tmp_path: Path) -> None:
    """D-07 and D-09's invariant made observable: a FLAT series across chunks is the
    invariant holding, and a series with slope is the +0.31 GiB-per-chunk growth
    phaze-b2qs9 measured before the D-09 fix. Growth is a bug, never a sizing input."""
    _run(audio, str(tmp_path / "no-models"))
    points = telemetry_sink.points("phaze.analysis.chunk.peak_rss")
    assert points, "no peak-RSS observation (unsupported platform would return None)"
    assert telemetry_sink.count("phaze.analysis.chunk.peak_rss") >= 2
    assert all(point.sum > 0 for point in points)


def test_no_analysis_metric_carries_an_identifier(telemetry_sink: TelemetrySink, audio: str, tmp_path: Path) -> None:
    """The cardinality guard, exercised against a REAL run rather than the catalogue.

    The static test reads declared label NAMES. This reads what a real analysis actually
    emitted, which is the only way to catch an attribute assembled from a variable.
    """
    _run(audio, str(tmp_path / "no-models"))
    for name in telemetry_sink.metric_names():
        for attributes in telemetry_sink.attribute_sets(name):
            for key, value in attributes.items():
                assert not any(bad in key.lower() for bad in FORBIDDEN_LABEL_SUBSTRINGS), f"{name} carries label {key!r}"
                assert audio not in str(value), f"{name}[{key}] carries the file path"


def test_the_file_identity_IS_on_the_span(telemetry_sink: TelemetrySink, audio: str, tmp_path: Path) -> None:
    """The other half of the same rule. Identity is not thrown away -- it is moved to where
    it is stored per-occurrence and aged out, instead of forever and per-series."""
    _run(audio, str(tmp_path / "no-models"))
    file_spans = [span for span in telemetry_sink.spans() if span.name == "analysis.file"]
    assert len(file_spans) == 1
    assert file_spans[0].attributes["phaze.file.path"] == audio
    assert file_spans[0].attributes["phaze.analysis.audio_duration_sec"] == pytest.approx(_TOTAL_SEC, rel=0.05)

    chunk_spans = [span for span in telemetry_sink.spans() if span.name == "analysis.chunk"]
    assert chunk_spans, "no chunk spans"
    assert all("phaze.analysis.chunk_index" in span.attributes for span in chunk_spans)


def test_a_failing_run_is_still_measured(telemetry_sink: TelemetrySink, tmp_path: Path) -> None:
    """A twelve-hour set that raises still cost twelve hours of node time. A histogram that
    only sees successes reports a throughput the operator does not have."""
    missing = tmp_path / "not-audio.wav"
    missing.write_bytes(b"definitely not a wav")
    with pytest.raises(Exception):  # noqa: B017  # the analysis path's own error, whatever it is
        _run(str(missing), str(tmp_path))
    outcomes = {attrs["outcome"] for attrs in telemetry_sink.attribute_sets("phaze.analysis.run.duration")}
    assert outcomes == {"error"}


@pytest.mark.skipif(not os.environ.get("PHAZE_TEST_MODELS_DIR"), reason="needs the real 3.26 GB essentia model set; set PHAZE_TEST_MODELS_DIR")
def test_analysis_with_real_models(telemetry_sink: TelemetrySink, audio: str) -> None:
    """The per-model claims, against the REAL 34-graph sweep (acceptance 2 and 8).

    Skipped in CI because the weights are 3.26 GB. When it runs, it is the test that shows
    the coarse tier resolving per phase AND per model by classifier_type -- which is the
    question phaze-8ifq8 asks and the cost-breakdown dashboard is built for.
    """
    models_dir = os.environ["PHAZE_TEST_MODELS_DIR"]
    _run(audio, models_dir)

    inference = telemetry_sink.attribute_sets("phaze.analysis.model.inference.duration")
    assert inference, "no per-model inference observations"
    assert {tuple(sorted(attrs)) for attrs in inference} == {("classifier_type", "model_name", "model_variant")}
    classifier_types = {attrs["classifier_type"] for attrs in inference}
    assert classifier_types <= {"musicnn", "vggish", "effnet_discogs"}
    assert len(inference) <= 34, "one attribute set per model, never per window"

    assert telemetry_sink.count("phaze.analysis.model.graph.build.duration") >= 1
    assert telemetry_sink.count("phaze.analysis.model.graph.release.duration") >= 1
    assert telemetry_sink.count("phaze.analysis.model.sweep.duration") >= 1
    assert "analysis.model_sweep" in telemetry_sink.span_names()
