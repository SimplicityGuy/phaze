"""Unit tests for the pure MPEG frame-chain detector (phaze-oap6y).

Every byte stream here is hand-built, so each test states the exact frames it contains. The
real-audio side (real ffmpeg, real ffprobe, real essentia) is in
``test_mpeg_junk_prefix_splice.py``.

The corpus facts these tests pin come from ``docs/spikes/phaze-di48m-mp3-junk-prefix-scan.md``:
real MP2 files must not be flagged, the longest foreign chain before a real stream is 3 frames,
and the trigger's junk holds 2-3-frame "Layer 1" chains that N = 3 would take for the stream.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from phaze.services import mpeg_frames
from phaze.services.mpeg_frames import (
    MAX_ID3V2_TAGS,
    MAX_SCAN_BYTES,
    MIN_CHAIN_FRAMES,
    MisdetectionVerdict,
    ScanWindow,
    chain_length,
    detect_misdetection,
    find_stream,
    id3v2_region_end,
    id3v2_tag_size,
    layer_for_codec,
    parse_frame_header,
    read_scan_window,
    scan_window_from_bytes,
)


if TYPE_CHECKING:
    from pathlib import Path


# MPEG-1 Layer 3, 128 kbps, 44.1 kHz, no padding: 144 * 128000 // 44100 = 417 bytes.
_L3_HEADER = b"\xff\xfb\x90\x00"
_L3_LEN = 417
# MPEG-1 Layer 2, 128 kbps, 44.1 kHz: 144 * 128000 // 44100 = 417 bytes.
_L2_HEADER = b"\xff\xfd\x80\x00"
_L2_LEN = 417
# MPEG-1 Layer 1, 32 kbps, 44.1 kHz: (12 * 32000 // 44100) * 4 = 32 bytes. The trigger's junk
# shape: FFmpeg resyncs into chains like this and calls the file mp1.
_L1_HEADER = b"\xff\xff\x10\x00"
_L1_LEN = 32


def _frame(header: bytes, length: int, fill: int = 0x55) -> bytes:
    return header + bytes([fill]) * (length - len(header))


def _frames(header: bytes, length: int, count: int) -> bytes:
    return _frame(header, length) * count


def _id3(total: int, *, footer: bool = False) -> bytes:
    body_len = total - 10 - (10 if footer else 0)
    size = bytes([(body_len >> 21) & 0x7F, (body_len >> 14) & 0x7F, (body_len >> 7) & 0x7F, body_len & 0x7F])
    flags = b"\x10" if footer else b"\x00"
    body = b"TIT2\x00\x00\x00\x05\x00\x00\x03abcd".ljust(body_len, b"\x00")
    return b"ID3\x04\x00" + flags + size + body + (b"3DI\x04\x00\x10" + size if footer else b"")


def _junk(length: int, seed: int) -> bytes:
    """Seeded noise with every 0xFF byte removed, so it holds no frame sync of its own."""
    return _noise(length, seed).replace(b"\xff", b"\xfe")


def _noise(length: int, seed: int) -> bytes:
    """Deterministic noise: the same bytes on every run and every runner."""
    return hashlib.shake_256(f"noise-{seed}".encode()).digest(length)


# ID3v2


def test_id3v2_tag_size_reads_the_syncsafe_size() -> None:
    assert id3v2_tag_size(_id3(4096)) == 4096


def test_id3v2_tag_size_counts_a_footer() -> None:
    assert id3v2_tag_size(_id3(4096, footer=True)) == 4096


@pytest.mark.parametrize(
    ("header", "why"),
    [
        (b"ID3\x04\x00\x00\x00\x00\x80\x00", "a size byte with its high bit set is not syncsafe"),
        (b"ID3\xff\x00\x00\x00\x00\x00\x00", "version byte 0xFF is invalid"),
        (b"TAG\x04\x00\x00\x00\x00\x00\x00", "not an ID3v2 magic"),
        (b"ID3\x04", "shorter than a tag header"),
    ],
)
def test_id3v2_tag_size_rejects_what_is_not_a_tag(header: bytes, why: str) -> None:
    assert id3v2_tag_size(header) is None, why


def _window(data: bytes) -> ScanWindow:
    window = scan_window_from_bytes(data)
    assert window is not None
    return window


def _detect(data: bytes, probed_codec: str | None, **kwargs: int) -> MisdetectionVerdict:
    return detect_misdetection(_window(data), probed_codec, **kwargs)


def test_the_scan_window_skips_consecutive_tags() -> None:
    frames = _frames(_L3_HEADER, _L3_LEN, 2)
    window = _window(_id3(100) + _id3(200) + frames)

    assert window.id3v2_end == 300
    assert window.data == frames


def test_the_scan_window_starts_at_zero_without_a_tag() -> None:
    assert _window(_frames(_L3_HEADER, _L3_LEN, 2)).id3v2_end == 0


# The ID3v2 walk is bounded whatever the tags claim (security review, phaze-oap6y)


def _claiming(size: int) -> bytes:
    """A bare ID3v2 header whose syncsafe size field claims ``size`` body bytes."""
    return b"ID3\x04\x00\x00" + bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])


_MAX_SYNCSAFE = (1 << 28) - 1  # ~256 MiB, the most one tag header can claim


class _CountingFile:
    """A ``read_at`` over ``total_size`` virtual bytes that records every byte it hands out."""

    def __init__(self, headers: dict[int, bytes], total_size: int) -> None:
        self.headers = headers
        self.total_size = total_size
        self.bytes_read = 0

    def read_at(self, offset: int, length: int) -> bytes:
        chunk = self.headers.get(offset, b"\x00" * 10)[:length]
        self.bytes_read += len(chunk)
        return chunk


def test_a_tag_claiming_256_mib_is_not_a_candidate_and_reads_only_its_header() -> None:
    fake = _CountingFile({0: _claiming(_MAX_SYNCSAFE)}, total_size=1 << 40)

    assert id3v2_region_end(fake.read_at, fake.total_size) is None
    assert fake.bytes_read == 10


def test_chained_tags_are_bounded_by_count_and_by_total() -> None:
    many = _CountingFile({i * 10: _claiming(0) for i in range(1000)}, total_size=1 << 40)
    assert id3v2_region_end(many.read_at, many.total_size) is None
    assert many.bytes_read == 10 * (MAX_ID3V2_TAGS + 1)

    step = 32 * 1024 * 1024
    big = _CountingFile({0: _claiming(step), step + 10: _claiming(step)}, total_size=1 << 40)
    assert id3v2_region_end(big.read_at, big.total_size) is None
    assert big.bytes_read == 20

    chained = _CountingFile({0: _claiming(90), 100: _claiming(190)}, total_size=10_000)
    assert id3v2_region_end(chained.read_at, chained.total_size) == 300


def test_a_tag_claiming_past_the_end_of_the_file_is_not_a_candidate() -> None:
    assert scan_window_from_bytes(_id3(4096)[:2000]) is None


def test_read_scan_window_on_a_file_claiming_256_mib_reads_nothing_past_the_header(tmp_path: Path) -> None:
    """A real file, sparse past its header: the claim is refused without reading the body."""
    path = tmp_path / "huge_claim.mp3"
    with path.open("wb") as fh:
        fh.write(_claiming(_MAX_SYNCSAFE))
        fh.truncate(_MAX_SYNCSAFE + 4096)

    assert read_scan_window(path) is None


def test_read_scan_window_never_returns_more_than_the_scan_bound(tmp_path: Path) -> None:
    path = tmp_path / "long.mp3"
    with path.open("wb") as fh:
        fh.write(_id3(4096))
        fh.truncate(4096 + MAX_SCAN_BYTES + 1024 * 1024)

    window = read_scan_window(path)

    assert window is not None
    assert window.id3v2_end == 4096
    assert len(window.data) == MAX_SCAN_BYTES


def test_read_scan_window_clamps_to_the_file_size(tmp_path: Path) -> None:
    frames = _frames(_L3_HEADER, _L3_LEN, 3)
    path = tmp_path / "short.mp3"
    path.write_bytes(_id3(4096) + frames)

    window = read_scan_window(path)

    assert window == ScanWindow(id3v2_end=4096, data=frames)


# Frame headers


@pytest.mark.parametrize(
    ("header", "version", "layer", "rate", "kbps", "length"),
    [
        (b"\xff\xfb\x90\x00", 3, 3, 44100, 128, 417),
        (b"\xff\xfb\x92\x00", 3, 3, 44100, 128, 418),  # padding bit set
        (b"\xff\xfb\xe4\x00", 3, 3, 48000, 320, 960),
        (b"\xff\xfd\x80\x00", 3, 2, 44100, 128, 417),
        (b"\xff\xff\x10\x00", 3, 1, 44100, 32, 32),
        (b"\xff\xf3\x80\x00", 2, 3, 22050, 64, 208),  # MPEG-2 Layer 3 uses 72 * br / sr
        (b"\xff\xf5\x80\x00", 2, 2, 22050, 64, 417),  # MPEG-2 Layer 2 keeps 144 * br / sr
        (b"\xff\xf7\x80\x00", 2, 1, 22050, 128, 276),  # MPEG-2 Layer 1
        (b"\xff\xe3\x80\x00", 0, 3, 11025, 64, 417),  # MPEG-2.5 Layer 3
    ],
)
def test_parse_frame_header_computes_each_versions_frame_length(header: bytes, version: int, layer: int, rate: int, kbps: int, length: int) -> None:
    frame = parse_frame_header(header, 0)

    assert frame is not None
    assert (frame.version, frame.layer, frame.sample_rate, frame.bitrate_kbps, frame.length) == (version, layer, rate, kbps, length)


@pytest.mark.parametrize(
    ("header", "why"),
    [
        (b"\xff\xeb\x90\x00", "version code 01 is reserved"),
        (b"\xff\xf9\x90\x00", "layer code 00 is reserved"),
        (b"\xff\xfb\x00\x00", "bitrate index 0 is free format"),
        (b"\xff\xfb\xf0\x00", "bitrate index 15 is invalid"),
        (b"\xff\xfb\x9c\x00", "sample-rate index 3 is reserved"),
        (b"\xff\xfb\x90\x02", "emphasis 10 is reserved"),
        (b"\xfe\xfb\x90\x00", "no frame sync"),
        (b"\xff\xfb\x90", "truncated header"),
    ],
)
def test_parse_frame_header_rejects_invalid_headers(header: bytes, why: str) -> None:
    assert parse_frame_header(header, 0) is None, why


def test_parse_frame_header_rejects_a_negative_offset() -> None:
    assert parse_frame_header(_L3_HEADER, -1) is None


# Chains


def test_chain_length_counts_back_to_back_frames_up_to_the_limit() -> None:
    data = _frames(_L3_HEADER, _L3_LEN, 12)

    count, first = chain_length(data, 0, MIN_CHAIN_FRAMES)

    assert count == MIN_CHAIN_FRAMES
    assert first is not None
    assert first.layer == 3


def test_chain_length_stops_where_the_parameters_change() -> None:
    data = _frames(_L3_HEADER, _L3_LEN, 3) + _frames(_L2_HEADER, _L2_LEN, 10)

    assert chain_length(data, 0, MIN_CHAIN_FRAMES)[0] == 3


def test_chain_length_stops_at_a_gap() -> None:
    data = _frames(_L3_HEADER, _L3_LEN, 3) + b"\x00" + _frames(_L3_HEADER, _L3_LEN, 10)

    assert chain_length(data, 0, MIN_CHAIN_FRAMES)[0] == 3


def test_chain_length_does_not_count_a_frame_cut_off_by_the_buffer() -> None:
    data = _frames(_L3_HEADER, _L3_LEN, 3)

    assert chain_length(data, 0, MIN_CHAIN_FRAMES)[0] == 3
    assert chain_length(data[:-1], 0, MIN_CHAIN_FRAMES)[0] == 2
    assert chain_length(_L3_HEADER, 0, MIN_CHAIN_FRAMES) == (0, None)


def test_find_stream_returns_none_when_no_chain_is_long_enough() -> None:
    data = _junk(5000, seed=1) + _frames(_L3_HEADER, _L3_LEN, MIN_CHAIN_FRAMES - 1)

    assert find_stream(data) is None


def test_find_stream_returns_none_for_noise() -> None:
    assert find_stream(_noise(200_000, seed=7)) is None


def test_find_stream_sees_a_sync_that_overlaps_the_previous_candidate() -> None:
    """``FF FF FB``: the regex must not consume the second 0xFF and miss the real frame at 1."""
    data = b"\xff" + _frames(_L3_HEADER, _L3_LEN, MIN_CHAIN_FRAMES)

    found = find_stream(data)

    assert found is not None
    assert found[0] == 1


# layer_for_codec


@pytest.mark.parametrize(("codec", "layer"), [("mp3", 3), ("mp2", 2), ("mp1", 1), ("aac", None), ("vvc", None), (None, None)])
def test_layer_for_codec(codec: str | None, layer: int | None) -> None:
    assert layer_for_codec(codec) == layer


# detect_misdetection: the acceptance criteria


def test_a_clean_mp3_is_not_flagged() -> None:
    data = _id3(4096) + _frames(_L3_HEADER, _L3_LEN, 40)

    verdict = _detect(data, "mp3")

    assert not verdict.misdetected
    assert verdict.splice_offset == 4096
    assert verdict.chain_layer == verdict.probed_layer == 3


def test_a_junk_prefixed_mp3_is_flagged_at_the_first_real_frame() -> None:
    """The trigger's shape: ID3v2 to 4,096, junk holding 2-3-frame Layer 1 chains, then L3."""
    junk = bytearray(_junk(20_000, seed=2))
    for pos, count in ((0, 3), (4000, 2), (9000, 3), (15000, 2)):
        junk[pos : pos + count * _L1_LEN] = _frames(_L1_HEADER, _L1_LEN, count)
    data = _id3(4096) + bytes(junk) + _frames(_L3_HEADER, _L3_LEN, 40)

    verdict = _detect(data, "mp1")

    assert verdict.misdetected
    assert verdict.splice_offset == 4096 + len(junk)
    assert verdict.id3v2_end == 4096
    assert (verdict.chain_layer, verdict.probed_layer) == (3, 1)


