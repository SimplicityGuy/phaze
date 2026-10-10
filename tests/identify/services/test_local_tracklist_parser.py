"""Synthetic exact expectations for supported rows, excluded lines and source semantics."""

from datetime import UTC, datetime
from fractions import Fraction

import pytest

from phaze.tracklist_providers import local_registry
from phaze.tracklist_providers.domain import LoadBudget, ProviderCandidate, SourceIdentity, SourceRead, SourceReference
from phaze.tracklist_providers.local_parser import LocalSourceInterpreter


def parse(text: str, fmt: str = "txt", **kwargs):
    identity = SourceIdentity(provider_id="local", native_id="companion:synthetic")
    candidate = ProviderCandidate(identity=identity, source=SourceReference(identity=identity, channel="companion", format=fmt))
    content = SourceRead.model_validate(
        {"status": "found", "code": "captured", "scope": "source", "text": text, "encoding": "utf-8", "retrieved_at": datetime.now(UTC)}
        | kwargs.pop("read", {})
    )
    return LocalSourceInterpreter().interpret(candidate, content, LoadBudget(**kwargs))


def errors(outcome) -> str:
    return " ".join(outcome.snapshot.completeness.evidence)


def test_cue_scope_exact_frames_inheritance_nulls_and_original_text():
    text = '\ufeffPERFORMER "Set Artist"\r\nTITLE "Release"\r\nREM DATE 2024\r\nFILE "part.wav" WAVE\r\nTRACK 01 AUDIO\r\nTITLE "Opening"\r\nINDEX 00 00:00:00\r\nINDEX 01 00:03:10\r\nTRACK 02 AUDIO\r\nPERFORMER "ID"\r\nTITLE "unknown"\r\n'
    result = parse(text, "cue")
    assert result.status == "found" and result.snapshot.text == text
    first, second = result.snapshot.tracks
    assert (first.position, first.artist, first.title) == (1, "Set Artist", "Opening")
    assert first.timestamp.offset.as_fraction() == Fraction(235, 75)
    assert first.timestamp.original == "00:03:10" and first.timestamp.precision == "cue_frame_75"
    assert first.timestamp.offset_usability == "qualified" and ":file:1" in first.timestamp.origin
    assert "INDEX00" in " ".join(first.evidence)
    assert second.artist is second.title is second.timestamp is None
    assert result.snapshot.artist.value == "Set Artist"
    assert result.snapshot.artist.evidence == ("line:1:set_PERFORMER",)
    assert "line:1:set_PERFORMER" in first.evidence and "line:6:TITLE" in first.evidence


def test_multiple_file_origins_are_intrinsic_unresolved_and_never_concatenated():
    result = parse(
        'FILE "../part-a.wav" WAVE\nTRACK 01 AUDIO\nINDEX 01 20:00:74\nFILE "/unresolved/part-b.mp3" MP3\nTRACK 02 AUDIO\nINDEX 01 00:00:01', "cue"
    )
    a, b = result.snapshot.tracks
    assert result.status == "found"
    assert a.timestamp.origin != b.timestamp.origin
    assert a.timestamp.offset.as_fraction() == Fraction(90074, 75)
    assert b.timestamp.offset.as_fraction() == Fraction(1, 75)
    assert any("../part-a.wav" in value for value in a.evidence)
    assert any("target recording unresolved" in value for value in a.timestamp.evidence)


