"""Tests for query derivation (phaze-fq9h.2) -- pure logic, no mocking or DB needed.

Bucket: ``identify`` (alongside ``test_tracklist_matcher.py``, the naive heuristic this module
supersedes for messy scene filenames). Four required test categories per the bead's acceptance
criteria -- scene-group, source-tag, date, parenthetical-radio-show -- each get their own class,
plus a labeled-sample comparison against the pre-existing naive heuristic
(``tracklist_matcher.parse_live_set_filename``) demonstrating derivation wins materially more
often on this corpus's realistic scene-release shapes.
"""

from __future__ import annotations

from datetime import date

from phaze.services.tracklist_matcher import parse_live_set_filename
from phaze.services.tracklist_query import DerivedQuery, derive_query, resolve_ambiguous_date_order


# --------------------------------------------------------------------------------------------
# Scene-group stripping
# --------------------------------------------------------------------------------------------


class TestSceneGroupStripping:
    def test_trailing_uppercase_group_is_stripped_and_reported(self) -> None:
        result = derive_query("Nova Ryn - Nightgrove Festival - 2019-08-10-WEB-FLAC-GRVMSTR.mp3")
        assert result.scene_group == "GRVMSTR"
        assert "GRVMSTR" not in result.query
        assert result.artist == "Nova Ryn"
        assert result.event == "Nightgrove Festival"

    def test_alnum_group_with_digit_is_stripped(self) -> None:
        result = derive_query("Voltrex - Duskfield - 2018.04.15-320-C4.mp3")
        assert result.scene_group == "C4"
        assert result.source_tags == ("320",)
        assert result.artist == "Voltrex"
        assert result.event == "Duskfield"

    def test_group_is_stripped_even_when_source_tags_follow_it_in_the_filename(self) -> None:
        # Convention is DATE-SOURCE-...-SOURCE-GROUP (group last); this asserts the group pop
        # does not stop early and leave trailing source noise behind in the event text.
        result = derive_query("Nova Ryn - Nightgrove - 05-08-2021-SBD-0DAY.mp3")
        assert result.scene_group == "0DAY"
        assert result.source_tags == ("SBD",)
        assert result.event == "Nightgrove"

    def test_all_lowercase_trailing_word_is_not_mistaken_for_a_group(self) -> None:
        # Documented limitation (see _looks_like_scene_group docstring): an all-lowercase trailing
        # token is left as event text rather than risk eating a real word -- exactly the gap
        # phaze-5fta's corpus evidence will close later via resolve_ambiguous_date_order's sibling
        # lookup. This asserts the CONSERVATIVE side of that tradeoff, not a case we get "wrong".
        result = derive_query("Marin Sol - Nightgrove - 2021-06-12-thegroup.mp3")
        assert result.scene_group is None
        assert "thegroup" in (result.event or "").lower()

    def test_no_scene_group_present_is_left_none(self) -> None:
        result = derive_query("nightfox9 - Duskfield - 2018-04-15.mp3")
        assert result.scene_group is None
        assert result.artist == "nightfox9"
        assert result.event == "Duskfield"


