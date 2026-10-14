"""Date and event hints from a file name and path. SYNTHETIC invented names only (CLAUDE.md "No local identifiers").

The labelled set at the bottom reports precision and abstention per pattern on THESE invented names. It is a regression
check on the parsing rules, not a measurement: how often a hint is right on the real archive is unmeasured
(docs/spikes/phaze-fi8rs-recording-facts-for-remote-search.md).
"""

from dataclasses import dataclass
import uuid

import pytest

from phaze.models.metadata import FileMetadata
from phaze.services.remote_lookup import context_from_metadata
from phaze.tracklist_providers.domain import Fact, RecordingContext
from phaze.tracklist_providers.recording_hints import hints_from_name


def test_year_first_full_date_is_a_certain_inferred_date_with_day_precision() -> None:
    for name in ("Example Artist - Example Event - 2024-06-15.mp3", "Example_Artist-2024_06_15.mp3", "Example Artist 2024.06.15.m4a"):
        fact = hints_from_name(name).date
        assert (fact.value, fact.certainty) == ("2024-06-15", "inferred"), name
        assert "precision=day" in fact.evidence
    compact = hints_from_name("Example Artist 20240615.mp3")
    assert compact.date.value == "2024-06-15"
    assert compact.date_pattern == "full_date_compact"


def test_day_first_or_month_first_ambiguous_date_keeps_both_alternatives_and_is_not_collapsed() -> None:
    hints = hints_from_name("Example Artist - Example Event - 03-04-2024.mp3")
    assert hints.date.value == "2024", "reduced to year precision, not one reading"
    assert hints.date.certainty == "inferred"
    assert {"alt=2024-03-04", "alt=2024-04-03", "precision=year", "name:date=03-04-2024"} <= set(hints.date.evidence)
    # A form that reads one way only is not ambiguous: the day cannot be a month, or both parts agree.
    assert hints_from_name("Example Artist 15-06-2024.mp3").date.value == "2024-06-15"
    assert hints_from_name("Example Artist 06.15.2024.mp3").date.value == "2024-06-15"
    assert hints_from_name("Example Artist 05-05-2024.mp3").date.value == "2024-05-05"
    # 31-02 is no calendar date either way: a malformed date is not a year hint, so nothing is derived.
    assert hints_from_name("Example Artist 31-02-2024.mp3").date == Fact()


def test_year_alone_is_year_precision_not_a_day() -> None:
    named = hints_from_name("Example Artist Festival 2019 Set.mp3")
    assert (named.date.value, named.date.certainty, "precision=year" in named.date.evidence) == ("2019", "inferred", True)
    directory = hints_from_name("Example Set.mp3", "/archive/Concerts/2021/Example Artist")
    assert (directory.date.value, directory.date_pattern) == ("2021", "year_directory")
    # Several different years are a series or a title containing a year: no year is chosen.
    assert hints_from_name("Example Artist 2019 vs 2021.mp3").date == Fact()


def test_compact_eight_digit_date_is_not_matched_inside_a_longer_digit_run() -> None:
    for name in ("Example 120240615.mp3", "Example 202406151.mp3", "Example 2024061512.mp3", "Example 9920240615.mp3"):
        assert hints_from_name(name).date == Fact(), name
    assert hints_from_name("Example 20241345.mp3").date == Fact(), "not a valid calendar date"
    assert hints_from_name("Example-20240615-WEB.mp3").date.value == "2024-06-15"


def test_dash_pattern_yields_an_inferred_event_with_the_token_as_evidence() -> None:
    hints = hints_from_name("Example Artist - Example Festival Main Stage - 2024-06-15.mp3")
    assert (hints.event.value, hints.event.certainty, hints.event_pattern) == ("Example Festival Main Stage", "inferred", "dash_pattern")
    assert hints.event.evidence == ("name:dash=Example Festival Main Stage",)
    live = hints_from_name("Example Artist Live at Example Arena 2024.mp3")
    assert (live.event.value, live.event_pattern, live.event.evidence) == ("Example Arena", "live_hint", ("name:live=Example Arena",))
    assert hints_from_name("Example Artist @ Example Club.mp3").event.value == "Example Club"
    # One separator is not the artist - event - date shape, and a date or number is not an event name.
    assert hints_from_name("Example Artist - Example Set.mp3").event == Fact()
    assert hints_from_name("Example Artist - 2024-06-15 - 12.mp3").event == Fact()


def test_album_is_never_used_as_an_event() -> None:
    media_id = uuid.uuid4()
    metadata = FileMetadata(file_id=media_id, artist="Example Artist", album="Example Album Live at Nowhere", year=2020)
    context = context_from_metadata(media_id, metadata, file_name="track.mp3", path="/archive/Example Album")
    assert context.event == Fact()
    assert context_from_metadata(media_id, metadata).event == Fact()


def test_a_name_with_no_hint_yields_no_facts() -> None:
    for name, path in (("Example Set.mp3", None), ("Example Artist - Mix.mp3", "/archive/Misc"), ("", ""), (None, None)):
        hints = hints_from_name(name, path)
        assert (hints.date, hints.event, hints.date_pattern, hints.event_pattern) == (Fact(), Fact(), None, None), name
    media_id = uuid.uuid4()
    assert context_from_metadata(media_id, None, file_name="Example Set.mp3", path="/archive/Misc") == RecordingContext(recording_id=str(media_id))