def test_n_equals_three_would_have_taken_the_junk_for_the_stream() -> None:
    """Why N is not 3: the trigger's first 3-frame chain is the junk's "Layer 1", which agrees
    with ffprobe's wrong mp1, so the file would pass as correctly detected."""
    junk = _frames(_L1_HEADER, _L1_LEN, 3) + _junk(5000, seed=3)
    data = _id3(4096) + junk + _frames(_L3_HEADER, _L3_LEN, 40)

    assert not _detect(data, "mp1", min_frames=3).misdetected
    assert _detect(data, "mp1").misdetected


def test_stray_layer3_frame_pairs_in_junk_are_not_taken_for_the_stream() -> None:
    """``FF FB`` pairs, and real Layer 3 runs shorter than the threshold, inside the junk.

    The longest foreign chain the corpus holds before a real stream is 3 frames. Here the junk
    carries isolated ``FF FB`` pairs (headers whose next frame is not where it should be) and a
    7-frame Layer 3 run, one short of N. None of them is the splice point.
    """
    junk = bytearray(_junk(30_000, seed=4))
    for pos in (100, 2_000, 6_000, 11_000):
        junk[pos : pos + 4] = _L3_HEADER
    short_run = _frames(_L3_HEADER, _L3_LEN, MIN_CHAIN_FRAMES - 1)
    junk[15_000 : 15_000 + len(short_run)] = short_run
    data = _id3(4096) + bytes(junk) + _frames(_L3_HEADER, _L3_LEN, 40)

    verdict = _detect(data, None)

    assert verdict.misdetected
    assert verdict.splice_offset == 4096 + len(junk)


