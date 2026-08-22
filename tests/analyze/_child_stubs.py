"""Stub analysis targets for the Phase 101 subprocess-execution tests (phaze-bo3p).

Pointed at via ``PHAZE_ANALYSIS_CHILD_TARGET=tests.analyze._child_stubs:<name>`` so the
REAL ``phaze.analysis_child`` subprocess (and the ``phaze.services.analysis_exec``
driver above it) can be exercised end-to-end without an essentia wheel: the child
imports THIS module instead of ``phaze.services.analysis``. Each stub mirrors the
``analyze_file`` call contract — ``(file_path, models_dir, *, progress_cb=None,
heartbeat_cb=None, **windowing)`` returning the aggregates + windows + the four
progress counts.

``heartbeat_cb`` is named EXPLICITLY on every stub rather than swept into ``**windowing``
(phaze-w55w1). It has to be: ``_result`` echoes ``**windowing`` back through the JSON
protocol, so a callable landing there makes the child die with "Object of type function is
not JSON serializable" — a failure that looks like a protocol bug and is really a stub bug.

Importable from a child subprocess because the test runner's cwd (the repo root) is
on ``sys.path[0]`` under ``python -m``; driver tests pass the repo root cwd explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable


# The gate seam (phaze-2mz81). ``PHAZE_STUB_GATE_AFTER`` beats in, ``crawling_analyze`` parks
# until ``PHAZE_STUB_GATE_FILE`` appears, then finishes its run as normal. Unset (the default)
# leaves every stub exactly as it was.
_GATE_FILE_ENV = "PHAZE_STUB_GATE_FILE"
_GATE_AFTER_ENV = "PHAZE_STUB_GATE_AFTER"
# Safety bound only: a test that never opens its gate gets a slow failure, not a wedged child.
_GATE_MAX_WAIT_SEC = 30.0
_GATE_POLL_SEC = 0.005


def _result(file_path: str, models_dir: str, **windowing: Any) -> dict[str, Any]:
    """A deterministic analyze_file-shaped result that echoes its inputs.

    The ``echo`` key is stub-only (absent from real results): tests use it to assert
    the child passed argv windowing overrides through — and ONLY the provided ones.
    """
    return {
        "bpm": 128.0,
        "musical_key": "C minor",
        "mood": "happy",
        "style": "Electronic/House",
        "danceability": 0.42,
        "features": {"genre": {"predictions": [{"label": "Electronic/House", "confidence": 0.85}]}},
        "windows": [
            {"tier": "fine", "window_index": 0, "start_sec": 0.0, "end_sec": 30.0, "bpm": 128.0, "musical_key": "C minor"},
        ],
        "fine_windows_analyzed": 3,
        "fine_windows_total": 3,
        "coarse_windows_analyzed": 1,
        "coarse_windows_total": 1,
        "echo": {"file_path": file_path, "models_dir": models_dir, **windowing},
    }


def fake_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """Happy path: START + three bumps, then the deterministic result."""
    if progress_cb is not None:
        for analyzed in (0, 1, 2, 3):
            progress_cb(analyzed, 3)
    return _result(file_path, models_dir, **windowing)


def slow_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """Like fake_analyze but sleeps between bumps so a parent can observe MID-RUN progress."""
    if progress_cb is not None:
        for analyzed in (0, 1, 2, 3):
            progress_cb(analyzed, 3)
            time.sleep(0.15)
    return _result(file_path, models_dir, **windowing)


def _beat(heartbeat_cb: Callable[[str, int, int], None] | None, stage: str, done: int, total: int) -> None:
    """Emit one liveness heartbeat if the caller wired the channel."""
    if heartbeat_cb is not None:
        heartbeat_cb(stage, done, total)


def _wait_at_gate() -> None:
    """Park until the parent creates ``PHAZE_STUB_GATE_FILE`` (phaze-2mz81).

    What this buys a test is QUIESCENCE AT A KNOWN INSTANT. Every protocol line the child
    writes is flushed as it is written (``analysis_child._emit``), so while the child sits
    here it has emitted exactly the beats the parent has already been handed and nothing
    more: the pipe is empty, the parent's stdout pump is parked in ``_wait_for_data``, and
    no callback is queued behind it. A parent that has counted those beats can then take a
    synchronous snapshot that is EXACT rather than racing a beat already travelling through
    the pipe -- which is the whole of phaze-2mz81.

    Reopening the gate is equally a probe: whatever the parent does at the instant it
    touches the file, everything the child emits afterwards is attributable to work the
    parent did AFTER that instant. The cancellation test opens the gate from ``proc.kill``
    so "beats after the gate" means precisely "a pump that outlived the reap".
    """
    path = os.environ.get(_GATE_FILE_ENV)
    if not path:
        return
    gate = Path(path)
    deadline = time.monotonic() + _GATE_MAX_WAIT_SEC
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(_GATE_POLL_SEC)


def hang_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """Emits START then wedges, reporting NOTHING further — for the driver's stall/kill tests.

    This is what a genuine hang looks like to the stall watchdog: some initial output, then
    silence. Contrast ``crawling_analyze``, which is equally slow but keeps reporting.
    """
    if progress_cb is not None:
        progress_cb(0, 5)
    _beat(heartbeat_cb, "fine", 0, 5)
    time.sleep(300.0)
    return _result(file_path, models_dir, **windowing)  # pragma: no cover - killed long before


def crash_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """Raises mid-analysis — for the child error line + nonzero exit path."""
    if progress_cb is not None:
        progress_cb(0, 3)
    msg = "essentia exploded"
    raise RuntimeError(msg)


def noisy_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """Writes raw banner bytes to fd 1 (as essentia's C++ does) plus a stray print.

    After the child's fd re-route BOTH must land on stderr, keeping the protocol
    channel machine-clean — the banner-capture assertion of OBS-03.
    """
    os.write(1, b"[ INFO ] MusicExtractor: banner straight to fd 1\n")
    print("stray print from the analysis child")  # deliberate: proves sys.stdout is re-routed too
    if progress_cb is not None:
        progress_cb(0, 1)
        progress_cb(1, 1)
    return _result(file_path, models_dir, **windowing)


def crawling_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """SLOW but always progressing — the file the wall-clock timeout used to kill (phaze-w55w1).

    Heartbeats every ``PHAZE_STUB_BEAT_SEC`` (default 0.05 s) for ``PHAZE_STUB_BEATS`` beats
    (default 40), so total runtime is many multiples of the stall threshold a test arms while
    no single gap between beats ever reaches it. This is a 6-hour concert set in miniature: the
    property under test is that elapsed time alone never kills it.

    ``PHAZE_STUB_GATE_AFTER`` (default 0 = never) parks the run at :func:`_wait_at_gate` once
    that many beats have been emitted, handing the parent a moment of guaranteed silence.
    """
    beat_sec = float(os.environ.get("PHAZE_STUB_BEAT_SEC", "0.05"))
    beats = int(os.environ.get("PHAZE_STUB_BEATS", "40"))
    gate_after = int(os.environ.get(_GATE_AFTER_ENV, "0"))
    for i in range(beats):
        time.sleep(beat_sec)
        _beat(heartbeat_cb, "fine", i + 1, beats)
        if gate_after and i + 1 == gate_after:
            _wait_at_gate()
    if progress_cb is not None:
        progress_cb(beats, beats)
    return _result(file_path, models_dir, **windowing)


# ---------------------------------------------------------------------------
# REAL-result stubs (phaze-qiwdk, seam-inventory row A3)
#
# Everything above this line returns `_result` — a hand-built dict of plain Python floats
# with one fine window and a one-entry `features` dict. It is a PROXY, and it is a proxy of
# the one shape that cannot exhibit what `_emit`'s strict `json.dumps` and the parent pump
# actually have to survive: a numpy scalar leaf, a non-finite float, or a line big enough
# to matter to a 64 KiB pipe. The four stubs below carry the REAL artifact instead
# (`tests/analyze/_real_result.py` documents where it came from), plus two deliberately
# corrupted variants that exist to PROVE the real-artifact assertions have teeth — an
# assertion that "the real result contains no numpy scalar" is worth nothing unless a
# numpy scalar would have failed it.
# ---------------------------------------------------------------------------


def real_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """Return the REAL captured ``analyze_file`` result, verbatim.

    Deliberately does NOT echo its arguments (unlike :func:`_result`): the caller asserts
    byte-for-byte equality against the same artifact, so any added key would be the stub's
    fingerprint rather than essentia's output.
    """
    from tests.analyze._real_result import real_analysis_result  # deferred: the CHILD process imports this, not the parent

    return real_analysis_result()


def numpy_leaf_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """The real result with ONE ``numpy.float32`` leaf spliced into a window's features.

    The control for :func:`real_analyze`. ``_predict_single`` returns a numpy array and
    ``np.mean`` returns a ``numpy.float64``; the ONLY reason one never reaches the wire is
    the explicit ``float(pred)`` coercion in ``_run_model_sets_over_windows``. Delete that
    coercion and this is what the child would emit — so the stub reproduces the failure
    that coercion prevents, rather than asserting a negative into thin air.
    """
    import numpy as np  # deferred, child-side; numpy is already an analysis-path dependency

    from tests.analyze._real_result import real_analysis_result  # deferred: the CHILD process imports this, not the parent

    result = real_analysis_result()
    coarse = next(w for w in result["windows"] if w["tier"] == "coarse")
    coarse["features"]["genre"]["predictions"][0]["confidence"] = np.float32(0.5)
    return result


def nan_leaf_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """The real result with ONE ``NaN`` spliced into a window's features.

    A separate control from :func:`numpy_leaf_analyze` because the two fail at DIFFERENT
    hops: a numpy scalar dies in the child at ``json.dumps``, while a ``NaN`` is emitted
    (``allow_nan`` defaults True), is accepted by the parent's ``json.loads``, and only dies
    two hops later inside ``AnalysisWindowPayload``. Conflating them would hide that gap.
    """
    from tests.analyze._real_result import real_analysis_result  # deferred: the CHILD process imports this, not the parent

    result = real_analysis_result()
    coarse = next(w for w in result["windows"] if w["tier"] == "coarse")
    coarse["features"]["genre"]["predictions"][0]["confidence"] = float("nan")
    return result


# A 24-hour recording's exact window counts at the 30 s / 180 s defaults: 86400 / 30 and
# 86400 / 180, both of which divide evenly. The archive's own longest file today is 6 h 08 m
# (737 fine + 123 coarse windows, ~750 KB of protocol line -- already 11x the pipe buffer);
# 24 h is the product's stated ceiling for a concert set and the point at which the line first
# clears 2 MiB, so it is the scale the framing case is built at. See the
# `long_recording_analyze` docstring for why replicating one window is the right fixture.
_FINE_WINDOWS_IN_24H = 2880
_COARSE_WINDOWS_IN_24H = 480


def long_recording_analyze(
    file_path: str,
    models_dir: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
    heartbeat_cb: Callable[[str, int, int], None] | None = None,
    **windowing: Any,
) -> dict[str, Any]:
    """The real result inflated to a 24-hour recording's window count — a multi-MiB line.

    Built by REPLICATING the real coarse window rather than by analyzing 24 hours of audio,
    and that is the right fixture for this test specifically: the mechanism under test is
    PIPE FRAMING, which is a function of byte count and of nothing else. The per-window
    VALUES are what :func:`real_analyze` exists to carry; duplicating them here costs the
    framing case nothing and buys a fixture that builds in milliseconds.

    Bounded by FIXTURE SIZE, never by a timer — D-08 liveness is progress-based and this
    module must not smuggle a wall clock into any lane (phaze-1b39).
    """
    from tests.analyze._real_result import real_analysis_result  # deferred: the CHILD process imports this, not the parent

    result = real_analysis_result()
    fine_template = next(w for w in result["windows"] if w["tier"] == "fine")
    coarse_template = next(w for w in result["windows"] if w["tier"] == "coarse")
    fine = [{**fine_template, "window_index": i, "start_sec": float(i * 30), "end_sec": float((i + 1) * 30)} for i in range(_FINE_WINDOWS_IN_24H)]
    coarse = [
        {**coarse_template, "window_index": i, "start_sec": float(i * 180), "end_sec": float((i + 1) * 180)} for i in range(_COARSE_WINDOWS_IN_24H)
    ]
    result["windows"] = fine + coarse
    result["fine_windows_analyzed"] = len(fine)
    result["fine_windows_total"] = len(fine)
    result["coarse_windows_analyzed"] = len(coarse)
    result["coarse_windows_total"] = len(coarse)
    return result
