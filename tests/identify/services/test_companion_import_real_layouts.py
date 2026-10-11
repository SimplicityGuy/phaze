"""Archive-observed syntax, rebuilt with invented names; no private archive data."""

from fractions import Fraction
import hashlib
from pathlib import Path
import uuid

import pytest

from phaze.schemas.agent_companion_capture import CaptureBudget, CaptureTarget
from phaze.services.companion_capture_worker import read_capture
from phaze.services.companion_features import extract_content_features
from phaze.tracklist_providers.domain import LoadBudget
from phaze.tracklist_providers.local_release import extract_release_metadata
from tests.identify.services.test_local_release import read
from tests.identify.services.test_local_tracklist_parser import parse


SCENE = "\n".join(
    [
        "▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓",
        " █▓ /ARTiST..........: Example Artist",
        " ▓▒ /TiTLE...........: Example Set",
        " ▒░ /GENRE...........: House",
        "    /SOURCE..........: FM",
        "    /YEAR............: 2013",
        "    /LENGTH..........: 987:24 min",
        "    /QUALiTY.........: 320kbps / 44,1kHz / Joint Stereo",
        " Tracklist:",
        " 01. First Artist - Opening",
        " 02. Second Artist - Closing",
        " Visit our website for more releases.",
    ]
)


def test_cp437_scene_capture_through_both_interpreters(tmp_path: Path) -> None:
    raw = SCENE.encode("cp437")
    path = tmp_path / "release.nfo"
    path.write_bytes(raw)
    captured = read_capture(
        CaptureTarget(file_id=uuid.uuid4(), path=str(path), expected_sha256=hashlib.sha256(raw).hexdigest(), expected_size=len(raw)),
        CaptureBudget(),
        [str(tmp_path)],
    )
    assert captured.status == "found" and captured.encoding == "cp437" and captured.text == SCENE
    result = parse(captured.text, "nfo")
    assert result.status == "found" and [track.title for track in result.snapshot.tracks] == ["Opening", "Closing"]
    assert result.snapshot.text == SCENE and "excluded_non_track_line" in " ".join(result.snapshot.provenance)
    release = extract_release_metadata(captured, source_format="nfo")
    assert release.completeness.state == "complete"
    fields = {fact.field: fact for fact in release.facts}
    assert fields["artist"].value == "Example Artist"
    assert fields["duration"].normalized_value == "59244"
    assert fields["quality"].original_value == "320kbps / 44,1kHz / Joint Stereo"
    for fact in release.facts:
        _, _, start, end = fact.evidence[0].split(":")
        assert SCENE[int(start) : int(end)] == fact.original_value


def test_export_wrapper_and_footer_do_not_invalidate_track_rows() -> None:
    text = "/----------------------\n| Export information:\n| Source: Recorded FM\n| File(s):\n| set.mp3\n\\----------------------\n01) Example Artist - Opening\n02) Other Artist - Closing (Club Mix)\nThanks for completing this tracklist!\n"
    result = parse(text)
    assert result.status == "found" and len(result.snapshot.tracks) == 2
    assert result.snapshot.tracks[1].title == "Closing (Club Mix)"
    assert result.snapshot.text == text


def test_unnumbered_artist_title_run_and_unsupported_numbered_row() -> None:
    result = parse("Export information\nFirst Artist - Opening\nOther Artist - Closing\nFooter\n")
    assert result.status == "found" and [track.position for track in result.snapshot.tracks] == [1, 2]
    unsupported = parse("01. A - B\nA2. C - D\n03. E - F\n")
    assert unsupported.status == "incomplete" and "unsupported_track_number" in " ".join(unsupported.snapshot.completeness.evidence)


@pytest.mark.parametrize("prefix", ["01 ", "01- ", "01: ", "[01] "])
def test_observed_number_delimiters(prefix: str) -> None:
    result = parse(prefix + "Example Artist - Opening")
    assert result.status == "found" and result.snapshot.tracks[0].artist == "Example Artist"


@pytest.mark.parametrize("prefix", ["[12:35] ", "12:35 ", "01:02:03 "])
def test_timestamp_led_rows_remain_unqualified(prefix: str) -> None:
    result = parse(prefix + "Example Artist - Opening")
    assert result.status == "found" and result.snapshot.tracks[0].position == 1
    time = result.snapshot.tracks[0].timestamp
    assert time.original == prefix.strip(" []") and time.offset is time.origin is None
    assert time.offset_usability == "unusable"


def test_declared_title_table_and_trailing_duration_do_not_invent_offsets() -> None:
    result = parse("Tracklist:\n01. Opening                  [59:32]\n02. Closing                  64:41 min\n")
    assert result.status == "found" and [track.title for track in result.snapshot.tracks] == ["Opening", "Closing"]
    assert all(track.timestamp is None and track.artist is None for track in result.snapshot.tracks)
    assert "trailing_time:59:32:origin_unknown" in " ".join(result.snapshot.tracks[0].evidence)


