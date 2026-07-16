"""Stub analysis targets for the Phase 101 subprocess-execution tests (phaze-bo3p).

Pointed at via ``PHAZE_ANALYSIS_CHILD_TARGET=tests.analyze._child_stubs:<name>`` so the
REAL ``phaze.analysis_child`` subprocess (and the ``phaze.services.analysis_exec``
driver above it) can be exercised end-to-end without an essentia wheel: the child
imports THIS module instead of ``phaze.services.analysis``. Each stub mirrors the
``analyze_file`` call contract — ``(file_path, models_dir, *, progress_cb=None,
**windowing)`` returning the aggregates + windows + five-field coverage dict.

Importable from a child subprocess because the test runner's cwd (the repo root) is
on ``sys.path[0]`` under ``python -m``; driver tests pass the repo root cwd explicitly.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable


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
            {"tier": "fine", "window_index": 0, "start_sec": 0.0, "end_sec": 30.0, "bpm": 128.0, "musical_key": "C minor", "confidence": 3.8},
        ],
        "fine_windows_analyzed": 3,
        "fine_windows_total": 3,
        "coarse_windows_analyzed": 1,
        "coarse_windows_total": 1,
        "sampled": False,
        "echo": {"file_path": file_path, "models_dir": models_dir, **windowing},
    }


def fake_analyze(file_path: str, models_dir: str, *, progress_cb: Callable[[int, int], None] | None = None, **windowing: Any) -> dict[str, Any]:
    """Happy path: START + three bumps, then the deterministic result."""
    if progress_cb is not None:
        for analyzed in (0, 1, 2, 3):
            progress_cb(analyzed, 3)
    return _result(file_path, models_dir, **windowing)


def slow_analyze(file_path: str, models_dir: str, *, progress_cb: Callable[[int, int], None] | None = None, **windowing: Any) -> dict[str, Any]:
    """Like fake_analyze but sleeps between bumps so a parent can observe MID-RUN progress."""
    if progress_cb is not None:
        for analyzed in (0, 1, 2, 3):
            progress_cb(analyzed, 3)
            time.sleep(0.15)
    return _result(file_path, models_dir, **windowing)


def hang_analyze(file_path: str, models_dir: str, *, progress_cb: Callable[[int, int], None] | None = None, **windowing: Any) -> dict[str, Any]:
    """Emits START then wedges — for driver timeout/kill tests."""
    if progress_cb is not None:
        progress_cb(0, 5)
    time.sleep(300.0)
    return _result(file_path, models_dir, **windowing)  # pragma: no cover - killed long before


def crash_analyze(file_path: str, models_dir: str, *, progress_cb: Callable[[int, int], None] | None = None, **windowing: Any) -> dict[str, Any]:
    """Raises mid-analysis — for the child error line + nonzero exit path."""
    if progress_cb is not None:
        progress_cb(0, 3)
    msg = "essentia exploded"
    raise RuntimeError(msg)


def noisy_analyze(file_path: str, models_dir: str, *, progress_cb: Callable[[int, int], None] | None = None, **windowing: Any) -> dict[str, Any]:
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
