"""Unit tests for the one-shot ``job_runner`` (Phase 52, Plan 02).

Drives ``src/phaze/job_runner.py`` against a respx fake control plane + object
store. Coverage:

- ``happy_path`` — presign → download → sha256-verify → analyze → PUT all
  succeed → the runner ``sys.exit(0)`` (KJOB-02).
- ``exit_code`` — the failure→exit-code matrix: presign/download → 10,
  sha256 mismatch → 11, analyze raises → 12, PUT fails → 13 (KJOB-04). A failed
  analysis NEVER exits 0.
- ``ca_verify`` — the agent client is constructed with ``verify=<internal CA>``
  (KJOB-05; CA mounted at runtime per KDEPLOY-06); ``verify=False`` appears nowhere.
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

import asyncio
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


def _capturing_analyze(paths: list[str]):  # type: ignore[no-untyped-def]
    """Build a fake ``analyze_file`` that records the temp-file path it was handed."""

    def _analyze(path, *_a, **_k):  # type: ignore[no-untyped-def]
        paths.append(path)
        return _fake_result()

    return _analyze


# ---------------------------------------------------------------------------
# cloud-analyze-empty-no-ext: the downloaded temp file MUST carry the file's REAL
# audio extension (essentia detects format by extension). The staged S3 key has
# none, so the pod must use the server-threaded ``audio_ext`` — never the old
# ``.audio`` fallback, which yielded duration 0 -> 0 windows -> a silent empty
# "success".
# ---------------------------------------------------------------------------


@respx.mock
async def test_temp_file_suffix_derives_from_real_audio_ext(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """The temp file passed to analyze_file is suffixed from the threaded ``audio_ext`` (regression).

    Pre-fix this used ``Path(urlparse(url).path).suffix or ".audio"`` on an extension-less
    staged key -> ``.audio`` (undecodable) -> the empty-analysis bug. Asserting ``.mp3`` here
    FAILS on the old fallback and PASSES once the real extension is threaded through.
    """
    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]

    # The download URL has NO extension (mirrors the real staged key phaze-staging/<file_id>).
    respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA, "audio_ext": "mp3"}),
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    seen: list[str] = []
    monkeypatch.setattr(jr, "_load_analyze_file", lambda: _capturing_analyze(seen))

    with pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == 0
    assert len(seen) == 1
    suffix = Path(seen[0]).suffix
    assert suffix == ".mp3", f"temp file must carry the real audio extension, got {suffix!r}"
    # The bug's fingerprint: the extension-less staged key must NOT collapse to `.audio`.
    assert suffix != ".audio"


@respx.mock
async def test_temp_file_suffix_falls_back_to_url_suffix_when_ext_absent(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """An older control plane omits ``audio_ext`` -> the pod uses the URL path suffix."""
    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]
    url_with_ext = "http://bucket.test/obj.ogg"

    respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": url_with_ext, "expected_sha256": _GOOD_SHA}),
    )
    respx.get(url_with_ext).mock(return_value=httpx.Response(200, content=_AUDIO))
    respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    seen: list[str] = []
    monkeypatch.setattr(jr, "_load_analyze_file", lambda: _capturing_analyze(seen))

    with pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == 0
    assert Path(seen[0]).suffix == ".ogg"


@pytest.mark.parametrize(
    ("audio_ext", "url", "expected"),
    [
        ("mp3", "http://bucket.test/phaze-staging/abc", ".mp3"),  # real ext wins over extension-less key
        (".m4a", "http://bucket.test/obj", ".m4a"),  # a stray leading dot is normalized, not doubled
        ("  ogg  ", "http://bucket.test/obj", ".ogg"),  # whitespace is stripped
        (None, "http://bucket.test/obj.flac", ".flac"),  # no ext threaded -> URL suffix
        ("", "http://bucket.test/obj.wav", ".wav"),  # empty ext -> URL suffix
        (None, "http://bucket.test/phaze-staging/abc", ".audio"),  # last-resort sentinel
    ],
)
def test_temp_suffix_precedence(audio_ext, url, expected):  # type: ignore[no-untyped-def]
    """``_temp_suffix`` prefers the real extension, then the URL suffix, then ``.audio``."""
    import phaze.job_runner as jr

    assert jr._temp_suffix(audio_ext, url) == expected


@respx.mock
async def test_zero_window_analysis_fails_loudly(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """A zero-window analyze result exits EXIT_ANALYSIS (12), never a false success (hardening).

    Guards against ANY future silent empty-success (e.g. a mis-suffixed download essentia
    can't decode): both ``*_windows_total`` == 0 means duration probed 0s, so the pod must
    fail non-zero -> Kueue reads failed_at instead of recording NULL-everything as complete.
    """
    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]

    respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA, "audio_ext": "mp3"}),
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    put = respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    def _empty_result() -> dict:
        r = _fake_result()
        r["windows"] = []
        r["fine_windows_analyzed"] = 0
        r["fine_windows_total"] = 0
        r["coarse_windows_analyzed"] = 0
        r["coarse_windows_total"] = 0
        return r

    monkeypatch.setattr(jr, "_load_analyze_file", lambda: lambda *_a, **_k: _empty_result())

    with pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == jr.EXIT_ANALYSIS == 12
    # The empty result must NEVER be PUT back as a completion.
    assert not put.called


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


@pytest.mark.parametrize(
    ("scenario", "env_value"),
    [
        ("missing_file_id", None),
        ("invalid_file_id", "not-a-uuid"),
    ],
)
async def test_precondition_failures_exit_config(job_env, monkeypatch, scenario, env_value):  # type: ignore[no-untyped-def]
    """Startup/precondition failures map to EXIT_CONFIG (20), not EXIT_DOWNLOAD (10) (WR-02).

    A missing PHAZE_JOB_FILE_ID or a malformed UUID is a PERMANENT
    misconfiguration; it must be distinct from the transient download code so a
    Kueue controller never re-drives a Job that can never succeed.
    """
    import phaze.job_runner as jr

    if env_value is None:
        monkeypatch.delenv("PHAZE_JOB_FILE_ID", raising=False)
    else:
        monkeypatch.setenv("PHAZE_JOB_FILE_ID", env_value)

    with pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == jr.EXIT_CONFIG == 20
    assert exc.value.code != jr.EXIT_DOWNLOAD


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


# ---------------------------------------------------------------------------
# Phase 57.1 (PROG-01): the k8s one-shot lane progress bridge.
# ---------------------------------------------------------------------------


def _emitting_analyze(counts):  # type: ignore[no-untyped-def]
    """Build a fake ``analyze_file`` that invokes its ``progress_cb`` for each count, then returns."""

    def _analyze(*_a, progress_cb=None, **_k):  # type: ignore[no-untyped-def]
        assert progress_cb is not None, "the k8s bridge must thread a progress_cb into analyze_file"
        for analyzed, total in counts:
            progress_cb(analyzed, total)
        return _fake_result()

    return _analyze


@respx.mock
async def test_progress_posts_midflight_via_run_coroutine_threadsafe(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """≥2 mid-flight counter POSTs land during one run; analyze runs off-loop via to_thread (PROG-01)."""
    monkeypatch.setenv("PHAZE_ANALYSIS_PROGRESS_INTERVAL_SEC", "0")  # no throttle: every emitted count posts
    from phaze.config import get_settings

    get_settings.cache_clear()

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]

    respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA}),
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    progress = respx.post(f"{base}/api/internal/agent/analysis/{file_id}/progress").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )
    respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    monkeypatch.setattr(jr, "_load_analyze_file", lambda: _emitting_analyze([(0, 3), (1, 3), (2, 3), (3, 3)]))

    with pytest.raises(SystemExit) as exc:
        await jr.run()
    # Drain any progress tasks still scheduled via run_coroutine_threadsafe.
    await asyncio.sleep(0.1)

    assert exc.value.code == 0
    assert progress.call_count >= 2, "at least two distinct mid-flight progress POSTs must land"
    # The counter payloads carry the advancing fine counts (denominator == fine_windows_total).
    bodies = [c.request.content for c in progress.calls]
    assert any(b'"fine_windows_analyzed":0' in body for body in bodies), "the START count (0, N) must be posted"


@respx.mock
async def test_progress_failure_does_not_change_exit_code(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """A failing progress POST is swallowed and never changes the success exit (best-effort, D-16)."""
    monkeypatch.setenv("PHAZE_ANALYSIS_PROGRESS_INTERVAL_SEC", "0")
    from phaze.config import get_settings

    get_settings.cache_clear()

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]

    respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA}),
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    # Every progress POST 500s (and keeps 500ing through the client's retries) — must NOT fail the job.
    respx.post(f"{base}/api/internal/agent/analysis/{file_id}/progress").mock(return_value=httpx.Response(500))
    respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    monkeypatch.setattr(jr, "_load_analyze_file", lambda: _emitting_analyze([(0, 2), (1, 2), (2, 2)]))

    with pytest.raises(SystemExit) as exc:
        await jr.run()
    await asyncio.sleep(0.1)

    # The completion path still wins: exit 0 despite the progress endpoint failing.
    assert exc.value.code == 0


@respx.mock
async def test_progress_connect_timeout_does_not_change_exit_code(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """Phase 99 OBS-01 criterion 4: a persistent ConnectTimeout on the progress POST -- the exact
    transport-error class the GIL-starved event loop produces -- is swallowed and never changes the
    success exit (best-effort, D-16). Clone of ``test_progress_failure_does_not_change_exit_code``
    with the progress route raising ``httpx.ConnectTimeout`` instead of returning a 500."""
    monkeypatch.setenv("PHAZE_ANALYSIS_PROGRESS_INTERVAL_SEC", "0")
    from phaze.config import get_settings

    get_settings.cache_clear()

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]

    respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA}),
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    # Every progress POST hits a ConnectTimeout (the sustained event-loop-starvation class) — must NOT fail the job.
    respx.post(f"{base}/api/internal/agent/analysis/{file_id}/progress").mock(side_effect=httpx.ConnectTimeout("simulated"))
    respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    monkeypatch.setattr(jr, "_load_analyze_file", lambda: _emitting_analyze([(0, 2), (1, 2), (2, 2)]))

    with pytest.raises(SystemExit) as exc:
        await jr.run()
    await asyncio.sleep(0.1)

    # The completion path still wins: exit 0 despite the progress endpoint's persistent ConnectTimeout.
    assert exc.value.code == 0


def test_progress_bridge_source_guard():  # type: ignore[no-untyped-def]
    """Source guard: analyze runs via to_thread; progress posts via run_coroutine_threadsafe, never .result()."""
    import phaze.job_runner as jr

    src = Path(jr.__file__).read_text(encoding="utf-8")
    assert "asyncio.to_thread(" in src, "analyze_file must run off-loop via asyncio.to_thread (Pitfall 3)"
    assert "run_coroutine_threadsafe" in src, "progress POSTs must schedule via run_coroutine_threadsafe"
    # Fire-and-forget: the scheduled future must not be chained to .result() (that would deadlock the
    # analysis thread on a saturated loop). The docstring legitimately names ``.result()`` in prose, so
    # guard on the dangerous CALL form (a closing paren immediately followed by .result()).
    assert ").result()" not in src, "the cb must be fire-and-forget — chaining .result() would deadlock the analysis thread"
    assert "post_analysis_progress" in src, "the k8s lane must post counter-only progress"


# ---------------------------------------------------------------------------
# Phase 100 (phaze-sfbx.3, OBS-02): human-friendly banner, step lines, and windowed
# progress lines. The console-readability layer over the machine JSON log.
# ---------------------------------------------------------------------------


_FULL_METADATA = {
    "original_filename": "coachella-2019-full-set.mp3",
    "current_path": "/mnt/library/live/coachella-2019-full-set.mp3",
    "source_agent_id": "fileserver-01",
    "duration_sec": 3723.5,
    "file_size": 136_839_168,  # ~130.5 MiB
    "staging_bucket": "phaze-staging",
    "backend_id": "cluster-01",
}


def _wire_happy_flow(respx_mock, base, file_id, *, presign_json):  # type: ignore[no-untyped-def]
    """Stub presign/download/PUT for a success run; the caller supplies the presign body."""
    respx_mock.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json=presign_json),
    )
    respx_mock.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    respx_mock.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, True),  # ABSENT -> the one-shot pod defaults friendly ON
        ("", True),  # blank is treated as absent -> ON
        ("0", False),  # explicit off wins even for this pod
        ("false", False),
        ("no", False),
        ("1", True),
        ("true", True),
    ],
)
def test_resolve_friendly_default_precedence(monkeypatch, env_value, expected):  # type: ignore[no-untyped-def]
    """The pod defaults friendly ON, but an explicit PHAZE_LOG_FRIENDLY=0 still turns it OFF (OBS-02)."""
    import phaze.job_runner as jr

    if env_value is None:
        monkeypatch.delenv("PHAZE_LOG_FRIENDLY", raising=False)
    else:
        monkeypatch.setenv("PHAZE_LOG_FRIENDLY", env_value)

    assert jr._resolve_friendly_default() is expected


def test_main_configures_friendly_from_pod_default(monkeypatch):  # type: ignore[no-untyped-def]
    """``main()`` threads the pod's friendly default into ``configure_logging`` (OBS-02)."""
    import phaze.job_runner as jr

    captured: dict = {}

    def _fake_configure(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)

    monkeypatch.delenv("PHAZE_LOG_FRIENDLY", raising=False)
    monkeypatch.setattr(jr, "configure_logging", _fake_configure)
    monkeypatch.setattr(jr.asyncio, "run", lambda _coro: _coro.close())

    jr.main()

    # Absent env -> the pod default (True) is passed explicitly, never left to configure_logging's
    # own default-off env fallback.
    assert captured.get("friendly") is True


@respx.mock
async def test_banner_emitted_with_full_metadata(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """The banner carries every present metadata field, right after presign (OBS-02)."""
    import structlog

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]
    _wire_happy_flow(respx, base, file_id, presign_json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA, "metadata": _FULL_METADATA})
    monkeypatch.setattr(jr, "_load_analyze_file", lambda: lambda *_a, **_k: _fake_result())

    with structlog.testing.capture_logs() as logs, pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == 0
    banners = [e for e in logs if e["event"] == "job_runner_banner"]
    assert len(banners) == 1
    banner = banners[0]
    assert banner["file_id"] == str(file_id)
    assert banner["filename"] == "coachella-2019-full-set.mp3"
    assert banner["source_path"] == "/mnt/library/live/coachella-2019-full-set.mp3"
    assert banner["source_agent_id"] == "fileserver-01"
    assert banner["duration_sec"] == 3723.5
    assert banner["file_size_mb"] == 130.5
    assert banner["staging_bucket"] == "phaze-staging"
    assert banner["backend_id"] == "cluster-01"


