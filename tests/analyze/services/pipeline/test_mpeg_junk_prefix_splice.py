"""phaze-oap6y: an MP3 behind a junk prefix is analyzed from its real frames, on REAL tools.

Two misdetection shapes are measured in ``docs/spikes/phaze-di48m-mp3-junk-prefix-scan.md``,
and each gets a synthetic fixture here, built at test time (no archive file, no committed audio):

* **The trigger shape.** An ID3v2 tag ending at byte 4,096, junk that holds 2-3-frame "Layer 1"
  chains, then a real MPEG-1 Layer 3 stream (44.1 kHz, 320 kbps). Real ffprobe calls it ``mp1``
  with a duration many times the real one, and real essentia's ``AudioLoader`` SIGSEGVs on it
  (reproduced on the macOS arm64 wheel while writing this; the same loader crashed on the
  trigger file in the deployed image).
* **The VVC shape.** A 58,648-byte ID3v2 tag, 562 junk bytes carrying VVC NAL start codes, then a
  real MPEG-1 Layer 3 stream (48 kHz). Real ffprobe reads it as raw VVC video, ``probe_score``
  51, with no audio stream.

Each test names the consumer it uses, per rule 3 of
``docs/design/0012-verification-fidelity-and-operator-attribution.md``. The splice is checked by
ffprobe (the D-10 analysis probe), by ``es.MetadataReader``, by ``es.AudioLoader`` and streaming
``MonoLoader``, and by ``analyze_file`` end to end. The essentia loaders run in a child process
wherever a crash is possible, so a SIGSEGV fails one test instead of killing the run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from typing import Any
import uuid

import pytest

from phaze.services import mpeg_frames
import phaze.services.video_audio as video_audio
from phaze.services.video_audio import extract_audio_track


_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _has_libmp3lame() -> bool:
    if not _HAS_FFMPEG:
        return False
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-encoders"], capture_output=True, text=True, check=False)  # noqa: S607
    return " libmp3lame " in proc.stdout


pytestmark = pytest.mark.skipif(not _has_libmp3lame(), reason="ffmpeg/ffprobe with libmp3lame not installed on this runner")

# The real stream's length in every fixture. Long enough that a duration several times too long
# is unmistakable, short enough to encode and analyze in seconds.
_STREAM_SEC = 30
# The fine window analyze_file is run with below. "Within one window" is measured in these.
_WINDOW_SEC = 5
_TRIGGER_ID3_END = 4096
_TRIGGER_JUNK_BYTES = 400_000
_VVC_ID3_END = 58_648
_VVC_JUNK_BYTES = 562

# MPEG-1 Layer 1, 32 kbps, 44.1 kHz: a 32-byte frame. The trigger's junk holds short chains of
# frames like this, which is what FFmpeg resyncs into.
_L1_HEADER = b"\xff\xff\x10\x00"
_L1_LEN = 32
# VVC NAL units: start code, then a two-byte header with nuh_temporal_id_plus1 = 1. Types SPS
# (15), PPS (16) and IDR_N_LP (8) together are what FFmpeg's raw-VVC probe scores 51.
_VVC_NALS = (b"\x00\x00\x01\x00\x79", b"\x00\x00\x01\x00\x81", b"\x00\x00\x01\x00\x41")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603


def _encode_stream(dest: Path, *, rate: int, bitrate: str) -> bytes:
    """A real MPEG-1 Layer 3 stream with no ID3 tag of its own (the fixtures prepend one)."""
    proc = _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=330:duration={_STREAM_SEC}",
            "-ar",
            str(rate),
            "-c:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            "-id3v2_version",
            "0",
            str(dest),
        ]
    )
    if proc.returncode != 0:  # pragma: no cover - a broken fixture, not a tested path
        msg = f"fixture ffmpeg failed: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return dest.read_bytes()


def _id3(total: int) -> bytes:
    """An ID3v2.3 tag of exactly ``total`` bytes: one title frame plus padding."""
    title = b"\x03synthetic"
    frame = b"TIT2" + len(title).to_bytes(4, "big") + b"\x00\x00" + title
    body = frame.ljust(total - 10, b"\x00")
    size = len(body)
    return b"ID3\x03\x00\x00" + bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F]) + body


def _noise(length: int, seed: int) -> bytearray:
    """Deterministic noise: the same bytes on every run and every runner."""
    return bytearray(hashlib.shake_256(f"noise-{seed}".encode()).digest(length))


def _trigger_junk() -> bytes:
    """Noise holding 2-3-frame Layer 1 chains every few KB, starting at the first byte."""
    junk = _noise(_TRIGGER_JUNK_BYTES, seed=1)
    choices = _noise(4096, seed=11)
    pos, i = 0, 0
    while pos < _TRIGGER_JUNK_BYTES - 200:
        count = 2 + choices[i] % 2
        chain = b"".join(_L1_HEADER + bytes(_noise(_L1_LEN - 4, seed=1000 + i * 3 + n)) for n in range(count))
        junk[pos : pos + len(chain)] = chain
        pos += len(chain) + 500 + int.from_bytes(choices[i + 1 : i + 3], "big") % 2500
        i += 3
    return bytes(junk)


def _vvc_junk() -> bytes:
    junk = _noise(_VVC_JUNK_BYTES, seed=2)
    for i, nal in enumerate(_VVC_NALS):
        junk[20 + i * 100 : 20 + i * 100 + len(nal)] = nal
    return bytes(junk)


class _Fixture:
    """One synthetic file and the facts its construction makes known exactly."""

    def __init__(self, path: Path, stream: Path, id3_end: int, splice_offset: int) -> None:
        self.path = path
        self.stream = stream
        self.id3_end = id3_end
        self.splice_offset = splice_offset


def _make_trigger(tmp_path: Path) -> _Fixture:
    stream = tmp_path / "stream_44k.mp3"
    frames = _encode_stream(stream, rate=44100, bitrate="320k")
    path = tmp_path / "trigger_shape.mp3"
    path.write_bytes(_id3(_TRIGGER_ID3_END) + _trigger_junk() + frames)
    return _Fixture(path, stream, _TRIGGER_ID3_END, _TRIGGER_ID3_END + _TRIGGER_JUNK_BYTES)


def _make_vvc(tmp_path: Path) -> _Fixture:
    stream = tmp_path / "stream_48k.mp3"
    frames = _encode_stream(stream, rate=48000, bitrate="192k")
    path = tmp_path / "vvc_shape.mp3"
    path.write_bytes(_id3(_VVC_ID3_END) + _vvc_junk() + frames)
    return _Fixture(path, stream, _VVC_ID3_END, _VVC_ID3_END + _VVC_JUNK_BYTES)


_SHAPES = [pytest.param(_make_trigger, id="trigger_shape"), pytest.param(_make_vvc, id="vvc_shape")]


def _ffprobe(path: Path) -> dict[str, Any]:
    proc = _run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_entries",
            "stream=codec_name,codec_type,duration:format=format_name,probe_score,duration",
            str(path),
        ]
    )
    payload: dict[str, Any] = json.loads(proc.stdout or "{}")
    return payload


def _audio_codecs(probe: dict[str, Any]) -> list[str]:
    return [s.get("codec_name", "") for s in probe.get("streams", []) if s.get("codec_type") == "audio"]


def _real_duration(fixture: _Fixture) -> float:
    """The real stream's duration, from ffprobe on the stream file the fixture was built from."""
    return float(_ffprobe(fixture.stream)["format"]["duration"])


