"""The agent-side companion content extractor, against real synthetic files of every type (phaze-osy6j).

Every fixture is written to ``tmp_path`` as real bytes and read back through ``read_companion`` -- the
function the scan, the watcher and the backfill task call -- never handed to the classifier as a
pre-decoded string. All names and contents are invented.
"""

from __future__ import annotations

import codecs
import hashlib
import typing
from typing import TYPE_CHECKING

import pytest

from phaze.services import companion_features
from phaze.services.companion_features import (
    MAX_FOLDER_MEDIA,
    MAX_REFERENCES,
    MediaReference,
    detect_encoding,
    extract_content_features,
    folder_media,
    read_companion,
)


if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# CP437 box-drawing art: the block and frame bytes 0xB0-0xDF a scene NFO is drawn with.
_ART = bytes([0xDB, 0xDC, 0xDF, 0xB0, 0xB1, 0xB2, 0xC4, 0xCD, 0xBA, 0xC9, 0xBB, 0xC8, 0xBC]) * 4


def _cp437_nfo() -> bytes:
    lines = [
        _ART,
        b"\xba  Artist ....: Example Artist" + b" " * 10 + b"\xba",
        b"\xba  Genre .....: Trance" + b" " * 10 + b"\xba",
        b"\xba  Source ....: FM" + b" " * 10 + b"\xba",
        b"\xba  Air date ..: 2024-04-12" + b" " * 10 + b"\xba",
        _ART,
        b"\xb3 01. Example Artist - First Tune [Label A]",
        b"\xb3 02. Other Artist - Second Tune [Label B]",
        b"\xb3 03. Third Artist - Third Tune [Label C]",
        _ART,
    ]
    return b"\r\n".join(lines) + b"\r\n"


# A release-site stamp: a short advert, the same bytes in every release a site touched.
_STAMP = b"Downloaded from www.example-release-site.test\r\nVisit us for more free sets!\r\nJoin us on the forum.\r\n"


def test_cp437_nfo_is_detected_and_its_release_tracklist_is_read(tmp_path: Path) -> None:
    path = _write(tmp_path / "rel" / "00-example-artist-live-2024.nfo", _cp437_nfo())

    reading = read_companion(str(path))

    assert reading.features.encoding == "cp437"
    assert reading.features.is_tracklist is True
    assert reading.features.junk_class is None
    assert reading.fingerprint == hashlib.sha256(path.read_bytes()).hexdigest()
    assert reading.byte_size == path.stat().st_size


def test_no_decision_depends_on_telling_cp437_from_windows_1252() -> None:
    """The two code pages agree on every byte below 0x80, and only those bytes reach a decision."""
    raw = _cp437_nfo()
    as_cp437 = companion_features._decision_text(raw.decode("cp437"), "cp437")
    as_cp1252 = companion_features._decision_text(raw.decode("cp1252", errors="replace"), "cp1252")
    assert as_cp437 == as_cp1252

    # A low-high-byte file falls to the Windows-1252 fallback, a box-heavy one to CP437; with the same
    # ASCII content the two reach the same verdicts.
    few_high = raw.replace(_ART, b"\xaf" * 8)
    assert detect_encoding(few_high) == "cp1252"
    for suffix in (".nfo", ".txt"):
        a = extract_content_features(raw, suffix)
        b = extract_content_features(few_high, suffix)
        assert (a.is_tracklist, a.junk_class, a.references) == (b.is_tracklist, b.junk_class, b.references)


def test_windows_1251_is_never_detected() -> None:
    """Survey §4.5: every Windows-1251 detection was CP437 block art (5 false positives, 0 true)."""
    block_art = bytes([0xDB, 0xDC, 0xDF]) * 30 + b"\r\nExample release notes\r\n"
    assert block_art.decode("cp1251", errors="replace").count("Ы") >= 30  # decodes to Cyrillic capitals...
    assert detect_encoding(block_art) == "cp437"  # ...and is read as the art it is.

    cyrillic = "Треклист: первый трек, второй трек, третий трек".encode("cp1251")
    assert detect_encoding(cyrillic) == "cp1252"
    assert "cp1251" not in typing.get_args(companion_features.Encoding)


def test_all_nul_file_is_its_own_junk_class(tmp_path: Path) -> None:
    path = _write(tmp_path / "rel" / "broken.nfo", b"\x00" * 4096)

    reading = read_companion(str(path))

    assert reading.features.encoding == "all-nul"
    assert reading.features.junk_class == "all_nul"
    assert reading.features.references == ()
    assert reading.features.is_tracklist is False


