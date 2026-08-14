"""phaze-l832u.3: the extraction → analysis handoff, on REAL ffmpeg and REAL essentia.

**Nothing in the suite caught a change that broke every audio file in the corpus.** 2026.8.3
shipped phaze-w55w1 (exhaustive analysis) alongside phaze-3ea41 (unconditional pre-analysis
audio extraction). Extraction remuxes to Matroska; ``_probe_duration_sec`` read duration from
``es.MetadataReader``; TagLib cannot read a duration out of Matroska and returns 0; zero
duration yields zero natural windows; the zero-window guard failed every file. The pipeline was
dead for 11.5 hours and the suite stayed green throughout, because phaze-3ea41's own acceptance
criterion ("existing audio-file analysis is unchanged") was tested against MOCKS on both sides
of the seam that actually broke.

That is the same lesson D-09 recorded for the memory fix: **a mocked essentia cannot hold this
line** (``test_analysis_streaming_decode.py`` runs the real network for exactly that reason).
This module is the equivalent guard for the extraction → analysis handoff, and it is written so
that no mock can satisfy it:

* the fixtures are produced by REAL ``ffmpeg`` at test time (no committed audio),
* the extraction is the REAL :func:`extract_audio_track` (real ``ffprobe`` + ``ffmpeg``),
* the duration probe is the REAL :func:`_probe_duration_sec`,
* and the assertion is on REAL DECODED PCM out of the handed path — sample counts that only a
  genuine decode of genuine audio produces.

**What fails without the fix.** Against pre-phaze-l832u code every test here fails on the same
line: the probe returns ``0.0`` for the ``.mka`` extraction intermediate, so
``_iter_windows`` yields nothing and there is no PCM to decode. Verified by reverting
``services/analysis.py``'s probe and ``services/video_audio.py``'s skip branch and re-running
this module — see the bead for the recorded output.

**Container shapes.** The parametrisation is the set of shapes the pipeline can actually hand
the analyzer, both branches of the extraction step included: a plain audio file and an audio
file with embedded cover art (both SKIP extraction now), a video container and a multi-track
container (both still EXTRACT to ``.mka``). Runtime stays proportionate — every fixture is a
few seconds of generated sine, and the one test that exercises the >60-window chunk path does
it with a short window size rather than a long file.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import Any
import uuid

import numpy as np
import pytest

from phaze.services.analysis import _FINE_CHUNK_WINDOWS, _FINE_SAMPLE_RATE, _decode_windows, _iter_windows, _probe_duration_sec
from phaze.services.video_audio import extract_audio_track


_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

# The fixtures need these ENCODERS, not just the binary. ``libmp3lame`` and ``libx264`` are
# compile-time options and absent from some minimal ffmpeg packages; skipping on their absence
# (rather than erroring out of a fixture) keeps this module's CI treatment identical to
# test_video_audio.py's real-ffmpeg section, which skips when ffmpeg itself is missing.
_REQUIRED_ENCODERS = ("libmp3lame", "libx264", "aac", "png")


def _missing_encoders() -> list[str]:
    if not _HAS_FFMPEG:
        return []
    # Fixed argv, no shell, no interpolation (a test fixture).
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-encoders"]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    return [name for name in _REQUIRED_ENCODERS if f" {name} " not in proc.stdout]


_MISSING = _missing_encoders()

pytestmark = pytest.mark.skipif(
    not _HAS_FFMPEG or bool(_MISSING),
    reason=f"ffmpeg/ffprobe not installed on this runner (missing encoders: {_MISSING})"
    if _HAS_FFMPEG
    else "ffmpeg/ffprobe not installed on this runner",
)

# Fixture length. Long enough that a duration probe reading 0 is unmistakable and that two fine
# windows decode out of it, short enough that generating and decoding it is trivial.
_FIXTURE_SEC = 12
# Tolerance on the probed duration. Lossy container round-trips move the last few ms (the
# measured .mka in the bead reads 90.044 for a 90 s source); this is about the probe returning
# THE FILE'S LENGTH rather than 0, not about millisecond fidelity.
_DURATION_TOLERANCE_SEC = 1.0


def _run_ffmpeg(*args: str) -> None:
    """Run a fixed ffmpeg argv, raising with its stderr when it fails."""
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    if proc.returncode != 0:  # pragma: no cover - a broken fixture, not a tested path
        msg = f"fixture ffmpeg failed (exit {proc.returncode}): {proc.stderr.strip()}"
        raise RuntimeError(msg)


def _sine(freq: int, seconds: int = _FIXTURE_SEC) -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}"]


def _make_plain_audio(tmp_path: Path) -> str:
    """A bare .mp3: one audio stream, nothing else. The overwhelming majority of the corpus."""
    dest = tmp_path / "plain.mp3"
    _run_ffmpeg(*_sine(220), "-c:a", "libmp3lame", "-q:a", "9", str(dest))
    return str(dest)


def _make_audio_with_cover_art(tmp_path: Path) -> str:
    """An .mp3 carrying embedded artwork — an ``attached_pic`` "video" stream, not a video.

    Ubiquitous in a real archive, and the shape a naive "any video stream means extract"
    predicate would put back through the remux for essentially the whole corpus.
    """
    cover = tmp_path / "cover.png"
    _run_ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1", "-frames:v", "1", str(cover))
    dest = tmp_path / "with_cover.mp3"
    _run_ffmpeg(
        *_sine(240),
        "-i",
        str(cover),
        "-map",
        "0:a",
        "-map",
        "1:v",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "9",
        "-c:v",
        "copy",
        "-disposition:v:0",
        "attached_pic",
        str(dest),
    )
    return str(dest)


def _make_video_container(tmp_path: Path) -> str:
    """An .mp4 with a real video track — phaze-3ea41's actual feature; still extracted."""
    dest = tmp_path / "concert.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=160x120:rate=10:duration={_FIXTURE_SEC}",
        *_sine(260),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(dest),
    )
    return str(dest)


