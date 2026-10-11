"""Invented examples of archive-observed text grammar and grouping."""

import pytest

from tests.identify.services.test_local_tracklist_parser import parse


@pytest.mark.parametrize("separator", [" / ", " • ", "\t", " -", "- "])
def test_supported_columns_keep_artist_title_and_unknown_origin(separator: str) -> None:
    outcome = parse(f"Setlist:\n01. First Artist{separator}Opening\n02. Other Artist{separator}Closing\n")
    assert outcome.status == "found"
    assert [(row.artist, row.title) for row in outcome.snapshot.tracks] == [("First Artist", "Opening"), ("Other Artist", "Closing")]
    assert all(row.section_ordinal == 1 and row.timestamp is None for row in outcome.snapshot.tracks)


def test_quoted_and_parenthesized_hyphens_are_not_field_separators() -> None:
    outcome = parse('01. Artist - "Title - Movement" (Club - Mix)\n02. Other (One - Two) - Closing\n')
    assert outcome.status == "found"
    assert outcome.snapshot.tracks[0].title == '"Title - Movement" (Club - Mix)'
    assert outcome.snapshot.tracks[1].artist == "Other (One - Two)"


def test_compound_rows_keep_original_structure_as_review_warning() -> None:
    text = "Tracklist:\n01. Artist - Title / Other - Second\n02. Artist-Title\n"
    outcome = parse(text)
    assert outcome.status == "found" and len(outcome.snapshot.tracks) == 2
    assert all(row.artist is row.title is None for row in outcome.snapshot.tracks)
    assert "ambiguous_artist_title" in " ".join(outcome.snapshot.sections[0].warnings)
    assert "unspaced_artist_title_unresolved" in " ".join(outcome.snapshot.sections[0].warnings)
    assert outcome.snapshot.text == text


def test_unknown_annotations_and_missing_title_do_not_invent_identity() -> None:
    outcome = parse("Setlist:\n01. ID (heard elsewhere)\n02. ??? maybe a remix\n03. Known Artist - \n")
    assert outcome.status == "found" and len(outcome.snapshot.tracks) == 3
    assert all(row.title is None for row in outcome.snapshot.tracks)
    assert outcome.snapshot.tracks[2].artist == "Known Artist"


def test_title_only_duration_table_requires_no_invented_artist() -> None:
    outcome = parse("Source: FM\n01. Opening [04:35]\n02. Closing [05:12]\n")
    assert outcome.status == "found"
    assert [row.title for row in outcome.snapshot.tracks] == ["Opening", "Closing"]
    assert all(row.artist is None and row.timestamp is None for row in outcome.snapshot.tracks)


@pytest.mark.parametrize("prefix", ["[012] ", "00 [012] ", "[12:35] 00. "])
def test_minute_and_zero_based_prefixes_do_not_qualify_timing(prefix: str) -> None:
    outcome = parse("Tracklist:\n" + prefix + "Artist - Title\n")
    assert outcome.status == "found"
    row = outcome.snapshot.tracks[0]
    assert row.position == 1 and row.timestamp.offset is row.timestamp.origin is None
    assert row.timestamp.offset_usability == "unusable"


def test_digit_artist_names_do_not_become_side_ordinals() -> None:
    outcome = parse("Tracklist:\nA2 - Opening\n808 State - Closing\n")
    assert outcome.status == "found" and [row.artist for row in outcome.snapshot.tracks] == ["A2", "808 State"]


def test_release_frame_and_dated_headers_are_not_tracks() -> None:
    outcome = parse(
        "/--------------------\n| File(s):\n| 01 - Artist - Live (2024-01-01).mp3\n| 02 - Artist - Live Part 2.mp3\n\\--------------------\n2024-01-01 Artist - Live Set\nTracklist:\n01. Artist - Opening\n02. Other - Closing\n"
    )
    assert outcome.status == "found" and [row.title for row in outcome.snapshot.tracks] == ["Opening", "Closing"]


@pytest.mark.parametrize("bar", ["|", "│", "║"])
@pytest.mark.parametrize("outer_frame", [False, True])
def test_framed_tracklist_rows_are_not_export_headers(bar: str, outer_frame: bool) -> None:
    text = f"{bar} Tracklist:\n{bar} 01. Artist - Opening\n{bar} 02. Other - Closing\n"
    if outer_frame:
        text = "/--------------------\n" + text + "\\--------------------\n"
    outcome = parse(text)
    assert outcome.status == "found" and outcome.snapshot.text == text
    assert [(row.artist, row.title) for row in outcome.snapshot.tracks] == [("Artist", "Opening"), ("Other", "Closing")]
    assert all(row.section_ordinal == 1 for row in outcome.snapshot.tracks)
    assert [row.evidence[0] for row in outcome.snapshot.tracks] == (["line:3", "line:4"] if outer_frame else ["line:2", "line:3"])