@pytest.mark.parametrize("payload", [b"", b"  \r\n -- \r\n", b"see you\r\n"])
def test_zero_byte_and_near_empty_files_are_empty(tmp_path: Path, payload: bytes) -> None:
    path = _write(tmp_path / "rel" / "Tracklist.txt", payload)

    assert read_companion(str(path)).features.junk_class == "empty"


def test_a_stamp_is_a_site_advert_and_a_stamp_with_real_text_appended_is_not_junk(tmp_path: Path) -> None:
    stamp = _write(tmp_path / "a" / "site.nfo", _STAMP)
    appended = _write(
        tmp_path / "b" / "site.nfo",
        _STAMP + b"\r\nTracklist:\r\n01. Example Artist - First Tune\r\n02. Other Artist - Second Tune\r\n03. Third Artist - Third Tune\r\n",
    )

    plain = read_companion(str(stamp))
    extended = read_companion(str(appended))

    assert plain.features.junk_class == "site_ad"
    assert extended.features.junk_class is None
    assert extended.features.is_tracklist is True
    # Different bytes, so the appended copy can never join the stamp's content group.
    assert plain.fingerprint != extended.fingerprint


def test_a_release_note_with_a_url_is_not_a_site_advert() -> None:
    """The layout the operator corrected on the phaze-9aker labels: a real release note, not an ad."""
    note = (
        b"Artist: Example Artist | Date: April 12, 2024 | Location: Example Club | Set Type: Live | "
        b"Set Length: 122 mins | Recording Quality: 3/5 | Comments: a fine set, download it at www.example.test\r\n"
    )
    assert extract_content_features(note.replace(b" | ", b"\r\n"), ".txt").junk_class is None


def test_cue_file_lines_are_references_and_its_tracks_a_tracklist(tmp_path: Path) -> None:
    sheet = (
        'PERFORMER "Example Artist"\r\nTITLE "Live at Example Festival"\r\n'
        'FILE "Example Artist - Live @ Example Festival - 2024-04-12.mp3" MP3\r\n'
        '  TRACK 01 AUDIO\r\n    TITLE "First Tune"\r\n    INDEX 01 00:00:00\r\n'
        '  TRACK 02 AUDIO\r\n    TITLE "Second Tune"\r\n    INDEX 01 05:10:00\r\n'
        "FILE part2.WAV WAVE\r\n"
    )
    path = _write(tmp_path / "rel" / "set.cue", codecs.BOM_UTF8 + sheet.encode())

    features = read_companion(str(path)).features

    assert features.encoding == "utf-8-sig"
    assert features.is_tracklist is True
    assert features.references == (
        MediaReference("Example Artist - Live @ Example Festival - 2024-04-12.mp3", "cue_file"),
        MediaReference("part2.WAV", "cue_file"),
    )


def test_m3u_entries_are_references_and_a_playlist_is_never_a_tracklist(tmp_path: Path) -> None:
    playlist = (
        "#EXTM3U\n#EXTINF:3600,Example Artist - Live\n"
        "01-example_artist-live-2024.mp3\n"
        "sub\\02-example_artist-live-2024.MP3\n"
        "http://stream.example.test/live.mp3\n"
        "01-example_artist-live-2024.mp3\n"
        "cover.jpg\n"
    )
    path = _write(tmp_path / "rel" / "00-example_artist-live-2024.m3u", playlist.encode())

    features = read_companion(str(path)).features

    assert features.references == (
        MediaReference("01-example_artist-live-2024.mp3", "m3u"),
        MediaReference("02-example_artist-live-2024.MP3", "m3u"),
    )
    assert features.reference_count == 2
    assert features.is_tracklist is False


def test_pls_file_values_are_references(tmp_path: Path) -> None:
    pls = "[playlist]\nFile1=/music/set one.flac\nTitle1=Set one\nFile2=https://radio.example.test/stream\nNumberOfEntries=2\n"
    path = _write(tmp_path / "rel" / "set.pls", pls.encode())

    assert read_companion(str(path)).features.references == (MediaReference("set one.flac", "pls"),)