def _make_multi_audio(tmp_path: Path) -> str:
    """An .mkv with TWO audio tracks — which one essentia would pick is exactly the opacity
    ``video_audio.py`` exists to remove, so this shape is always extracted."""
    dest = tmp_path / "multi.mkv"
    _run_ffmpeg(
        *_sine(280),
        *_sine(300),
        "-map",
        "0:a",
        "-map",
        "1:a",
        # ffmpeg's NATIVE aac encoder — always present in any build, unlike libvorbis/libopus,
        # which are compile-time options and absent from some distro and CI ffmpeg packages.
        "-c:a",
        "aac",
        "-disposition:a:1",
        "default",
        str(dest),
    )
    return str(dest)


# (id, builder, does this shape SKIP the remux?)
_SHAPES: list[tuple[str, Any, bool]] = [
    ("plain_audio", _make_plain_audio, True),
    ("audio_with_cover_art", _make_audio_with_cover_art, True),
    ("video_container", _make_video_container, False),
    ("multi_audio", _make_multi_audio, False),
]


def _unlink(path: str | None) -> None:
    if path is not None:
        Path(path).unlink(missing_ok=True)


@pytest.mark.parametrize(("builder", "skips_remux"), [(b, s) for _, b, s in _SHAPES], ids=[i for i, _, _ in _SHAPES])
async def test_the_path_extraction_hands_analysis_probes_a_real_duration(tmp_path: Path, builder: Any, skips_remux: bool) -> None:
    """THE regression guard: whatever extraction hands forward, the duration probe reads it.

    This is the single assertion 2026.8.3 needed and did not have. It fails against the
    pre-fix code on every shape — the extracted ``.mka`` probes 0 through TagLib, and pre-fix
    EVERY shape produced an ``.mka`` because extraction was unconditional.
    """
    source = builder(tmp_path)
    audio_source = await extract_audio_track(source)
    try:
        assert (audio_source.cleanup_path is None) is skips_remux, "the skip/extract branch taken is not the one this shape should take"

        duration = _probe_duration_sec(audio_source.analysis_path)

        assert duration > 0, "the analyzer was handed a path whose duration probes 0 — this is the 2026.8.3 outage"
        assert abs(duration - _FIXTURE_SEC) < _DURATION_TOLERANCE_SEC
    finally:
        _unlink(audio_source.cleanup_path)


