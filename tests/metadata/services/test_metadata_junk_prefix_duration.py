"""phaze-v03x9: the metadata stage stores the REAL duration of an MP3 behind a junk prefix.

Epic ``phaze-pjkbu``. mutagen resyncs into the junk of the trigger shape as Layer 1 and reports a
length several times the real one; the analyze lane already splices past it
(``phaze-oap6y``). Here ``extract_tags`` asks the same detector and takes the length from the real
frames. The fixtures are the synthetic builders shared with the splice tests
(``tests/_mpeg_junk_fixtures.py``), real ffmpeg output; the consumer under test is the real
``extract_tags`` over real mutagen, and the expected values come from ffprobe on the stream file the
fixture was built from. The population rule (rule 4 of CLAUDE.md): only files the shared detector
flags may change; every negative control below pins the stored value to mutagen's own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import mutagen
from mutagen.mp3 import MP3
import pytest

from phaze.services import metadata, mpeg_frames
from phaze.services.metadata import extract_tags
from tests._mpeg_junk_fixtures import (
    _L1_LEN_MUTAGEN,
    _MUTAGEN_JUNK_BYTES,
    _STREAM_SEC,
    _TRIGGER_ID3_END,
    _encode_stream,
    _encode_varied_vbr,
    _id3,
    _make_mutagen_trigger,
    _make_trigger_vbr,
    _make_vvc,
    _real_duration,
    _run,
    _trigger_junk,
    requires_libmp3lame,
)


if TYPE_CHECKING:
    from pathlib import Path


pytestmark = requires_libmp3lame


def _mutagen_length(path: Path) -> float:
    audio = mutagen.File(str(path))
    assert audio is not None
    return float(audio.info.length)


def _mutagen_bitrate(path: Path) -> int:
    audio = mutagen.File(str(path))
    assert audio is not None
    return int(audio.info.bitrate)


def _forbid_chain_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the 64 MiB frame-chain parse runs: the cheap gate must have declined."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        msg = "the frame-chain parse ran for a file the gate should have skipped"
        raise AssertionError(msg)

    monkeypatch.setattr(mpeg_frames, "read_scan_window", _boom)


# Flagged files: the stored duration is the real one


def test_the_trigger_shape_stores_the_real_duration_not_mutagens_misparse(tmp_path: Path) -> None:
    fixture = _make_mutagen_trigger(tmp_path)
    real = _real_duration(fixture)
    misparsed = _mutagen_length(fixture.path)
    info = MP3(str(fixture.path)).info
    assert (info.layer, info.sketchy) == (1, True), "precondition: mutagen reads the trigger shape as sketchy Layer 1"
    assert abs(misparsed - real) > 1, "precondition: mutagen's length is wrong"

    tags = extract_tags(str(fixture.path))

    assert tags.duration is not None
    assert abs(tags.duration - real) <= 1
    assert abs(tags.duration - _STREAM_SEC) <= 1
    assert tags.duration_corrected is True


def test_the_trigger_shape_stores_the_real_bitrate_not_mutagens_misparse(tmp_path: Path) -> None:
    """The fixture is a 320 kbps encode; mutagen reads the junk's Layer 1 frame, which is nowhere near it."""
    fixture = _make_mutagen_trigger(tmp_path)
    misparsed = _mutagen_bitrate(fixture.path)
    assert abs(misparsed - 320_000) > 100_000, "precondition: mutagen's bitrate is wrong"

    tags = extract_tags(str(fixture.path))

    assert tags.bitrate is not None
    assert abs(tags.bitrate - 320_000) <= 3_200  # within 1%


def test_the_trigger_shape_over_a_vbr_stream_stores_the_real_duration(tmp_path: Path) -> None:
    """VBR: the Xing frame count gives the duration; a bitrate/size estimate would not."""
    fixture = _make_trigger_vbr(tmp_path)
    real = _real_duration(fixture)
    first = mpeg_frames.parse_frame_header(fixture.stream.read_bytes(), 0)
    assert first is not None
    size_estimate = 8 * fixture.stream.stat().st_size / (first.bitrate_kbps * 1000)
    assert abs(size_estimate - real) > 1, "precondition: a bitrate/size estimate would be wrong on this stream"

    tags = extract_tags(str(fixture.path))

    assert tags.duration is not None
    assert abs(tags.duration - real) <= 1
    assert tags.duration_corrected is True
    # VBR: the Xing average, i.e. the stream's bytes over its duration, which the fixture's encode fixes.
    encode_average = 8 * fixture.stream.stat().st_size / real
    assert tags.bitrate is not None
    assert abs(tags.bitrate - encode_average) <= 0.02 * encode_average
    assert abs(_mutagen_bitrate(fixture.path) - encode_average) > 0.02 * encode_average, "precondition: mutagen's bitrate is wrong"


# Negative controls: what currently works keeps mutagen's length, and never pays for the parse