class TestSceneGroupPositionalCorroboration:
    """Regression tests for review round 1: a trailing ALL-CAPS word after a plain SPACE is a
    prose event acronym, not a scene-group tag -- only a token glued on with a TIGHT
    hyphen/underscore (no whitespace) counts as scene-tail evidence. See
    ``_pop_trailing_group_and_sources``'s docstring for the full reasoning.
    """

    def test_live_at_event_acronym_is_not_eaten_as_a_group(self) -> None:
        # The exact regression from review: "ZKX" is the event, reached via " @ " (whitespace
        # separator) -- not a scene-group tag.
        result = derive_query("Nova Ryn - Live @ ZKX.mp3")
        assert result.scene_group is None
        assert result.artist == "Nova Ryn"
        assert result.event == "ZKX"
        assert result.query == "Nova Ryn ZKX"

    def test_trailing_country_code_after_a_space_is_not_a_group(self) -> None:
        result = derive_query("Artist - Nova Bay QRS.mp3")
        assert result.scene_group is None
        assert result.event == "Nova Bay QRS"

    def test_trailing_city_code_after_a_space_is_not_a_group(self) -> None:
        result = derive_query("Artist - Nightgrove VNL.mp3")
        assert result.scene_group is None
        assert result.event == "Nightgrove VNL"

    def test_trailing_short_country_code_after_a_space_is_not_a_group(self) -> None:
        result = derive_query("Artist - Vortex Loop KV.mp3")
        assert result.scene_group is None
        assert result.event == "Vortex Loop KV"

    def test_true_positive_scene_group_still_works_after_the_positional_guard(self) -> None:
        # The canonical scene-tail shape (tight hyphens, DATE-SOURCE-SOURCE-GROUP) must keep
        # working -- the fix must not have overcorrected into never detecting a real group.
        result = derive_query("Nova Ryn - Nightgrove Festival - 2019-08-10-WEB-FLAC-GRVMSTR.mp3")
        assert result.scene_group == "GRVMSTR"

    def test_no_bare_punctuation_token_ever_reaches_the_query(self) -> None:
        # Before the fix, "Live @ ZKX" with ZKX wrongly popped as a group left a dangling "@" as
        # the entire event. Assert directly that no token in the final query is pure punctuation.
        for filename in (
            "Nova Ryn - Live @ ZKX.mp3",
            "Artist - Live @ .mp3",
            "Artist - Nova Bay QRS.mp3",
        ):
            result = derive_query(filename)
            for token in result.query.split(" "):
                assert any(ch.isalnum() for ch in token), f"bare-punctuation token {token!r} in query {result.query!r} for {filename!r}"

    def test_ampersand_in_a_real_artist_name_is_not_treated_as_stray_punctuation(self) -> None:
        # The structural-symbol filter that fixes the dangling "@" must stay narrow: "&" is a
        # legitimate part of a real artist name (North & Vale) and must survive.
        result = derive_query("North & Vale - Circuit Session - 2020-03-14.mp3")
        assert result.artist == "North & Vale"
        assert "&" in result.query


# --------------------------------------------------------------------------------------------
# Source-tag stripping
# --------------------------------------------------------------------------------------------


class TestSourceTagStripping:
    def test_multiple_trailing_source_tags_are_all_collected(self) -> None:
        result = derive_query("Marin Sol - Nightgrove - 2021-06-12-AAC-PSYCON.mp3")
        assert result.source_tags == ("AAC",)
        assert result.scene_group == "PSYCON"

    def test_bracketed_source_tag_is_dropped_from_query_and_recorded(self) -> None:
        result = derive_query("Mireille de Haan - Skyline Mainstage [WEB] 2022.mp3")
        assert result.source_tags == ("WEB",)
        assert "WEB" not in result.query
        assert result.event == "Skyline Mainstage"

    def test_source_token_substring_in_a_real_word_is_not_clipped(self) -> None:
        # "Webrunner" legitimately CONTAINS "web" -- must not be treated as a source tag, since
        # source tokens are matched as WHOLE trailing fields only, never substrings.
        result = derive_query("Webrunner - Duskwing Hall - 2020-01-05.mp3")
        assert result.source_tags == ()
        assert result.artist == "Webrunner"

    def test_quality_bitrate_token_is_recognized_as_a_source_tag(self) -> None:
        result = derive_query("Lena Voss - Meridian - 2019-06-15-320.mp3")
        assert result.source_tags == ("320",)
        assert "320" not in result.query


# --------------------------------------------------------------------------------------------
# Date extraction
# --------------------------------------------------------------------------------------------