# A child process, so a SIGSEGV is a return code rather than the end of the test run.
_LOADER_SCRIPT = """
import json, sys
import essentia
import essentia.standard as es
import essentia.streaming as ess
path = sys.argv[1]
essentia.log.infoActive = False
essentia.log.warningActive = False
audio = es.AudioLoader(filename=path)()
out = {"audio_loader_samples": int(audio[0].shape[0]), "audio_loader_rate": float(audio[1])}
loader = ess.MonoLoader(filename=path, sampleRate=44100)
pool = essentia.Pool()
loader.audio >> (pool, "audio")
essentia.run(loader)
out["mono_loader_samples"] = int(len(pool["audio"]))
print(json.dumps(out))
"""


def _load_in_child(path: str) -> tuple[int, dict[str, Any] | None, str]:
    proc = _run([sys.executable, "-c", _LOADER_SCRIPT, path])
    result = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None
    return proc.returncode, result, proc.stderr[-2000:]


def _unlink(path: str | None) -> None:
    if path is not None:
        Path(path).unlink(missing_ok=True)


# The fixtures reproduce the bug


def test_the_trigger_fixture_is_misdetected_as_layer_1_by_real_ffprobe(tmp_path: Path) -> None:
    fixture = _make_trigger(tmp_path)

    probe = _ffprobe(fixture.path)

    assert _audio_codecs(probe)[:1] == ["mp1"]
    assert float(probe["format"]["duration"]) > 2 * _real_duration(fixture), "the wrong layer must also give the wrong duration"


