"""Find the real MPEG audio stream behind a junk prefix, by walking frame chains (phaze-oap6y).

FFmpeg can misdetect an MP3 whose real Layer 3 frames sit behind non-MPEG bytes. Two shapes are
measured in ``docs/spikes/phaze-di48m-mp3-junk-prefix-scan.md``:

* **A long junk prefix FFmpeg resyncs into as Layer 1.** ffprobe reports ``mp1`` with a duration
  several times the real one, and every essentia loader SIGSEGVs decoding it.
* **A few hundred junk bytes after a large ID3v2 tag.** FFmpeg's raw-VVC probe outscores its mp3
  probe, so ffprobe reports a ``vvc`` video stream and no audio stream at all.

The detector (:func:`detect_misdetection`) is pure: a scan window in, verdict and splice offset
out. :func:`read_scan_window` is the one function here that touches a file, and it reads a
bounded amount whatever the file's tags claim. The analyze lane (:mod:`phaze.services.video_audio`)
and the metadata stage (bead ``phaze-v03x9``) share both, so they agree on "where the real stream
starts".

Operator decision 2026-10-19 (bead ``phaze-di48m``): asked *"The scan found 4 misdetected MP3s:
the trigger plus 3 that FFmpeg mistakes for raw video. None has silently wrong analysis. Which
detector should the splice fix use?"*, the operator selected **"N=8 + codec prefilter
(Recommended)"**. The durable record is the bead comment on ``phaze-di48m``, and the measurement
is the spike doc above. The prefilter half lives in the caller, which has the ffprobe result;
this module is the confirm half. The thresholds, as measured over 92,740 MP3s with a real stream:

* **N = 8 chained frames.** The longest chain with foreign parameters anywhere before a real
  stream is 3 frames (the trigger's junk), so N = 3 misses the trigger and every N from 4 to 64
  measured 0 false positives and 0 false negatives. 8 keeps a 2.7x margin.
* **No probe_score gate.** The three VVC misdetections score 51, the same as a healthy MP3.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable


# Minimum consecutive frames that make a chain "the stream" (see the module docstring).
MIN_CHAIN_FRAMES = 8

# How far past the ID3v2 tag(s) the parse looks for the stream. Matches the corpus scan, which
# gave up after the same distance; the longest measured junk prefix is 3,440,428 bytes.
MAX_SCAN_BYTES = 64 * 1024 * 1024

# The leading ID3v2 region is walked header by header, and a junk or hostile size field can claim
# up to ~256 MiB per tag. Past either bound the file is not a candidate at all. Tag bytes are never
# read into memory (a splice streams them), so this bounds the walk, not an allocation. The
# largest real tag in the corpus scan is ~90 KB.
MAX_ID3V2_BYTES = MAX_SCAN_BYTES
MAX_ID3V2_TAGS = 8

_ID3V2_HEADER_LEN = 10
_ID3V2_FOOTER_FLAG = 0x10

# ffprobe codec name -> the MPEG audio layer it implies. Anything else implies no MPEG layer.
_CODEC_LAYER = {"mp3": 3, "mp2": 2, "mp1": 1}

# Bitrates in kbps, indexed by the header's 4-bit bitrate index (0 = free format, 15 = bad).
_BITRATES_V1 = {
    1: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    2: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
}
_BITRATES_V2 = {
    1: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    3: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
# Sample rates in Hz by version code (3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5) and 2-bit index.
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}

# A frame sync is 11 set bits. The lookahead keeps overlapping candidates (0xFF 0xFF 0xFB).
_SYNC = re.compile(rb"\xff(?=[\xe0-\xff])")


@dataclass(frozen=True, slots=True)
class FrameHeader:
    """One parsed MPEG audio frame header. ``version`` is the raw 2-bit code (3 = MPEG-1)."""

    version: int
    layer: int
    sample_rate: int
    bitrate_kbps: int
    length: int


@dataclass(frozen=True, slots=True)
class ScanWindow:
    """The bytes the detector parses: up to :data:`MAX_SCAN_BYTES` starting at ``id3v2_end``.

    ``id3v2_end`` is where the file's leading ID3v2 tag(s) end, as an absolute file offset. The
    tag bytes themselves are not held here; a splice copies them from the file.
    """

    id3v2_end: int
    data: bytes


@dataclass(frozen=True, slots=True)
class MisdetectionVerdict:
    """What :func:`detect_misdetection` concluded about one file's head.

    ``splice_offset`` is where the first chain of at least ``min_frames`` frames starts, or
    ``None`` when no such chain exists. ``misdetected`` is True only when that chain exists and
    its layer differs from the one ffprobe's codec implies. ``id3v2_end`` is where the leading
    ID3v2 tag(s) end, so a splice keeps exactly the bytes before it.
    """

    misdetected: bool
    splice_offset: int | None
    id3v2_end: int
    chain_layer: int | None
    probed_layer: int | None


def id3v2_tag_size(header: bytes) -> int | None:
    """The total size of the ID3v2 tag ``header`` starts, footer included, or ``None``.

    ``header`` needs at least the 10-byte tag header. Returns ``None`` when it is not one,
    including a size field that is not syncsafe.
    """
    if len(header) < _ID3V2_HEADER_LEN or header[:3] != b"ID3" or 0xFF in header[3:5]:
        return None
    size_bytes = header[6:10]
    if any(b & 0x80 for b in size_bytes):
        return None
    size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
    footer = _ID3V2_HEADER_LEN if header[5] & _ID3V2_FOOTER_FLAG else 0
    return _ID3V2_HEADER_LEN + size + footer


def id3v2_region_end(read_at: Callable[[int, int], bytes], total_size: int) -> int | None:
    """Where the leading ID3v2 tag(s) end, or ``None`` when the region is out of bounds.

    ``read_at(offset, length)`` returns at most ``length`` bytes from ``offset``; this only ever
    asks it for 10-byte tag headers, so following a hostile size field costs nothing. Returns
    ``None`` -- not a candidate -- when the tags claim to run past ``total_size`` (a truncated or
    junk header), past :data:`MAX_ID3V2_BYTES`, or chain more than :data:`MAX_ID3V2_TAGS` tags.
    0 means the file starts with no tag.
    """
    end = 0
    for _ in range(MAX_ID3V2_TAGS + 1):
        size = id3v2_tag_size(read_at(end, _ID3V2_HEADER_LEN))
        if size is None:
            return end
        end += size
        if end > min(total_size, MAX_ID3V2_BYTES):
            return None
    return None


def scan_window_from_bytes(data: bytes) -> ScanWindow | None:
    """:func:`read_scan_window` over an in-memory file, with the same bounds."""
    tag_end = id3v2_region_end(lambda offset, length: data[offset : offset + length], len(data))
    if tag_end is None:
        return None
    return ScanWindow(id3v2_end=tag_end, data=data[tag_end : tag_end + MAX_SCAN_BYTES])


def read_scan_window(path: str | os.PathLike[str]) -> ScanWindow | None:
    """Read the detector's window from ``path``: at most 10 bytes per tag header, then at most
    :data:`MAX_SCAN_BYTES` from where the tags end, clamped to the file's size.

    Returns ``None`` when the ID3v2 region is out of bounds (see :func:`id3v2_region_end`).
    """
    with Path(path).open("rb") as fh:
        total_size = os.fstat(fh.fileno()).st_size

        def _read_at(offset: int, length: int) -> bytes:
            fh.seek(offset)
            return fh.read(length)

        tag_end = id3v2_region_end(_read_at, total_size)
        if tag_end is None:
            return None
        return ScanWindow(id3v2_end=tag_end, data=_read_at(tag_end, min(MAX_SCAN_BYTES, total_size - tag_end)))


def parse_frame_header(data: bytes, pos: int) -> FrameHeader | None:
    """Parse the 4-byte MPEG audio frame header at ``data[pos]``, or ``None`` if it is invalid.

    Invalid means: no sync, a reserved version or layer, bitrate index 0 (free format) or 15,
    sample-rate index 3, or reserved emphasis. Those are the corpus scan's rules, which the
    thresholds were measured with. The scan also rejected a computed frame length under 21
    bytes, which no remaining header can produce (the shortest is 24 bytes, MPEG-2 Layer 3 at
    8 kbps and 24 kHz), so that rule is not repeated here.
    """
    if pos < 0 or pos + 4 > len(data):
        return None
    b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
    if data[pos] != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 0x03
    layer = 4 - ((b1 >> 1) & 0x03)
    bitrate_index = (b2 >> 4) & 0x0F
    rate_index = (b2 >> 2) & 0x03
    if version == 1 or layer == 4 or bitrate_index in {0, 15} or rate_index == 3 or (b3 & 0x03) == 2:
        return None
    table = _BITRATES_V1 if version == 3 else _BITRATES_V2
    bitrate = table[layer][bitrate_index] * 1000
    sample_rate = _SAMPLE_RATES[version][rate_index]
    padding = (b2 >> 1) & 0x01
    if layer == 1:
        length = (12 * bitrate // sample_rate + padding) * 4
    elif layer == 3 and version != 3:
        length = 72 * bitrate // sample_rate + padding
    else:
        length = 144 * bitrate // sample_rate + padding
    return FrameHeader(version=version, layer=layer, sample_rate=sample_rate, bitrate_kbps=bitrate // 1000, length=length)


def chain_length(data: bytes, start: int, limit: int) -> tuple[int, FrameHeader | None]:
    """Count consecutive frames from ``start``, stopping once ``limit`` is reached.

    Every frame must begin exactly where the previous one ended and share its version, layer
    and sample rate, and must end within ``data``. Returns the count and the first header.
    """
    first = parse_frame_header(data, start)
    if first is None or start + first.length > len(data):
        return 0, None
    count, pos = 1, start + first.length
    while count < limit:
        frame = parse_frame_header(data, pos)
        if frame is None or (frame.version, frame.layer, frame.sample_rate) != (first.version, first.layer, first.sample_rate):
            break
        if pos + frame.length > len(data):
            break
        count += 1
        pos += frame.length
    return count, first


def find_stream(data: bytes, start: int = 0, *, min_frames: int = MIN_CHAIN_FRAMES) -> tuple[int, FrameHeader] | None:
    """The offset and first header of the first chain of at least ``min_frames`` frames.

    Candidates are visited in byte order from ``start``, so a shorter chain inside junk is
    passed over rather than taken.
    """
    for match in _SYNC.finditer(data, start):
        count, first = chain_length(data, match.start(), min_frames)
        if count >= min_frames and first is not None:
            return match.start(), first
    return None


def layer_for_codec(codec_name: str | None) -> int | None:
    """The MPEG audio layer an ffprobe codec name implies: mp3 -> 3, mp2 -> 2, mp1 -> 1, else None.

    ``None`` covers both a non-MPEG codec and a file with no audio stream at all.
    """
    return _CODEC_LAYER.get(codec_name or "")


def detect_misdetection(window: ScanWindow, probed_codec: str | None, *, min_frames: int = MIN_CHAIN_FRAMES) -> MisdetectionVerdict:
    """Confirm whether ffprobe misdetected the MPEG audio stream in ``window``.

    ``window`` is what :func:`read_scan_window` read: the bytes after the file's ID3v2 tag(s).
    ``probed_codec`` is ffprobe's codec for the file's first audio stream, or ``None`` when it
    reported none. The verdict is misdetected when the first chain of at least ``min_frames``
    frames has a layer other than the one ``probed_codec`` implies. That chain's start, as an
    absolute file offset, is the splice offset.

    No chain means no verdict to act on: ``misdetected`` is False and there is nothing to splice.
    """
    probed_layer = layer_for_codec(probed_codec)
    found = find_stream(window.data, min_frames=min_frames)
    if found is None:
        return MisdetectionVerdict(misdetected=False, splice_offset=None, id3v2_end=window.id3v2_end, chain_layer=None, probed_layer=probed_layer)
    offset, header = found
    return MisdetectionVerdict(
        misdetected=header.layer != probed_layer,
        splice_offset=window.id3v2_end + offset,
        id3v2_end=window.id3v2_end,
        chain_layer=header.layer,
        probed_layer=probed_layer,
    )