def test_utf16_txt_tracklist_with_a_filename_token(tmp_path: Path) -> None:
    text = "Tracklist for: Example Show 046.mp3\r\n[00:00] Example Artist - First Tune\r\n[05:12] Other Artist - Second Tune\r\n[11:40] Third Artist - Third Tune\r\n"
    path = _write(tmp_path / "rel" / "Example Show 046.txt", codecs.BOM_UTF16_LE + text.encode("utf-16-le"))

    features = read_companion(str(path)).features

    assert features.encoding == "utf-16"
    assert features.is_tracklist is True
    # The token keeps the words before the filename; resolving a token that ENDS with a media
    # filename is the linking chain's job.
    assert features.references == (MediaReference("Tracklist for: Example Show 046.mp3", "text_token"),)


def test_utf16_without_a_bom_is_read_by_its_nul_pattern() -> None:
    text = "01. Example Artist - First Tune\n02. Other Artist - Second Tune\n03. Third Artist - Third Tune\n"
    assert detect_encoding(text.encode("utf-16-le")) == "utf-16-le"
    assert extract_content_features(text.encode("utf-16-le"), ".txt").is_tracklist is True


def test_reference_basenames_are_nfc_and_the_list_is_capped() -> None:
    nfd = "café set.mp3"
    features = extract_content_features(f'FILE "{nfd}" MP3\r\n'.encode(), ".cue")
    assert features.references == (MediaReference("café set.mp3", "cue_file"),)

    many = "".join(f"track-{index:04d}.mp3\n" for index in range(MAX_REFERENCES + 5)).encode()
    capped = extract_content_features(many, ".m3u")
    assert len(capped.references) == MAX_REFERENCES
    assert capped.reference_count == MAX_REFERENCES + 1  # extraction stops one past the cap


def test_a_file_past_the_read_cap_is_fingerprinted_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(companion_features, "MAX_FEATURE_BYTES", 64)
    payload = b"01. Example Artist - First Tune\n" * 10
    path = _write(tmp_path / "rel" / "big.txt", payload)

    reading = read_companion(str(path))

    assert reading.truncated is True
    assert reading.byte_size == len(payload)
    assert reading.fingerprint == hashlib.sha256(payload).hexdigest()


def test_folder_media_lists_only_media_files_sorted_and_capped(tmp_path: Path) -> None:
    folder = tmp_path / "rel"
    for name in ("b-set.mp3", "a-set.FLAC", "cover.jpg", "notes.nfo"):
        _write(folder / name, b"x")
    (folder / "sub.mp3").mkdir()  # a directory named like media is not media

    assert folder_media(str(folder)) == (("a-set.FLAC", "b-set.mp3"), 2)
    assert folder_media(str(tmp_path / "missing")) == ((), 0)

    dump = tmp_path / "dump"
    for index in range(MAX_FOLDER_MEDIA + 3):
        _write(dump / f"{index:04d}.mp3", b"x")
    names, count = folder_media(str(dump))
    assert len(names) == MAX_FOLDER_MEDIA
    assert count == MAX_FOLDER_MEDIA + 3