def test_the_vvc_fixture_is_misprobed_as_raw_video_with_no_audio_by_real_ffprobe(tmp_path: Path) -> None:
    """Reproduced on a synthetic file: format ``vvc``, no audio stream (the three corpus files)."""
    fixture = _make_vvc(tmp_path)

    probe = _ffprobe(fixture.path)

    assert probe["format"]["format_name"] == "vvc"
    assert _audio_codecs(probe) == []


def test_without_the_fix_real_essentia_crashes_or_misdecodes_the_trigger_fixture(tmp_path: Path) -> None:
    """Consumer: real ``es.AudioLoader`` and streaming ``MonoLoader`` on the ORIGINAL bytes.

    The acceptance criterion is "SIGSEGVs or misdecodes", and which one depends on the essentia
    wheel. Either way the original must not decode to the real stream's length.
    """
    fixture = _make_trigger(tmp_path)
    real = _real_duration(fixture)

    returncode, result, _stderr = _load_in_child(str(fixture.path))

    crashed = returncode != 0
    decoded = result["audio_loader_samples"] / result["audio_loader_rate"] if result is not None else None
    misdecoded = decoded is not None and abs(decoded - real) > _WINDOW_SEC
    assert crashed or misdecoded, f"the unfixed original decoded to the real length ({decoded}s) -- the fixture proves nothing"


# The splice


@pytest.mark.parametrize("builder", _SHAPES)
async def test_extraction_splices_the_id3_tag_onto_the_real_frames(tmp_path: Path, builder: Any) -> None:
    """The scratch file is the original ID3v2 bytes plus the bytes from the first real frame,
    in an ``.mp3`` container, and the original is untouched."""
    fixture = builder(tmp_path)
    original = fixture.path.read_bytes()

    source = await extract_audio_track(str(fixture.path), scratch_dir=tmp_path / "scratch")
    try:
        assert source.cleanup_path == source.analysis_path
        spliced = Path(source.analysis_path)
        assert spliced.suffix == ".mp3", "a splice keeps the .mp3 container -- it is never remuxed"
        assert spliced.parent == tmp_path / "scratch"
        assert spliced.read_bytes() == original[: fixture.id3_end] + original[fixture.splice_offset :]
        assert fixture.path.read_bytes() == original
    finally:
        _unlink(source.cleanup_path)


@pytest.mark.parametrize("builder", _SHAPES)
async def test_the_analysis_probe_reads_the_splices_duration(tmp_path: Path, builder: Any) -> None:
    """Consumer: the D-10 analysis probe, which sizes the window geometry.

    On the original it reads a duration several times too long (trigger) or fails for lack of
    an audio stream (VVC). On the splice it reads the real stream's duration.
    """
    from phaze.services.analysis import _probe_duration_sec

    fixture = builder(tmp_path)
    real = _real_duration(fixture)

    source = await extract_audio_track(str(fixture.path), scratch_dir=tmp_path)
    try:
        assert abs(_probe_duration_sec(source.analysis_path) - real) < 0.1
    finally:
        _unlink(source.cleanup_path)


@pytest.mark.parametrize("builder", _SHAPES)
async def test_metadatareader_reads_a_nonzero_duration_from_the_splice(tmp_path: Path, builder: Any) -> None:
    """Consumer: ``es.MetadataReader``, the reader that returned 0 for phaze-3ea41's remux."""
    import essentia.standard as es

    fixture = builder(tmp_path)

    source = await extract_audio_track(str(fixture.path), scratch_dir=tmp_path)
    try:
        # TagLib reads the trigger ORIGINAL correctly too, so the reader must be handed the splice.
        assert source.cleanup_path == source.analysis_path != str(fixture.path)
        duration = es.MetadataReader(filename=source.analysis_path)()[-4]
        assert duration > 0
        assert abs(duration - _STREAM_SEC) <= 1
    finally:
        _unlink(source.cleanup_path)