class TestDateExtraction:
    def test_iso_hyphenated_date_is_extracted_and_unambiguous(self) -> None:
        result = derive_query("Nova Ryn - Nightgrove Festival - 2019-08-10-WEB-FLAC-GRVMSTR.mp3")
        assert result.date == date(2019, 8, 10)
        assert result.year == 2019
        assert result.date_ambiguous is False

    def test_dotted_date_is_extracted(self) -> None:
        result = derive_query("Voltrex - Duskfield - 2018.04.15-320-C4.mp3")
        assert result.date == date(2018, 4, 15)
        assert result.year == 2018

    def test_day_over_12_disambiguates_dd_mm_yyyy_without_a_convention_lookup(self) -> None:
        # 27 can only be a day, so this resolves unambiguously even with no phaze-5fta evidence.
        result = derive_query("Nova Ryn - Nightgrove - 27-08-2021-SBD.mp3")
        assert result.date == date(2021, 8, 27)
        assert result.date_ambiguous is False

    def test_genuinely_ambiguous_date_degrades_to_year_only(self) -> None:
        # Both components <=12: no filename-internal signal disambiguates DD-MM from MM-DD, and
        # with no phaze-5fta convention store to consult (resolve_ambiguous_date_order stubbed to
        # None), this MUST fall back to year-only rather than silently guessing an order.
        result = derive_query("Nova Ryn - Nightgrove - 05-08-2021-SBD-0DAY.mp3")
        assert result.date is None
        assert result.year == 2021
        assert result.date_ambiguous is True
        # The year is still trustworthy enough to help the search text even though the exact date
        # is not.
        assert "2021" in result.query

    def test_bare_year_with_no_full_date_is_still_captured(self) -> None:
        result = derive_query("Mireille de Haan - Skyline Mainstage [WEB] 2022.mp3")
        assert result.date is None
        assert result.year == 2022
        assert result.date_ambiguous is False

    def test_no_date_present_leaves_both_none(self) -> None:
        result = derive_query("Lena Voss - Duskwing Hall.mp3")
        assert result.date is None
        assert result.year is None
        assert result.date_ambiguous is False

    def test_track_number_prefix_is_not_mistaken_for_part_of_a_year(self) -> None:
        # The leading "01" must never bleed into the (correctly separate) 2019 year later in the
        # filename -- this is the "must not eat a year while stripping other noise" requirement.
        result = derive_query("01 - Nova Ryn - Nightgrove Festival - 2019-08-10-WEB-FLAC-GRVMSTR.mp3")
        assert result.year == 2019
        assert result.artist == "Nova Ryn"


class TestResolveAmbiguousDateOrderSeam:
    """The phaze-5fta integration seam itself: always None today, by design (see its docstring)."""

    def test_always_returns_none_today(self) -> None:
        assert resolve_ambiguous_date_order(scene_group="grvmstr", year=2021, first=5, second=8) is None
        assert resolve_ambiguous_date_order(scene_group=None, year=2021, first=5, second=8) is None


# --------------------------------------------------------------------------------------------
# Parenthetical radio-show handling
# --------------------------------------------------------------------------------------------


class TestParentheticalRadioShow:
    def test_radio_essential_mix_parenthetical_is_dropped_and_flagged(self) -> None:
        result = derive_query("North & Vale - Circuit Session (Overnight Radio Essential Mix) 2020-03-14.mp3")
        assert result.radio_show is True
        assert "radio" not in result.query.lower()
        assert "essential mix" not in result.query.lower()
        assert result.artist == "North & Vale"
        assert result.event == "Circuit Session"
        assert result.date == date(2020, 3, 14)

    def test_broadcast_parenthetical_is_dropped_and_flagged(self) -> None:
        result = derive_query("Nova Ryn - Live Broadcast (Radio Show) - 2019-05-01.mp3")
        assert result.radio_show is True
        assert "radio" not in result.query.lower()

    def test_non_radio_parenthetical_is_kept_not_dropped(self) -> None:
        # Only radio/broadcast-shaped bracket content is noise; anything else bracketed may be
        # real event text and must be preserved (unwrapped, not discarded).
        result = derive_query("Nova Ryn - Nightgrove (Duskwing Hall Stage) - 2019-08-10.mp3")
        assert result.radio_show is False
        assert "duskwing hall stage" in result.query.lower()

    def test_bracketed_source_tag_does_not_trip_the_radio_flag(self) -> None:
        result = derive_query("Mireille de Haan - Skyline Mainstage [WEB] 2022.mp3")
        assert result.radio_show is False


# --------------------------------------------------------------------------------------------
# Mojibake repair (acceptance: "repaired before searching")
# --------------------------------------------------------------------------------------------


