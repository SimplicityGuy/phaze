"""Classification, query normalization and dedup for the 1001TL candidate-set builder (phaze-fq9h.3).

Pure tests -- no database, no network. The heuristics here decide how long a months-long,
externally rate-capped drain takes, so the assertions are written as claims about BUDGET ("this
collapses to one lookup", "this must not merge") rather than about internal structure.

Every filename below is INVENTED. CLAUDE.md forbids committing names, paths or ids from the real
archive; the scene-tag shapes are reproduced faithfully, the artists and events are not real.
"""

from __future__ import annotations

import uuid

import pytest

from phaze.enums.tracklist_candidate import CandidateClass, DuplicateConfidence, meets_confidence
from phaze.services.tracklist_candidates import (
    CandidateSignals,
    _UnionFind,
    classify,
    collapse_ratio,
    detect_embedded_tracklist,
    duration_tolerance,
    durations_match,
    extract_part_index,
    group_unique_sets,
    normalize_query,
    query_has_date,
    set_key,
)


def signals(
    filename: str,
    *,
    duration: float | None = None,
    sha256: str = "",
    bitrate: int | None = None,
    track_number: int | None = None,
    derived_query: str | None = None,
    path: str = "/archive/x.mp3",
    **kwargs: object,
) -> CandidateSignals:
    """Build a CandidateSignals with a distinct id, keeping the tests readable."""
    return CandidateSignals(
        file_id=uuid.uuid4(),
        filename=filename,
        sha256_hash=sha256,
        original_path=path,
        duration_seconds=duration,
        bitrate=bitrate,
        track_number=track_number,
        derived_query=derived_query,
        **kwargs,  # type: ignore[arg-type]
    )


# --- Query normalization ----------------------------------------------------------------------


class TestNormalizeQuery:
    """Two spellings of one set must produce one query, or the drain pays twice for it."""

    def test_scene_variants_of_one_set_normalize_identically(self) -> None:
        """The core promise: differently-tagged releases of one set collapse to one query."""
        variants = [
            "Nova_Halcyon-Live_at_Riverlight_Festival-2024-04-12-WEB-320-CREW.mp3",
            "Nova Halcyon - Live @ Riverlight Festival 2024.04.12 [FLAC].flac",
            "Nova Halcyon - Live @ Riverlight Festival (2024-04-12) (WEB) (24bit).m4a",
        ]
        normalized = {normalize_query(v) for v in variants}
        assert len(normalized) == 1, f"expected one query, got {normalized}"
        assert "2024-04-12" in normalized.pop()

    def test_date_token_survives_punctuation_collapse(self) -> None:
        assert normalize_query("Artist - Event 2024.04.12.mp3").endswith("2024-04-12")

    def test_iso_date_with_no_separators_is_recognized(self) -> None:
        assert "2024-04-12" in normalize_query("artist_event_20240412.mp3")

    def test_impossible_iso_date_is_left_alone(self) -> None:
        """``2024.99.99`` is not a date -- rewriting it would invent one."""
        assert "2024-99-99" not in normalize_query("artist event 2024.99.99.mp3")

    def test_ambiguous_dmy_date_keeps_component_order(self) -> None:
        """``04-05-2024`` is April 5th or May 4th and nothing says which.

        Both spellings normalize consistently with THEMSELVES (so same-convention duplicates still
        merge) but NOT with each other -- the conservative side of the trade, documented in
        ``_canonicalize_dates``. This test exists so the choice cannot be quietly reversed into
        "assume US ordering", which would invent dates and could merge two different nights.
        """
        assert normalize_query("artist event 04-05-2024.mp3") == normalize_query("artist_event_04.05.2024.flac")
        assert normalize_query("artist event 04-05-2024.mp3") != normalize_query("artist event 05-04-2024.mp3")

    def test_impossible_dmy_components_are_left_alone(self) -> None:
        assert "2024-44" not in normalize_query("artist event 44-55-2024.mp3")

    def test_noise_only_brackets_are_dropped_but_meaningful_ones_survive(self) -> None:
        assert normalize_query("Artist - Event [WEB-320]") == normalize_query("Artist - Event")
        assert "warehouse" in normalize_query("Artist - Event (Warehouse Stage)")

    def test_diacritics_fold(self) -> None:
        assert normalize_query("Mónica Solstice - Live") == normalize_query("Monica Solstice - Live")

    def test_leading_track_index_and_domain_prefixes_are_dropped(self) -> None:
        assert normalize_query("03. Artist - Title") == normalize_query("Artist - Title")
        assert normalize_query("www.somesite.com Artist - Title") == normalize_query("Artist - Title")

    def test_empty_and_noise_only_input_yield_empty(self) -> None:
        assert normalize_query(None) == ""
        assert normalize_query("") == ""
        assert normalize_query("[WEB-320].mp3") == ""

    def test_query_has_date(self) -> None:
        assert query_has_date("artist event 2024-04-12")
        assert not query_has_date("artist event")


