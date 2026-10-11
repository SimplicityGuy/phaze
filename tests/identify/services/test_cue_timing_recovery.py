"""Invalid source frames retain exhaustive metadata without manufacturing timing."""

from fractions import Fraction

import pytest

from tests.identify.services.test_local_tracklist_parser import parse


@pytest.mark.parametrize("frame", [75, 77, 99])
def test_isolated_invalid_frames_preserve_all_metadata_and_raw_timing(frame):
    text = (
        'FILE "recording.mp3" MP3\nPERFORMER "Artist"\n'
        'TRACK 01 AUDIO\nTITLE "Opening"\nINDEX 01 00:01:01\n'
        f'TRACK 02 AUDIO\nTITLE "Middle"\nINDEX 01 01:02:{frame}\n'
        'TRACK 03 AUDIO\nTITLE "Closing"\nINDEX 01 02:03:74\n'
    )
    result = parse(text, "cue")
    assert result.status == "found" and result.snapshot.completeness.state == "complete"
    assert result.snapshot.text == text and [track.title for track in result.snapshot.tracks] == ["Opening", "Middle", "Closing"]
    first, invalid, last = result.snapshot.tracks
    assert first.timestamp.offset.as_fraction() == Fraction(76, 75)
    assert last.timestamp.offset.as_fraction() == Fraction(9299, 75)
    assert first.timestamp.offset_usability == last.timestamp.offset_usability == "qualified"
    assert invalid.artist == "Artist" and invalid.timestamp.original == f"01:02:{frame}"
    assert invalid.timestamp.offset is None and invalid.timestamp.offset_usability == "unusable"
    assert invalid.timestamp.origin == first.timestamp.origin and invalid.timestamp.precision == "unknown"
    assert any("invalid_INDEX01_frames" in item for item in result.snapshot.completeness.evidence)
    assert f"line:8:INDEX01:01:02:{frame}" in invalid.evidence


def test_invalid_nonstart_index_has_warning_without_poisoning_valid_start():
    result = parse('FILE "recording.mp3" MP3\nTRACK 01 AUDIO\nINDEX 00 00:00:77\nINDEX 01 00:01:01', "cue")
    assert result.status == "found"
    assert result.snapshot.tracks[0].timestamp.offset.as_fraction() == Fraction(76, 75)
    assert any("invalid_INDEX00_frames" in item for item in result.snapshot.completeness.evidence)


@pytest.mark.parametrize("indices", ["00:00:77\nINDEX 01 00:01:00", "00:01:00\nINDEX 01 00:00:77", "00:00:75\nINDEX 01 00:00:77"])
def test_conflicting_start_indices_still_block_selection(indices):
    result = parse('FILE "recording.mp3" MP3\nTRACK 01 AUDIO\nINDEX 01 ' + indices, "cue")
    assert result.status == "incomplete"
    assert result.snapshot.tracks[0].timestamp.offset_usability == "unusable"
    assert any("conflicting_INDEX01" in item for item in result.snapshot.completeness.evidence)


def test_timing_recovery_does_not_hide_truncation_or_track_caps():
    text = 'FILE "recording.mp3" MP3\nTRACK 01 AUDIO\nINDEX 01 00:00:77\nTRACK 02 AUDIO\nINDEX 01 00:02:00\n'
    for result in (parse(text, "cue", max_tracks=1), parse(text, "cue", read={"truncated": True})):
        assert result.status == "incomplete"
        assert any("cap" in item or "partial_input" in item for item in result.snapshot.completeness.evidence)
