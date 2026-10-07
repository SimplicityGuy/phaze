"""Offline detail-page parser for captured 1001Tracklists HTML (phaze-fq9h.4).

The renderer owns readiness and network behavior; this module owns the single verified set of
per-track selectors so captured fixtures can be re-parsed without host requests. Position comes from
``data-trno`` with a logged enumeration fallback. Artist, title, and remix text come from the visible
``.trackValue`` spans; label uses ``.trackLabel`` and timestamp uses visible ``.cue`` text. Mashup
detection remains unverified against a captured mashup row and defaults false.

Every matched track container must yield an artist or title. A partial parse raises
``TracklistParseError`` instead of caching a truncated tracklist as a permanent success; zero
containers remains a valid empty result. Selector derivation and exact fixture measurements are
preserved in ``docs/design/0014-tracklist-candidate-sets.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from bs4 import BeautifulSoup, Tag
import structlog

from phaze.services.tracklist_render import TRACK_CONTAINER_SELECTOR


logger = structlog.get_logger(__name__)


# Verified against both recorded fixtures (see the module docstring for the derivation and the
# rows sampled). `_ARTIST_TITLE_SEPARATOR_TEXT` is the exact stripped text of the span that marks
# the artist/title boundary within `.trackValue` -- checking for this literal, rather than
# splitting joined text on a "-" substring, means a title that itself contains a hyphen (e.g.
# "Some Title - Reprise") is never mis-split, because the separator is a specific DOM node here,
# not a character inside a text blob.
_TRACK_VALUE_SELECTOR = ".trackValue"
_ARTIST_TITLE_SEPARATOR_TEXT = "-"
_LABEL_SELECTOR = ".trackLabel"
_CUE_SELECTOR = ".cue"
_MASHUP_CLASS = "mashupTrack"

# phaze-7zoh established this same shape of defense for TracklistTrack.timestamp (String(20)):
# validate the extracted text looks like a cue time before trusting it, rather than persisting
# whatever text a drifted selector happened to land on. "H:MM:SS" or "M:SS", one or two digits in
# the leading unit.
_CUE_TIME_PATTERN = re.compile(r"^\d{1,2}(:\d{2}){1,2}$")


@dataclass(frozen=True, slots=True)
class TracklistTrackPayload:
    """One parsed track row, shaped to map directly onto `TracklistTrack` columns.

    A plain dataclass -- like `tracklist_scraper.ScrapedTrack`, which this deliberately mirrors --
    rather than a Pydantic model: this is an internal service value, not a wire schema, and every
    sibling parser in this codebase (`tracklist_scraper.py`) uses the same shape. `confidence` is
    never set by this module (nothing here scores a track); it exists so a caller assembling a
    `TracklistTrack` row has a single payload type to construct from, present for field parity
    with the ORM model rather than because this parser produces a value for it.
    """

    position: int
    artist: str | None = None
    title: str | None = None
    label: str | None = None
    timestamp: str | None = None
    is_mashup: bool = False
    remix_info: str | None = None
    confidence: float | None = None


class TracklistParseError(RuntimeError):
    """Raised when the track container matched but one or more rows yielded no usable data.

    Deliberately mirrors `tracklist_scraper.SearchParseFailureError`'s shape: candidates were
    found, extraction produced nothing usable for at least one of them, and that is a stale-
    selector defect, not "this set has fewer tracks than it looks like it should have". The
    caller (phaze-fq9h.7's drain, not yet built) is expected to catch this and record
    `LookupOutcome.PARSE_FAILED` -- see `phaze.enums.tracklist_candidate.LookupOutcome` -- rather
    than this module importing that enum directly and reaching across the render/parse boundary
    into drain-level bookkeeping it has no other business with.
    """

    def __init__(self, *, container_count: int, parsed_count: int) -> None:
        super().__init__(
            f"Parsed {parsed_count} of {container_count} track row(s) matched by "
            f"{TRACK_CONTAINER_SELECTOR!r} -- selectors are likely stale, not a short tracklist"
        )
        self.container_count = container_count
        self.parsed_count = parsed_count


def parse_tracklist_tracks(html: str) -> list[TracklistTrackPayload]:
    """Parse a rendered 1001Tracklists detail page into track rows.

    Expects `html` from an `OK` `RenderResult` (i.e. `TRACK_CONTAINER_SELECTOR` is expected to be
    present) but does not require it -- a page with zero matching containers simply parses to an
    empty list; see the module docstring for why that is not treated as a failure.

    Raises:
        TracklistParseError: at least one matched container yielded neither an artist nor a
            title. See the module docstring's "PARTIAL PARSES ARE FAILURES" section -- this
            function never returns a truncated subset of a page's tracks.
    """
    soup = BeautifulSoup(html, "lxml")
    containers = soup.select(TRACK_CONTAINER_SELECTOR)

    tracks: list[TracklistTrackPayload] = []
    unparsed = 0
    for index, container in enumerate(containers, start=1):
        track = _parse_track_container(container, index)
        if track is None:
            unparsed += 1
            continue
        tracks.append(track)

    if unparsed:
        logger.error(
            "Detail-page parse yielded unusable rows -- selectors are likely stale",
            container_count=len(containers),
            parsed_count=len(tracks),
            unparsed_count=unparsed,
        )
        raise TracklistParseError(container_count=len(containers), parsed_count=len(tracks))

    return tracks


def _parse_track_container(container: Tag, fallback_position: int) -> TracklistTrackPayload | None:
    """Parse one `.tlpItem` container into a track, or None if nothing usable could be extracted.

    None (rather than a payload with every field blank) is exactly the signal `parse_tracklist_
    tracks` uses to detect drift -- see its docstring.
    """
    position = _parse_position(container, fallback_position)
    artist, title, remix_info = _parse_artist_title_remix(container)

    if not artist and not title:
        return None

    return TracklistTrackPayload(
        position=position,
        artist=artist,
        title=title,
        label=_parse_label(container),
        timestamp=_parse_timestamp(container),
        is_mashup=_parse_is_mashup(container),
        remix_info=remix_info,
    )


def _parse_position(container: Tag, fallback_position: int) -> int:
    """Return the 1-based track position from `data-trno` (verified 0-based, unique, contiguous).

    Falls back to `fallback_position` (the container's 1-based rank in document order) if the
    attribute is missing or not an integer -- both fixtures always carry it, so hitting this
    fallback means the verified invariant broke and is logged as a warning rather than silently
    renumbering every row after the bad one.
    """
    raw = container.get("data-trno")
    if isinstance(raw, str):
        try:
            return int(raw) + 1
        except ValueError:
            pass
    logger.warning(
        "Track row missing a valid data-trno -- falling back to document-order position",
        raw_data_trno=raw,
        fallback_position=fallback_position,
    )
    return fallback_position


def _parse_artist_title_remix(container: Tag) -> tuple[str | None, str | None, str | None]:
    """Return (artist, title, remix_info) from the row's `.trackValue` span structure.

    See the module docstring for why this reads the visible span structure instead of the
    schema.org microdata (absent on unresolved "ID - ID" rows) or a naive text split on "-"
    (would mis-split a title that itself contains a hyphen).
    """
    track_value = container.select_one(_TRACK_VALUE_SELECTOR)
    if track_value is None:
        return None, None, None

    children = track_value.find_all("span", recursive=False)
    separator_index = next(
        (i for i, child in enumerate(children) if child.get_text(strip=True) == _ARTIST_TITLE_SEPARATOR_TEXT),
        None,
    )
    if separator_index is None:
        # Shape the parser has never seen in either fixture (no artist/title separator span at
        # all) -- fall back to the row's whole text as a title rather than discarding it outright.
        text = track_value.get_text(" ", strip=True)
        return None, (text or None), None

    artist_text = " ".join(span.get_text(" ", strip=True) for span in children[:separator_index] if span.get_text(strip=True))
    artist = artist_text or None

    title: str | None = None
    remix_info: str | None = None
    for span in children[separator_index + 1 :]:
        text = span.get_text(" ", strip=True)
        if not text:
            continue
        if text.startswith("(") and text.endswith(")"):
            remix_info = text[1:-1].strip() or None
        elif title is None:
            title = text

    return artist, title, remix_info


def _parse_label(container: Tag) -> str | None:
    """Return the record label's visible text, or None -- absence is common and legitimate."""
    label_el = container.select_one(_LABEL_SELECTOR)
    if label_el is None:
        return None
    text = label_el.get_text(strip=True)
    return text or None


def _parse_timestamp(container: Tag) -> str | None:
    """Return the row's cue timestamp, or None when the site itself rendered no cue.

    See the module docstring for why this reads `.cue`'s rendered text rather than the always-
    "0" hidden `_cue_seconds` input, and why an empty `.cue` is trusted as "no cue" even for
    position 1.
    """
    cue_el = container.select_one(_CUE_SELECTOR)
    if cue_el is None:
        return None
    text = cue_el.get_text(strip=True)
    return text if text and _CUE_TIME_PATTERN.match(text) else None


# Decoy detection (phaze-y5fc7)
#
# A client 1001Tracklists has flagged is reportedly served a real-looking page whose visible track
# names are randomised while cues and layout stay intact. Each resolved row also carries schema.org
# microdata -- a `meta[itemprop=name]` whose content is "Artist - Title (Remix)" -- inside the row's
# own `itemscope`. On a clean page the two agree; on a decoy they do not.
#
# PAIRING IS PER ROW, NOT PER META. Measured on the two recorded captures (2026-08-02, see
# tests/identify/fixtures/tracklist_render/README.md): the 52-row capture has 46 name metas, the
# 12-row capture 13. Page-level metas (the playlist, the publisher organisation, a body-level one)
# sit outside every row, and unresolved "ID - ID" rows have no microdata at all. Zipping metas to
# rows would therefore misalign every row after the first ID row. A row is PAIRED only when it
# holds exactly one name meta and exactly one `.trackValue`, both non-empty after normalisation;
# anything else (no meta, no visible text, or more than one of either -- an unseen nested shape) is
# excluded from both the numerator and the denominator rather than guessed at.
#
# NORMALISATION is the sequence of Unicode word tokens of the NFKC-casefolded text. Measured on the
# same two captures before this was written: with whitespace-collapse + casefold alone, 2 of 10 and
# 12 of 42 paired rows "disagreed", every one of them solely because the visible span structure
# renders "(Day Mix)" as "( Day Mix )" when its spans are joined. Under word tokens both captures
# measure 0 of 10 and 0 of 42. Punctuation and spacing are formatting; a randomised name changes the
# words, which is what this compares.
DECOY_MIN_PAIRED_ROWS = 3
"""Below this many paired rows the page is never classified decoy -- too little evidence."""

_NAME_META_SELECTOR = 'meta[itemprop="name"]'
_WORD_TOKEN = re.compile(r"\w+")


@dataclass(frozen=True, slots=True)
class DecoyAssessment:
    """How a rendered detail page's visible names compare with its own per-row microdata."""

    paired_rows: int
    """Rows holding exactly one name meta and one `.trackValue`, both non-empty."""
    disagreeing_rows: int
    """Paired rows whose normalised meta name and visible text differ."""

    @property
    def disagreement_rate(self) -> float:
        """Disagreeing / paired, or 0.0 when nothing could be paired."""
        return self.disagreeing_rows / self.paired_rows if self.paired_rows else 0.0

    @property
    def is_decoy(self) -> bool:
        """True when at least 3 rows paired and at least half of them disagree.

        Integer arithmetic (``2 * disagreeing >= paired``) so "exactly 50%" is not at the mercy of a
        float comparison.
        """
        return self.paired_rows >= DECOY_MIN_PAIRED_ROWS and 2 * self.disagreeing_rows >= self.paired_rows


def _name_tokens(text: str) -> tuple[str, ...]:
    """Normalise a track name to its word tokens -- see the decoy section comment for the measurement."""
    return tuple(_WORD_TOKEN.findall(unicodedata.normalize("NFKC", text).casefold()))


def assess_decoy(html: str) -> DecoyAssessment:
    """Compare every row's visible track name with the row's own `meta[itemprop=name]`.

    Pure and offline, like the rest of this module: the drain calls it on an ``OK`` render before
    parsing, and a decoy verdict means nothing from the page is stored.
    """
    soup = BeautifulSoup(html, "lxml")
    paired = 0
    disagreeing = 0
    for container in soup.select(TRACK_CONTAINER_SELECTOR):
        metas = container.select(_NAME_META_SELECTOR)
        values = container.select(_TRACK_VALUE_SELECTOR)
        if len(metas) != 1 or len(values) != 1:
            continue
        content = metas[0].get("content")
        meta_tokens = _name_tokens(content if isinstance(content, str) else "")
        visible_tokens = _name_tokens(values[0].get_text(" ", strip=True))
        if not meta_tokens or not visible_tokens:
            continue
        paired += 1
        if meta_tokens != visible_tokens:
            disagreeing += 1
    return DecoyAssessment(paired_rows=paired, disagreeing_rows=disagreeing)


def _parse_is_mashup(container: Tag) -> bool:
    """Return True if the row carries the site's own `mashupTrack` marker class.

    UNVERIFIED against real markup -- see the module docstring's "Mashup" paragraph. `class` is a
    multi-valued attribute; bs4's lxml backend always parses it to `list[str]` (or `None` when
    absent), never a bare string, so no `str(...)` fallback is needed here.
    """
    return _MASHUP_CLASS in (container.get("class") or [])