class TestPartExtraction:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("Artist - Event 2024-04-12 part2.mp3", 2),
            ("Artist - Event 2024-04-12 pt.3.mp3", 3),
            ("Artist - Event CD1.flac", 1),
            ("Artist - Event disc 2.flac", 2),
            ("Artist - Event 1 of 2.mp3", 1),
            ("Artist - Event 2024-04-12.mp3", None),
        ],
    )
    def test_extract_part_index(self, filename: str, expected: int | None) -> None:
        assert extract_part_index(filename) == expected

    def test_part_marker_is_stripped_from_the_query(self) -> None:
        """Parts of one recording must share a query so one lookup answers all of them."""
        assert normalize_query("Artist - Event 2024-04-12 part1.mp3") == normalize_query("Artist - Event 2024-04-12 part2.mp3")


# --- Classification ---------------------------------------------------------------------------


class TestClassify:
    def test_long_duration_is_a_set_whatever_the_filename_says(self) -> None:
        result = classify(signals("01. Some Track (Original Mix).mp3", duration=4200.0, track_number=1))
        assert result.candidate_class is CandidateClass.LIVE_SET
        assert result.confidence > 0.9

    def test_short_duration_is_a_track_even_when_the_filename_says_live(self) -> None:
        """The asymmetry that protects the budget: a 6-minute "live @" file is one track."""
        result = classify(signals("Artist - Live @ Event 2024.04.12.mp3", duration=360.0))
        assert result.candidate_class is CandidateClass.TRACK

    def test_track_number_is_reported_as_evidence_on_the_short_path(self) -> None:
        result = classify(signals("Artist - Title.mp3", duration=360.0, track_number=4))
        assert any("track number 4" in reason for reason in result.reasons)

    def test_no_duration_falls_back_to_filename_markers(self) -> None:
        assert classify(signals("Artist - Live @ Event 2024.04.12.mp3")).candidate_class is CandidateClass.LIVE_SET
        assert classify(signals("04 - Artist - Title (Extended Mix).mp3")).candidate_class is CandidateClass.TRACK

    def test_a_track_number_alone_is_enough_to_call_it_a_track(self) -> None:
        """No duration, no filename marker -- but a tag says it is track 7 of something."""
        result = classify(signals("some name.mp3", track_number=7))
        assert result.candidate_class is CandidateClass.TRACK
        assert any("track number 7" in reason for reason in result.reasons)

    def test_no_duration_and_no_signal_is_unknown_not_a_guess(self) -> None:
        result = classify(signals("audio.mp3"))
        assert result.candidate_class is CandidateClass.UNKNOWN
        assert result.confidence == 0.0
        assert result.reasons == ("no duration and no filename signal",)

    def test_ambiguous_band_leans_set_but_a_track_filename_still_wins(self) -> None:
        """17 minutes: long for a track, short for a set. The filename breaks the tie."""
        assert classify(signals("Artist - Live @ Event.mp3", duration=1020.0)).candidate_class is CandidateClass.LIVE_SET
        assert classify(signals("01. Artist - Title (Original Mix) feat. Other.mp3", duration=1020.0)).candidate_class is CandidateClass.TRACK

    def test_ambiguous_band_with_no_filename_signal_leans_set(self) -> None:
        result = classify(signals("audio.mp3", duration=1020.0))
        assert result.candidate_class is CandidateClass.LIVE_SET
        assert any("ambiguous band" in reason for reason in result.reasons)

    def test_confidence_is_capped(self) -> None:
        loud = classify(signals("Artist b2b Other - Live @ Event Festival mainstage dj set 2024-04-12.mp3"))
        assert loud.confidence <= 0.9


