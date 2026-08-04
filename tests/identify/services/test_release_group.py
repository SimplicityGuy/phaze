"""Tests for the corrected release-group extractor (phaze-5fta.1).

Every filename in this file is INVENTED. It reproduces a filename *shape* observed in the real
archive -- separator style, field order, source tag, re-release marker -- with placeholder
artist/event text, because the repo convention (``CLAUDE.md``, "No local identifiers in tracked
files") forbids committing real archive filenames. The release-group tokens themselves
(``talion``, ``1king``, ``cin_int``, ``hsalive``, ...) are naming-convention vocabulary already
recorded in the phaze-5fta epic, not local identifiers.

The organizing principle: the extractor exists because the epic's corpus measurement used a naive
trailing-token regex that MISTOOK YEARS FOR RELEASE GROUPS, so the rejection tests below are the
load-bearing half of this file, not an afterthought. :func:`test_never_returns_a_year_for_any_year`
and :func:`test_never_returns_scene_vocabulary_as_a_group` sweep whole input families rather than
sampling them, so the guarantee holds for tokens nobody thought to write a case for.
"""

from __future__ import annotations

import pytest

from phaze.services.release_group import (
    QUALITY_TOKENS,
    SOURCE_TOKENS,
    STATUS_TOKENS,
    extract_release_group,
    extract_release_group_detail,
)


# The eight groups the epic's measurement names, plus hsalive (the "-proper-" re-release shape).
KNOWN_GROUPS = ("talion", "1king", "1real", "cin_int", "ffm", "sob", "coin_int", "edc", "hsalive")


# --------------------------------------------------------------------------------------------
# Accepted shapes
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # Fully glued scene tail: the dominant shape.
        ("Artist-Event-04-05-2014-sbd-talion.mp3", "talion"),
        ("Artist-Event-Night-2-04-05-2014-cable-1king.mp3", "1king"),
        # Spaced-out messy tail: whitespace separators, readable only because "sbd" corroborates.
        ("Artist - Event - 03-04-2014 - sbd - talion.mp3", "talion"),
        ("Artist - Event - 12-06-2013 - line - ffm.m4a", "ffm"),
        # Underscore-for-space fields around a glued tail.
        ("Artist_-_Event_-_08-09-2015-sat-1real.mp3", "1real"),
        # Fully underscore-delimited: underscore is a separator like any other.
        ("artist_event_04-05-2014_stream_sob.mp4", "sob"),
        # Underscore-glued "internal" suffix stays part of the group key.
        ("Artist-Event-2016-11-07-web-flac-cin_int.mp3", "cin_int"),
        ("Artist-Event-09-10-2011-fm-coin_int.mp3", "coin_int"),
        # A group whose moniker is also a plausible venue acronym -- fine WITH scene corroboration.
        ("Artist-Event-01-02-2010-sbd-edc.mp3", "edc"),
        # The hsalive "-proper-" re-release shape, in both orders it appears in.
        ("Artist-Event-05-06-2017-sbd-proper-hsalive.mp3", "hsalive"),
        ("Artist-Event-05-06-2017-sbd-hsalive-proper.mp3", "hsalive"),
        # Messy names: semicolons inside the title, and semicolons used AS the separator.
        ("Artist; Other Artist - Event - 03-04-2014 - sbd - talion.mp3", "talion"),
        ("Artist;Event;04-05-2014;cable;1king.mp3", "1king"),
        ("Artist - Event, Night 2 - 04-05-2014 - sbd - talion.flac", "talion"),
        # Case is normalized: the store key must not fork on an uploader's shift key.
        ("Artist-Event-04-05-2014-SBD-TALION.mp3", "talion"),
        ("Artist-Event-04-05-2014-Sbd-Talion.mp3", "talion"),
        # ISO-shaped date instead of the ambiguous one.
        ("Artist-Event-2014-05-04-sbd-talion.mp3", "talion"),
        # Quality tokens stacked between date and group.
        ("Artist-Event-04-05-2014-web-flac-24bit-96khz-talion.mp3", "talion"),
        ("Artist-Event-04-05-2014-web-320kbps-talion.mp3", "talion"),
        # Multi-part markers after the group.
        ("Artist-Event-04-05-2014-sbd-talion-cd2.mp3", "talion"),
        # Spaced separators AND a trailing re-release marker: the popped marker is the evidence.
        ("Artist - Event - 04-05-2014 - sbd - talion - proper.mp3", "talion"),
        # A year immediately before the group, with no source tag at all, spaced separators.
        ("Artist - Event - 2014 - talion.mp3", "talion"),
        # Only a path's final component is examined.
        ("scene/incoming/Artist-Event-04-05-2014-sbd-talion.mp3", "talion"),
        ("scene\\incoming\\Artist-Event-04-05-2014-sbd-talion.mp3", "talion"),
        # No recognized media extension: the name is parsed as-is.
        ("Artist-Event-04-05-2014-sbd-talion", "talion"),
        # Nothing but the tail.
        ("sbd-talion.mp3", "talion"),
        # A dot-delimited scene tail.
        ("Artist.Event.04-05-2014.sbd.talion.mp3", "talion"),
    ],
)
def test_extracts_group_from_real_corpus_shapes(filename: str, expected: str) -> None:
    assert extract_release_group(filename) == expected


