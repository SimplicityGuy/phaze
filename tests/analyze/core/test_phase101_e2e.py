"""Phase 101 done-gate: end-to-end local simulation on BOTH lanes (phaze-bo3p.5).

The automated stand-in for live pod UAT (verification tier chosen at planning:
automated + local sim). Each test runs the REAL ``python -m phaze.analysis_child``
subprocess — via the REAL ``services.analysis_exec`` driver, unpatched — with the
slow stub target emitting incremental fine-window counts, and observes that:

1. progress POSTs carry MID-RUN counts (``analyzed < total``) — values that only
   exist while the child is still analyzing, proving the event loop serviced the
   POST mid-analysis instead of jumping 0→100% at completion (OBS-03 criteria 1+2);
2. the persisted counter fields are ``fine_windows_analyzed``/``fine_windows_total``
   — the one shared source both the console lines and the UI surfaces read
   (criterion 3);
3. the completion payload carries the child's result dict values unchanged
   (criterion 4's passthrough at the lane level; byte-identity of the dict itself
   is proven by the parity test in test_analysis_child.py, and
   ``services/analysis.py`` is untouched by the epic).

Pod lane: ``job_runner.run()`` against a respx-mocked control plane — the parent's
httpx is mocked, the analysis child is a genuine subprocess. Worker lane:
``process_file`` with a mocked agent client, same genuine subprocess underneath.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from phaze.analysis_child import _TARGET_ENV
from phaze.tasks.functions import process_file
from tests.analyze._child_stubs import _result


if TYPE_CHECKING:
    from collections.abc import Iterator


_REPO_ROOT = Path(__file__).resolve().parents[3]
_STUBS = "tests.analyze._child_stubs"
_AUDIO = b"phase101-e2e-audio-bytes"
_GOOD_SHA = hashlib.sha256(_AUDIO).hexdigest()
_DOWNLOAD_URL = "http://bucket.test/obj"


@pytest.fixture(autouse=True)
def _real_child_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the REAL analysis child at the slow stub and run from the repo root
    (the child resolves ``tests.analyze._child_stubs`` via ``sys.path[0] == cwd``)."""
    monkeypatch.chdir(_REPO_ROOT)
    monkeypatch.setenv(_TARGET_ENV, f"{_STUBS}:slow_analyze")
    yield


@respx.mock
async def test_pod_lane_end_to_end_progress_arrives_mid_analysis(job_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Pod lane, no seams patched: real driver, real child subprocess, respx control plane."""
    monkeypatch.setenv("PHAZE_ANALYSIS_PROGRESS_INTERVAL_SEC", "0")  # post every emitted count
    from phaze.config import get_settings

    get_settings.cache_clear()

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]

    respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA, "audio_ext": "mp3"}),
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    progress = respx.post(f"{base}/api/internal/agent/analysis/{file_id}/progress").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )
    put = respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    with pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == 0
    # ≥1 POST with analyzed < total: a value that only exists mid-analysis. The GIL-starved
    # in-process model could never land one of these — every POST waited for essentia to finish.
    counts = [json.loads(c.request.content) for c in progress.calls]
    mid_run = [c for c in counts if c["fine_windows_analyzed"] < c["fine_windows_total"]]
    assert mid_run, f"no mid-analysis progress POST landed; posted counts: {counts}"
    assert all(c["fine_windows_total"] == 3 for c in counts), "one shared denominator: the natural fine total"
    final = counts[-1]
    assert (final["fine_windows_analyzed"], final["fine_windows_total"]) == (3, 3), "the final N/N always posts"

    # Completion passthrough: the PUT body carries the child's result values unchanged.
    stub = _result("ignored", "ignored")
    body = json.loads(put.calls.last.request.content)
    for field in (
        "bpm",
        "musical_key",
        "danceability",
        "fine_windows_analyzed",
        "fine_windows_total",
        "coarse_windows_analyzed",
        "coarse_windows_total",
        "sampled",
    ):
        assert body[field] == stub[field], field


async def test_worker_lane_end_to_end_progress_arrives_mid_analysis() -> None:
    """SAQ worker lane, no seams patched: process_file → real driver → real child subprocess."""
    from phaze.config import AgentSettings

    cfg = MagicMock(spec=AgentSettings)
    cfg.analysis_inner_timeout_sec = 60
    cfg.analysis_fine_cap = 60
    cfg.analysis_coarse_cap = 30
    cfg.analysis_progress_interval_sec = 0.0  # post every emitted count

    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.post_analysis_progress = AsyncMock(return_value=None)
    ctx: dict[str, Any] = {"api_client": api}

    with patch("phaze.tasks.functions.get_settings", return_value=cfg):
        result = await process_file(
            ctx,
            file_id="a559cd4f-c114-4396-8032-d01fddc6810c",
            original_path="/fake/audio.mp3",
            file_type="mp3",
            agent_id="test-agent",
            models_path="/fake/models",
        )

    assert result["status"] == "analyzed"
    posted = [(c.args[1].fine_windows_analyzed, c.args[1].fine_windows_total) for c in api.post_analysis_progress.await_args_list]
    mid_run = [(a, t) for a, t in posted if a < t]
    assert mid_run, f"no mid-analysis progress POST landed; posted counts: {posted}"
    assert posted[-1] == (3, 3), "the final count reaches the shared counter source"

    # Completion passthrough on this lane too.
    stub = _result("ignored", "ignored")
    body = api.put_analysis.await_args.args[1]
    assert body.bpm == stub["bpm"]
    assert body.musical_key == stub["musical_key"]
    assert body.fine_windows_analyzed == stub["fine_windows_analyzed"]
    assert body.fine_windows_total == stub["fine_windows_total"]
    assert body.sampled == stub["sampled"]
