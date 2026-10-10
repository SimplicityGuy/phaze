"""Synthetic notes: conservative facts, exact spans and bounded uncertainty."""

from datetime import UTC, datetime

import pytest

from phaze.tracklist_providers.domain import LoadBudget, SourceRead
from phaze.tracklist_providers.local_release import extract_release_metadata


def read(text: str | None, **kwargs: object) -> SourceRead:
    return SourceRead.model_validate(
        {"status": "found", "code": "captured", "scope": "synthetic", "text": text, "retrieved_at": datetime.now(UTC), **kwargs}
    )


def test_all_labeled_fields_original_spans_and_unicode() -> None:
    labels = [
        "Artist",
        "Title",
        "Album",
        "Event",
        "Date",
        "Year",
        "Venue",
        "Location",
        "Label",
        "Catalog Number",
        "Genre",
        "Source",
        "Ripper",
        "Encoder",
        "Quality",
        "Bitrate",
        "Duration",
        "Set Type",
    ]
    values = [
        "Example Artist",
        "Concert",
        "Release",
        "Festival",
        "2024-02-29",
        "2024",
        "Stage",
        "München",
        "Example Records",
        "CAT-01",
        "HOUSE",
        "FM",
        "Example",
        "Encoder",
        "VBR",
        "320 kbps",
        "01:02:03",
        "live",
    ]
    text = "\ufeff" + "\r\n".join(f"{label}: {value}" for label, value in zip(labels, values, strict=True))
    content = read(text, encoding="utf-8-sig")
    result = extract_release_metadata(content, source_format=".TXT")
    assert len(result.facts) == 18 and result.completeness.state == "complete"
    for index, fact in enumerate(result.facts):
        assert fact.original_value == values[index] and fact.source_line == index + 1
        _, _, start, end = fact.evidence[0].split(":")
        assert text[int(start) : int(end)] == fact.original_value
    assert result.facts[15].normalized_value == "320000" and result.facts[15].unit == "bit/s"
    assert result.facts[16].normalized_value == "3723" and result.facts[16].unit == "s"
    assert content.text == text


@pytest.mark.parametrize(
    ("field", "value", "normalized", "unit", "certainty"),
    [
        ("date", "2024-02-29", "2024-02-29", None, "known"),
        ("date", "2023-02-29", None, None, "unknown"),
        ("date", "03/04/2024", None, None, "unknown"),
        ("date", "2024", "2024", "year", "inferred"),
        ("date", "2024-03", "2024-03", "month", "inferred"),
        ("date", "2024-13", None, None, "unknown"),
        ("date", "0000", None, None, "unknown"),
        ("year", "0000", None, None, "unknown"),
        ("year", "2024", "2024", "year", "known"),
        ("bitrate", "1.5 Mbit/s", "1500000", "bit/s", "known"),
        ("bitrate", "128 bit/s", "128", "bit/s", "known"),
        ("bitrate", "0 kbps", None, None, "unknown"),
        ("bitrate", "320", None, None, "unknown"),
        ("bitrate", "VBR 320 kbps", None, None, "unknown"),
        ("bitrate", "9" * 100 + " kbps", None, None, "unknown"),
        ("duration", "62:03", "3723", "s", "known"),
        ("duration", "01:62:03", None, None, "unknown"),
        ("duration", "12:60", None, None, "unknown"),
        ("duration", "1.500 seconds", "1.5", "s", "known"),
        ("duration", "90", None, None, "unknown"),
    ],
)
def test_conservative_normalization(field: str, value: str, normalized: str | None, unit: str | None, certainty: str) -> None:
    fact = extract_release_metadata(read(f"{field}: {value}"), source_format="nfo").facts[0]
    assert (fact.original_value, fact.normalized_value, fact.unit, fact.certainty) == (value, normalized, unit, certainty)


def test_conflicts_retained_and_equivalent_units_agree() -> None:
    result = extract_release_metadata(read("Label: First\nLabel: Second\nBitrate: 320 kbps\nBit Rate: 320000 bps\nArtist:\n"), source_format="txt")
    assert [fact.certainty for fact in result.facts] == ["conflicting", "conflicting", "known", "known", "unknown"]
    assert [fact.value for fact in result.facts[:2]] == ["First", "Second"]
    assert result.facts[-1].value is None


