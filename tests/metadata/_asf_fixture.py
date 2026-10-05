"""Synthetic ASF (.wma/.wmv) header builder for the phaze-3p82d bitrate tests.

Builds the bytes of an ASF Header Object from the spec's own layout (GUIDs as ``uuid.bytes_le``,
not mutagen's helpers), carrying a File Properties object, a Content Description object and one
Stream Properties object per requested stream. No real archive file is involved.

A video stream's type-specific data is the spec's Video Media Type: encoded width/height (DWORD
each), reserved flags (BYTE, always 0x02), format data size (WORD), then a BITMAPINFOHEADER whose
``biSize`` equals that format data size. mutagen's ASF reader decodes bytes [2:12] of ANY stream's
type-specific data as WAVEFORMATEX (``<HII`` -> channels, sample rate, avg bytes/sec), so for a
video stream its "avg bytes/sec" is ``0x02 | size << 8 | size << 24``.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING
import uuid


if TYPE_CHECKING:
    from pathlib import Path


_HEADER = uuid.UUID("75B22630-668E-11CF-A6D9-00AA0062CE6C").bytes_le
_FILE_PROPERTIES = uuid.UUID("8CABDCA1-A947-11CF-8EE4-00C00C205365").bytes_le
_CONTENT_DESCRIPTION = uuid.UUID("75B22633-668E-11CF-A6D9-00AA0062CE6C").bytes_le
_STREAM_PROPERTIES = uuid.UUID("B7DC0791-A9B7-11CF-8EE6-00C00C205365").bytes_le
_AUDIO_MEDIA = uuid.UUID("F8699E40-5B4D-11CF-A8FD-00805F5C442B").bytes_le
_VIDEO_MEDIA = uuid.UUID("BC19EFC0-5B4D-11CF-A8FD-00805F5C442B").bytes_le
_NO_ERROR_CORRECTION = uuid.UUID("20FB5700-5B55-11CF-A8FD-00805F5C442B").bytes_le


def _obj(guid: bytes, payload: bytes) -> bytes:
    return guid + struct.pack("<Q", 24 + len(payload)) + payload


def _file_properties(duration_s: float) -> bytes:
    play_duration = round(duration_s * 10_000_000)  # 100-ns units
    payload = (
        uuid.UUID(int=1).bytes_le  # file id
        + struct.pack("<QQQ", 0, 0, 0)  # file size, creation date, data packets count
        + struct.pack("<QQQ", play_duration, play_duration, 0)  # play/send duration, preroll (ms)
        + struct.pack("<IIII", 0x02, 3200, 3200, 0)  # flags, min/max packet size, max bitrate
    )
    return _obj(_FILE_PROPERTIES, payload)


def _content_description(title: str, author: str) -> bytes:
    texts = [t.encode("utf-16-le") + b"\x00\x00" for t in (title, author)] + [b"", b"", b""]
    return _obj(_CONTENT_DESCRIPTION, struct.pack("<HHHHH", *map(len, texts)) + b"".join(texts))


def _stream_properties(stream_type: bytes, stream_number: int, type_specific: bytes) -> bytes:
    payload = (
        stream_type
        + _NO_ERROR_CORRECTION
        + struct.pack("<Q", 0)  # time offset
        + struct.pack("<II", len(type_specific), 0)  # type-specific / error-correction data lengths
        + struct.pack("<HI", stream_number, 0)  # flags (stream number), reserved
        + type_specific
    )
    return _obj(_STREAM_PROPERTIES, payload)


def audio_stream(*, channels: int = 2, sample_rate: int = 44_100, avg_bytes_per_sec: int = 16_000) -> bytes:
    """A WMA audio stream whose WAVEFORMATEX carries a real average byte rate."""
    waveformatex = struct.pack("<HHIIHHH", 0x0161, channels, sample_rate, avg_bytes_per_sec, 0x0800, 16, 0)
    return _stream_properties(_AUDIO_MEDIA, 1, waveformatex)


def video_stream(*, width: int = 1280, height: int = 720, format_data_size: int = 44) -> bytes:
    """A video stream; ``format_data_size`` - 40 bytes of codec-private data follow the BITMAPINFOHEADER."""
    bitmapinfoheader = struct.pack("<IiiHHIIiiII", format_data_size, width, height, 1, 24, 0x33564D57, 0, 0, 0, 0, 0)
    type_specific = struct.pack("<IIBH", width, height, 0x02, format_data_size) + bitmapinfoheader
    type_specific += b"\x00" * (format_data_size - len(bitmapinfoheader))
    return _stream_properties(_VIDEO_MEDIA, 2, type_specific)


def write_asf(path: Path, *streams: bytes, title: str = "Synthetic Title", author: str = "Synthetic Artist", duration_s: float = 60.0) -> Path:
    """Write an ASF header with the given streams (in order) to ``path``."""
    objects = [_file_properties(duration_s), _content_description(title, author), *streams]
    body = b"".join(objects)
    header = _HEADER + struct.pack("<QI", 30 + len(body), len(objects)) + b"\x01\x02" + body
    path.write_bytes(header)
    return path