def test_explicit_sections_restart_ordinals_without_flattening() -> None:
    outcome = parse("Part 1: First Set\n01. Artist - Opening\n02. Other - Next\nPart 2: Second Set\n01. Third - Closing\n")
    assert outcome.status == "found" and len(outcome.snapshot.sections) == 2
    assert [row.section_ordinal for row in outcome.snapshot.tracks] == [1, 1, 2]
    assert [row.position for row in outcome.snapshot.tracks] == [1, 2, 3]
    assert "declared_position:1" in " ".join(outcome.snapshot.tracks[2].evidence)


def test_untyped_headers_between_restarting_lists_are_explicit_sections() -> None:
    outcome = parse("First Set\n01. Artist - Opening\n02. Other - Next\n\nSecond Set\n\n01. Third - Closing\n")
    assert outcome.status == "found"
    assert [section.title for section in outcome.snapshot.sections] == ["First Set", "Second Set"]


def test_absent_section_is_retained_and_does_not_hide_usable_other_section() -> None:
    outcome = parse("Part 1:\nTracklist unknown\nPart 2:\n01. Artist - Title\n")
    assert outcome.status == "incomplete" and outcome.snapshot.section_scan_complete
    assert [section.completeness.state for section in outcome.snapshot.sections] == ["incomplete", "complete"]
    assert outcome.snapshot.tracks[0].section_ordinal == 2


@pytest.mark.parametrize(
    "text", ["Tracklist unknown", "No tracklist", "Visit our website for downloads", "Categories:\n01: Music: MP3\n02: Music: FLAC\n"]
)
def test_absent_or_promotional_documents_do_not_invent_tracks(text: str) -> None:
    assert parse(text).snapshot.tracks == ()


@pytest.mark.parametrize("options", [{"max_tracks": 1}, {"max_lines": 2}, {"max_characters": 30}, {"read": {"truncated": True}}])
def test_limits_never_allow_independent_section_selection(options: dict) -> None:
    outcome = parse("Part 1:\n01. Artist - Title\nPart 2:\n01. Other - Closing\n", **options)
    assert outcome.status == "incomplete" and not outcome.snapshot.section_scan_complete


def test_duplicate_numbers_within_section_remain_incomplete() -> None:
    outcome = parse("Part 1:\n01. Artist - First\n01. Other - Second\n")
    assert outcome.status == "incomplete" and outcome.snapshot.sections[0].completeness.state == "incomplete"


def test_additional_tab_columns_remain_unresolved_evidence() -> None:
    outcome = parse("01. Artist\tTitle\tAlbum\n02. Other\tClosing\tLabel\n")
    assert outcome.status == "found" and outcome.snapshot.tracks[0].title == "Title"
    assert "additional_columns_unresolved" in " ".join(outcome.snapshot.sections[0].warnings)


def test_document_tracklist_heading_is_not_an_empty_section_before_parts() -> None:
    outcome = parse("Collection - Radio Shows\nTracklisting:\nPart 1: First\n01. A - B\nPart 2: Other\n01. C - D\n")
    assert outcome.status == "found" and len(outcome.snapshot.sections) == 2


def test_repeated_absent_sets_keep_their_independent_boundaries() -> None:
    outcome = parse("Part 1: First\nNo tracklist\nPart 2: Second\nTracklist unknown\nPart 3: Third\n01. A - B\n")
    assert len(outcome.snapshot.sections) == 3
    assert [section.completeness.state for section in outcome.snapshot.sections] == ["incomplete", "incomplete", "complete"]


def test_unnumbered_compound_rows_and_title_media_suffix_are_retained() -> None:
    outcome = parse("Tracklist:\nFirst - Opening\nArtist - Title / Alternate\nOther - - Closing\n01. Artist - Title.wav\n")
    assert len(outcome.snapshot.tracks) == 4
    assert outcome.snapshot.tracks[1].artist is None and outcome.snapshot.tracks[2].title is None
    assert outcome.snapshot.tracks[3].title == "Title.wav"


def test_unsupported_row_before_any_section_does_not_claim_global_scan_complete() -> None:
    outcome = parse("A2. Artist - Title\nPart 1:\n01. Other - Closing\n")
    assert outcome.status == "incomplete" and not outcome.snapshot.section_scan_complete