@pytest.mark.parametrize("builder", [b for _, b, _ in _SHAPES], ids=[i for i, _, _ in _SHAPES])
async def test_the_handed_path_decodes_to_real_pcm_for_every_natural_window(tmp_path: Path, builder: Any) -> None:
    """The probe's duration must survive contact with a REAL decode of the handed path.

    A mocked essentia cannot satisfy this: the assertion is on the SAMPLE COUNT of genuinely
    decoded float32 PCM, window by window, and on the windows being derived from the probed
    duration rather than from a constant. Together with the probe test above this closes the
    full seam — extraction output → duration → natural windows → decoded audio.
    """
    source = builder(tmp_path)
    audio_source = await extract_audio_track(source)
    try:
        duration = _probe_duration_sec(audio_source.analysis_path)
        windows = _iter_windows(duration, win_sec=5, min_sec=1, drop_short_trailing=True)
        assert len(windows) >= 2, f"a {_FIXTURE_SEC}s file must yield >= 2 natural 5s windows, got {len(windows)}"

        skipped: list[int] = []
        buffers = _decode_windows(
            audio_source.analysis_path,
            _FINE_SAMPLE_RATE,
            windows,
            lambda idx, _s, _e, _x: skipped.append(idx),
        )

        assert not skipped, f"windows {skipped} produced no audio out of the handed path"
        assert set(buffers) == {idx for idx, _s, _e in windows}
        for idx, start, end in windows:
            buf = np.asarray(buffers[idx])
            assert buf.dtype == np.float32
            # Real decoded PCM: (end - start) seconds at the fine tier's rate, within one
            # resampler frame. A stub returning an empty or fixed-length array fails here.
            assert abs(len(buf) - (end - start) * _FINE_SAMPLE_RATE) < _FINE_SAMPLE_RATE * 0.05
            assert float(np.abs(buf).max()) > 0.0, "decoded silence — the handoff carried no audio"
    finally:
        _unlink(audio_source.cleanup_path)


def test_the_duration_probe_fails_loudly_rather_than_returning_zero(tmp_path: Path) -> None:
    """A probe failure stays FATAL — the contract the ffprobe rewrite had to preserve.

    This is why a mis-suffixed or truncated download fails with a stored ``error_message``
    instead of recording a NULL-everything analysis as a success: a 0 would sail through
    ``_iter_windows`` and out the other side as "analyzed nothing, all good". Unmocked on
    purpose — a stub cannot tell you what ffprobe does with garbage.
    """
    from phaze.services.analysis import AnalysisProbeError

    truncated = tmp_path / "truncated.mp3"
    truncated.write_bytes(b"\x00\x01\x02 definitely not an audio frame")

    with pytest.raises(AnalysisProbeError):
        _probe_duration_sec(str(truncated))
    with pytest.raises(AnalysisProbeError, match="ffprobe failed"):
        _probe_duration_sec(str(tmp_path / "does-not-exist.mp3"))