def test_cue_headers_only_with_quotes_and_source_lines() -> None:
    text = 'REM DATE 2024-03\r\nPERFORMER "Example Artist"\r\nTITLE "Set title"\r\nCATALOG CAT-01\r\nFILE "part.mp3" MP3\r\nTRACK 01 AUDIO\r\nTITLE "Track title"\r\nPERFORMER "Track artist"\r\nREM DATE 2001\r\nFILE "part-two.mp3" MP3\r\nREM GENRE House\r\n'
    result = extract_release_metadata(read(text), source_format="cue")
    assert [fact.field for fact in result.facts] == ["date", "artist", "title", "catalog"]
    assert result.facts[1].original_value == '"Example Artist"' and result.facts[1].value == "Example Artist"
    assert result.evidence == ("line:5:unrecognized",)
    assert result.facts[0].certainty == "inferred"


def test_unrecognized_adverts_unknown_keys_and_empty_source_preserve_text() -> None:
    text = "*** release notes ***\nhttps://example.invalid\nWebsite: Example\nOddkey: Meaningful\nProse without a supported grammar\n"
    content = read(text, encoding="cp437")
    result = extract_release_metadata(content, source_format="nfo")
    assert result.facts == () and len(result.evidence) == 5 and content.text == text
    assert "line:3:unknown-field" in result.evidence
    assert extract_release_metadata(read(""), source_format="txt").completeness.evidence


@pytest.mark.parametrize("kwargs", [{"truncated": True}, {"status": "incomplete"}])
def test_partial_tail_cannot_be_certain(kwargs: dict) -> None:
    result = extract_release_metadata(read("Artist: Full\nLabel: Par", **kwargs), source_format="txt")
    assert [fact.value for fact in result.facts] == ["Full"]
    assert result.completeness.state == "incomplete" and "line:2:partial" in result.evidence
    terminated = extract_release_metadata(read("Label: Complete\n", **kwargs), source_format="txt")
    assert terminated.facts[0].value == "Complete" and terminated.completeness.state == "incomplete"


def test_limits_and_failure_states() -> None:
    assert not extract_release_metadata(read(None), source_format="txt").facts
    assert not extract_release_metadata(read("Artist: Full", status="retry"), source_format="txt").facts
    assert extract_release_metadata(read("Artist: Full"), source_format="m3u").completeness.state == "incomplete"
    line = extract_release_metadata(read("Artist: Full\nLabel: More\n"), source_format="txt", budget=LoadBudget(max_lines=1))
    assert line.evidence == ("limit:lines",)
    chars = extract_release_metadata(read("Artist: Full\nLabel: More"), source_format="txt", budget=LoadBudget(max_characters=15))
    assert chars.evidence == ("limit:characters", "line:2:partial")
    byte = extract_release_metadata(read("Artist: 🎵🎵"), source_format="txt", budget=LoadBudget(max_bytes=10))
    assert byte.evidence == ("limit:bytes", "line:1:partial")
    oversized = extract_release_metadata(read("Artist: " + "a" * 4097 + "\n"), source_format="txt")
    assert oversized.evidence == ("line:1:oversized",)
    facts = extract_release_metadata(read("Label: First\n" * 257), source_format="txt")
    assert len(facts.facts) == 256 and facts.evidence == ("limit:facts",)
    evidence = extract_release_metadata(read("unknown\n" * 70), source_format="txt")
    assert len(evidence.evidence) == 64 and evidence.evidence[-1] == "limit:evidence"
    assert evidence.completeness.state == "incomplete"


@pytest.mark.parametrize("directive", ["REM\tDATE", "rem   date", "ReM DATE"])
def test_cue_rem_whitespace(directive: str) -> None:
    fact = extract_release_metadata(read(f'{directive} "2024-02-29"'), source_format="cue").facts[0]
    assert fact.field == "date" and fact.normalized_value == "2024-02-29" and fact.certainty == "known"


@pytest.mark.parametrize("value", ['"Unterminated', '"Closed" extra', 'Bare "quoted" token'])
def test_malformed_cue_quotes_remain_uncertain(value: str) -> None:
    fact = extract_release_metadata(read(f"TITLE {value}"), source_format="cue").facts[0]
    assert fact.original_value == value and fact.value == value and fact.normalized_value is None
    assert fact.certainty == "unknown" and "syntax:malformed-quote" in fact.evidence


def test_bounded_revision_is_not_text_truncation_and_source_bytes_enforce_budget() -> None:
    bounded = extract_release_metadata(read("Artist: Full\nLabel: Complete", revision_scope="bounded"), source_format="txt")
    assert [fact.value for fact in bounded.facts] == ["Full", "Complete"]
    assert bounded.completeness.state == "complete" and bounded.evidence == ("revision:bounded",)
    encoded = extract_release_metadata(read("Artist: Full\n", encoding="utf-16", bytes_read=28), source_format="txt", budget=LoadBudget(max_bytes=20))
    assert encoded.completeness.state == "incomplete" and encoded.evidence == ("limit:source-bytes",)
    assert encoded.facts[0].value == "Full"
