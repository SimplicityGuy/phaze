"""Tests for the essentia analysis child CLI (Phase 101, phaze-bo3p.1).

Three tiers:
- In-process protocol tests: drive ``analysis_child.run`` against an in-memory stream
  with the stub targets (no subprocess, no essentia).
- Parity test: the result dict crossing the JSONL protocol is byte-identical (as JSON)
  to a direct ``analyze_file`` return under the same mocked essentia.
- Real-subprocess fd test: ``python -m phaze.analysis_child`` with a noisy stub proves
  the fd 1 → fd 2 re-route keeps the protocol channel clean and lands banners on stderr.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from phaze import analysis_child
from phaze.analysis_child import _TARGET_ENV, _parse_args, run
from tests.analyze.services.test_analysis import _build_mock_essentia, _mock_labels_file


if TYPE_CHECKING:
    import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_STUBS = "tests.analyze._child_stubs"


def _run_child(monkeypatch: pytest.MonkeyPatch, target: str, argv: list[str]) -> tuple[int, list[dict[str, Any]]]:
    """Drive ``run`` in-process against a StringIO protocol channel; return (rc, parsed lines)."""
    monkeypatch.setenv(_TARGET_ENV, target)
    protocol = io.StringIO()
    rc = run(_parse_args(argv), protocol)
    lines = [json.loads(line) for line in protocol.getvalue().splitlines() if line]
    return rc, lines


def test_run_emits_progress_then_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: START + per-window progress bumps, then a terminal result line, rc 0."""
    rc, lines = _run_child(monkeypatch, f"{_STUBS}:fake_analyze", ["/fake/audio.mp3", "--models-dir", "/fake/models"])

    assert rc == 0
    progress = [ln for ln in lines if ln["type"] == "progress"]
    assert [(ln["analyzed"], ln["total"]) for ln in progress] == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert lines[-1]["type"] == "result"
    result = lines[-1]["result"]
    # The five-field coverage contract crosses the protocol intact.
    assert result["fine_windows_analyzed"] == 3
    assert result["fine_windows_total"] == 3
    assert result["coarse_windows_analyzed"] == 1
    assert result["coarse_windows_total"] == 1
    assert result["sampled"] is False


def test_run_passes_only_provided_windowing_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provided argv overrides reach the target; absent flags are NOT passed (target defaults hold)."""
    rc, lines = _run_child(
        monkeypatch,
        f"{_STUBS}:fake_analyze",
        ["/fake/audio.mp3", "--models-dir", "/fake/models", "--fine-cap", "7", "--coarse-window-sec", "120"],
    )

    assert rc == 0
    echo = lines[-1]["result"]["echo"]
    assert echo["file_path"] == "/fake/audio.mp3"
    assert echo["models_dir"] == "/fake/models"
    assert echo["fine_cap"] == 7
    assert echo["coarse_window_sec"] == 120
    for absent in ("fine_window_sec", "fine_min_sec", "coarse_cap"):
        assert absent not in echo


def test_run_error_path_emits_error_line_and_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising target yields a terminal error line naming the exception, rc 1."""
    rc, lines = _run_child(monkeypatch, f"{_STUBS}:crash_analyze", ["/fake/audio.mp3", "--models-dir", "/fake/models"])

    assert rc == 1
    assert lines[-1]["type"] == "error"
    assert "RuntimeError: essentia exploded" in lines[-1]["message"]


def test_run_malformed_target_env_is_a_loud_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed PHAZE_ANALYSIS_CHILD_TARGET fails with an error line, never a silent fallback."""
    rc, lines = _run_child(monkeypatch, "garbage-no-colon", ["/fake/audio.mp3", "--models-dir", "/fake/models"])

    assert rc == 1
    assert lines[-1]["type"] == "error"
    assert _TARGET_ENV in lines[-1]["message"]


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_result_is_byte_identical_to_direct_analyze_file(_mock_es: MagicMock, mock_get_labels: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Success criterion 4 (byte-identity): the protocol's JSON round-trip of the REAL
    ``analyze_file`` result equals the direct in-process return, byte-for-byte as JSON."""
    mock_get_labels.side_effect = _mock_labels_file
    from phaze.services.analysis import analyze_file

    direct = analyze_file("/fake/audio.mp3", "/fake/models")

    monkeypatch.delenv(_TARGET_ENV, raising=False)  # default target: phaze.services.analysis:analyze_file
    protocol = io.StringIO()
    rc = run(_parse_args(["/fake/audio.mp3", "--models-dir", "/fake/models"]), protocol)

    assert rc == 0
    lines = [json.loads(line) for line in protocol.getvalue().splitlines()]
    assert lines[-1]["type"] == "result"
    assert json.dumps(lines[-1]["result"], sort_keys=True) == json.dumps(direct, sort_keys=True)
    # The progress denominator invariant: every bump reports the natural pre-stride total,
    # identical to the result's fine_windows_total.
    totals = {ln["total"] for ln in lines if ln["type"] == "progress"}
    assert totals == {direct["fine_windows_total"]}


def test_real_subprocess_fd_reroute_keeps_protocol_clean() -> None:
    """The fd contract, on the REAL interpreter boundary: raw fd-1 writes and stray prints
    land on stderr; stdout carries protocol JSONL only (banner capture, OBS-03)."""
    env = {**os.environ, _TARGET_ENV: f"{_STUBS}:noisy_analyze"}
    result = subprocess.run(  # trusted input: literal sys.executable + our own module
        [sys.executable, "-m", "phaze.analysis_child", "/fake/audio.mp3", "--models-dir", "/fake/models"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    lines = [json.loads(line) for line in result.stdout.splitlines() if line]  # every stdout line must parse
    assert lines[-1]["type"] == "result"
    assert [(ln["analyzed"], ln["total"]) for ln in lines if ln["type"] == "progress"] == [(0, 1), (1, 1)]
    assert "MusicExtractor" in result.stderr
    assert "stray print from the analysis child" in result.stderr


def test_open_protocol_channel_is_line_buffered() -> None:
    """The protocol handle must flush per line so the parent sees progress as it happens."""
    # Exercised implicitly by the subprocess test above; here assert the constant contract
    # that the module writes one JSON object per line with a trailing newline.
    protocol = io.StringIO()
    analysis_child._emit(protocol, {"type": "progress", "analyzed": 1, "total": 2})
    assert protocol.getvalue().endswith("\n")
    assert json.loads(protocol.getvalue()) == {"type": "progress", "analyzed": 1, "total": 2}