@pytest.mark.parametrize(
    ("tail", "reason"),
    [
        ("INDEX 01 00:60:00", "malformed_INDEX"),
        ("INDEX 01 00:00:75", "malformed_INDEX"),
        ("INDEX 01 broken", "malformed_INDEX"),
        ("INDEX 02 00:00:00", "malformed_INDEX"),
        ("PREGAP 00:00:00", "unsupported_PREGAP"),
        ("FLAGS PRE", "unsupported_FLAGS"),
        ("garbage", "malformed_directive"),
        ("TITLE unquoted", "malformed_TITLE"),
        ('TITLE "ID"\nTITLE "Later"', "duplicate_TITLE"),
        ('PERFORMER "ID"\nPERFORMER "Later"', "duplicate_PERFORMER"),
        ("INDEX 01 00:00:00\nINDEX 01 00:01:00", "conflicting_INDEX01"),
    ],
)
def test_cue_rejected_lines_preserve_source_and_do_not_create_tracks(tail, reason):
    text = 'FILE "part.wav" WAVE\nTRACK 01 AUDIO\n' + tail
    result = parse(text, "cue")
    assert result.status == "incomplete" and reason in errors(result)
    assert len(result.snapshot.tracks) == 1 and result.snapshot.text == text


def test_cue_conflicting_numbering_nonmonotonic_and_missing_origins():
    result = parse('FILE "part.wav" WAVE\nTRACK 02 AUDIO\nINDEX 01 00:02:00\nTRACK 02 AUDIO\nINDEX 01 00:01:00', "cue")
    assert [track.position for track in result.snapshot.tracks] == [2, 3]
    assert "conflicting_numbering" in errors(result) and "nonmonotonic" in errors(result)
    assert "declared_position:2" in result.snapshot.tracks[1].evidence
    assert result.snapshot.tracks[1].timestamp.offset_usability == "unusable"
    missing = parse("TRACK 01 AUDIO\nINDEX 01 00:00:00", "cue")
    assert missing.status == "incomplete" and missing.snapshot.tracks[0].timestamp.origin is None
    assert missing.snapshot.tracks[0].timestamp.offset_usability == "unusable"


@pytest.mark.parametrize(
    "text",
    [
        'FILE "part.flac" BINARY\nTRACK 01 AUDIO',
        'TRACK 01 DATA\nTITLE "Orphan"',
        "INDEX 01 00:00:00",
        'PERFORMER "A"\nPERFORMER "B"',
        'TRACK 01 AUDIO\nFILE "second.wav" WAVE\nTITLE "Orphan"',
        "x" * 4097,
    ],
)
def test_cue_unsupported_or_orphan_scope_incomplete(text):
    assert parse(text, "cue").status == "incomplete"


@pytest.mark.parametrize("fmt", ["txt", "nfo", "embedded", "comments", "lyrics", ".TXT"])
def test_text_ordered_untimed_unknown_and_explicit_annotations(fmt):
    result = parse(
        "Tracklist:\r\n===\r\nArtist: Example\r\n01. Artist - Title [label: Imprint] (remix: Club Mix) (mashup)\r\n02. ID - unknown\r\n03. Title: Untimed Title",
        fmt,
    )
    assert result.status == "found"
    first, unknown, untimed = result.snapshot.tracks
    assert (first.artist, first.title, first.label, first.remix_info, first.is_mashup) == ("Artist", "Title", "Imprint", "Club Mix", True)
    assert unknown.artist is unknown.title is None
    assert untimed.artist is None and untimed.title == "Untimed Title" and untimed.timestamp is None
    assert "line:4" in first.evidence and len(result.snapshot.tracks) == 3