class TestMojibakeRepair:
    def test_double_encoded_artist_is_repaired_before_the_rest_of_derivation_runs(self) -> None:
        # The exact example named in this bead's acceptance criteria.
        result = derive_query("VÃƒÂ¶rn - Vortex Loop - 2017-04-01-WEB.mp3")
        assert result.artist == "Vörn"
        assert "Ã" not in result.query

    def test_already_clean_unicode_artist_is_left_alone(self) -> None:
        result = derive_query("Vörn - Vortex Loop - 2017-04-01-WEB.mp3")
        assert result.artist == "Vörn"

    def test_mojibake_repair_does_not_disturb_downstream_date_or_group_extraction(self) -> None:
        result = derive_query("VÃƒÂ¶rn - Vortex Loop - 2017-04-01-WEB-GRVMSTR.mp3")
        assert result.artist == "Vörn"
        assert result.date == date(2017, 4, 1)
        assert result.scene_group == "GRVMSTR"
        assert result.source_tags == ("WEB",)


# --------------------------------------------------------------------------------------------
# Structured query shape + graceful fallback
# --------------------------------------------------------------------------------------------


class TestQueryShapeAndFallback:
    def test_query_is_artist_event_year_in_order(self) -> None:
        result = derive_query("Nova Ryn - Nightgrove Festival - 2019-08-10-WEB-FLAC-GRVMSTR.mp3")
        assert result.query == "Nova Ryn Nightgrove Festival 2019"

    def test_returns_a_dataclass_instance(self) -> None:
        assert isinstance(derive_query("Nova Ryn - Nightgrove - 2019-08-10.mp3"), DerivedQuery)

    def test_never_raises_on_pure_noise_input(self) -> None:
        result = derive_query("[WEB]-[FLAC]-GROUPTAG.mp3")
        assert isinstance(result.query, str)

    def test_never_raises_on_empty_stem(self) -> None:
        result = derive_query(".mp3")
        assert isinstance(result.query, str)

    def test_bare_artist_no_event_falls_back_like_the_naive_heuristic(self) -> None:
        result = derive_query("Lena Voss.mp3")
        assert result.artist == "Lena Voss"
        assert result.event is None
        assert result.query == "Lena Voss"

    def test_live_at_convention_is_still_recognized_without_the_naive_heuristics_rigid_suffix(self) -> None:
        # Same "Live @" signal tracklist_matcher.parse_live_set_filename requires, but WITHOUT
        # that heuristic's mandatory trailing ".YYYY.MM.DD" shape.
        result = derive_query("nightfox9 - Live @ Skyline 2019.07.20.flac")
        assert result.artist == "nightfox9"
        assert result.event == "Skyline"
        assert result.date == date(2019, 7, 20)


# --------------------------------------------------------------------------------------------
# Internal-heuristic edge cases (branch coverage for the conservative-by-design guards)
# --------------------------------------------------------------------------------------------


class TestInternalHeuristicEdgeCases:
    def test_empty_bracket_content_is_a_harmless_no_op(self) -> None:
        result = derive_query("Nova Ryn - Nightgrove () - 2019-08-10.mp3")
        assert result.radio_show is False
        assert result.event == "Nightgrove"

    def test_calendar_invalid_iso_date_yields_no_date_but_keeps_the_year(self) -> None:
        # 2020-02-30 doesn't exist; _safe_date must swallow the ValueError rather than raise.
        result = derive_query("Nova Ryn - Nightgrove - 2020-02-30.mp3")
        assert result.date is None
        assert result.year == 2020

    def test_single_character_trailing_token_is_too_short_to_be_a_group(self) -> None:
        result = derive_query("Nova Ryn - Nightgrove - 2019-08-10-X.mp3")
        assert result.scene_group is None
        assert result.event == "Nightgrove X"

    def test_non_alnum_trailing_token_is_never_treated_as_a_group(self) -> None:
        # A TIGHT separator on purpose -- this must fail on the isalnum() guard inside
        # _looks_like_scene_group itself, not on the (separately tested) positional/whitespace
        # guard in _pop_trailing_group_and_sources.
        result = derive_query("Nova Ryn - Nightgrove-D&B!.mp3")
        assert result.scene_group is None
        assert result.event == "Nightgrove D&B!"

    def test_all_noise_stem_falls_back_to_the_raw_filename(self) -> None:
        # Every token in the cleaned stem is filtered out as noise -- _clean_field's fallback
        # candidate goes empty too, so derive_query must fall back to the untouched original.
        result = derive_query("_LIVE_")
        assert result.artist is None
        assert result.event is None
        assert result.query == "_LIVE_"


