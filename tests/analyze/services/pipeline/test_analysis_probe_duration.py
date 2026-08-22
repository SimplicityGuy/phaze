"""Coverage for `_probe_duration_sec` / `_duration_from_ffprobe_payload`'s failure paths (D-10).

phaze-mp0op P0b: these four paths were the only nine uncovered lines in `analysis.py` --
every existing `AnalysisProbeError` test asserts PROPAGATION out of `analyze_file` (see
`test_analysis.py::test_analyze_file_raises_on_corrupt_file`), never the probe itself. These
exercise `subprocess.run` directly, mocked, so no real `ffprobe` binary is required.

The two names under test are still imported from `phaze.services.analysis` -- that module
re-exports them and remains the import site -- but phaze-bk9el.15 moved the probe's BODY into
`services/analysis_probe.py`, so the `subprocess.run` patch has to name the module the call
now lives in. `_probe_duration_sec` itself is still patchable at `phaze.services.analysis`,
which is what every caller-level test does.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from phaze.services.analysis import AnalysisProbeError, _duration_from_ffprobe_payload, _probe_duration_sec


def test_probe_duration_raises_when_ffprobe_cannot_be_run() -> None:
    """`subprocess.run` raising `OSError` (ffprobe missing / not executable) is fatal, not swallowed."""
    with (
        patch("phaze.services.analysis_probe.subprocess.run", side_effect=FileNotFoundError("no such file: ffprobe")),
        pytest.raises(AnalysisProbeError, match="could not be run"),
    ):
        _probe_duration_sec("/fake/audio.mp3")


def test_probe_duration_raises_on_non_json_stdout() -> None:
    """ffprobe exiting 0 but emitting unparseable stdout is fatal, not a silent 0.0 duration."""
    completed = subprocess.CompletedProcess(args=["ffprobe"], returncode=0, stdout="not json at all", stderr="")
    with (
        patch("phaze.services.analysis_probe.subprocess.run", return_value=completed),
        pytest.raises(AnalysisProbeError, match="non-JSON"),
    ):
        _probe_duration_sec("/fake/audio.mp3")


def test_probe_duration_raises_when_no_stream_or_format_duration_is_readable() -> None:
    """Valid JSON with no usable duration anywhere (per-stream or container-level) is fatal."""
    payload = {"streams": [{"duration": "N/A"}], "format": {}}
    completed = subprocess.CompletedProcess(args=["ffprobe"], returncode=0, stdout=json.dumps(payload), stderr="")
    with (
        patch("phaze.services.analysis_probe.subprocess.run", return_value=completed),
        pytest.raises(AnalysisProbeError, match="no readable audio duration"),
    ):
        _probe_duration_sec("/fake/audio.mp3")


def test_duration_from_ffprobe_payload_rejects_a_non_dict_payload() -> None:
    """A payload that parsed as JSON but is not a dict (e.g. a bare JSON list) yields `None`."""
    assert _duration_from_ffprobe_payload(["not", "a", "dict"]) is None
    assert _duration_from_ffprobe_payload(None) is None
