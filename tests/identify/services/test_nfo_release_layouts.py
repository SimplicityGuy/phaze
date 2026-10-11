"""Invented release values in source-observed NFO field layouts."""

from datetime import UTC, datetime

import pytest

from phaze.tracklist_providers.domain import LoadBudget, SourceRead
from phaze.tracklist_providers.local_release import RELEASE_PARSER_VERSION, RELEASE_PARSER_VERSIONS, extract_release_metadata


def read(text: str) -> SourceRead:
    return SourceRead(status="found", code="captured", scope="synthetic", text=text, retrieved_at=datetime.now(UTC))


@pytest.mark.parametrize(
    ("line", "field", "value"),
    [
        (" ::   Artist ....: Example Artist                                   ::", "artist", "Example Artist"),
        (" ::: :::  TiTLE ....: A Title. Vol. Two.                             :::  ::", "title", "A Title. Vol. Two."),
        (" `::  PLAYTiME .: 62:03 min                                        ::'", "duration", "62:03 min"),
        ("| o |                       QUALiTY:  VBR Kbps / 44,1kHz                 | o |", "quality", "VBR Kbps / 44,1kHz"),
        (" ▄▄██  ▓███ █   ARTIST..[ Example Artist                      █ ███▓  ██▄▄", "artist", "Example Artist"),
        (" ▄▓██ ▀ ▓█ ▄█   GENRE  .[ House                         █▄ █▓ ▀ ██▓▄", "genre", "House"),
        (":: ARTIST  Example Artist                                            ::", "artist", "Example Artist"),
        (":: TITLE   Live at Example Hall (Evening Set) ::", "title", "Live at Example Hall (Evening Set)"),
        ("      --> Artist: Example Artist <--", "artist", "Example Artist"),
        ("      --> Quality: VBR/44.1kHz/Joint-Stereo <--", "quality", "VBR/44.1kHz/Joint-Stereo"),
        (" Runtime: 01.02.03 h/m/s", "duration", "01.02.03 h/m/s"),
        ("Album: [Example Release]", "album", "[Example Release]"),
        ("██ Album: Example Release██", "album", "Example Release"),
        ("██ Quality: 320kbps/44,1kHz    ▄███▓▓     ▄▄█▀   AB/XYZ", "quality", "320kbps/44,1kHz"),
        ("Title: A : B | C / D - E '", "title", "A : B | C / D - E '"),
        ("Title: A ::", "title", "A ::"),
        ("Title: A <--", "title", "A <--"),
        ("Genre: House           ▐▌▀███▓▓▄", "genre", "House"),
    ],
)
def test_frames_and_explicit_field_grammars_preserve_exact_spans(line: str, field: str, value: str) -> None:
    text = "\ufeff" + line + "\r\n"
    result = extract_release_metadata(read(text), source_format="nfo")
    assert result.completeness.state == "complete"
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.field == field and fact.value == value and fact.original_value == value
    _, _, start, end = fact.evidence[0].split(":")
    assert text[int(start) : int(end)] == value
    assert fact.source_line == 1


@pytest.mark.parametrize("duration", ["01.60.03 h/m/s", "01.02.60 h/m/s", "01.02.03", "01.02.03 minutes"])
def test_ambiguous_dot_duration_is_preserved_without_guessing(duration: str) -> None:
    fact = extract_release_metadata(read(f"Runtime: {duration}"), source_format="nfo").facts[0]
    assert fact.value == duration and fact.normalized_value is None and fact.certainty == "unknown"


def test_duration_date_quality_and_empty_framed_values() -> None:
    text = ":: Date  03/04/2024 ::\n:: Runtime  01.02.03 h/m/s ::\n--> Quality: VBR/44.1kHz/Joint-Stereo <--\n:: Artist:     ::"
    result = extract_release_metadata(read(text), source_format="nfo")
    date, duration, quality, artist = result.facts
    assert date.value == "03/04/2024" and date.normalized_value is None and date.certainty == "unknown"
    assert duration.normalized_value == "3723" and duration.unit == "s"
    assert quality.value == quality.normalized_value == "VBR/44.1kHz/Joint-Stereo"
    assert artist.value is None and artist.certainty == "unknown"


def test_promotional_prose_is_not_a_release_fact() -> None:
    text = "Artist examples include Example Artist.\nGenre names are mentioned in notices.\nVisit our site for music.\nUnknown  Example\nARTIST\n"
    result = extract_release_metadata(read(text), source_format="nfo")
    assert not result.facts and result.completeness.state == "complete"


def test_padded_explicit_columns_are_separate_facts_with_exact_spans() -> None:
    text = "██ GENRE: House              QUALITY: VBR/44,1kHz/Joint Stereo██\r\n"
    result = extract_release_metadata(read(text), source_format="nfo")
    assert [(fact.field, fact.value) for fact in result.facts] == [("genre", "House"), ("quality", "VBR/44,1kHz/Joint Stereo")]
    for fact in result.facts:
        _, _, start, end = fact.evidence[0].split(":")
        assert text[int(start) : int(end)] == fact.original_value == fact.value


def test_columns_with_internal_frames_and_global_fact_budget() -> None:
    text = "██ Title: Example Set▓    Year: 2024██\n"
    result = extract_release_metadata(read(text), source_format="nfo")
    assert [(fact.field, fact.value) for fact in result.facts] == [("title", "Example Set"), ("year", "2024")]
    limited = extract_release_metadata(read(text * 129), source_format="nfo")
    assert len(limited.facts) == 256 and limited.completeness.state == "incomplete"
    assert "limit:facts" in limited.evidence


def test_partial_layout_tail_is_not_promoted_and_old_candidates_remain_reviewable() -> None:
    result = extract_release_metadata(read(":: Artist  Example Artist ::"), source_format="nfo", budget=LoadBudget(max_characters=22))
    assert not result.facts and result.completeness.state == "incomplete"
    assert RELEASE_PARSER_VERSION == "local-release-v3"
    assert {"local-release-v1", "local-release-v2"} < RELEASE_PARSER_VERSIONS