class TestAlreadyTracklisted:
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"has_scraped_tracklist": True}, "scraped"),
            ({"has_cue_companion": True}, "cue"),
            ({"has_embedded_tracklist": True}, "embedded"),
            ({}, None),
        ],
    )
    def test_tracklist_source(self, kwargs: dict[str, bool], expected: str | None) -> None:
        item = signals("Artist - Live @ Event.mp3", **kwargs)
        assert item.tracklist_source == expected
        assert item.already_tracklisted is (expected is not None)


class TestDetectEmbeddedTracklist:
    def test_three_timestamps_in_a_comment_is_a_tracklist(self) -> None:
        tags = {"comment": "00:00 Opener\n05:30 Second\n12:45 Third\n"}
        assert detect_embedded_tracklist(tags) is True

    def test_two_timestamps_is_not(self) -> None:
        assert detect_embedded_tracklist({"comment": "recorded 21:30\nended 23:15"}) is False

    def test_list_valued_tags_are_scanned(self) -> None:
        assert detect_embedded_tracklist({"description": ["00:00 a\n01:00 b\n02:00 c"]}) is True

    def test_non_descriptive_tags_are_ignored(self) -> None:
        assert detect_embedded_tracklist({"artist": "00:00 a\n01:00 b\n02:00 c"}) is False

    def test_empty_and_non_string_values(self) -> None:
        assert detect_embedded_tracklist(None) is False
        assert detect_embedded_tracklist({}) is False
        assert detect_embedded_tracklist({"comment": [1, 2, 3]}) is False


# --- Duration helpers -------------------------------------------------------------------------


class TestDurationMatching:
    def test_tolerance_is_the_larger_of_absolute_and_relative(self) -> None:
        assert duration_tolerance(600.0) == 45.0  # 2% of 10min < 45s
        assert duration_tolerance(3600.0) == pytest.approx(72.0)  # 2% of 60min > 45s

    def test_reencode_jitter_matches_but_a_different_set_does_not(self) -> None:
        assert durations_match(3600.0, 3630.0) is True
        assert durations_match(3600.0, 3900.0) is False

    def test_none_never_matches(self) -> None:
        """An unknown duration is not agreement -- otherwise every untagged file would fuse."""
        assert durations_match(None, 3600.0) is False
        assert durations_match(3600.0, None) is False
        assert durations_match(None, None) is False


class TestSetKey:
    def test_key_is_stable_and_duration_bucketed(self) -> None:
        assert set_key("artist event 2024-04-12", 3600.0) == set_key("artist event 2024-04-12", 3660.0)
        assert set_key("artist event 2024-04-12", 3600.0) != set_key("artist event 2024-04-13", 3600.0)

    def test_missing_duration_gets_its_own_bucket(self) -> None:
        assert set_key("q", None) != set_key("q", 3600.0)

    def test_key_does_not_leak_the_query_text(self) -> None:
        """The key is hashed so it is safe in a log line; the readable text lives in a column."""
        key = set_key("artist event 2024-04-12", 3600.0)
        assert "artist" not in key
        assert len(key) == 64


# --- Grouping ---------------------------------------------------------------------------------