@respx.mock
async def test_banner_degrades_to_uuid_only_without_metadata(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """An older control plane omits the metadata block -> the banner is file_id-only, never a failure (OBS-02)."""
    import structlog

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]
    _wire_happy_flow(respx, base, file_id, presign_json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA})
    monkeypatch.setattr(jr, "_load_analyze_file", lambda: lambda *_a, **_k: _fake_result())

    with structlog.testing.capture_logs() as logs, pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == 0
    banners = [e for e in logs if e["event"] == "job_runner_banner"]
    assert len(banners) == 1
    banner = banners[0]
    # UUID-only worst case: file_id present, but NONE of the human identity fields leak in.
    assert banner["file_id"] == str(file_id)
    for absent in ("filename", "source_path", "source_agent_id", "duration_sec", "file_size_mb", "staging_bucket", "backend_id"):
        assert absent not in banner


@respx.mock
async def test_banner_degrades_field_by_field_on_partial_metadata(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """A partial metadata block (no backend_id/duration) still emits the present fields (OBS-02)."""
    import structlog

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]
    partial = {"original_filename": "song.mp3", "file_size": 5_242_880, "staging_bucket": "phaze-staging"}  # 5 MiB
    _wire_happy_flow(respx, base, file_id, presign_json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA, "metadata": partial})
    monkeypatch.setattr(jr, "_load_analyze_file", lambda: lambda *_a, **_k: _fake_result())

    with structlog.testing.capture_logs() as logs, pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == 0
    banner = next(e for e in logs if e["event"] == "job_runner_banner")
    assert banner["filename"] == "song.mp3"
    assert banner["file_size_mb"] == 5.0
    assert banner["staging_bucket"] == "phaze-staging"
    # Absent individual fields are simply omitted, not None-valued.
    assert "backend_id" not in banner
    assert "duration_sec" not in banner
    assert "source_path" not in banner


