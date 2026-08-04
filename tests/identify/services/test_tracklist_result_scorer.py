"""Tests for the 1001Tracklists result scorer (phaze-fq9h.6).

The acceptance-criteria tests run against the SIX REAL search-result captures in
``tests/identify/fixtures/tracklist_search/`` -- 180 real rows, 6 host requests, documented in
that directory's README. That is deliberate and it is the point of the bead: the scorer's job is
to beat a plausible-but-wrong top row that the site itself ranked first, and a hand-written decoy
is one the scorer beats by construction, which would make these tests look like they guard
something they do not.

Synthetic rows appear only where a fixture cannot supply the shape (a malformed date string, an
empty result list) and are labelled as such.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re

from bs4 import BeautifulSoup
import pytest

from phaze.enums.tracklist_candidate import LookupOutcome
from phaze.services.tracklist_query import derive_query
from phaze.services.tracklist_result_scorer import (
    MIN_SELECTION_MARGIN,
    SELECTION_THRESHOLD,
    Disqualifier,
    score_results,
    select_result,
)
from phaze.services.tracklist_scraper import TracklistScraper, TracklistSearchResult


FIXTURES = Path(__file__).parent.parent / "fixtures" / "tracklist_search"

# The two sets whose DETAIL pages phaze-fq9h.1 captured, so the ids asserted here are anchored to
# pages that exist in this repo rather than to ids only the scorer's author ever saw.
ANCHOR_ID = "25fhn7c9"  # Sven Väth @ Time Warp, Maimarkthalle Mannheim, 2024-10-25
BBC_ID = "19h6nw7t"  # Sven Väth @ BBC Radio 1 Dance Presents (Time Warp, ...), 2024-10-12

# A scene-style filename for the anchor set. Invented text describing a public event -- it is not
# an archive filename and carries no local identifier (see CLAUDE.md's conventions).
ANCHOR_FILENAME = "Sven_Vath-Live_At_Time_Warp_Mannheim-2024-10-25-WEB-FLAC-GRVMSTR.mp3"


def load_fixture(slug: str) -> list[TracklistSearchResult]:
    """Parse a captured search page through the real scraper parser."""
    html = (FIXTURES / f"{slug}.html").read_text(encoding="utf-8")
    return TracklistScraper()._parse_search_results(html)


def ranked_ids(slug: str, filename: str) -> list[str]:
    return [scored.result.external_id for scored in score_results(derive_query(filename), load_fixture(slug))]


ALL_SLUGS = (
    "sven-vath-time-warp-2024",
    "sven-vath-time-warp",
    "carl-cox-space-ibiza-2016",
    "amelie-lens-awakenings",
    "time-warp-2024",
    "no-such-set",
)


class TestFixtureProvenance:
    """The captures are the evidence base; assert they are what the README says they are."""

    @pytest.mark.parametrize("slug", ALL_SLUGS)
    def test_every_capture_parses_to_thirty_rows(self, slug: str) -> None:
        assert len(load_fixture(slug)) == 30

    @pytest.mark.parametrize("slug", ALL_SLUGS)
    def test_sidecar_records_provenance(self, slug: str) -> None:
        meta = json.loads((FIXTURES / f"{slug}.json").read_text(encoding="utf-8"))
        assert meta["bead"] == "phaze-fq9h.6"
        assert meta["status"] == 200
        assert meta["query"]
        assert meta["why"]

    @pytest.mark.parametrize("slug", ALL_SLUGS)
    def test_site_orders_rows_by_date_within_blocks_not_by_match_quality(self, slug: str) -> None:
        """The premise of the whole module: 'top row' means 'newest', not 'best'.

        Measured precisely rather than asserted loosely. Ordering is not globally descending: each
        page decomposes into at most TWO contiguous date-descending runs (four of the six captures
        have a single inversion, where a second, lower-relevance block begins; the other two are
        descending throughout). What matters for the scorer is that within a block the order is
        purely chronological -- there is no relevance component at all -- so the first row is the
        most recent keyword match, not the best one.
        """
        dates = [r.date for r in load_fixture(slug)]
        assert all(d is not None for d in dates)
        inversions = [i for i in range(1, len(dates)) if dates[i] > dates[i - 1]]  # type: ignore[operator]
        assert len(inversions) <= 1, "ordering is no longer block-chronological -- re-justify the scorer's premise"
        start = 0
        for boundary in [*inversions, len(dates)]:
            block = dates[start:boundary]
            assert block == sorted(block, reverse=True)  # type: ignore[type-var]
            start = boundary


class TestSearchRowSelectorAudit:
    """Guards the stale-selector findings recorded in the fixture README.

    phaze-fq9h.4 found every per-track DETAIL selector matching zero nodes (phaze-2akf). The search
    selectors had never been audited; one of them was equally dead. These tests are what stop that
    recurring silently, because a dead field degrades into a plausible ``None`` rather than an error.
    """

    def test_row_and_link_selectors_match_every_row(self) -> None:
        total = 0
        for slug in ALL_SLUGS:
            soup = BeautifulSoup((FIXTURES / f"{slug}.html").read_text(encoding="utf-8"), "lxml")
            rows = soup.select(TracklistScraper._SEARCH_ITEM_SELECTOR)
            assert len(rows) == 30
            for row in rows:
                assert row.select_one(TracklistScraper._SEARCH_RESULT_LINK_SELECTOR) is not None
                total += 1
        assert total == 180

    def test_every_row_yields_a_date_from_both_independent_sources_which_agree(self) -> None:
        """The regression guard for the dead ``_HREF_DATE_PATTERN``.

        As written before this bead the pattern ended ``(?:[/?#]|$)`` while live hrefs end
        ``.html``, so it matched 0 of these 180 rows and every ``TracklistSearchResult.date`` was
        None. Asserting BOTH sources resolve AND agree catches a future drift in either one,
        instead of letting the fallback silently paper over a broken primary.
        """
        checked = 0
        for slug in ALL_SLUGS:
            soup = BeautifulSoup((FIXTURES / f"{slug}.html").read_text(encoding="utf-8"), "lxml")
            for row in soup.select(TracklistScraper._SEARCH_ITEM_SELECTOR):
                link = row.select_one(TracklistScraper._SEARCH_RESULT_LINK_SELECTOR)
                assert link is not None
                from_row = TracklistScraper._extract_row_date(row)
                from_href = TracklistScraper._extract_date_from_href(str(link.get("href", "")))
                assert from_row is not None, "row date cell selector is stale"
                assert from_href is not None, "href date pattern is stale"
                assert from_row == from_href
                assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_row)
                checked += 1
        assert checked == 180

    def test_artist_separator_is_live_but_absent_on_non_set_rows(self) -> None:
        """148 of 180 rows carry ``" @ "``; the other 32 are aftermovie/promo videos with no artist."""
        with_artist = sum(1 for slug in ALL_SLUGS for r in load_fixture(slug) if r.artist)
        assert with_artist == 148
        without = [r for slug in ALL_SLUGS for r in load_fixture(slug) if not r.artist]
        assert len(without) == 32
        assert all(r.event is None for r in without)

    def test_event_is_extracted_for_set_rows(self) -> None:
        anchor = next(r for r in load_fixture("sven-vath-time-warp-2024") if r.external_id == ANCHOR_ID)
        assert anchor.artist == "Sven Väth"
        assert anchor.event == "Time Warp, Maimarkthalle Mannheim, Germany"
        assert anchor.date == "2024-10-25"


class TestCorrectResultOutranksDecoys:
    """Acceptance: 'for a filename whose true set exists, the correct result outranks decoys'."""

    def test_anchor_set_is_selected_from_its_own_query(self) -> None:
        selection = select_result(derive_query(ANCHOR_FILENAME), load_fixture("sven-vath-time-warp-2024"))
        assert selection.selected is not None
        assert selection.selected.result.external_id == ANCHOR_ID
        assert selection.rejection is None
        assert selection.confidence >= SELECTION_THRESHOLD

    def test_correct_row_beats_the_wrong_top_row_the_site_ranked_first(self) -> None:
        """THE WRONG-MATCH CASE, from a real capture -- not a planted decoy.

        Searching ``Time Warp 2024`` puts ``Miss Monique @ MiMo Radio 009 (Floor 1, Time Warp,
        Maimarkthalle Mannheim)`` (2024-11-05) at rank 1 by the site's own date-descending order.
        Same festival, same venue, same year; wrong artist. The correct row sits at rank 4. Taking
        the top row -- what the spike did -- renders that wrong set.
        """
        results = load_fixture("time-warp-2024")
        assert results[0].external_id != ANCHOR_ID, "fixture drifted: the top row is no longer the decoy"
        assert [r.external_id for r in results].index(ANCHOR_ID) == 3

        ranked = score_results(derive_query(ANCHOR_FILENAME), results)
        assert ranked[0].result.external_id == ANCHOR_ID
        decoy = next(s for s in ranked if s.result.external_id == results[0].external_id)
        assert ranked[0].confidence >= SELECTION_THRESHOLD > decoy.confidence
        assert ranked[0].confidence - decoy.confidence >= 40

    def test_near_identical_sibling_event_does_not_win(self) -> None:
        """The two captured DETAIL pages are the same artist at overlapping events, 13 days apart.

        ``Time Warp`` vs ``BBC Radio 1 Dance Presents (Time Warp, ...)`` -- one event string
        contains the other, so artist and event similarity cannot separate them. Only the date can.
        """
        ranked = score_results(derive_query(ANCHOR_FILENAME), load_fixture("sven-vath-time-warp-2024"))
        by_id = {s.result.external_id: s for s in ranked}
        assert by_id[ANCHOR_ID].confidence > by_id[BBC_ID].confidence
        assert by_id[BBC_ID].confidence < SELECTION_THRESHOLD

    def test_exact_date_match_wins_over_a_near_dated_broadcast(self) -> None:
        """Carl Cox residency: the correct row leads a 3-day-later broadcast by only 4 points.

        The date weight is 0.2 and an exact date scores 100 against a 3-day miss's 80, so being
        right about the day is worth 4 points -- under a bare margin rule the scorer refused a row
        whose date matched exactly. The uniqueness exemption is what makes this selectable.
        """
        selection = select_result(derive_query("Carl_Cox-Live_At_Space_Ibiza-2016-09-13-WEB-FLAC-GRP.mp3"), load_fixture("carl-cox-space-ibiza-2016"))
        assert selection.selected is not None
        assert selection.selected.row_date == date(2016, 9, 13)
        assert selection.runner_up is not None
        assert selection.selected.confidence - selection.runner_up.confidence < MIN_SELECTION_MARGIN

    def test_second_artist_corroborates_the_shape(self) -> None:
        """Awakenings, different artist. The winner is dated one day off -- a real uploader habit
        for a weekend festival -- which the date-proximity band absorbs while the year gate still
        rejects the 2020 and 2022 editions."""
        selection = select_result(
            derive_query("Amelie_Lens-Live_At_Awakenings_Festival-2019-06-29-WEB-FLAC-GRP.mp3"), load_fixture("amelie-lens-awakenings")
        )
        assert selection.selected is not None
        assert selection.selected.row_date == date(2019, 6, 30)
        assert selection.selected.result.artist == "Amelie Lens"
        rejected_years = {s.row_year for s in selection.ranked if Disqualifier.YEAR_MISMATCH in s.disqualifiers}
        assert {2020, 2022} <= rejected_years


class TestBelowThresholdVerdict:
    """Acceptance: 'a filename with no real match yields a below-threshold verdict (no wrong-set render)'."""

    def test_unmatchable_file_is_rejected_despite_thirty_returned_rows(self) -> None:
        """A query for a set that does not exist still returns 30 rows, topped by an aftermovie video.

        There is no empty-page signal to detect -- that is the whole difficulty. Rendering the top
        row here produces a page with no tracklist on it, which is exactly the spike's "wrong,
        empty set".
        """
        results = load_fixture("no-such-set")
        assert len(results) == 30
        assert results[0].artist is None, "fixture drifted: the top row is no longer the artist-less video"

        selection = select_result(derive_query("Zzyzx_Quorum-Live_At_Nonesuch_Festival-2019-08-11-WEB-MP3-XYZ.mp3"), results)
        assert selection.selected is None
        assert selection.confidence == 0
        assert selection.rejection is LookupOutcome.NOT_FOUND
        assert selection.rejection.is_definitive_negative
        assert max(s.confidence for s in selection.ranked) < SELECTION_THRESHOLD

    def test_artist_less_video_rows_are_disqualified_outright(self) -> None:
        ranked = score_results(derive_query("Zzyzx_Quorum-Live_At_Nonesuch_Festival-2019-08-11-WEB-MP3-XYZ.mp3"), load_fixture("no-such-set"))
        videos = [s for s in ranked if s.result.artist is None]
        assert videos
        assert all(Disqualifier.NO_ARTIST in s.disqualifiers for s in videos)
        assert all(not s.is_selectable for s in videos)

    def test_empty_result_list_is_a_definitive_negative(self) -> None:
        selection = select_result(derive_query(ANCHOR_FILENAME), [])
        assert selection.selected is None
        assert selection.rejection is LookupOutcome.NOT_FOUND
        assert selection.ranked == ()
        assert "no result rows" in selection.reason


class TestYearGate:
    """The recurring-festival guard. Score alone does not separate a festival's editions."""

    def test_wrong_year_rows_score_above_threshold_yet_are_disqualified(self) -> None:
        """The measurement that justifies a hard gate rather than a penalty.

        Same-artist, same-event rows from other years score at or above ``SELECTION_THRESHOLD``.
        Nothing about the score rejects them; only the year gate does.
        """
        ranked = score_results(derive_query(ANCHOR_FILENAME), load_fixture("sven-vath-time-warp-2024"))
        wrong_year = [s for s in ranked if Disqualifier.YEAR_MISMATCH in s.disqualifiers]
        assert wrong_year
        assert max(s.confidence for s in wrong_year) >= SELECTION_THRESHOLD
        assert all(not s.is_selectable for s in wrong_year)

    def test_year_only_file_would_otherwise_pick_a_two_thousand_two_set(self) -> None:
        """A 2024 file with no resolved day scores the 2002 and 2003 rows ABOVE the correct one.

        Their event text (``Time Warp, Germany``) matches the derived event more cleanly than the
        correct row's longer venue string, and with no date term to separate them the year gate is
        the only thing standing between this file and a 22-year-old set.
        """
        ranked = score_results(
            derive_query("Sven_Vath-Live_At_Time_Warp_Mannheim-2024-WEB-FLAC-GRVMSTR.mp3"), load_fixture("sven-vath-time-warp-2024")
        )
        anchor = next(s for s in ranked if s.result.external_id == ANCHOR_ID)
        older = [s for s in ranked if s.row_year is not None and s.row_year < 2010]
        assert older
        assert max(s.confidence for s in older) > anchor.confidence
        assert all(Disqualifier.YEAR_MISMATCH in s.disqualifiers for s in older)
        assert ranked[0].confidence > anchor.confidence
        assert next(s for s in ranked if s.is_selectable).result.external_id == ANCHOR_ID

    def test_new_year_boundary_is_forgiven_when_dates_are_days_apart(self) -> None:
        """Synthetic row: a 2023-12-31 set filed by the uploader as 2024-01-01.

        Fixture-unavailable shape (no captured query straddles a new year), so it is built by hand
        and labelled. The years disagree while the dates are one day apart, and a naive
        year-equality gate would disqualify the correct row.
        """
        row = TracklistSearchResult(
            external_id="nye01",
            title="Sven Väth @ Time Warp, Germany",
            url="https://www.1001tracklists.com/tracklist/nye01/x.html",
            artist="Sven Väth",
            event="Time Warp, Germany",
            date="2023-12-31",
        )
        derived = derive_query("Sven_Vath-Live_At_Time_Warp-2024-01-01-WEB-FLAC-GRP.mp3")
        assert derived.year == 2024
        scored = score_results(derived, [row])[0]
        assert scored.row_year == 2023
        assert scored.disqualifiers == ()
        assert scored.is_selectable

    def test_far_apart_dates_across_a_year_boundary_are_still_disqualified(self) -> None:
        """Synthetic counterpart: the forgiveness window must not swallow a real year mismatch."""
        row = TracklistSearchResult(
            external_id="far01",
            title="Sven Väth @ Time Warp, Germany",
            url="https://www.1001tracklists.com/tracklist/far01/x.html",
            artist="Sven Väth",
            event="Time Warp, Germany",
            date="2023-04-01",
        )
        scored = score_results(derive_query("Sven_Vath-Live_At_Time_Warp-2024-01-01-WEB-FLAC-GRP.mp3"), [row])[0]
        assert Disqualifier.YEAR_MISMATCH in scored.disqualifiers

    def test_year_in_title_gates_a_row_that_has_no_date(self) -> None:
        """Synthetic row with no date cell but a year in its title -- gated, not exempted."""
        row = TracklistSearchResult(
            external_id="ttl01",
            title="Sven Väth @ Time Warp 2019",
            url="https://www.1001tracklists.com/tracklist/ttl01/x.html",
            artist="Sven Väth",
            event="Time Warp 2019",
            date=None,
        )
        scored = score_results(derive_query(ANCHOR_FILENAME), [row])[0]
        assert scored.row_date is None
        assert scored.row_year == 2019
        assert Disqualifier.YEAR_MISMATCH in scored.disqualifiers


class TestAmbiguityIsRefusedNotGuessed:
    """A coin flip must never be laundered into a confident pick -- phaze-fq9h.7 propagates it."""

    def test_file_with_no_year_refuses_because_editions_are_indistinguishable(self) -> None:
        selection = select_result(derive_query("Sven_Vath-Live_At_Time_Warp-WEB-FLAC-GRVMSTR.mp3"), load_fixture("sven-vath-time-warp"))
        assert selection.selected is None
        assert selection.rejection is LookupOutcome.SEARCH_FAILED
        assert selection.rejection.is_transient
        assert not selection.rejection.is_definitive_negative
        assert "too close to call" in selection.reason
        assert selection.runner_up is not None

    def test_ambiguous_day_month_order_still_selects_via_the_year(self) -> None:
        """``date_ambiguous`` leaves the exact day unresolved but the YEAR fully determined.

        The scorer must not score the unresolved date as if it were certain, yet must still use the
        year -- which here is enough to reach the right row.
        """
        derived = derive_query("Sven_Vath-Live_At_Time_Warp_Mannheim-10-12-2024-WEB-FLAC-GRVMSTR.mp3")
        assert derived.date_ambiguous
        assert derived.date is None
        assert derived.year == 2024

        selection = select_result(derived, load_fixture("sven-vath-time-warp-2024"))
        assert selection.selected is not None
        assert selection.selected.result.external_id == ANCHOR_ID

    def test_unresolved_date_is_never_scored_as_a_certain_one(self) -> None:
        """Belt-and-braces on the phaze-5fta seam.

        If ``resolve_ambiguous_date_order`` ever starts returning a date while ``date_ambiguous``
        remains set, the scorer must still refuse to treat it as settled.
        """
        row = TracklistSearchResult(
            external_id="amb01",
            title="Sven Väth @ Time Warp, Germany",
            url="https://www.1001tracklists.com/tracklist/amb01/x.html",
            artist="Sven Väth",
            event="Time Warp, Germany",
            date="2024-10-25",
        )
        derived = derive_query(ANCHOR_FILENAME)
        settled = score_results(derived, [row])[0]

        from dataclasses import replace

        unsettled = score_results(replace(derived, date_ambiguous=True), [row])[0]
        assert settled.confidence > unsettled.confidence

    def test_file_with_neither_artist_nor_event_is_refused_transiently(self) -> None:
        derived = derive_query("2024-10-25.mp3")
        assert not derived.artist
        assert not derived.event
        selection = select_result(derived, load_fixture("sven-vath-time-warp-2024"))
        assert selection.selected is None
        assert selection.rejection is LookupOutcome.SEARCH_FAILED
        assert "nothing to score" in selection.reason
        assert selection.ranked, "candidates are still reported for the operator panel"

    def test_two_rows_sharing_the_exact_date_do_not_get_the_uniqueness_exemption(self) -> None:
        """Synthetic: two stages of one festival, same artist, same day.

        The exact-date exemption must not re-open the hole the margin gate closes, so it requires
        the winning date to single ONE row out.
        """
        common = {"url": "https://www.1001tracklists.com/tracklist/x/x.html", "artist": "Carl Cox", "date": "2016-09-13"}
        rows = [
            TracklistSearchResult(external_id="stage1", title="Carl Cox @ Space Ibiza", event="Main Room, Space Ibiza", **common),  # type: ignore[arg-type]
            TracklistSearchResult(external_id="stage2", title="Carl Cox @ Space Ibiza", event="Terrace, Space Ibiza", **common),  # type: ignore[arg-type]
        ]
        selection = select_result(derive_query("Carl_Cox-Live_At_Space_Ibiza-2016-09-13-WEB-FLAC-GRP.mp3"), rows)
        assert selection.selected is None
        assert selection.rejection is LookupOutcome.SEARCH_FAILED
        assert "too close to call" in selection.reason


class TestScoringMechanics:
    """Unit-level behaviour of the ranking and the parsing helpers."""

    def test_ranking_is_deterministic_and_independent_of_site_order(self) -> None:
        forward = ranked_ids("time-warp-2024", ANCHOR_FILENAME)
        results = load_fixture("time-warp-2024")
        reversed_order = [s.result.external_id for s in score_results(derive_query(ANCHOR_FILENAME), list(reversed(results)))]
        assert forward == reversed_order

    def test_qualified_candidates_sort_ahead_of_disqualified_ones_at_equal_score(self) -> None:
        ranked = score_results(
            derive_query("Sven_Vath-Live_At_Time_Warp_Mannheim-2024-WEB-FLAC-GRVMSTR.mp3"), load_fixture("sven-vath-time-warp-2024")
        )
        tied = [s for s in ranked if s.confidence == ranked[0].confidence]
        if len(tied) > 1:
            qualified_flags = [s.is_qualified for s in tied]
            assert qualified_flags == sorted(qualified_flags, reverse=True)

    def test_malformed_row_date_degrades_to_none_rather_than_raising(self) -> None:
        """Synthetic: a date string the site could start emitting after a redesign."""
        row = TracklistSearchResult(
            external_id="bad01",
            title="Sven Väth @ Time Warp, Germany",
            url="https://www.1001tracklists.com/tracklist/bad01/x.html",
            artist="Sven Väth",
            event="Time Warp, Germany",
            date="25 October 2024",
        )
        scored = score_results(derive_query(ANCHOR_FILENAME), [row])[0]
        assert scored.row_date is None
        # The exact day is unreadable, but the year is still plainly there -- recovering it keeps
        # the row subject to the year gate instead of silently exempting it.
        assert scored.row_year == 2024
        assert scored.disqualifiers == ()

    def test_row_with_no_date_and_no_year_anywhere_has_no_year(self) -> None:
        row = TracklistSearchResult(
            external_id="nod01",
            title="Sven Väth @ Time Warp, Germany",
            url="https://www.1001tracklists.com/tracklist/nod01/x.html",
            artist="Sven Väth",
            event="Time Warp, Germany",
            date=None,
        )
        scored = score_results(derive_query(ANCHOR_FILENAME), [row])[0]
        assert scored.row_year is None
        assert scored.disqualifiers == ()

    def test_year_only_match_is_capped_below_a_perfect_score(self) -> None:
        """Without a cap, dropping the date weight from the denominator normalizes to a flat 100."""
        row = TracklistSearchResult(
            external_id="cap01",
            title="Sven Väth @ Time Warp Mannheim",
            url="https://www.1001tracklists.com/tracklist/cap01/x.html",
            artist="Sven Vath",
            event="At Time Warp Mannheim",
            date=None,
        )
        scored = score_results(derive_query("Sven_Vath-Live_At_Time_Warp_Mannheim-WEB-FLAC-GRP.mp3"), [row])[0]
        assert scored.confidence < 100
        assert scored.confidence >= SELECTION_THRESHOLD

    def test_selection_reason_names_the_chosen_id_and_confidence(self) -> None:
        selection = select_result(derive_query(ANCHOR_FILENAME), load_fixture("sven-vath-time-warp-2024"))
        assert selection.selected is not None
        assert ANCHOR_ID in selection.reason
        assert str(selection.confidence) in selection.reason

    def test_below_threshold_reason_names_the_best_candidate_and_its_disqualifiers(self) -> None:
        """A single wrong-year row: rejected, and the reason says why rather than just 'too low'."""
        row = TracklistSearchResult(
            external_id="wy01",
            title="Sven Väth @ Time Warp, Germany",
            url="https://www.1001tracklists.com/tracklist/wy01/x.html",
            artist="Sven Väth",
            event="Time Warp, Germany",
            date="2019-04-06",
        )
        selection = select_result(derive_query(ANCHOR_FILENAME), [row])
        assert selection.selected is None
        assert selection.rejection is LookupOutcome.NOT_FOUND
        assert "wy01" in selection.reason
        assert Disqualifier.YEAR_MISMATCH.value in selection.reason

    def test_single_selectable_candidate_needs_no_margin(self) -> None:
        selection = select_result(
            derive_query("Amelie_Lens-Live_At_Awakenings_Festival-2019-06-29-WEB-FLAC-GRP.mp3"), load_fixture("amelie-lens-awakenings")
        )
        assert selection.selected is not None
        selectable = [s for s in selection.ranked if s.is_selectable]
        assert len(selectable) == 1
        assert selection.runner_up is None
