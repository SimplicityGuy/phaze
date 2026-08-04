"""Tests for the detail-page track parser (phaze-fq9h.4).

The anchor and short-listing fixtures under `tests/identify/fixtures/tracklist_render/` are REAL
captures (see that directory's README) -- reused here, exactly as phaze-fq9h.1 intended, so
neither this suite nor a future re-derivation spends a host request. Neither fixture happens to
carry a populated cue time or a mashup row (see `tracklist_parser`'s module docstring), so those
two paths are exercised with small synthetic HTML snippets built in the same shape as the real
rows -- the same kind of honest substitution phaze-fq9h.1 used for its interstitial test.
"""

from pathlib import Path

import pytest

from phaze.services.tracklist_parser import (
    TracklistParseError,
    TracklistTrackPayload,
    parse_tracklist_tracks,
)


FIXTURES = Path(__file__).parent.parent / "fixtures" / "tracklist_render"

ANCHOR_HTML = (FIXTURES / "25fhn7c9-ok.html").read_text(encoding="utf-8")
SHORT_LISTING_HTML = (FIXTURES / "19h6nw7t-ok.html").read_text(encoding="utf-8")


def _row(
    *,
    trno: int = 0,
    artist: str = "Sven Väth",
    title: str = "Ritual Of Life",
    label_html: str = (
        '<span class="iBlock notranslate"><span class="trackLabel noWrap blueTxt" title="label">'
        'COCOON<a href="/label/x/y/index.html"><i class="fa fa-external-link"></i></a></span></span>'
    ),
    cue_html: str = '<div class="cue noWrap action mt5" data-mode="hours" onclick="toggleCue(event);"></div>',
    extra_classes: str = "",
) -> str:
    """Build one synthetic `.tlpItem` row in the same shape as the real captures.

    Mirrors the verified structure: a `data-trno`-bearing container, a `.trackValue` span whose
    children are artist span(s), a literal "-" separator span, and a title span -- plus the
    `.trackLabel` and `.cue` elements this module reads independently.
    """
    return f"""
    <div class="tlpTog bItm tlpItem trRow{trno + 1} {extra_classes}" data-trno="{trno}" id="tlp_{trno}">
      <div class="bPlay">
        <input id="tlp{trno}_cue_seconds" type="hidden" value="0" />
        <span class="fontXL" id="tlp{trno}_tracknumber_value">{trno + 1:02d}</span>
        {cue_html}
      </div>
      <div class="bCont tl">
        <div class="fontL" id="tlp{trno}_content">
          <span class="trackValue notranslate blueTxt" id="tr_{trno}">
            <span class="notranslate blueTxt" translate="no">{artist}</span>
            <span class="notranslate" translate="no">-</span>
            <span class="blueTxt"><span class="notranslate" translate="no">{title}</span></span>
          </span>
          {label_html}
        </div>
      </div>
    </div>
    """


def _page(*rows: str) -> str:
    return f"<html><body>{''.join(rows)}</body></html>"


class TestOnlyOneSelectorSetInTheTree:
    """phaze-2akf's acceptance criterion: NO selector set in the tree matches zero nodes.

    This is a build-failing anti-drift guard, not documentation. The defect it exists to prevent is
    specific and it already happened once: two per-track selector sets coexisted, one of them
    (``TracklistScraper``'s ``.tp a`` / ``.tN`` / ``.tL`` / ``.cueTime``) matched ZERO nodes against
    the very fixtures committed to this repo, and nothing failed -- the path using it simply
    returned empty tracklists that looked like successfully scraped sets with no tracks. A dead
    selector is invisible precisely because "no nodes matched" and "nothing to match" render
    identically.
    """

    def test_every_selector_this_module_uses_matches_the_anchor(self) -> None:
        """Each selector resolves on the real capture -- a stale one is a build failure, not a shrug."""
        from bs4 import BeautifulSoup

        from phaze.services import tracklist_parser as parser_module
        from phaze.services.tracklist_render import TRACK_CONTAINER_SELECTOR

        soup = BeautifulSoup(ANCHOR_HTML, "lxml")
        assert len(soup.select(TRACK_CONTAINER_SELECTOR)) == 52
        for name in ("_TRACK_VALUE_SELECTOR", "_LABEL_SELECTOR", "_CUE_SELECTOR"):
            selector = getattr(parser_module, name)
            assert soup.select(selector), f"{name} ({selector!r}) matches ZERO nodes on the committed anchor capture"

    def test_the_scraper_carries_no_per_track_selectors_any_more(self) -> None:
        """The retired set must not come back -- one selector set, or the next redesign breaks the untested one."""
        from phaze.services.tracklist_scraper import TracklistScraper

        dead = [name for name in vars(TracklistScraper) if name.startswith(("_TRACK_", "_META_", "_CUE_TIME"))]
        assert dead == [], f"TracklistScraper regrew detail-page selectors: {dead}"