@respx.mock
async def test_step_events_preserve_machine_keys_and_add_human_fields(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """Every ``job_runner_step_ok`` keeps event/step/elapsed_ms; download carries downloaded_mb (OBS-02)."""
    import structlog

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]
    _wire_happy_flow(respx, base, file_id, presign_json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA, "metadata": _FULL_METADATA})
    monkeypatch.setattr(jr, "_load_analyze_file", lambda: lambda *_a, **_k: _fake_result())

    with structlog.testing.capture_logs() as logs, pytest.raises(SystemExit) as exc:
        await jr.run()

    assert exc.value.code == 0
    steps = {e["step"]: e for e in logs if e["event"] == "job_runner_step_ok"}
    # Machine keys preserved on ALL five steps (parsers depend on them).
    assert set(steps) == {"presign", "download", "verify", "analyze", "callback"}
    for step_event in steps.values():
        assert step_event["event"] == "job_runner_step_ok"
        assert "step" in step_event
        assert isinstance(step_event["elapsed_ms"], int)
    # Additive human fields.
    assert steps["download"]["downloaded_mb"] == pytest.approx(len(_AUDIO) / (1024 * 1024), abs=0.1)
    assert steps["verify"]["sha256"] == _GOOD_SHA[:12]
    assert steps["analyze"]["fine_windows_total"] == 1
    assert steps["callback"]["result"] == "analysis written"