class TestGroupUniqueSets:
    def test_scene_variants_collapse_to_one_lookup(self) -> None:
        corpus = [
            signals("Nova_Halcyon-Live_at_Riverlight_Festival-2024-04-12-WEB-320-CREW.mp3", duration=3600.0, bitrate=320000),
            signals("Nova Halcyon - Live @ Riverlight Festival 2024.04.12 [FLAC].flac", duration=3612.0, bitrate=1000000),
            signals("Nova Halcyon - Live @ Riverlight Festival (2024-04-12) (WEB).m4a", duration=3595.0, bitrate=256000),
        ]
        groups = group_unique_sets(corpus)
        assert len(groups) == 1
        assert groups[0].file_count == 3
        assert groups[0].requests_saved == 2

    def test_canonical_is_the_highest_bitrate_copy(self) -> None:
        best = signals("Artist - Live @ Event 2024-04-12.flac", duration=3600.0, bitrate=1000000)
        corpus = [signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0, bitrate=320000), best]
        assert group_unique_sets(corpus)[0].canonical_file_id == best.file_id

    def test_identical_hashes_collapse_across_different_filenames(self) -> None:
        """A hand-renamed copy has no query in common -- content hash is what saves the request."""
        digest = "a" * 64
        corpus = [
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0, sha256=digest),
            signals("my favourite mix.mp3", duration=3600.0, sha256=digest),
        ]
        groups = group_unique_sets(corpus)
        assert len(groups) == 1
        assert {m.confidence for m in groups[0].members} == {DuplicateConfidence.EXACT}

    def test_two_nights_of_one_residency_do_not_merge(self) -> None:
        """THE false-merge case. Same artist, same event, near-identical length, different date."""
        corpus = [
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0),
            signals("Artist - Live @ Event 2024-04-13.mp3", duration=3605.0),
        ]
        assert len(group_unique_sets(corpus)) == 2

    def test_same_query_but_far_apart_durations_do_not_merge(self) -> None:
        corpus = [
            signals("Artist - Live @ Event.mp3", duration=3600.0),
            signals("Artist - Live @ Event.mp3", duration=7200.0),
        ]
        assert len(group_unique_sets(corpus)) == 2

    def test_unmerged_clusters_that_collide_on_the_coarser_key_bucket_get_distinct_keys(self) -> None:
        """phaze-3t8kc: the cache key's 300s bucket is coarser than the 45s merge tolerance.

        1250s and 1330s are 80s apart -- over tolerance, so grouping correctly keeps them as TWO
        clusters -- but both floor-divide into the same 300s bucket (1250 // 300 == 1330 // 300
        == 4), so the raw ``set_key`` for each would be byte-identical. Confirmed at the
        primitive level below, then asserted that the two UniqueSets group_unique_sets actually
        returns do NOT collide: an undisambiguated collision would let the second set silently
        share the first's cache row and never be looked up.
        """
        query = normalize_query("Artist - Live @ Event.mp3")
        assert set_key(query, 1250.0) == set_key(query, 1330.0), "the raw bucketed keys must collide for this to test anything"

        corpus = [
            signals("Artist - Live @ Event.mp3", duration=1250.0),
            signals("Artist - Live @ Event.mp3", duration=1330.0),
        ]
        groups = group_unique_sets(corpus)
        assert len(groups) == 2
        assert groups[0].key != groups[1].key

    def test_three_way_key_collision_all_get_distinct_keys(self) -> None:
        """The disambiguation must hold for a group larger than two, not just a pair."""
        query = normalize_query("Artist - Live @ Event.mp3")
        assert len({set_key(query, d) for d in (1210.0, 1290.0, 1490.0)}) == 1, "all three must land in the same raw bucket"

        corpus = [
            signals("Artist - Live @ Event.mp3", duration=1210.0),
            signals("Artist - Live @ Event.mp3", duration=1290.0),
            signals("Artist - Live @ Event.mp3", duration=1490.0),
        ]
        groups = group_unique_sets(corpus)
        assert len(groups) == 3
        assert len({g.key for g in groups}) == 3

    def test_key_disambiguation_is_stable_across_input_order(self) -> None:
        """The salted key must not depend on the order candidates were scanned in."""
        a = signals("Artist - Live @ Event.mp3", duration=1250.0)
        b = signals("Artist - Live @ Event.mp3", duration=1330.0)
        keys_forward = sorted(g.key for g in group_unique_sets([a, b]))
        keys_reversed = sorted(g.key for g in group_unique_sets([b, a]))
        assert keys_forward == keys_reversed

    def test_a_lone_set_keeps_its_plain_key_when_nothing_collides(self) -> None:
        """Disambiguation must not perturb the overwhelming common case: no collision at all."""
        corpus = [signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0)]
        query = normalize_query("Artist - Live @ Event 2024-04-12.mp3")
        assert group_unique_sets(corpus)[0].key == set_key(query, 3600.0)

    def test_multipart_recording_is_one_lookup(self) -> None:
        """Parts have wildly different durations by design; the part markers rejoin them."""
        corpus = [
            signals("Artist - Live @ Event 2024-04-12 part1.mp3", duration=3300.0),
            signals("Artist - Live @ Event 2024-04-12 part2.mp3", duration=4100.0),
        ]
        groups = group_unique_sets(corpus)
        assert len(groups) == 1
        assert {m.confidence for m in groups[0].members} == {DuplicateConfidence.EXACT, DuplicateConfidence.MEDIUM}

    def test_repeated_part_indices_refuse_to_merge(self) -> None:
        """Two parted sets sharing a query: merging them would be a false merge, so we do not."""
        corpus = [
            signals("Artist - Live @ Event part1.mp3", duration=3300.0),
            signals("Artist - Live @ Event part1.mp3", duration=4100.0),
            signals("Artist - Live @ Event part2.mp3", duration=4500.0),
        ]
        assert len(group_unique_sets(corpus)) == 3

    def test_duration_less_file_joins_an_unambiguous_query_bucket(self) -> None:
        corpus = [
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0),
            signals("Artist - Live @ Event 2024-04-12.flac"),
        ]
        groups = group_unique_sets(corpus)
        assert len(groups) == 1
        assert DuplicateConfidence.LOW in {m.confidence for m in groups[0].members}

    def test_duration_less_file_stays_separate_when_the_bucket_is_ambiguous(self) -> None:
        """Two real sets share this query; placing the untagged file would be a coin flip."""
        corpus = [
            signals("Artist - Live @ Event.mp3", duration=3600.0),
            signals("Artist - Live @ Event.mp3", duration=7200.0),
            signals("Artist - Live @ Event.flac"),
        ]
        assert len(group_unique_sets(corpus)) == 3

    def test_duration_less_files_alone_still_group_by_query(self) -> None:
        corpus = [signals("Artist - Live @ Event 2024-04-12.mp3"), signals("Artist - Live @ Event 2024-04-12.flac")]
        assert len(group_unique_sets(corpus)) == 1

    def test_files_with_no_query_signal_never_fuse(self) -> None:
        """An empty normalized query is "no signal", not a bucket everything can join."""
        corpus = [signals("[WEB-320].mp3", duration=3600.0), signals("(FLAC).mp3", duration=3600.0)]
        assert len(group_unique_sets(corpus)) == 2

    def test_derived_query_overrides_the_filename(self) -> None:
        """The phaze-fq9h.2 seam: two unrelated-looking filenames, one upstream-derived query."""
        corpus = [
            signals("aaaa.mp3", duration=3600.0, derived_query="Nova Halcyon Riverlight Festival 2024-04-12"),
            signals("bbbb.mp3", duration=3605.0, derived_query="nova_halcyon riverlight festival 2024.04.12"),
        ]
        groups = group_unique_sets(corpus)
        assert len(groups) == 1
        assert groups[0].query_text == "nova halcyon riverlight festival 2024-04-12"
        assert {m.confidence for m in groups[0].members} == {DuplicateConfidence.EXACT, DuplicateConfidence.HIGH}

    def test_ordering_is_deterministic_and_widest_first(self) -> None:
        corpus = [
            signals("Solo Artist - Live @ Other 2024-05-01.mp3", duration=3600.0),
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0),
            signals("Artist - Live @ Event 2024-04-12.flac", duration=3610.0),
        ]
        groups = group_unique_sets(corpus)
        assert [g.file_count for g in groups] == [2, 1]
        assert [g.key for g in groups] == [g.key for g in group_unique_sets(list(reversed(corpus)))]

    def test_empty_corpus(self) -> None:
        assert group_unique_sets([]) == []