async def test_the_duration_probe_never_goes_through_taglib(tmp_path: Path) -> None:
    """The probe must not be ``es.MetadataReader`` — asserted by MECHANISM, not by outcome.

    **This is the test that fails against pre-fix code on every platform**, and it exists
    because the outage does NOT reproduce everywhere. The TagLib that essentia bundles reads a
    Matroska duration on some builds and returns 0 on others; the deployed linux image is one of
    the latter (measured: ``.mka`` → 0 through MetadataReader, 90.044 through ffprobe on the
    same file), while a developer's macOS wheel can happily return the right answer and leave
    every outcome-based test above GREEN on pre-fix code. A guard that only holds on the
    platform where the bug happens to reproduce is how this shipped in the first place.

    So this one pins the mechanism: ``MetadataReader`` is replaced by something that raises if
    called at all, and the probe must still return the real duration of a real ``.mka``. Pre-fix
    it raises; post-fix nothing ever reaches it. The audio and the probe are otherwise
    completely real — the stub is not standing in for essentia, it is a tripwire on a path that
    must be dead.
    """
    from unittest.mock import MagicMock, patch

    import phaze.services.analysis as analysis_mod

    source = _make_video_container(tmp_path)
    audio_source = await extract_audio_track(source)
    assert audio_source.cleanup_path is not None, "a video container must still be extracted"
    try:
        assert Path(audio_source.analysis_path).suffix == ".mka", "the extraction intermediate is Matroska — the container TagLib cannot measure"

        tripwire = MagicMock(side_effect=AssertionError("the duration probe reached es.MetadataReader (TagLib reads 0 from Matroska)"))
        with patch.object(analysis_mod.es, "MetadataReader", tripwire):
            duration = _probe_duration_sec(audio_source.analysis_path)

        assert abs(duration - _FIXTURE_SEC) < _DURATION_TOLERANCE_SEC
        tripwire.assert_not_called()
    finally:
        _unlink(audio_source.cleanup_path)


# ---------------------------------------------------------------------------
# Both lanes' handoff: the path each lane actually hands to the analysis driver
# ---------------------------------------------------------------------------


def _probe_at_the_seam(seen: dict[str, Any]) -> Any:
    """Build a fake analysis driver that REAL-probes and REAL-decodes the path it is handed.

    The probe has to happen HERE, inside the seam, because both lanes delete the extraction
    scratch file in their outer ``finally`` — by the time the test resumes there may be nothing
    left to probe. Doing it at the seam is also the more honest assertion: it measures the exact
    path, at the exact moment, the real analysis child would have received it.
    """

    async def _driver(file_path: str, _models_dir: str, **_kwargs: Any) -> dict[str, Any]:
        duration = _probe_duration_sec(file_path)
        windows = _iter_windows(duration, win_sec=5, min_sec=1, drop_short_trailing=True)
        buffers = _decode_windows(file_path, _FINE_SAMPLE_RATE, windows, lambda *_a: None)
        seen["path"] = file_path
        seen["duration"] = duration
        seen["windows"] = len(windows)
        seen["decoded"] = len(buffers)
        return {
            "bpm": 120.0,
            "musical_key": "C major",
            "mood": None,
            "style": None,
            "danceability": None,
            "features": {},
            "windows": [],
            "fine_windows_analyzed": len(buffers),
            "fine_windows_total": len(windows),
            "coarse_windows_analyzed": 0,
            "coarse_windows_total": 1,
        }

    return _driver


@pytest.mark.parametrize("builder", [b for _, b, _ in _SHAPES], ids=[i for i, _, _ in _SHAPES])
async def test_local_lane_reaches_analysis_with_a_probeable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_env: dict, builder: Any
) -> None:
    """LOCAL (SAQ) lane: ``process_file`` hands the analysis driver a path that probes correctly.

    Real extraction, real probe, real decode — only the analysis driver itself is replaced, and
    it is replaced by something that measures rather than pretends.
    """
    from unittest.mock import AsyncMock, MagicMock

    import phaze.tasks.functions as functions

    source = builder(tmp_path)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(functions, "run_analysis_subprocess", _probe_at_the_seam(seen))

    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    result = await functions.process_file(
        {"api_client": api},
        file_id=str(uuid.uuid4()),
        original_path=source,
        file_type="mp3",
        agent_id="test-agent",
        models_path=job_env["models_dir"],
    )

    assert result["status"] == "analyzed"
    assert seen["duration"] > 0, "the local lane handed analysis a path whose duration probes 0"
    assert abs(seen["duration"] - _FIXTURE_SEC) < _DURATION_TOLERANCE_SEC
    assert seen["decoded"] == seen["windows"] >= 2