@respx.mock
async def test_progress_lines_throttled_and_final_always_emitted(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """Console progress lines share the POST throttle; the final N/N (100%) line always emits (OBS-02)."""
    import structlog

    # A large interval: only the first (unthrottled) count and the final count get through.
    monkeypatch.setenv("PHAZE_ANALYSIS_PROGRESS_INTERVAL_SEC", "3600")
    from phaze.config import get_settings

    get_settings.cache_clear()

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]
    respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA}),
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    respx.post(f"{base}/api/internal/agent/analysis/{file_id}/progress").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )
    respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    monkeypatch.setattr(jr, "_load_analyze_file", lambda: _emitting_analyze([(0, 5), (1, 5), (2, 5), (5, 5)]))

    with structlog.testing.capture_logs() as logs, pytest.raises(SystemExit) as exc:
        await jr.run()
    await asyncio.sleep(0.1)

    assert exc.value.code == 0
    progress = [e for e in logs if e["event"] == "job_runner_progress"]
    # Throttled: NOT one line per emitted count -- only the first + the final pass the gate.
    assert len(progress) == 2
    assert progress[0]["fine_windows_analyzed"] == 0
    assert progress[0]["percent"] == 0.0
    # The final count ALWAYS emits (is_final bypasses the throttle) at 100%.
    final = progress[-1]
    assert final["fine_windows_analyzed"] == 5
    assert final["fine_windows_total"] == 5
    assert final["percent"] == 100.0