# --------------------------------------------------------------------------------------------
# Labeled-sample comparison against the naive spike heuristic (the bead's core acceptance bar)
# --------------------------------------------------------------------------------------------

# (filename, expected_artist, expected_event) -- realistic-but-invented scene-release shapes,
# deliberately NOT the one rigid "{Artist} - Live @ {Event} {YYYY.MM.DD}.{ext}" shape the naive
# heuristic (tracklist_matcher.parse_live_set_filename) requires.
_LABELED_SAMPLE: tuple[tuple[str, str, str], ...] = (
    ("01 - Nova Ryn - Nightgrove Festival - 2019-08-10-WEB-FLAC-GRVMSTR.mp3", "Nova Ryn", "Nightgrove Festival"),
    ("Voltrex - Duskfield - 2018.04.15-320-C4.mp3", "Voltrex", "Duskfield"),
    ("Marin Sol - Nightgrove - 2021-06-12-AAC-PSYCON.mp3", "Marin Sol", "Nightgrove"),
    ("Mireille de Haan - Skyline Mainstage [WEB] 2022.mp3", "Mireille de Haan", "Skyline Mainstage"),
    ("North & Vale - Circuit Session (Overnight Radio Essential Mix) 2020-03-14.mp3", "North & Vale", "Circuit Session"),
    ("Nova Ryn - Nightgrove - 27-08-2021-SBD-0DAY.mp3", "Nova Ryn", "Nightgrove"),
    ("Lena Voss - Meridian - 2019-06-15-320.mp3", "Lena Voss", "Meridian"),
    ("VÃƒÂ¶rn - Vortex Loop - 2017-04-01-WEB-GRVMSTR.mp3", "Vörn", "Vortex Loop"),
    ("02 - Adrian Vale - Nightgrove - 2020-07-19-CDR-TSP.mp3", "Adrian Vale", "Nightgrove"),
    ("Nova Ryn - Duskwing Hall (Radio Broadcast) - 2018-11-02.mp3", "Nova Ryn", "Duskwing Hall"),
)


def _naive_query(filename: str) -> str | None:
    """Mirror the pre-existing naive heuristic's query-building shape from tasks/tracklist.py."""
    parsed = parse_live_set_filename(filename)
    if parsed is None:
        return None
    artist, event, _file_date = parsed
    return f"{artist} {event}"


class TestBeatsNaiveHeuristicOnLabeledSample:
    def test_derivation_gets_artist_and_event_right_materially_more_often(self) -> None:
        derived_correct = sum(
            1
            for filename, artist, event in _LABELED_SAMPLE
            if (result := derive_query(filename)) and result.artist == artist and result.event == event
        )
        naive_correct = sum(1 for filename, _artist, _event in _LABELED_SAMPLE if parse_live_set_filename(filename) is not None)

        assert derived_correct == len(_LABELED_SAMPLE), (
            f"expected every labeled case to derive correctly, got {derived_correct}/{len(_LABELED_SAMPLE)}"
        )
        # The naive heuristic requires the single rigid "Live @ ... YYYY.MM.DD" shape, which NONE
        # of these realistic scene releases match -- it should get every one of them wrong.
        assert naive_correct == 0
        assert derived_correct > naive_correct

    def test_naive_heuristic_query_is_never_better_than_derived_on_this_sample(self) -> None:
        for filename, artist, event in _LABELED_SAMPLE:
            derived = derive_query(filename)
            naive = _naive_query(filename)
            assert naive is None
            assert derived.query.startswith(artist)
            assert event in derived.query