@pytest.mark.parametrize("builder", [b for _, b, _ in _SHAPES], ids=[i for i, _, _ in _SHAPES])
async def test_cloud_lane_reaches_analysis_with_a_probeable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_env: dict, builder: Any
) -> None:
    """CLOUD (k8s one-shot job runner) lane: the same guarantee, through the download path.

    The burst lane is where the outage was most expensive — 8,683 queued jobs and 11.5 hours of
    pods failing in a redrive loop — so it gets its own coverage rather than being assumed
    symmetric with the local lane.
    """
    import hashlib

    import httpx
    import respx

    import phaze.job_runner as jr

    source = Path(builder(tmp_path))
    payload = source.read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    suffix = source.suffix.lstrip(".")

    seen: dict[str, Any] = {}
    monkeypatch.setattr(jr, "run_analysis_subprocess", _probe_at_the_seam(seen))

    file_id = job_env["file_id"]
    base = job_env["base_url"]
    download_url = "http://bucket.test/staged-object"
    with respx.mock:
        respx.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
            return_value=httpx.Response(200, json={"download_url": download_url, "expected_sha256": sha, "audio_ext": suffix}),
        )
        respx.get(download_url).mock(return_value=httpx.Response(200, content=payload))
        respx.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
            return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
        )

        with pytest.raises(SystemExit) as exc:
            await jr.run()

    assert exc.value.code == 0
    assert seen["duration"] > 0, "the cloud lane handed analysis a path whose duration probes 0"
    assert abs(seen["duration"] - _FIXTURE_SEC) < _DURATION_TOLERANCE_SEC
    assert seen["decoded"] == seen["windows"] >= 2


# ---------------------------------------------------------------------------
# Exhaustive coverage actually executing (phaze-w55w1, which has never run in prod)
# ---------------------------------------------------------------------------


async def test_a_plain_audio_file_analyzes_every_natural_window_across_more_than_one_chunk(tmp_path: Path) -> None:
    """End-to-end ``analyze_file`` on real audio: ``fine_windows_analyzed == fine_windows_total``,
    over MORE THAN ONE fine chunk.

    phaze-w55w1 removed the 60-window caps and 2026.8.3 shipped that removal — but the probe
    bug meant it never once executed: every completed analysis in the corpus still tops out at
    exactly 60 fine windows. This proves the chunk loop runs and covers the whole file, using a
    SHORT window size on a 5-minute fixture rather than the >30-minute file the same window
    count would need at production settings (``_FINE_CHUNK_WINDOWS`` is a count, not a
    duration, so the boundary is crossed identically either way).

    The coarse tier is deliberately reduced to a single window: the 34 TensorFlow model sets
    are gigabytes of weights this suite does not ship, so coarse windows fail and are skipped —
    which is itself the per-window failure isolation working, and leaves the FINE counters,
    the ones this test is about, untouched.
    """
    from phaze.services.analysis import analyze_file

    duration_sec = 310  # / 5s windows = 62 fine windows = 2 chunks at _FINE_CHUNK_WINDOWS == 60
    source = tmp_path / "long_plain.mp3"
    _run_ffmpeg(*_sine(220, duration_sec), "-c:a", "libmp3lame", "-q:a", "9", str(source))

    audio_source = await extract_audio_track(str(source))
    assert audio_source.cleanup_path is None, "a bare .mp3 must not be remuxed"

    result = analyze_file(audio_source.analysis_path, str(tmp_path / "no-models"), fine_window_sec=5, coarse_window_sec=duration_sec, fine_min_sec=1)

    assert result["fine_windows_total"] == duration_sec // 5
    assert result["fine_windows_total"] > _FINE_CHUNK_WINDOWS, "the fixture must cross a chunk boundary or this proves nothing"
    assert result["fine_windows_analyzed"] == result["fine_windows_total"], "exhaustive coverage means every natural window was analyzed"