@pytest.mark.parametrize("builder", _SHAPES)
async def test_real_essentia_loaders_decode_the_splice_to_the_real_length(tmp_path: Path, builder: Any) -> None:
    """Consumer: real ``es.AudioLoader`` and streaming ``MonoLoader``, the loaders that SIGSEGV
    on the trigger. Not mocked: the assertion is on decoded sample counts."""
    fixture = builder(tmp_path)
    real = _real_duration(fixture)

    source = await extract_audio_track(str(fixture.path), scratch_dir=tmp_path)
    try:
        returncode, result, stderr = _load_in_child(source.analysis_path)
    finally:
        _unlink(source.cleanup_path)

    assert returncode == 0, f"essentia failed on the splice (exit {returncode}): {stderr}"
    assert result is not None
    assert abs(result["audio_loader_samples"] / result["audio_loader_rate"] - real) < 0.1
    assert abs(result["mono_loader_samples"] / 44100 - real) < 0.1


@pytest.mark.parametrize("builder", _SHAPES)
async def test_analyze_file_on_the_splice_covers_the_real_duration_within_one_window(tmp_path: Path, builder: Any) -> None:
    """Consumer: real ``analyze_file``, end to end, on what extraction hands it.

    The window geometry comes from the splice's probed duration, so the fine windows cover the
    real stream to within one window, and every one of them is analyzed. The coarse tier is a
    single window because the TensorFlow models are not shipped to this suite.
    """
    from phaze.services.analysis import analyze_file

    fixture = builder(tmp_path)
    real = _real_duration(fixture)

    source = await extract_audio_track(str(fixture.path), scratch_dir=tmp_path)
    try:
        result = analyze_file(
            source.analysis_path, str(tmp_path / "no-models"), fine_window_sec=_WINDOW_SEC, coarse_window_sec=_STREAM_SEC, fine_min_sec=1
        )
    finally:
        _unlink(source.cleanup_path)

    assert abs(result["fine_windows_total"] * _WINDOW_SEC - real) <= _WINDOW_SEC
    assert result["fine_windows_analyzed"] == result["fine_windows_total"]


# Negative controls: what currently works must keep working, untouched


def _tripwire(*_args: Any, **_kwargs: Any) -> Any:
    msg = "the frame-chain parse ran on a file the prefilter should have passed"
    raise AssertionError(msg)