def test_context_builder_uses_name_hints_but_the_tag_year_wins_a_disagreement() -> None:
    media_id = uuid.uuid4()
    tagged = FileMetadata(file_id=media_id, artist="Example Artist", year=2024)
    agreeing = context_from_metadata(media_id, tagged, file_name="Example - Event - 2024-06-15.mp3")
    assert (agreeing.date.value, agreeing.event.value) == ("2024-06-15", "Event")
    assert {"name:date=2024-06-15", "tag:year"} <= set(agreeing.date.evidence)
    ambiguous = context_from_metadata(media_id, tagged, file_name="Example 03-04-2024.mp3")
    assert ambiguous.date.value == "2024"
    clash = context_from_metadata(media_id, tagged, file_name="Example 2019-06-15.mp3")
    assert (clash.date.value, clash.date.evidence) == ("2024", ("tag:year",))
    assert context_from_metadata(media_id, None, file_name="Example 2019-06-15.mp3").date.value == "2019-06-15"


@dataclass(frozen=True)
class Case:
    pattern: str
    name: str
    path: str = ""
    date: str | None = None  # expected date value, None = should abstain
    event: str | None = None  # expected event value, None = should abstain


# Hand-labelled. `pattern` is the rule the case exercises, including cases built to tempt it into a wrong answer.
LABELLED = (
    Case("full_date_year_first", "Ann Example - Spring Fest - 2024-06-15.mp3", date="2024-06-15", event="Spring Fest"),
    Case("full_date_year_first", "Ann Example 2023_12_31.mp3", date="2023-12-31"),
    Case("full_date_year_first", "Ann Example 2024-13-45.mp3"),
    Case("full_date_year_first", "Ann Example 2024-06-15 and 2025-01-02.mp3"),
    Case("full_date_compact", "Ann Example 20240615.mp3", date="2024-06-15"),
    Case("full_date_compact", "Ann Example 120240615.mp3"),
    Case("full_date_compact", "Ann Example 20240615123.mp3"),
    Case("full_date_compact", "Ann Example 20241399.mp3"),
    Case("full_date_day_or_month_first", "Ann Example 15-06-2024.mp3", date="2024-06-15"),
    Case("full_date_day_or_month_first", "Ann Example 03-04-2024.mp3", date="2024"),
    Case("full_date_day_or_month_first", "Ann Example 12.11.2022.mp3", date="2022"),
    Case("full_date_day_or_month_first", "Ann Example 31-02-2024.mp3"),
    Case("year_in_name", "Ann Example Open Air 2019.mp3", date="2019"),
    Case("year_in_name", "Ann Example 2019 to 2021 Anthology.mp3"),
    Case("year_in_name", "Ann Example Vol 2.mp3"),
    Case("year_directory", "Ann Example Set.mp3", "/lib/2021/Ann", date="2021"),
    Case("year_directory", "Ann Example Set.mp3", "/lib/2020/2021/Ann"),
    Case("year_directory", "Ann Example Set.mp3", "/lib/Misc/Ann"),
    Case("dash_pattern", "Ann Example - Spring Fest - Day 2.mp3", event="Spring Fest"),
    Case("dash_pattern", "Ann Example - Sunset Stage - Part 1.mp3", event="Sunset Stage"),
    Case("dash_pattern", "Ann Example - Set One.mp3"),
    Case("dash_pattern", "Ann Example - 2024-06-15 - Spring Fest.mp3"),
    Case("live_hint", "Ann Example Live at Harbor Hall.mp3", event="Harbor Hall"),
    Case("live_hint", "Ann Example live from Pier Nine - 2024-06-15.mp3", event="Pier Nine"),
    Case("live_hint", "Ann Example @ Corner Club (WEB).mp3", event="Corner Club"),
    Case("live_hint", "Ann Example Live Mix.mp3"),
)


def _score(pattern: str) -> tuple[int, int, int, int]:
    """(emitted, correct, should_abstain, abstained) over the cases labelled for ``pattern``, on the field it produces."""
    emitted = correct = should_abstain = abstained = 0
    is_date = pattern.startswith(("full_date", "year"))
    for case in (c for c in LABELLED if c.pattern == pattern):
        hints = hints_from_name(case.name, case.path)
        fired, expected, got = (hints.date_pattern, case.date, hints.date.value) if is_date else (hints.event_pattern, case.event, hints.event.value)
        if fired == pattern:
            emitted += 1
            correct += expected == got
        if expected is None:
            should_abstain += 1
            abstained += got is None
    return emitted, correct, should_abstain, abstained


EXPECTED_SCORES = {
    "full_date_year_first": (2, 2, 2, 2),
    "full_date_compact": (1, 1, 3, 3),
    "full_date_day_or_month_first": (3, 3, 1, 1),
    "year_in_name": (1, 1, 2, 2),
    "year_directory": (1, 1, 2, 2),
    "dash_pattern": (2, 2, 2, 2),
    "live_hint": (3, 3, 1, 1),
}


@pytest.mark.parametrize("pattern", sorted(EXPECTED_SCORES))
def test_labelled_synthetic_set_precision_and_abstention_per_pattern(pattern: str, capsys: pytest.CaptureFixture[str]) -> None:
    emitted, correct, should_abstain, abstained = _score(pattern)
    with capsys.disabled():
        print(f"\nhint eval [{pattern}] (synthetic): precision {correct}/{emitted}, abstention {abstained}/{should_abstain}")
    assert (emitted, correct, should_abstain, abstained) == EXPECTED_SCORES[pattern]
    assert correct == emitted, "a wrong hint is worse than none: every emitted hint on the labelled set must be right"
    assert abstained == should_abstain