@respx.mock
async def test_progress_line_failure_never_escapes_callback(job_env, monkeypatch):  # type: ignore[no-untyped-def]
    """A rendering failure in the progress line is swallowed and never changes the exit code (OBS-02)."""
    monkeypatch.setenv("PHAZE_ANALYSIS_PROGRESS_INTERVAL_SEC", "0")
    from phaze.config import get_settings

    get_settings.cache_clear()

    import phaze.job_runner as jr

    file_id = job_env["file_id"]
    base = job_env["base_url"]
    respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
        return_value=httpx.Response(200, json={"download_url": _DOWNLOAD_URL, "expected_sha256": _GOOD_SHA}),
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_AUDIO))
    respx.post(f"{base}/api/internal/agent/analysis/{file_id}/progress").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )
    respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )

    # Force ONLY the progress LOG call to raise (delegate every other event/method to the real
    # logger); the swallow contract must keep the run at exit 0 despite the rendering failure.
    class _BoomOnProgress:
        def __init__(self, real):  # type: ignore[no-untyped-def]
            self._real = real

        def info(self, event, *args, **kwargs):  # type: ignore[no-untyped-def]
            if event == "job_runner_progress":
                raise RuntimeError("render failure")
            return self._real.info(event, *args, **kwargs)

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(self._real, name)

    monkeypatch.setattr(jr, "log", _BoomOnProgress(jr.log))
    monkeypatch.setattr(jr, "_load_analyze_file", lambda: _emitting_analyze([(0, 2), (1, 2), (2, 2)]))

    with pytest.raises(SystemExit) as exc:
        await jr.run()
    await asyncio.sleep(0.1)

    assert exc.value.code == 0