class TestMemberConfidence:
    def test_dated_query_earns_high_and_undated_earns_medium(self) -> None:
        dated = group_unique_sets(
            [
                signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0, bitrate=320000),
                signals("Artist - Live @ Event 2024-04-12.flac", duration=3610.0, bitrate=1000000),
            ]
        )[0]
        undated = group_unique_sets(
            [
                signals("Artist - Live @ Event.mp3", duration=3600.0, bitrate=320000),
                signals("Artist - Live @ Event.flac", duration=3610.0, bitrate=1000000),
            ]
        )[0]
        assert {m.confidence for m in dated.members} == {DuplicateConfidence.EXACT, DuplicateConfidence.HIGH}
        assert {m.confidence for m in undated.members} == {DuplicateConfidence.EXACT, DuplicateConfidence.MEDIUM}

    def test_transitive_membership_is_graded_pairwise_not_inherited(self) -> None:
        """A file pulled in through a third party gets the weaker confidence it actually earned."""
        digest = "b" * 64
        corpus = [
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0, bitrate=1000000, sha256=digest),
            signals("unrelated name.mp3", duration=3600.0, bitrate=320000, sha256=digest),
            signals("Artist - Live @ Event 2024-04-12.flac", duration=3605.0, bitrate=128000),
        ]
        group = group_unique_sets(corpus)[0]
        by_id = {m.file_id: m for m in group.members}
        assert by_id[corpus[1].file_id].confidence is DuplicateConfidence.EXACT
        assert by_id[corpus[2].file_id].confidence is DuplicateConfidence.HIGH

    def test_members_at_least_is_the_propagation_gate(self) -> None:
        group = group_unique_sets(
            [
                signals("Artist - Live @ Event.mp3", duration=3600.0, bitrate=1000000),
                signals("Artist - Live @ Event.flac", duration=3610.0, bitrate=320000),
                signals("Artist - Live @ Event.m4a", bitrate=128000),
            ]
        )[0]
        assert len(group.members_at_least(DuplicateConfidence.LOW)) == 3
        assert len(group.members_at_least(DuplicateConfidence.MEDIUM)) == 2
        assert len(group.members_at_least(DuplicateConfidence.EXACT)) == 1

    def test_confidence_ladder_ordering_is_not_lexicographic(self) -> None:
        """``StrEnum`` would order these exact < high < low < medium and invert every gate."""
        assert meets_confidence(DuplicateConfidence.EXACT, DuplicateConfidence.MEDIUM)
        assert not meets_confidence(DuplicateConfidence.LOW, DuplicateConfidence.MEDIUM)