@pytest.mark.parametrize("variant", ["cbr", "vbr"])
def test_a_clean_mp3_keeps_mutagens_length_and_skips_the_chain_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str) -> None:
    path = tmp_path / f"clean_{variant}.mp3"
    frames = (
        _encode_stream(tmp_path / "s.mp3", rate=44100, bitrate="192k") if variant == "cbr" else _encode_varied_vbr(tmp_path / "s.mp3", rate=44100)
    )
    path.write_bytes(_id3(_TRIGGER_ID3_END) + frames)
    expected = _mutagen_length(path)
    expected_bitrate = _mutagen_bitrate(path)
    _forbid_chain_parse(monkeypatch)

    tags = extract_tags(str(path))

    assert tags.duration == expected
    assert tags.bitrate == expected_bitrate
    assert tags.duration_corrected is False


def test_the_vvc_shape_keeps_mutagens_correct_length_and_skips_the_chain_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 3 measured raw-VVC files: mutagen reads them right (layer 3, not sketchy), so no parse."""
    fixture = _make_vvc(tmp_path)
    info = MP3(str(fixture.path)).info
    assert (info.layer, info.sketchy) == (3, False), "precondition: mutagen reads the VVC shape correctly"
    expected = _mutagen_length(fixture.path)
    assert abs(expected - _real_duration(fixture)) <= 1
    expected_bitrate = _mutagen_bitrate(fixture.path)
    _forbid_chain_parse(monkeypatch)

    tags = extract_tags(str(fixture.path))

    assert tags.duration == expected
    assert tags.bitrate == expected_bitrate
    assert tags.duration_corrected is False


def test_a_real_layer_2_file_passes_the_gate_but_is_not_flagged(tmp_path: Path) -> None:
    """The 37 measured MP2-in-.mp3 files: mutagen layer 2 enters the gate; chain layer 2 == mutagen's."""
    path = tmp_path / "real_mp2.mp3"
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
            "-c:a",
            "mp2",
            "-b:a",
            "192k",
            "-f",
            "mp2",
            str(path),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    assert MP3(str(path)).info.layer == 2, "precondition: mutagen reads it as Layer 2"
    expected = _mutagen_length(path)
    expected_bitrate = _mutagen_bitrate(path)

    tags = extract_tags(str(path))

    assert tags.duration == expected
    assert tags.bitrate == expected_bitrate
    assert tags.duration_corrected is False


# Failure containment: an unreadable or hostile head leaves mutagen's value alone


def test_an_unreadable_head_keeps_mutagens_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _make_mutagen_trigger(tmp_path)
    expected = _mutagen_length(fixture.path)

    def _raise(*_a: Any, **_k: Any) -> Any:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(mpeg_frames, "read_scan_window", _raise)

    tags = extract_tags(str(fixture.path))

    assert tags.duration == expected
    assert tags.duration_corrected is False


def test_an_out_of_bounds_tag_region_keeps_mutagens_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``read_scan_window`` returns ``None`` for a tag region past the file or the bound: not a candidate."""
    fixture = _make_mutagen_trigger(tmp_path)
    expected = _mutagen_length(fixture.path)
    monkeypatch.setattr(mpeg_frames, "read_scan_window", lambda _path: None)

    tags = extract_tags(str(fixture.path))

    assert tags.duration == expected
    assert tags.duration_corrected is False


def test_the_detector_returns_none_for_a_hostile_tag_size_on_a_real_file(tmp_path: Path) -> None:
    """The real ``read_scan_window`` bound the stub above stands in for: a ~256 MiB size field."""
    fixture = _make_mutagen_trigger(tmp_path)
    data = bytearray(fixture.path.read_bytes())
    data[6:10] = b"\x7f\x7f\x7f\x7f"
    fixture.path.write_bytes(bytes(data))

    assert mpeg_frames.read_scan_window(fixture.path) is None


def test_a_zero_length_reread_keeps_mutagens_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the re-read of the real stream yields no positive length, nothing is corrected."""
    fixture = _make_mutagen_trigger(tmp_path)
    expected, expected_bitrate = _mutagen_length(fixture.path), _mutagen_bitrate(fixture.path)

    class _EmptyInfo:
        length = 0.0
        bitrate = 0

        def __init__(self, *_a: Any, **_k: Any) -> None: ...

    monkeypatch.setattr(metadata, "MPEGInfo", _EmptyInfo)

    tags = extract_tags(str(fixture.path))

    assert (tags.duration, tags.bitrate, tags.duration_corrected) == (expected, expected_bitrate, False)


def test_a_junk_prefixed_file_with_no_real_stream_keeps_mutagens_value(tmp_path: Path) -> None:
    """A sketchy Layer 1 read but no chain of 8 frames anywhere: nothing to splice, nothing changes."""
    path = tmp_path / "junk_only.mp3"
    path.write_bytes(_id3(_TRIGGER_ID3_END) + _trigger_junk(_MUTAGEN_JUNK_BYTES, _L1_LEN_MUTAGEN))
    expected = _mutagen_length(path)
    assert MP3(str(path)).info.sketchy is True, "precondition: mutagen reads the junk as a sketchy stream"

    tags = extract_tags(str(path))

    assert tags.duration == expected
    assert tags.duration_corrected is False


def test_the_gate_is_the_modules_own_choice_of_signal() -> None:
    assert metadata._LAYER_CODEC == {1: "mp1", 2: "mp2", 3: "mp3"}