@pytest.mark.parametrize(
    ("timestamp", "kind", "precision"),
    [
        ("12:35", "unknown", "second"),
        ("30:00", "unknown", "second"),
        ("01:02:03", "unknown", "second"),
        ("clock 23:00:01", "clock", "second"),
        ("12 min", "unknown", "minute"),
    ],
)
def test_text_time_syntax_or_magnitude_never_qualifies_offset(timestamp, kind, precision):
    result = parse(f"01. [{timestamp}] Artist - Title")
    timing = result.snapshot.tracks[0].timestamp
    assert result.status == "found" and timing.original == timestamp
    assert (timing.kind, timing.precision) == (kind, precision)
    assert timing.offset is timing.origin is None and timing.offset_usability == "unusable"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("01. Artist - Title - Ambiguous", "ambiguous_artist_title"),
        ("02. Title: First\n02. Title: Second", "conflicting_numbering"),
        ("01. Title\nUnrecognized prose\n02. Next", "excluded_non_grammar_line"),
        ("01. [12:60] Title", "malformed_timestamp"),
        ("01. [bad] Title", "unsupported_timestamp"),
        ("01. [00:00]", "empty_track_body"),
        ("01. Title [label: A] [label: B]", "duplicate_label"),
        ("01. Title (remix: A) (remix: B)", "duplicate_remix"),
        ("01. Title (mashup) (mashup) (mashup) (mashup)", "annotation_cap"),
        ("01. " + "x" * 4097, "line_field_cap"),
    ],
)
def test_text_partial_ambiguous_and_excluded_evidence(text, reason):
    result = parse(text)
    assert result.status == "incomplete" and reason in errors(result)
    assert result.snapshot.text == text
    if "ambiguous" in reason:
        assert result.snapshot.tracks[0].artist is result.snapshot.tracks[0].title is None


@pytest.mark.parametrize("text", ["", "lyrics with 12:34\nprose at 13:35\nanother 14:36", "#EXTM3U\nset.mp3", "────\nArtist: Example"])
def test_recognition_only_is_never_found_empty(text):
    result = parse(text)
    assert result.status == "incomplete" and result.snapshot.tracks == ()
    assert "recognition_only" in errors(result)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_tracks": 1},
        {"max_lines": 1},
        {"max_characters": 10},
        {"max_bytes": 1, "read": {"bytes_read": 2}},
        {"read": {"truncated": True}},
        {"read": {"status": "incomplete"}},
    ],
)
def test_finite_input_or_track_caps_never_complete(limits):
    result = parse("01. First\n02. Second", **limits)
    assert result.status == "incomplete" and result.snapshot.completeness.state == "incomplete"


def test_unsupported_playlist_and_failed_reads_have_no_fake_snapshot():
    assert parse("set.mp3", "m3u").status == "unsupported"
    assert parse("text", read={"status": "unavailable"}).snapshot is None
    assert parse("text", read={"text": None}).snapshot is None
    identity = SourceIdentity(provider_id="local", native_id="embedded:synthetic:comment")
    outcome = LocalSourceInterpreter().interpret(
        ProviderCandidate(identity=identity), SourceRead(status="found", code="read", scope="x", retrieved_at=datetime.now(UTC)), LoadBudget()
    )
    assert outcome.status == "contract_error"


def test_interpreter_is_explicit_and_deterministic():
    result = parse("01. Artist - Title")
    assert result.snapshot.normalized_payload() == parse("01. Artist - Title").snapshot.normalized_payload()
    assert local_registry().descriptors()[0].id == "local"
    # Importing the parser does not silently register or enable interpretation.
    assert result.snapshot.parser_version == "local-tracklist-v1"


def test_positive_ordered_gaps_and_per_file_number_resets():
    text = parse("03. A - B\n05. C - D")
    assert text.status == "found" and [row.position for row in text.snapshot.tracks] == [3, 5]
    cue = parse('FILE "a.wav" WAVE\nTRACK 03 AUDIO\nTRACK 05 AUDIO\nFILE "b.wav" WAVE\nTRACK 01 AUDIO\nTRACK 03 AUDIO', "cue")
    assert cue.status == "found" and [row.position for row in cue.snapshot.tracks] == [3, 5, 6, 8]
    assert "declared_position:1" in cue.snapshot.tracks[2].evidence


def test_high_water_nonmonotonic_indexes_do_not_requalify_or_raise():
    cue = parse('FILE "a.wav" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:02:00\nTRACK 02 AUDIO\nINDEX 01 00:01:00\nTRACK 03 AUDIO\nINDEX 01 00:01:30', "cue")
    assert cue.status == "incomplete"
    assert [row.timestamp.offset_usability for row in cue.snapshot.tracks] == ["qualified", "unusable", "unusable"]