def test_numeric_scene_art_is_not_a_track() -> None:
    result = parse("▀▀██▄▄      ▀   2 0 2 4   ▀      ▄▄██▀▀\n01. A - B\n02. C - D")
    assert result.status == "found" and [track.position for track in result.snapshot.tracks] == [1, 2]


@pytest.mark.parametrize("artist", ["Artist", '"Artist; Ensemble"'])
def test_cue_comments_agree_across_interpreters_and_keep_source_spans(artist: str) -> None:
    text = f'\ufeff PERFORMER {artist} ; writer comment\r\n TITLE "Example; Set" ; writer comment\r\n FILE part.wav WAVE\r\n TRACK 01 AUDIO\r\n TITLE "Opening; Movement" ; track comment\r\n INDEX 01 00:00:00 ; timing comment\r\n'
    parsed = parse(text, "cue")
    release = extract_release_metadata(read(text), source_format="cue")
    assert parsed.status == "found"
    assert parsed.snapshot.tracks[0].artist == artist.strip('"')
    assert parsed.snapshot.tracks[0].title == "Opening; Movement"
    fields = {fact.field: fact for fact in release.facts}
    assert fields["artist"].value == parsed.snapshot.tracks[0].artist
    assert fields["title"].value == "Example; Set"
    assert all(fact.certainty == "known" for fact in release.facts)
    assert release.completeness.state == "complete"
    assert parsed.snapshot.text == text
    for fact in release.facts:
        _, _, start, end = fact.evidence[0].split(":")
        assert text[int(start) : int(end)] == fact.original_value
        assert "writer comment" not in fact.original_value


def test_cue_writer_extensions_optional_directives_and_intrinsic_index() -> None:
    text = '; Generator comment\nCATALOG 1234567890123\nCDTEXTFILE "text.cdt"\nSONGWRITER "Set Writer"\nINFO "Generated metadata"\nPERFORMER Artist\nFILE part.wav WAVE ; 1234.0 decoded samples\nTRACK 01 AUDIO\nTITLE "Opening; Keep this semicolon"\nISRC USAAA2400001\nFLAGS DCP PRE\nPREGAP 00:02:00\nINDEX 00 00:00:00\nINDEX 01 00:03:10\nINDEX 02 00:04:20\nPOSTGAP 00:01:00\n'
    result = parse(text, "cue")
    assert result.status == "found" and len(result.snapshot.tracks) == 1
    track = result.snapshot.tracks[0]
    assert track.artist == "Artist" and track.title == "Opening; Keep this semicolon"
    assert track.timestamp.offset.as_fraction() == Fraction(235, 75)
    assert track.timestamp.offset_usability == "qualified" and "FILE:1:part.wav" in track.evidence
    assert "INDEX02" in " ".join(track.evidence) and result.snapshot.text == text


@pytest.mark.parametrize("directive", ["ISRC USAAA2400001", "FLAGS PRE", "PREGAP 00:00:00"])
def test_orphan_optional_cue_track_directives_are_still_rejected(directive: str) -> None:
    result = parse(directive + '\nFILE "part.wav" WAVE\nTRACK 01 AUDIO', "cue")
    assert result.status == "incomplete" and "orphan_" in " ".join(result.snapshot.completeness.evidence)


@pytest.mark.parametrize("fmt", ["BINARY", "MOTOROLA", "WAVE", "MP3", "AIFF"])
def test_cue_file_types_are_literals_and_never_opened(fmt: str) -> None:
    result = parse(f'FILE "../unresolved.raw" {fmt}\nTRACK 01 AUDIO\nTITLE Opening\nINDEX 01 00:00:00', "cue")
    assert result.status == "found" and result.snapshot.tracks[0].title == "Opening"


def test_release_evidence_cap_does_not_stop_the_scan() -> None:
    content = read("Scene decoration\n" * 80 + "| Artist........: Example Artist |\r\n| PlayTime......: 59:32 min |\r\n")
    result = extract_release_metadata(content, source_format="nfo", budget=LoadBudget())
    assert result.completeness.state == "complete" and len(result.evidence) == 64
    assert [fact.value for fact in result.facts] == ["Example Artist", "59:32 min"]
    assert result.facts[1].normalized_value == "3572"


@pytest.mark.parametrize(
    ("fmt", "text"),
    [
        ("m3u", "set.mp3\n"),
        ("m3u8", "#EXTM3U\n#EXTINF:5590,Example Set\nset.mp3\n"),
        ("pls", "[playlist]\nFile1=set.mp3\nTitle1=Example Set\nLength1=5590\nNumberOfEntries=1\nVersion=2\n"),
    ],
)
def test_playlists_extract_media_references_without_inventing_mix_chapters(fmt: str, text: str) -> None:
    features = extract_content_features(text.encode(), "." + fmt)
    assert [reference.name for reference in features.references] == ["set.mp3"]
    assert not features.is_tracklist
    result = parse(text, fmt)
    assert result.status == "unsupported" and result.snapshot is None
