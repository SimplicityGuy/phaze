"""The cloud pod surfaces the analysis child's coarse-tier heartbeats (phaze-x85mi).

A burst pod went 7,947 s without a log line or a progress POST at the fine->coarse handover
and was NOT hung: its child kept heartbeating ``coarse_decode`` / ``coarse_model`` into the
driver's D-08 stall watchdog, but ``job_runner`` never wired ``heartbeat_cb``, so the beats
reset the watchdog and went nowhere else. A coarse chunk is 30 windows decoded and swept by
all 34 models before its FIRST window completes, so the window-driven progress channel is
silent for a whole chunk-sweep -- and the progress throttle had also swallowed the fine tier's
final count, leaving the row at fine N-1/N for the entire coarse phase.

This test runs the REAL analysis child over the REAL ``analyze_file`` with a real essentia
decode of a synthetic WAV (only the TF graph call is faked -- no ``.pb`` graphs in CI), through
the REAL ``run_analysis_subprocess`` driver, and asserts what an operator and the API see.
A mocked essentia or a stub child could not discharge it: the claim is that the beats the
production analysis emits at the handover reach the pod log and the progress API (CLAUDE.md
verification rule 3).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid
import wave

import numpy as np
import pytest
import structlog

from phaze.analysis_child import _TARGET_ENV


if TYPE_CHECKING:
    from phaze.schemas.agent_analysis import AnalysisProgressPayload


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_RATE = 8000  # cheap source rate; essentia resamples to 44.1k / 16k regardless
# 40 s -> one full 30 s fine window (the 10 s trailer is under the 15 s fine floor) and one
# coarse window (the coarse tier has no length floor): the smallest file with a real handover.
_WAV_SECONDS = 40


def _write_sine_wav(path: Path, total_sec: int) -> None:
    """Mono int16 sine WAV, one second at a time."""
    t = np.arange(_SOURCE_RATE) / _SOURCE_RATE
    chunk = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SOURCE_RATE)
        for _ in range(total_sec):
            w.writeframes(chunk)


class _RecordingClient:
    """Stands in for ``PhazeAgentClient``: records every progress POST body in arrival order."""

    def __init__(self) -> None:
        self.progress: list[AnalysisProgressPayload] = []

    async def post_analysis_progress(self, _file_id: uuid.UUID, payload: AnalysisProgressPayload) -> None:
        self.progress.append(payload)

    async def report_analysis_failed(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover - a failure fails the test
        pytest.fail("the analysis must not fail")


@pytest.mark.integration
async def test_coarse_heartbeats_reach_the_pod_log_and_progress_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Coarse decode / model-sweep beats surface as ``job_runner_heartbeat`` lines and progress POSTs
    before the first coarse window completes, and those POSTs carry the fine tier's FINAL count."""
    pytest.importorskip("essentia")
    import phaze.job_runner as jr

    audio = tmp_path / "handover.wav"
    _write_sine_wav(audio, _WAV_SECONDS)
    monkeypatch.chdir(_REPO_ROOT)  # the child resolves tests.analyze._child_stubs via sys.path[0] == cwd
    monkeypatch.setenv(_TARGET_ENV, "tests.analyze._child_stubs:real_decode_analyze")
    # Surface every beat, so the assertion is about WHICH beats arrive, not about wall-clock spacing.
    monkeypatch.setattr(jr, "_HEARTBEAT_SURFACE_INTERVAL_SEC", 0.0)

    client = _RecordingClient()
    # The production throttle shape, widened: only the START and the final count pass it, so any
    # POST in between can only have come from the heartbeat relay.
    cfg = SimpleNamespace(analysis_progress_interval_sec=3600.0, analysis_stall_timeout_sec=1800)
    file_id = uuid.uuid4()

    with structlog.testing.capture_logs() as logs:
        payload = await jr._analyze_step(client, file_id, str(file_id), str(audio), "/fake/models", cfg, ".wav")  # type: ignore[arg-type]

    fine_total = payload.fine_windows_total
    coarse_total = payload.coarse_windows_total
    assert fine_total is not None
    assert fine_total >= 1
    assert coarse_total is not None
    assert coarse_total >= 1

    # (1) The pod log: the coarse tier's liveness is visible, from the real analysis's own call sites.
    beats = [e for e in logs if e["event"] == "job_runner_heartbeat"]
    stages = {e["stage"] for e in beats}
    assert {"coarse_decode", "coarse_model"} <= stages, f"coarse-tier beats must reach the pod log, got {sorted(stages)}"

    # ...and it is visible BEFORE the first coarse window completes -- the silence this bead is about.
    events = [e["event"] if e["event"] != "job_runner_heartbeat" else f"beat:{e['stage']}" for e in logs]
    first_coarse_progress = next(i for i, e in enumerate(logs) if e["event"] == "job_runner_progress" and e["coarse_windows_analyzed"] > 0)
    assert "beat:coarse_model" in events[:first_coarse_progress], "a model-sweep beat must precede the first coarse window"

    # (2) The progress API: a POST lands at the handover carrying the fine tier's FINAL count with the
    # coarse tier not yet advanced. Without the relay the throttle swallows exactly this state.
    handover = [p for p in client.progress if p.fine_windows_analyzed == fine_total and p.coarse_windows_analyzed == 0]
    assert handover, f"no progress POST carried the handover state; got {[p.model_dump() for p in client.progress]}"
    assert all(p.fine_windows_total == fine_total and p.coarse_windows_total == coarse_total for p in handover)