async def test_the_reporter_skips_non_companions_and_unreadable_files_without_posting(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    from phaze.services.companion_features_report import report_companion_features

    api = AsyncMock()
    media = _write(tmp_path / "rel" / "set.mp3", b"audio")

    assert await report_companion_features(api, [str(media)]) == 0
    assert await report_companion_features(api, [str(tmp_path / "rel" / "vanished.nfo")]) == 0
    api.post_companion_features.assert_not_awaited()


async def test_the_reporter_swallows_a_control_plane_error(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    from phaze.services.agent_client import AgentApiServerError
    from phaze.services.companion_features_report import report_companion_features

    api = AsyncMock()
    api.post_companion_features.side_effect = AgentApiServerError("POST /api/internal/agent/companion-features -> 503")
    cue = _write(tmp_path / "rel" / "set.cue", b'FILE "set.mp3" MP3\r\n')

    assert await report_companion_features(api, [str(cue)]) == 0
    api.post_companion_features.assert_awaited_once()


def test_a_record_path_with_a_nul_is_refused() -> None:
    from pydantic import ValidationError

    from phaze.schemas.agent_companion_features import CompanionFeaturesRecord

    reading = companion_features.CompanionReading(fingerprint="0" * 64, byte_size=0, truncated=False, features=extract_content_features(b"", ".nfo"))
    assert CompanionFeaturesRecord.from_reading("/r/a.nfo", reading).content_junk_class == "empty"
    with pytest.raises(ValidationError, match="NUL"):
        CompanionFeaturesRecord.from_reading("/r/a\0.nfo", reading)


# Bounded work: the extractor reads files phaze does not control, on the agent.

_MIB = 1 << 20


def _adversarial_inputs() -> dict[str, bytes]:
    """Inputs built to make the survey's original patterns backtrack, as one line and as many long lines."""
    shapes = {
        "spaces after a number": b"1" + b" " * 1000 + b"x",
        "spaced dashes": b"a - " * 250,
        "a long word then one spaced dash": b"a" * 997 + b" - ",
        "repeated www prefixes": b"www." * 250,
        "a token with no stop character": b"a" * 1000 + b".mp3x",
        "field dots": b"Artist" + b"." * 1000,
        "timestamp separators": b"12" + b": " * 500,
    }
    inputs: dict[str, bytes] = {}
    for name, line in shapes.items():
        inputs[f"{name}, one line"] = (line * (_MIB // len(line) + 1))[:_MIB]
        inputs[f"{name}, many lines"] = ((line[:1000] + b"\n") * (_MIB // 1001 + 1))[:_MIB]
    return inputs


@pytest.mark.parametrize("name", sorted(_adversarial_inputs()))
@pytest.mark.parametrize("suffix", [".txt", ".nfo", ".cue", ".m3u", ".pls"])
def test_an_adversarial_megabyte_line_is_extracted_in_bounded_time(name: str, suffix: str) -> None:
    import time

    payload = _adversarial_inputs()[name]
    assert len(payload) == _MIB

    started = time.perf_counter()
    features = extract_content_features(payload, suffix)
    elapsed = time.perf_counter() - started

    # Measured 2026-10-07: on ONE 20,000-character line the survey's numbered-line pattern took 1.59 s
    # and its Artist - Title pattern 1.26 s -- both quadratic in the line length. The bounded
    # extractor handles every 1 MiB input here in about 0.1 s; 5 s is a generous ceiling.
    assert elapsed < 5.0, f"{name} {suffix}: {elapsed:.2f}s"
    assert features.reference_count <= MAX_REFERENCES + 1


_PATTERN_CORPUS = [
    "01. Example Artist - First Tune [Label A]",
    "Example Artist \u2013 Second Tune",
    "Example Artist \u2014 Third Tune",
    "Artist-Title-NoSpaces",
    " - leading dash",
    "trailing dash - ",
    "a - b",
    "- - -",
    "x - - y",
    "visit www.example.test for more - now",
    "see https://example.test/path - here",
    "www.nodot",
    "www.a.b",
    "http://x.y1",
    "www.www.www.example.com",
    "Files.: Example Artist - Live.mp3",
    "track01.MP3 and track02.flac",
    'FILE "a/b\\c.wav" WAVE',
    "no media here.mp3x",
    ".mp3 at the start",
    "x" * 300 + ".mp3",
    "weird.mp3.mp3",
    "a.mp3b.mp4",
    "",
]


def test_the_linear_rewrites_agree_with_the_survey_patterns() -> None:
    """The bounded scans decide exactly what the survey's regexes decided, on ordinary lines."""
    import re

    media = companion_features._MEDIA_EXT_ALT
    survey_token = re.compile(r"([^\\/\"'<>|*?\t\r\n]{1,240}?\.(?:" + media + r"))\b", re.IGNORECASE)
    survey_url = re.compile(r"(?:https?://|www\.)([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})", re.IGNORECASE)
    survey_artist_title = re.compile(r"\w.*\s[-\u2013\u2014]\s.*\w")
    survey_ts = re.compile(r"^\s*(?:\d{1,3}[.)\-:\s]+\s*)?[\[(]?\d{1,3}:\d{2}(?::\d{2})?[\])]?")
    survey_num = re.compile(r"^\s*(?:\d{1,3}|[A-Z]\d{1,2})\s*[.)\-:]?\s+\S")
    corpus = [*_PATTERN_CORPUS, "[00:00] A - B", "00:12:30 A - B", "1) A - B", "01 - A - B", "1.  x", "1 .x", "A1 x", "12:3", "  3:05 a"]

    for line in corpus:
        assert companion_features._text_tokens(line) == survey_token.findall(line), line
        assert companion_features._has_url(line) == bool(survey_url.search(line)), line
        assert companion_features._is_artist_title(line) == bool(survey_artist_title.search(line)), line
        assert bool(companion_features._TS_LINE.match(line)) == bool(survey_ts.match(line)), line
        assert bool(companion_features._NUM_LINE.match(line)) == bool(survey_num.match(line)), line