async def test_a_clean_mp3_is_neither_parsed_nor_rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control: ffprobe says mp3, so the prefilter passes it without reading it."""
    frames = _encode_stream(tmp_path / "stream.mp3", rate=44100, bitrate="320k")
    clean = tmp_path / "clean.mp3"
    clean.write_bytes(_id3(_TRIGGER_ID3_END) + frames)
    monkeypatch.setattr(mpeg_frames, "read_scan_window", _tripwire)

    source = await extract_audio_track(str(clean), scratch_dir=tmp_path / "scratch")

    assert source.analysis_path == str(clean)
    assert source.cleanup_path is None
    assert not (tmp_path / "scratch").exists() or not any((tmp_path / "scratch").iterdir())


async def test_junk_that_ffmpeg_reads_through_correctly_is_not_rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """562 junk bytes with no VVC start codes: ffprobe still says mp3, so nothing is spliced.

    9,397 corpus files carry 1-1,024 junk bytes and are read correctly; this is their shape.
    """
    frames = _encode_stream(tmp_path / "stream.mp3", rate=48000, bitrate="192k")
    path = tmp_path / "small_junk.mp3"
    path.write_bytes(_id3(_VVC_ID3_END) + bytes(_noise(_VVC_JUNK_BYTES, seed=3)).replace(b"\x00\x00\x01", b"\x00\x00\x02") + frames)
    assert _audio_codecs(_ffprobe(path)) == ["mp3"]
    monkeypatch.setattr(mpeg_frames, "read_scan_window", _tripwire)

    source = await extract_audio_track(str(path), scratch_dir=tmp_path)

    assert source.analysis_path == str(path)
    assert source.cleanup_path is None


async def test_a_real_mp2_stream_in_an_mp3_file_is_parsed_and_cleared(tmp_path: Path) -> None:
    """The 37 real MP2 files: the prefilter admits them (codec mp2) and the chain parse clears
    them (Layer 2 agrees with mp2), so they are analyzed in place exactly as before."""
    path = tmp_path / "real_mp2.mp3"
    proc = _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=5", "-c:a", "mp2", "-f", "mp2", str(path)])
    assert proc.returncode == 0, proc.stderr
    probe = _ffprobe(path)
    assert _audio_codecs(probe) == ["mp2"]
    assert probe["format"]["format_name"] == "mp3"
    window = mpeg_frames.read_scan_window(path)
    assert window is not None
    assert mpeg_frames.detect_misdetection(window, "mp2").chain_layer == 2

    source = await extract_audio_track(str(path), scratch_dir=tmp_path)

    assert source.analysis_path == str(path)
    assert source.cleanup_path is None


# A hand-built Matroska file: AC3 first, then an MP2 track written with fixed-size LACING, the way
# mkvmerge writes audio by default. ffmpeg's own Matroska muxer never laces, so it cannot make
# this shape; lacing is what puts MPEG frames back to back inside a container.


def _ebml_size(n: int) -> bytes:
    return (n | (1 << 56)).to_bytes(8, "big")


def _el(element_id: int, payload: bytes) -> bytes:
    return element_id.to_bytes((element_id.bit_length() + 7) // 8, "big") + _ebml_size(len(payload)) + payload


def _uint(element_id: int, value: int) -> bytes:
    return _el(element_id, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def _audio_track(number: int, codec_id: str) -> bytes:
    audio = _el(0xE1, _el(0xB5, struct.pack(">d", 48000.0)) + _uint(0x9F, 2))
    return _el(0xAE, _uint(0xD7, number) + _uint(0x73C5, number) + _uint(0x83, 2) + _el(0x86, codec_id.encode()) + audio)


def _simple_block(track: int, timecode_ms: int, frames: list[bytes]) -> bytes:
    if len(frames) == 1:
        return _el(0xA3, bytes([0x80 | track]) + struct.pack(">h", timecode_ms) + b"\x80" + frames[0])
    # Flags 0x84: keyframe, fixed-size lacing. Then the frame count minus one, then the frames.
    return _el(0xA3, bytes([0x80 | track]) + struct.pack(">h", timecode_ms) + b"\x84" + bytes([len(frames) - 1]) + b"".join(frames))


def _raw_frames(tmp_path: Path, codec: str, fmt: str, frame_len: int) -> list[bytes]:
    raw = tmp_path / f"raw.{fmt}"
    proc = _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=2",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            codec,
            "-b:a",
            "192k",
            "-f",
            fmt,
            str(raw),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    data = raw.read_bytes()
    return [data[i : i + frame_len] for i in range(0, len(data) - frame_len + 1, frame_len)]


def _make_mkv_ac3_with_laced_mp2(tmp_path: Path) -> Path:
    ac3 = _raw_frames(tmp_path, "ac3", "ac3", 768)  # 192 kbps at 48 kHz: 768-byte frames, 32 ms each
    mp2 = _raw_frames(tmp_path, "mp2", "mp2", 576)  # 192 kbps at 48 kHz: 576-byte frames, 24 ms each
    assert all(f[:2] == b"\x0b\x77" for f in ac3)
    assert all(mpeg_frames.parse_frame_header(f, 0) is not None for f in mp2)
    blocks = [(i * 32, _simple_block(1, i * 32, [f])) for i, f in enumerate(ac3)]
    blocks += [(j * 240, _simple_block(2, j * 240, mp2[j * 10 : j * 10 + 10])) for j in range(len(mp2) // 10)]
    cluster = _el(0x1F43B675, _uint(0xE7, 0) + b"".join(block for _, block in sorted(blocks, key=lambda b: b[0])))
    header = _el(
        0x1A45DFA3,
        _uint(0x4286, 1) + _uint(0x42F7, 1) + _uint(0x42F2, 4) + _uint(0x42F3, 8) + _el(0x4282, b"matroska") + _uint(0x4287, 4) + _uint(0x4285, 2),
    )
    info = _el(0x1549A966, _uint(0x2AD7B1, 1_000_000) + _el(0x4D80, b"phaze-test") + _el(0x5741, b"phaze-test"))
    tracks = _el(0x1654AE6B, _audio_track(1, "A_AC3") + _audio_track(2, "A_MPEG/L2"))
    path = tmp_path / "ac3_and_laced_mp2.mkv"
    path.write_bytes(header + _el(0x18538067, info + tracks + cluster))
    return path


async def test_an_mkv_with_ac3_and_a_laced_mp2_track_is_not_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The false positive the scope exists for, on real ffprobe and real ffmpeg.

    The file really does hold 8+ back-to-back MP2 frames, and the unscoped detector really would
    call it misdetected (first audio codec ac3 implies no MPEG layer). Scoped, it is never parsed
    and its AC3 track is extracted exactly as before.
    """
    path = _make_mkv_ac3_with_laced_mp2(tmp_path)
    probe = _ffprobe(path)
    assert probe["format"]["format_name"].startswith("matroska")
    assert _audio_codecs(probe) == ["ac3", "mp2"]
    window = mpeg_frames.scan_window_from_bytes(path.read_bytes())
    assert window is not None
    assert mpeg_frames.detect_misdetection(window, "ac3").misdetected, "precondition: unscoped, this file WOULD be spliced"
    monkeypatch.setattr(mpeg_frames, "read_scan_window", _tripwire)

    source = await extract_audio_track(str(path), scratch_dir=tmp_path / "scratch")
    try:
        assert source.cleanup_path == source.analysis_path
        assert Path(source.analysis_path).suffix == ".mka"
        assert _audio_codecs(_ffprobe(Path(source.analysis_path))) == ["ac3"]
    finally:
        _unlink(source.cleanup_path)