@pytest.mark.parametrize("limits", [{"read": {"truncated": True}}, {"read": {"status": "incomplete"}}, {"max_characters": 20}, {"max_bytes": 20}])
def test_partial_tail_cannot_become_factual_track(limits):
    result = parse("01. A - B\n02. Artist - Partial", **limits)
    assert result.status == "incomplete"
    assert len(result.snapshot.tracks) == 1 and result.snapshot.tracks[0].title == "B"


def test_tiny_bytes_budget_without_read_count_still_bounds_actual_input():
    result = parse("01. A - B\n02. C - D", max_bytes=1)
    assert result.snapshot.tracks == () and result.snapshot.text == "0"
    cue = parse('FILE "a.wav" WAVE\nTRACK 01 AUDIO\nTITLE "Partial"', "cue", read={"truncated": True})
    assert cue.snapshot.tracks[0].title is None and cue.status == "incomplete"


def test_numbered_prose_and_unclosed_timing_are_not_known_titles():
    prose = parse("01. Visit this site for downloads")
    assert prose.status == "incomplete" and prose.snapshot.tracks == ()
    unclosed = parse("01. [00:03 Artist - Title")
    assert unclosed.snapshot.tracks[0].artist is unclosed.snapshot.tracks[0].title is None
    assert "unclosed_timestamp" in errors(unclosed)


def test_empty_quoted_fields_and_duplicate_set_title():
    result = parse('TITLE "A"\nTITLE "B"\nFILE "a.wav" WAVE\nTRACK 01 AUDIO\nTITLE ""', "cue")
    assert result.status == "incomplete" and result.snapshot.tracks[0].title is None
    assert "duplicate_set_TITLE" in errors(result)


def test_partial_ended_line_and_track_cap_cue_and_bounded_rejections():
    result = parse("01. A - B\n", read={"truncated": True})
    assert len(result.snapshot.tracks) == 1 and result.status == "incomplete"
    cue = parse('FILE "a.wav" WAVE\nTRACK 01 AUDIO\nTRACK 02 AUDIO', "cue", max_tracks=1)
    assert len(cue.snapshot.tracks) == 1 and "track_cap" in errors(cue)
    rejected = parse("noise\n" * 100)
    assert len(rejected.snapshot.completeness.evidence) == 64
    partial = parse("noise\n" * 100, read={"truncated": True})
    assert "partial_input" in errors(partial)
    unicode = parse("éé", max_bytes=3)
    assert unicode.snapshot.text == "é" and unicode.status == "incomplete"


def test_remaining_supported_placeholders_and_oversized_cue_lines():
    placeholders = parse("01. ID\n02. unknown")
    assert placeholders.status == "found" and all(row.title is None for row in placeholders.snapshot.tracks)
    capped = parse("01. A - B\n02. C - D", max_tracks=1)
    assert len(capped.snapshot.tracks) == 1
    cue = parse('FILE "a.wav" WAVE\n\nTRACK 01 AUDIO\n' + "x" * 4097, "cue")
    assert "line_field_cap" in errors(cue) and len(cue.snapshot.tracks) == 1
    padded = parse("01. Artist - Title" + " " * 3000 + "(mashup)")
    assert padded.status == "found" and padded.snapshot.tracks[0].is_mashup
    assert padded.snapshot.tracks[0].title == "Title"


@pytest.mark.parametrize("count", [1024, 4096])
def test_inherited_metadata_expansion_is_typed_bounded_output(count):
    text = 'PERFORMER "' + "é" * 4000 + '"\nFILE "a.wav" WAVE\n' + "\n".join(f"TRACK {index} AUDIO" for index in range(1, count + 1))
    result = parse(text, "cue", max_tracks=count, max_lines=16384)
    assert result.status == "incomplete" and "snapshot_output_cap" in errors(result)
    assert 0 < len(result.snapshot.tracks) < count
    assert len(result.snapshot.model_dump_json().encode()) < 4 * 1024 * 1024
    assert result.snapshot.text == text