@pytest.mark.parametrize("group", KNOWN_GROUPS)
@pytest.mark.parametrize(
    "template",
    [
        "Artist-Event-04-05-2014-sbd-{group}.mp3",
        "Artist-Event-2014-05-04-web-flac-{group}.mp3",
        "Artist - Event - 04-05-2014 - cable - {group}.mp3",
        "Artist_-_Event_-_04-05-2014-sat-{group}.mp3",
        "Artist;Event;04-05-2014;line;{group}.mp3",
        "Artist-Event-04-05-2014-fm-{group}-proper.mp3",
        "Artist-Event-04-05-2014-stream-proper-{group}.mp4",
        "01 - Artist-Event-04-05-2014-web-{group}.mp3",
    ],
)
def test_every_known_group_survives_every_tail_shape(template: str, group: str) -> None:
    """The epic's eight named groups (plus hsalive) x the separator/source shapes they appear in."""
    assert extract_release_group(template.format(group=group)) == group


# --------------------------------------------------------------------------------------------
# The defect this bead exists to prevent: years are not release groups
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "Artist-Event-04-05-2014.mp3",
        "Artist-Event-2014-05-04.mp3",
        "Artist - Event - 04-05-2014.mp3",
        "Artist_-_Event_-_04-05-2014.mp3",
        "Artist;Event;09-10-2009.mp3",
        "Artist-Event-2009.mp3",
        "Artist - Event 2009.flac",
        # Source/quality tags AFTER the date and still no group: the walk must reach the date and
        # stop, not keep going until some title word looks group-shaped.
        "Artist-Event-04-05-2014-sbd.mp3",
        "Artist-Event-04-05-2014-web-flac-320.mp3",
        "Artist-Event-04-05-2014-sbd-proper.mp3",
    ],
)
def test_returns_none_when_the_trailing_field_is_a_date(filename: str) -> None:
    assert extract_release_group(filename) is None


@pytest.mark.parametrize("year", range(1950, 2050))
def test_never_returns_a_year_for_any_year(year: int) -> None:
    """Swept over the whole plausible year range, both date shapes, glued and spaced.

    The naive extractor's failure was bounded by nothing: any 4-digit trailing token became a
    "group". A test that sampled two years would have passed against a year blacklist that happened
    to cover them, so this sweeps every year the corpus could contain.
    """
    for filename in (
        f"Artist-Event-04-05-{year}.mp3",
        f"Artist - Event - 04-05-{year}.mp3",
        f"Artist-Event-{year}-05-04-sbd.mp3",
        f"Artist-Event-{year}.mp3",
    ):
        assert extract_release_group(filename) is None, filename


def test_year_rejection_is_reported_as_numeric() -> None:
    detail = extract_release_group_detail("Artist-Event-04-05-2014.mp3")
    assert (detail.group, detail.raw, detail.reason) == (None, "2014", "numeric")


# --------------------------------------------------------------------------------------------
# The other defect: source and quality tags are not release groups
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("token", sorted(SOURCE_TOKENS | QUALITY_TOKENS | STATUS_TOKENS))
def test_never_returns_scene_vocabulary_as_a_group(token: str) -> None:
    """No vocabulary token is ever returned, in any tail position, in any case."""
    for filename in (
        f"Artist-Event-04-05-2014-{token}.mp3",
        f"Artist-Event-04-05-2014-sbd-{token}.mp3",
        f"Artist - Event - 04-05-2014 - {token}.mp3",
        f"Artist-Event-04-05-2014-{token.upper()}.mp3",
    ):
        assert extract_release_group(filename) is None, filename


@pytest.mark.parametrize("token", sorted(SOURCE_TOKENS | QUALITY_TOKENS | STATUS_TOKENS))
def test_a_vocabulary_token_after_the_group_never_hides_the_group(token: str) -> None:
    """``...-talion-<vocabulary>`` still resolves to ``talion`` -- the ``-proper-`` shape, generalized."""
    assert extract_release_group(f"Artist-Event-04-05-2014-sbd-talion-{token}.mp3") == "talion"


@pytest.mark.parametrize(
    "filename",
    [
        # Numeric-with-unit quality tokens are recognized by shape, not enumeration.
        "Artist-Event-04-05-2014-web-192kbps.mp3",
        "Artist-Event-04-05-2014-web-11025hz.mp3",
        "Artist-Event-04-05-2014-web-1440p.mp3",
        # Multi-part markers describe the file too.
        "Artist-Event-04-05-2014-sbd-cd2.mp3",
        "Artist-Event-04-05-2014-sbd-pt3.mp3",
    ],
)
def test_shape_recognized_noise_is_not_a_group(filename: str) -> None:
    assert extract_release_group(filename) is None


def test_a_tail_of_nothing_but_vocabulary_reports_scene_vocabulary() -> None:
    detail = extract_release_group_detail("Artist-web-flac.mp3")
    assert (detail.group, detail.reason) == (None, "scene_vocabulary")