class TestClusterIdentity:
    def test_key_is_stable_when_a_better_copy_appears_later(self) -> None:
        """A fresh higher-bitrate rip must not rotate the cache key and buy a second lookup."""
        first = [
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0, bitrate=320000),
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3605.0, bitrate=320000),
        ]
        before = group_unique_sets(first)[0].key
        after = group_unique_sets([*first, signals("Artist - Live @ Event 2024-04-12.flac", duration=3603.0, bitrate=1000000)])[0].key
        assert before == after

    def test_cluster_query_prefers_the_most_common_spelling(self) -> None:
        digest = "c" * 64
        corpus = [
            signals("Artist - Live @ Event 2024-04-12.mp3", duration=3600.0, sha256=digest),
            signals("Artist - Live @ Event 2024-04-12.flac", duration=3600.0, sha256=digest),
            signals("odd name.mp3", duration=3600.0, sha256=digest),
        ]
        assert group_unique_sets(corpus)[0].query_text == normalize_query("Artist - Live @ Event 2024-04-12.mp3")

    def test_cluster_with_no_query_at_all_keeps_an_empty_query(self) -> None:
        digest = "d" * 64
        corpus = [signals("[WEB-320].mp3", duration=3600.0, sha256=digest), signals("(FLAC).mp3", duration=3600.0, sha256=digest)]
        group = group_unique_sets(corpus)[0]
        assert group.query_text == ""
        assert group.file_count == 2