def test_the_vvc_misprobe_shape_is_flagged() -> None:
    """A large ID3v2 tag, a few hundred junk bytes, a clean stream, and no audio per ffprobe."""
    data = _id3(58_648) + _junk(562, seed=5) + _frames(b"\xff\xfb\xe4\x00", 960, 40)

    verdict = _detect(data, None)

    assert verdict.misdetected
    assert verdict.splice_offset == 58_648 + 562
    assert verdict.probed_layer is None


def test_a_real_mp2_stream_in_an_mp3_file_is_not_flagged() -> None:
    """The 37 real MP2 files: ffprobe says mp2 and the chain is Layer 2, so they agree."""
    data = _id3(4096) + _frames(_L2_HEADER, _L2_LEN, 40)

    verdict = _detect(data, "mp2")

    assert not verdict.misdetected
    assert verdict.chain_layer == 2


def test_no_chain_means_nothing_to_splice() -> None:
    verdict = _detect(_id3(4096) + _junk(10_000, seed=6), None)

    assert not verdict.misdetected
    assert verdict.splice_offset is None
    assert verdict.chain_layer is None


def test_the_scan_bound_is_the_corpus_scans() -> None:
    assert mpeg_frames.MAX_SCAN_BYTES == 64 * 1024 * 1024
    assert MIN_CHAIN_FRAMES == 8
