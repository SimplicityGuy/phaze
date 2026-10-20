"""Synthetic MP3 fixtures for the junk-prefix misdetection shapes (epic phaze-pjkbu).

Shared by the analyze lane's splice tests (phaze-oap6y) and the metadata stage's duration tests
(phaze-v03x9). Built at test time with real ffmpeg: no archive file, no committed audio. See
``tests/analyze/services/pipeline/test_mpeg_junk_prefix_splice.py`` for the two shapes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from pathlib import Path


_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _has_libmp3lame() -> bool:
    if not _HAS_FFMPEG:
        return False
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-encoders"], capture_output=True, text=True, check=False)  # noqa: S607
    return " libmp3lame " in proc.stdout


requires_libmp3lame = pytest.mark.skipif(not _has_libmp3lame(), reason="ffmpeg/ffprobe with libmp3lame not installed on this runner")

# The real stream's length in every fixture. Long enough that a duration several times too long
# is unmistakable, short enough to encode and analyze in seconds.
_STREAM_SEC = 30
_TRIGGER_ID3_END = 4096
_TRIGGER_JUNK_BYTES = 400_000
# mutagen gives up looking for the stream after 1 MiB (``MPEGInfo``'s ``max_read``), so a junk run
# longer than that is what makes it report the junk's chains (Layer 1, sketchy) as the file. The
# real trigger's junk is 3.4 MB; ffprobe needs far less, which is why the shorter run above suffices
# for the analyze lane's tests.
_MUTAGEN_JUNK_BYTES = 1_200_000
_VVC_ID3_END = 58_648
_VVC_JUNK_BYTES = 562

# MPEG-1 Layer 1, 32 kbps, 44.1 kHz: a 32-byte frame. The trigger's junk holds short chains of
# frames like this, which is what FFmpeg resyncs into.
_L1_HEADER = b"\xff\xff\x10\x00"
_L1_LEN = 32
# mutagen computes the same frame's length as 136 bytes (it applies the Layer 1 four-byte slot after
# a bytes-per-frame formula that already includes it), so the chains it follows are spaced by this.
_L1_LEN_MUTAGEN = 136
# MPEG-1 Layer 3, 128 kbps, 44.1 kHz: a 417-byte frame, spaced identically for mutagen and ffmpeg.
# Junk chains of these are what mutagen syncs on while still calling the file Layer 3 (phaze-dn0km).
_L3_HEADER = b"\xff\xfb\x90\x00"
_L3_LEN = 417
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


def _encode_varied_vbr(dest: Path, *, rate: int) -> bytes:
    """A real VBR Layer 3 stream (with its Xing header) whose bitrate swings: a sine, then noise.

    The two halves cost very different bytes per second at the same quality, so a size/bitrate
    estimate is wrong by far more than a second on it; only the frame count gets the duration.
    """
    half = _STREAM_SEC // 2
    proc = _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-filter_complex",
            f"sine=frequency=330:duration={half}[a];anoisesrc=duration={half}:amplitude=0.5[b];[a][b]concat=n=2:v=0:a=1",
            "-ar",
            str(rate),
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            "-write_xing",
            "1",
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


def _trigger_junk(length: int = _TRIGGER_JUNK_BYTES, frame_len: int = _L1_LEN, header: bytes = _L1_HEADER) -> bytes:
    """Noise holding 2-3-frame chains of ``header`` frames (Layer 1 by default) every few KB, from the first byte."""
    junk = _noise(length, seed=1)
    choices = _noise(4096, seed=11)
    pos, i = 0, 0
    while pos < length - 200:
        count = 2 + choices[i] % 2
        chain = b"".join(header + bytes(_noise(frame_len - 4, seed=1000 + i * 3 + n)) for n in range(count))
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


def _make_mutagen_trigger(tmp_path: Path) -> _Fixture:
    """The trigger shape with a junk run past mutagen's 1 MiB search: mutagen reads Layer 1, sketchy."""
    stream = tmp_path / "stream_44k.mp3"
    frames = _encode_stream(stream, rate=44100, bitrate="320k")
    path = tmp_path / "mutagen_trigger_shape.mp3"
    path.write_bytes(_id3(_TRIGGER_ID3_END) + _trigger_junk(_MUTAGEN_JUNK_BYTES, _L1_LEN_MUTAGEN) + frames)
    return _Fixture(path, stream, _TRIGGER_ID3_END, _TRIGGER_ID3_END + _MUTAGEN_JUNK_BYTES)


def _make_layer3_junk(tmp_path: Path) -> _Fixture:
    """Junk past mutagen's 1 MiB search holding short LAYER 3 chains, then a real Layer 3 stream (phaze-dn0km).

    mutagen syncs on a junk chain (sketchy, layer 3, 128 kbps) so its length and bitrate are the
    junk's; the real stream is also Layer 3 at 320 kbps, so a layer comparison cannot tell them apart.
    """
    stream = tmp_path / "stream_44k.mp3"
    frames = _encode_stream(stream, rate=44100, bitrate="320k")
    path = tmp_path / "layer3_junk_shape.mp3"
    path.write_bytes(_id3(_TRIGGER_ID3_END) + _trigger_junk(_MUTAGEN_JUNK_BYTES, _L3_LEN, _L3_HEADER) + frames)
    return _Fixture(path, stream, _TRIGGER_ID3_END, _TRIGGER_ID3_END + _MUTAGEN_JUNK_BYTES)


def _make_trigger_vbr(tmp_path: Path) -> _Fixture:
    """The trigger shape over a VBR stream with a swinging bitrate (phaze-v03x9)."""
    stream = tmp_path / "stream_vbr_44k.mp3"
    frames = _encode_varied_vbr(stream, rate=44100)
    path = tmp_path / "trigger_shape_vbr.mp3"
    path.write_bytes(_id3(_TRIGGER_ID3_END) + _trigger_junk(_MUTAGEN_JUNK_BYTES, _L1_LEN_MUTAGEN) + frames)
    return _Fixture(path, stream, _TRIGGER_ID3_END, _TRIGGER_ID3_END + _MUTAGEN_JUNK_BYTES)


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