def test_the_noise_walk_is_bounded_and_stops_on_vocabulary() -> None:
    """Six stacked noise fields exhaust the pop budget; the walk stops rather than digging deeper.

    Without the budget, a tail made entirely of vocabulary-adjacent words would be chewed through
    until some real title word was claimed as a group.
    """
    detail = extract_release_group_detail("Artist-Event-web-flac-320-proper-repack-int.mp3")
    assert (detail.group, detail.reason) == (None, "scene_vocabulary")


def test_a_hyphen_delimited_internal_marker_is_noise_not_part_of_the_group() -> None:
    """``-cin-int`` is the marker as its own field; only the underscore spelling glues to the group."""
    assert extract_release_group("Artist-Event-04-05-2014-sbd-cin-int.mp3") == "cin"


@pytest.mark.parametrize(
    ("filename", "reason"),
    [
        # The nastiest re-entry route for the original defect: an "internal" marker glued straight
        # onto the year must NOT merge into a group-shaped "2014_int" and smuggle the year past the
        # numeric check.
        ("Artist-Event-04-05-2014_int.mp3", "numeric"),
        # Nor onto a source tag.
        ("Artist-Event-04-05-2014-sbd_int.mp3", "numeric"),
    ],
)
def test_the_internal_suffix_only_glues_onto_a_group_shaped_field(filename: str, reason: str) -> None:
    detail = extract_release_group_detail(filename)
    assert (detail.group, detail.reason) == (None, reason)


# --------------------------------------------------------------------------------------------
# Positional evidence: shape alone is never enough
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        # A prose venue acronym separated by whitespace is not a scene tail.
        "Artist - Live @ EDC.mp3",
        "Artist - Live at Some Festival USA.mp3",
        "Artist - Some Long Event Name.mp3",
        # A whitespace-separated trailing word with nothing scene-shaped around it.
        "Artist - Event - Encore.flac",
        # Two space-separated words and nothing else: no field precedes the candidate at all.
        "Artist Encore.flac",
    ],
)
def test_prose_tails_yield_nothing(filename: str) -> None:
    detail = extract_release_group_detail(filename)
    assert (detail.group, detail.reason) == (None, "no_scene_evidence")


def test_a_glued_separator_alone_is_sufficient_evidence() -> None:
    """The scene convention's own delimiter shape, with no source tag and no date present."""
    assert extract_release_group("Artist-Event-talion.mp3") == "talion"


# --------------------------------------------------------------------------------------------
# Degenerate and malformed input -- pure, total, never raises
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "reason"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("scene/incoming/", "empty"),
        ("talion.mp3", "no_tail_field"),
        ("talion", "no_tail_field"),
        # Shape rejections: too short, too long, interior punctuation, non-ASCII.
        ("Artist-Event-04-05-2014-sbd-x.mp3", "unlikely_shape"),
        ("Artist-Event-04-05-2014-sbd-abcdefghijklmnopqrstuvwxyz.mp3", "unlikely_shape"),
        ("Artist-Event-04-05-2014-sbd-a&b.mp3", "unlikely_shape"),
        ("Artist-Event-04-05-2014-sbd-!!!.mp3", "unlikely_shape"),
        ("Artist-Event-04-05-2014-sbd-grüppe.mp3", "unlikely_shape"),
    ],
)
def test_degenerate_input_is_rejected_with_a_reason(filename: str, reason: str) -> None:
    detail = extract_release_group_detail(filename)
    assert detail.group is None
    assert detail.reason == reason


def test_surrounding_punctuation_is_trimmed_from_the_token() -> None:
    """Brackets left over from a messy rename are trimmed at the ENDS; the group still resolves."""
    detail = extract_release_group_detail("Artist-Event-04-05-2014-sbd-(talion).mp3")
    assert (detail.group, detail.raw) == ("talion", "(talion)")


def test_extraction_is_deterministic_and_side_effect_free() -> None:
    """Same input, same answer -- the function reads nothing but its argument."""
    filename = "Artist-Event-04-05-2014-sbd-talion.mp3"
    assert extract_release_group(filename) == extract_release_group(filename) == "talion"


# --------------------------------------------------------------------------------------------
# Invariants over the accepted-shape corpus above
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("group", KNOWN_GROUPS)
def test_a_returned_group_is_always_a_normalized_key(group: str) -> None:
    """Whatever comes back is lowercase, unpadded, and never scene vocabulary or a bare number."""
    extracted = extract_release_group(f"Artist-Event-04-05-2014-SBD-{group.upper()}.mp3")
    assert extracted is not None
    assert extracted == extracted.lower().strip()
    assert extracted not in SOURCE_TOKENS | QUALITY_TOKENS | STATUS_TOKENS
    assert not extracted.isdigit()


def test_detail_reason_is_set_exactly_when_no_group_was_found() -> None:
    found = extract_release_group_detail("Artist-Event-04-05-2014-sbd-talion.mp3")
    assert found.group is not None and found.reason is None
    missing = extract_release_group_detail("Artist-Event-04-05-2014.mp3")
    assert missing.group is None and missing.reason is not None
