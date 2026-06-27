"""Unit tests for the one-shot ``job_runner`` (Phase 52, Plan 02).

Drives ``src/phaze/job_runner.py`` against a respx fake control plane + object
store. Coverage:

- ``happy_path`` — presign → download → sha256-verify → analyze → PUT all
  succeed → the runner ``sys.exit(0)`` (KJOB-02).
- ``exit_code`` — the failure→exit-code matrix: presign/download → 10,
  sha256 mismatch → 11, analyze raises → 12, PUT fails → 13 (KJOB-04). A failed
  analysis NEVER exits 0.
- ``ca_verify`` — the agent client is constructed with ``verify=<baked CA>``
  (KJOB-05); ``verify=False`` appears nowhere.
- ``no_monoloader`` — a source guard proving the windowed ``analyze_file`` path
  is wired and no whole-file ``MonoLoader`` decode is referenced (KJOB-03).

``phaze.job_runner`` is imported lazily INSIDE each test (never at module top)
so collection still succeeds before the module exists — the RED state then
surfaces as a per-test import error rather than a collection error.

The analyze seam is patched at ``phaze.job_runner._load_analyze_file`` (the same
deferred-import seam ``process_file`` uses) so the unit tests run without the GB
essentia models AND without importing the platform-gated essentia wheel. The
``no_monoloader`` source guard independently proves the windowed path is wired.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
import respx


_AUDIO = b"deterministic-audio-bytes-for-sha256"
_GOOD_SHA = hashlib.sha256(_AUDIO).hexdigest()
_DOWNLOAD_URL = "http://bucket.test/obj"


def _fake_result() -> dict:
    """A representative ``analyze_file`` return dict (windowed contract)."""
    return {
        "bpm": 120.0,
        "musical_key": "C major",
        "mood": "happy",
        "style": "house",
        "danceability": 0.5,
        "energy": 0.6,
        "features": {},
        "windows": [
            {
                "tier": "fine",
                "window_index": 0,
                "start_sec": 0.0,
                "end_sec": 30.0,
                "bpm": 120.0,
                "musical_key": "C major",
            },
        ],
        "fine_windows_analyzed": 1,
        "fine_windows_total": 1,
        "coarse_windows_analyzed": 0,
        "coarse_windows_total": 0,
        "sampled": False,
    }


@respx.mock
async def test_happy_path_exits_zero(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """presign → download → verify → analyze → PUT all succeed → exit 0 (KJOB-02)."""
    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]

    presign = respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA}),
    )
    download = respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    put = respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    monkeypatch.setattr(jr, "_load_analyze_file", lambda: lambda *_a, **_k: _fake_result())

    with pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == 0
    assert presign.called and download.called and put.called
    # T-52-04: the internal bearer must NOT leak to the (self-authenticating) object store.
    dl_headers = {k.lower() for k in download.calls.last.request.headers}
    assert "authorization" not in dl_headers


@respx.mock
@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("presign_fail", 10),
        ("download_fail", 10),
        ("integrity_mismatch", 11),
        ("analyze_raises", 12),
        ("analyze_non_dict_result", 12),
        ("analyze_bad_window_key", 12),
        ("callback_fail", 13),
    ],
)
async def test_exit_code_matrix(job_env, monkeypatch, scenario, expected_code):  # type: ignore[no-untyped-def]
    """Each failure class maps to a distinct non-zero exit; analysis never exits 0 (KJOB-04)."""
    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]
    presign_url = f"{base}/api/internal/agent/files/{file_id}/presign-download"
    put_url = f"{base}/api/internal/agent/analysis/{file_id}"

    def _ok_presign() -> None:
        respx.post(presign_url).mock(
            return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA}),
        )

    def _ok_download() -> None:
        respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))

    if scenario == "presign_fail":
        respx.post(presign_url).mock(return_value=httpx.Response(404, json={"detail": "no such file"}))
    elif scenario == "download_fail":
        _ok_presign()
        respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(404))
    elif scenario == "integrity_mismatch":
        respx.post(presign_url).mock(
            return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": "00" * 32}),
        )
        _ok_download()
    elif scenario == "analyze_raises":
        _ok_presign()
        _ok_download()

        def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("essentia crashed")

        monkeypatch.setattr(jr, "_load_analyze_file", lambda: _boom)
    elif scenario == "analyze_non_dict_result":
        # A malformed (non-dict) analyze result is a bad-analysis-output failure
        # and must map to EXIT_ANALYSIS (12), NOT EXIT_CALLBACK (13) (WR-01).
        _ok_presign()
        _ok_download()
        monkeypatch.setattr(jr, "_load_analyze_file", lambda: lambda *_a, **_k: ["not", "a", "dict"])
    elif scenario == "analyze_bad_window_key":
        # A window dict carrying an unexpected key fails AnalysisWindowPayload
        # (extra="forbid") during payload build — this is an analysis-output
        # error and must exit 12, not 13 (WR-01: payload build is part of the
        # analyze step).
        _ok_presign()
        _ok_download()

        def _bad_window_result() -> dict:
            result = _fake_result()
            result["windows"] = [{"tier": "fine", "window_index": 0, "unexpected_key": "boom"}]
            return result

        monkeypatch.setattr(jr, "_load_analyze_file", lambda: lambda *_a, **_k: _bad_window_result())
    elif scenario == "callback_fail":
        _ok_presign()
        _ok_download()
        monkeypatch.setattr(jr, "_load_analyze_file", lambda: lambda *_a, **_k: _fake_result())
        respx.put(put_url).mock(return_value=httpx.Response(404, json={"detail": "rejected"}))

    with pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == expected_code
    assert exc.value.code != 0


@respx.mock
async def test_ca_verify_threads_baked_ca(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """The client is built with ``verify=<baked CA path>``; never ``verify=False`` (KJOB-05, T-52-01)."""
    import phaze.job_runner as jr

    captured: dict = {}

    class _SpyClient:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)

        async def request_download_url(self, _file_id):  # type: ignore[no-untyped-def]
            # Stop the flow right after construction — we only assert how the
            # client was built. The presign failure maps to exit 10.
            raise RuntimeError("stop after construction")

        async def close(self) -> None:
            return None

    monkeypatch.setattr("phaze.tasks._shared.agent_bootstrap.PhazeAgentClient", _SpyClient)

    with pytest.raises(SystemExit):
        await jr.run()

    assert captured.get("verify") == job_env["ca_file"]
    assert captured.get("verify") is not False


def test_no_monoloader_source_guard():  # type: ignore[no-untyped-def]
    """Source guard: windowed ``analyze_file`` wired, no whole-file MonoLoader, no verify=False (KJOB-03/05)."""
    import phaze.job_runner as jr

    src = Path(jr.__file__).read_text(encoding="utf-8")
    assert "MonoLoader" not in src, "one-shot must use windowed analyze_file, not whole-file MonoLoader (KJOB-03)"
    assert "analyze_file" in src, "windowed analyze_file must be wired (KJOB-03)"
    assert "verify=False" not in src, "callback CA verification must never be disabled (KJOB-05)"