class TestAnchorFixture:
    """The acceptance criterion: the anchor renders+parses to 52 tracks."""

    def test_parses_all_52_rows(self) -> None:
        tracks = parse_tracklist_tracks(ANCHOR_HTML)
        assert len(tracks) == 52

    def test_every_row_has_position_and_artist_or_title(self) -> None:
        tracks = parse_tracklist_tracks(ANCHOR_HTML)
        for track in tracks:
            assert track.artist or track.title

    def test_positions_are_contiguous_from_one(self) -> None:
        tracks = parse_tracklist_tracks(ANCHOR_HTML)
        assert [t.position for t in tracks] == list(range(1, 53))

    def test_first_row_full_detail(self) -> None:
        """Row 1 has microdata (artist/title resolved) and a label -- verify the full shape."""
        track = parse_tracklist_tracks(ANCHOR_HTML)[0]
        assert track.position == 1
        assert track.artist == "Sven Väth"
        assert track.title == "Ritual Of Life"
        assert track.label == "COCOON"
        assert track.timestamp is None  # no cue recorded anywhere in this capture
        assert track.is_mashup is False

    def test_unidentified_id_row_still_populates_artist_and_title(self) -> None:
        """Position 5 in the anchor is an "ID - ID" row with NO schema.org microdata at all --
        the shape that would defeat a parser reading only `meta[itemprop=name]`."""
        tracks = parse_tracklist_tracks(ANCHOR_HTML)
        id_track = tracks[4]
        assert id_track.position == 5
        assert id_track.artist == "ID"
        assert id_track.title == "ID"

    def test_multi_artist_joiner_preserved(self) -> None:
        """Position 13 in the anchor is "Adam Beyer & Raxon - The Signal (Night Mix)"."""
        tracks = parse_tracklist_tracks(ANCHOR_HTML)
        track = tracks[12]
        assert track.artist == "Adam Beyer & Raxon"
        assert track.title == "The Signal"
        assert track.remix_info == "Night Mix"

    def test_remix_annotation_extracted_and_not_left_in_title(self) -> None:
        """Position 7: "Ede - Do My Thing (Dixon Rework)"."""
        tracks = parse_tracklist_tracks(ANCHOR_HTML)
        track = tracks[6]
        assert track.title == "Do My Thing"
        assert track.remix_info == "Dixon Rework"
        assert "Dixon" not in (track.title or "")

    def test_rows_without_a_label_are_none_not_dropped(self) -> None:
        """14 of the anchor's 52 rows carry no `.trackLabel` -- absence must not fail the row."""
        tracks = parse_tracklist_tracks(ANCHOR_HTML)
        unlabeled = [t for t in tracks if t.label is None]
        assert len(unlabeled) == 14
        # Every one of those rows still has an artist/title -- absence of a label never
        # suppresses the rest of the row.
        assert all(t.artist or t.title for t in unlabeled)


class TestShortListingFixture:
    """Developed against more than one page shape, per the fixture README."""

    def test_parses_all_12_rows(self) -> None:
        tracks = parse_tracklist_tracks(SHORT_LISTING_HTML)
        assert len(tracks) == 12

    def test_positions_are_contiguous_from_one(self) -> None:
        tracks = parse_tracklist_tracks(SHORT_LISTING_HTML)
        assert [t.position for t in tracks] == list(range(1, 13))


class TestTimestampPresent:
    """Neither real fixture has a populated cue -- exercise the positive path synthetically."""

    def test_populated_cue_is_parsed(self) -> None:
        html = _page(_row(cue_html='<div class="cue noWrap action mt5" data-mode="hours">1:02:34</div>'))
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].timestamp == "1:02:34"

    def test_short_mmss_cue_is_parsed(self) -> None:
        html = _page(_row(cue_html='<div class="cue noWrap action mt5" data-mode="hours">4:12</div>'))
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].timestamp == "4:12"

    def test_empty_cue_is_none(self) -> None:
        html = _page(_row())
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].timestamp is None

    def test_position_one_with_empty_cue_is_none_not_zero(self) -> None:
        """The ambiguous case the module docstring calls out: position 1 legitimately COULD
        start at 0:00, but an empty `.cue` is trusted as "no cue" regardless of position."""
        html = _page(_row(trno=0, cue_html='<div class="cue noWrap action mt5" data-mode="hours"></div>'))
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].position == 1
        assert tracks[0].timestamp is None

    def test_garbled_cue_text_is_dropped_not_persisted(self) -> None:
        """Defensive validation mirroring phaze-7zoh: text that doesn't look like a cue time is
        dropped to None rather than trusted verbatim, in case the selector drifts onto some other
        element's text."""
        html = _page(_row(cue_html='<div class="cue noWrap action mt5" data-mode="hours">not a time</div>'))
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].timestamp is None


class TestMashup:
    """Neither real fixture contains a mashup row -- see the module docstring."""

    def test_mashup_class_sets_is_mashup_true(self) -> None:
        html = _page(_row(extra_classes="mashupTrack"))
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].is_mashup is True

    def test_ordinary_row_is_not_a_mashup(self) -> None:
        html = _page(_row())
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].is_mashup is False