async def test_an_audio_less_video_is_not_scanned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No audio stream and not a ``.mp3``: the old NoAudioTrackError, without a 64 MiB scan."""
    path = tmp_path / "silent.mkv"
    proc = _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-c:v", "ffv1", str(path)])
    assert proc.returncode == 0, proc.stderr
    monkeypatch.setattr(mpeg_frames, "read_scan_window", _tripwire)

    with pytest.raises(video_audio.NoAudioTrackError):
        await extract_audio_track(str(path), scratch_dir=tmp_path / "scratch")


# Both lanes: the splice reaches analysis, and the scratch is deleted on success AND failure


def _driver_that_probes(seen: dict[str, Any]) -> Any:
    """A stand-in analysis driver that measures the path it is handed with the real probe."""
    from phaze.services.analysis import _probe_duration_sec

    async def _driver(file_path: str, _models_dir: str, **_kwargs: Any) -> dict[str, Any]:
        seen["path"] = file_path
        seen["exists_during_analysis"] = Path(file_path).exists()
        seen["duration"] = _probe_duration_sec(file_path)
        return {
            "bpm": 120.0,
            "musical_key": "C major",
            "mood": None,
            "style": None,
            "danceability": None,
            "features": {},
            "windows": [],
            "fine_windows_analyzed": 6,
            "fine_windows_total": 6,
            "coarse_windows_analyzed": 0,
            "coarse_windows_total": 1,
        }

    return _driver


def _driver_that_crashes(seen: dict[str, Any]) -> Any:
    from phaze.services.analysis_exec import AnalysisSubprocessError

    async def _driver(file_path: str, _models_dir: str, **_kwargs: Any) -> dict[str, Any]:
        seen["path"] = file_path
        seen["exists_during_analysis"] = Path(file_path).exists()
        msg = "analysis child exited -11 without a result"
        raise AnalysisSubprocessError(msg)

    return _driver


@pytest.mark.parametrize("builder", _SHAPES)
async def test_local_lane_analyzes_the_splice_and_deletes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_env: dict, builder: Any) -> None:
    """LOCAL (SAQ) lane, success: ``process_file`` hands analysis the splice, then deletes it.

    The input here is the lane's archive original, so it must survive unchanged.
    """
    from unittest.mock import AsyncMock, MagicMock

    import phaze.tasks.functions as functions

    fixture = builder(tmp_path)
    original = fixture.path.read_bytes()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(functions, "run_analysis_subprocess", _driver_that_probes(seen))
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())

    result = await functions.process_file(
        {"api_client": api},
        file_id=str(uuid.uuid4()),
        original_path=str(fixture.path),
        file_type="mp3",
        agent_id="test-agent",
        models_path=job_env["models_dir"],
    )

    assert result["status"] == "analyzed"
    assert seen["path"] != str(fixture.path)
    assert Path(seen["path"]).suffix == ".mp3"
    assert seen["exists_during_analysis"]
    assert abs(seen["duration"] - _real_duration(fixture)) < 0.1
    assert not Path(seen["path"]).exists(), "the splice outlived the job"
    assert fixture.path.read_bytes() == original


async def test_local_lane_deletes_the_splice_when_analysis_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_env: dict) -> None:
    """LOCAL (SAQ) lane, failure: a crashed analysis child still leaves no splice behind."""
    from unittest.mock import AsyncMock

    import phaze.tasks.functions as functions

    fixture = _make_trigger(tmp_path)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(functions, "run_analysis_subprocess", _driver_that_crashes(seen))
    api = AsyncMock()

    result = await functions.process_file(
        {"api_client": api},
        file_id=str(uuid.uuid4()),
        original_path=str(fixture.path),
        file_type="mp3",
        agent_id="test-agent",
        models_path=job_env["models_dir"],
    )

    assert result["status"] == "analysis_failed"
    assert seen["exists_during_analysis"]
    assert Path(seen["path"]).suffix == ".mp3"
    assert not Path(seen["path"]).exists(), "the splice outlived a failed job"
    assert fixture.path.exists()


async def _run_cloud_lane(fixture: _Fixture, job_env: dict, *, analysis_ok: bool) -> int:
    import httpx
    import respx

    import phaze.job_runner as jr

    payload = fixture.path.read_bytes()
    file_id = job_env["file_id"]
    base = job_env["base_url"]
    download_url = "http://bucket.test/staged-object"
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{base}/api/internal/agent/files/{file_id}/presign-download").mock(
            return_value=httpx.Response(
                200, json={"download_url": download_url, "expected_sha256": hashlib.sha256(payload).hexdigest(), "audio_ext": "mp3"}
            ),
        )
        router.get(download_url).mock(return_value=httpx.Response(200, content=payload))
        put = router.put(f"{base}/api/internal/agent/analysis/{file_id}").mock(
            return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
        )
        failed = router.post(f"{base}/api/internal/agent/analysis/{file_id}/failed").mock(return_value=httpx.Response(200, json={}))

        with pytest.raises(SystemExit) as exc:
            await jr.run()

        assert put.called is analysis_ok
        assert failed.called is not analysis_ok
    code = exc.value.code
    assert isinstance(code, int)
    return code


@pytest.mark.parametrize("builder", _SHAPES)
async def test_cloud_lane_analyzes_the_splice_and_deletes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_env: dict, builder: Any) -> None:
    """CLOUD (k8s one-shot job runner) lane, success: the downloaded file is spliced, the splice
    is analyzed, and the pod deletes it on exit."""
    import phaze.job_runner as jr

    fixture = builder(tmp_path)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(jr, "run_analysis_subprocess", _driver_that_probes(seen))

    code = await _run_cloud_lane(fixture, job_env, analysis_ok=True)

    assert code == 0
    assert Path(seen["path"]).suffix == ".mp3"
    assert Path(seen["path"]).name != f"{job_env['file_id']}.mp3", "analysis was handed the download, not the splice"
    assert seen["exists_during_analysis"]
    assert abs(seen["duration"] - _real_duration(fixture)) < 0.1
    assert not Path(seen["path"]).exists(), "the splice outlived the pod"


async def test_cloud_lane_deletes_the_splice_when_analysis_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_env: dict) -> None:
    """CLOUD lane, failure: the pod reports the failure, exits EXIT_ANALYSIS, and the splice is gone."""
    import phaze.job_runner as jr

    fixture = _make_trigger(tmp_path)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(jr, "run_analysis_subprocess", _driver_that_crashes(seen))

    code = await _run_cloud_lane(fixture, job_env, analysis_ok=False)

    assert code == jr.EXIT_ANALYSIS
    assert seen["exists_during_analysis"]
    assert Path(seen["path"]).suffix == ".mp3"
    assert Path(seen["path"]).name != f"{job_env['file_id']}.mp3", "analysis was handed the download, not the splice"
    assert not Path(seen["path"]).exists(), "the splice outlived a failed pod"