class TestUnionFind:
    """The transitive-closure primitive behind grouping (A-hash-B, B-query-C => one set)."""

    def test_transitive_chains_collapse_and_deep_paths_compress(self) -> None:
        union = _UnionFind(4)
        union.union(0, 1)
        union.union(2, 3)
        union.union(0, 2)
        assert len({union.find(i) for i in range(4)}) == 1
        assert union.find(3) == union.find(0)

    def test_union_of_an_already_merged_pair_is_a_no_op(self) -> None:
        union = _UnionFind(2)
        union.union(0, 1)
        union.union(1, 0)
        assert union.find(0) == union.find(1)


class TestCollapseRatio:
    def test_ratio(self) -> None:
        assert collapse_ratio(150, 50) == 3.0

    def test_empty_corpus_does_not_divide_by_zero(self) -> None:
        assert collapse_ratio(0, 0) == 0.0


class TestMeasuredCollapseOnASyntheticArchive:
    """A corpus-shaped measurement: the collapse ratio is the drain's schedule, so measure it.

    Synthetic, but built to the shape the bead documents -- scene re-encodes of the same set are
    the common case in this archive, alongside singleton sets and individual tracks.
    """

    @staticmethod
    def _corpus() -> list[CandidateSignals]:
        files: list[CandidateSignals] = []
        for set_index in range(40):
            base = 3000.0 + set_index * 37
            day = 1 + set_index % 28
            stem = f"Artist{set_index} - Live @ Event{set_index % 7} 2024-04-{day:02d}"
            files.append(signals(f"{stem}.mp3", duration=base, bitrate=320000))
            if set_index % 2 == 0:  # a second encoding
                files.append(signals(f"{stem.replace(' - ', '_').replace(' ', '_')}-WEB-FLAC-CREW.flac", duration=base + 9, bitrate=1000000))
            if set_index % 4 == 0:  # a third, byte-identical copy filed elsewhere
                files.append(signals(f"{stem}.mp3", duration=base, bitrate=320000, sha256=f"{set_index:064d}"))
                files.append(signals(f"copy of {set_index}.mp3", duration=base, bitrate=320000, sha256=f"{set_index:064d}"))
        for track_index in range(30):
            files.append(signals(f"{track_index:02d}. Artist - Title (Original Mix).mp3", duration=360.0, track_number=track_index))
        return files

    def test_sets_collapse_and_tracks_are_excluded(self) -> None:
        corpus = self._corpus()
        groups = group_unique_sets(corpus)
        set_groups = [g for g in groups if g.classification.candidate_class is CandidateClass.LIVE_SET]
        track_groups = [g for g in groups if g.classification.candidate_class is CandidateClass.TRACK]

        set_files = sum(g.file_count for g in set_groups)
        # 40 sets: 40 primary files + 20 second encodings + 10 x 2 byte-identical copies = 80.
        assert len(set_groups) == 40, "each distinct set must collapse to exactly one lookup"
        assert set_files == 80
        assert collapse_ratio(set_files, len(set_groups)) == pytest.approx(2.0)
        assert sum(g.file_count for g in track_groups) == 30, "individual tracks are never lookups"

    def test_no_two_distinct_sets_were_merged(self) -> None:
        """The recall win is only worth having if precision holds: 40 sets in, 40 sets out."""
        corpus = self._corpus()
        groups = [g for g in group_unique_sets(corpus) if g.classification.candidate_class is CandidateClass.LIVE_SET]
        assert len({g.key for g in groups}) == len(groups)