class TestEmptyOrStubTracklist:
    """A page with zero matching containers parses to an empty list -- not an error.

    Distinct from the partial-parse-failure case below: zero found vacuously satisfies "every
    container yielded usable data".
    """

    def test_no_track_container_at_all_returns_empty_list(self) -> None:
        html = "<html><body><div class='tlpTBIcon'>No tracklist available</div></body></html>"
        assert parse_tracklist_tracks(html) == []

    def test_empty_string_returns_empty_list(self) -> None:
        assert parse_tracklist_tracks("") == []


class TestPartialParseIsAFailure:
    """A page that yields fewer usable rows than containers found is a failure, not a partial
    success -- the core acceptance behavior this bead exists to get right."""

    def test_one_unparseable_row_among_several_raises(self) -> None:
        good_row = _row(trno=0)
        # A row whose .trackValue is missing entirely -- the shape neither real fixture has, but
        # exactly what selector drift would produce: containers found, nothing extractable.
        broken_row = """
        <div class="tlpTog bItm tlpItem trRow2" data-trno="1" id="tlp_1">
          <div class="bCont tl"><div class="fontL" id="tlp1_content"></div></div>
        </div>
        """
        html = _page(good_row, broken_row)
        with pytest.raises(TracklistParseError) as exc_info:
            parse_tracklist_tracks(html)
        assert exc_info.value.container_count == 2
        assert exc_info.value.parsed_count == 1

    def test_error_message_names_the_selector_and_counts(self) -> None:
        broken_row = """
        <div class="tlpTog bItm tlpItem trRow1" data-trno="0" id="tlp_0">
          <div class="bCont tl"><div class="fontL" id="tlp0_content"></div></div>
        </div>
        """
        with pytest.raises(TracklistParseError, match=r"Parsed 0 of 1 track row"):
            parse_tracklist_tracks(_page(broken_row))

    def test_no_rows_recovered_on_failure_not_a_truncated_list(self) -> None:
        """Even the rows that DID parse cleanly must not be handed back -- see the module
        docstring's "PARTIAL PARSES ARE FAILURES" section."""
        good_row = _row(trno=0)
        broken_row = """
        <div class="tlpTog bItm tlpItem trRow2" data-trno="1" id="tlp_1">
          <div class="bCont tl"><div class="fontL" id="tlp1_content"></div></div>
        </div>
        """
        try:
            parse_tracklist_tracks(_page(good_row, broken_row))
        except TracklistParseError as exc:
            # The exception carries the counts; the caller never sees a partial `tracks` list at
            # all -- there is no `.tracks` attribute to reach for here.
            assert not hasattr(exc, "tracks")
        else:
            pytest.fail("expected TracklistParseError")


class TestPositionFallback:
    def test_missing_data_trno_falls_back_to_document_order(self) -> None:
        html = """
        <html><body>
        <div class="tlpTog bItm tlpItem trRow1" id="tlp_0">
          <div class="bCont tl"><div class="fontL">
            <span class="trackValue"><span>Artist</span><span>-</span><span>Title</span></span>
          </div></div>
        </div>
        </body></html>
        """
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].position == 1

    def test_non_numeric_data_trno_falls_back_to_document_order(self) -> None:
        html = """
        <html><body>
        <div class="tlpTog bItm tlpItem trRow1" data-trno="not-a-number" id="tlp_0">
          <div class="bCont tl"><div class="fontL">
            <span class="trackValue"><span>Artist</span><span>-</span><span>Title</span></span>
          </div></div>
        </div>
        </body></html>
        """
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].position == 1


class TestTitleWithHyphenNotMisSplit:
    """The separator is a specific DOM node (a span whose STRIPPED text is exactly "-"), not a
    character search in joined text -- so a title containing a hyphen must not be mis-split."""

    def test_hyphen_inside_title_span_is_not_treated_as_the_separator(self) -> None:
        html = _page(
            _row(
                trno=0,
                artist="Artist",
                title="Part One - Part Two",
            )
        )
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].artist == "Artist"
        assert tracks[0].title == "Part One - Part Two"


class TestNoSeparatorFallsBackToWholeText:
    def test_missing_separator_span_falls_back_to_full_text_as_title(self) -> None:
        html = """
        <html><body>
        <div class="tlpTog bItm tlpItem trRow1" data-trno="0" id="tlp_0">
          <div class="bCont tl"><div class="fontL">
            <span class="trackValue"><span>Some Freeform Text</span></span>
          </div></div>
        </div>
        </body></html>
        """
        tracks = parse_tracklist_tracks(html)
        assert tracks[0].artist is None
        assert tracks[0].title == "Some Freeform Text"


def test_tracklist_track_payload_is_a_frozen_dataclass() -> None:
    """Basic construction/shape smoke test -- mirrors `ScrapedTrack`'s field set plus the
    detail-page-only additions (`label`, `is_mashup`, `remix_info`, `confidence`)."""
    payload = TracklistTrackPayload(position=1, artist="A", title="B")
    assert payload.position == 1
    assert payload.label is None
    assert payload.is_mashup is False
    assert payload.remix_info is None
    assert payload.confidence is None
    with pytest.raises(AttributeError):
        payload.position = 2  # type: ignore[misc]  # frozen dataclass
