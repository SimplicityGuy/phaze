"""Detail-page parser for 1001Tracklists (phaze-fq9h.4).

WHY THIS MODULE IS SEPARATE FROM THE RENDERER
----------------------------------------------
`tracklist_render.TracklistRenderer` hands back HTML; it deliberately knows only ONE selector
(`TRACK_CONTAINER_SELECTOR`, imported below rather than re-literaled), which it uses purely as a
readiness signal ("JS has delivered the listing"), not a parse. Per-track field selectors belong
here instead, so a parser fix never costs a host request and a captured fixture (
`tests/identify/fixtures/tracklist_render/`) can be re-parsed offline forever. This module does
not render, retry, or touch the network.

RE-DERIVED, NOT REUSED, FROM `TracklistScraper`
------------------------------------------------
`services/tracklist_scraper.py` carries an older, EXPLICITLY UNVERIFIED guess at these same
per-track selectors (`_TRACK_ARTIST_SELECTOR = ".tp a"`, `_TRACK_NAME_SELECTOR = ".tN"`,
`_TRACK_LABEL_SELECTOR = ".tL"`, `_TRACK_TIME_SELECTOR = ".cueTime"`) -- its own docstring (phaze
-mk6y) says the detail-page block was never checked against a live page, only the search-page
selectors were. Checked here against the two real captures phaze-fq9h.1 recorded (`25fhn7c9-ok
.html`, 52 rows; `19h6nw7t-ok.html`, 12 rows): every one of those four old selectors matches ZERO
nodes in both files. The site's actual per-track markup, as verified against those captures:

* **Position** -- `data-trno` on the `.tlpItem` container itself (0-based; verified unique and
  contiguous 0..N-1 across both fixtures). ``position = int(data-trno) + 1``. Falls back to a
  running enumeration index if the attribute is ever missing or non-numeric, logged as a
  warning since that would mean this verified invariant broke.
* **Artist / title / remix** -- the schema.org ``<meta itemprop="name">`` block looked like the
  obvious source (it carries the full "Artist - Title (Remix)" string) until an "ID - ID"
  (unidentified-track) row turned up with NO ``itemprop`` microdata at all: 10 of the anchor's 52
  rows are exactly this shape. The microdata block only exists once 1001TL has resolved the
  track to its own catalogue; the visible ``.trackValue`` span structure is present on every row
  regardless, so field extraction reads THAT instead. It is a flat run of ``<span>`` children,
  exactly one of which has the literal stripped text ``"-"`` (the artist/title separator).
  Everything before that span is artist credit, joined with a single space -- this reproduces
  multi-artist joiners ("&", "vs.", "pres.", "aka") verbatim, because each joiner is already its
  own span carrying its own surrounding text. The first non-empty span after the separator is the
  title. A later span whose text is wrapped in parentheses is the remix/edit annotation (built
  from the site's own `.remixValue` block) and is extracted separately rather than left attached
  to the title.
* **Label** -- ``.trackLabel``, present on 38 of the anchor's 52 rows. The other 14 (and 14 more
  across the second fixture) legitimately carry no label -- self-released/unlabeled tracks are
  common in this genre, so absence here is real signal, not selector drift.
* **Timestamp** -- the VISIBLE text of ``.cue`` (not the old, zero-hit ``.cueTime``, and
  deliberately not the hidden ``#..._cue_seconds`` input, which is an editor/debugging value that
  reads literally ``"0"`` on every single row of BOTH captures, including position 1). Reading
  the rendered text side-steps the "is 0 a real 0:00 or an unset cue" ambiguity entirely: the site
  itself renders `.cue` blank when it has nothing to show -- on every row of both fixtures,
  including position 1 -- so an empty `.cue` is "no cue", full stop, regardless of position.
  Neither fixture contains a populated cue, so the positive path (a real timestamp string) is
  exercised only by a synthetic unit test; see that test for why a synthetic fixture is honest
  here (the same shape of caveat phaze-fq9h.1 documented for its own Turnstile-interstitial test).
* **Mashup** -- UNVERIFIED against real markup. Neither capture contains a mashup row: the
  ``mashupTrack`` class this parser keys off appears exactly once in each file, as a stylesheet
  rule, applied to zero elements. Detected via that CSS class (the site's own naming for the
  concept), defaulting False. Flag this comment for re-verification if a captured mashup row ever
  turns up.

PARTIAL PARSES ARE FAILURES, NOT SMALL SUCCESSES
--------------------------------------------------
A page whose container selector matches 52 rows but whose field selectors only extract usable
data from 3 of them is NOT "3 tracks, successfully". The downstream drain (phaze-fq9h.7) caches
and propagates whatever this module returns, and phaze-fq9h.3's cache design explicitly
distinguishes a real negative (`LookupOutcome.NOT_FOUND`) from a transient failure
(`LookupOutcome.PARSE_FAILED` and friends, see `phaze.enums.tracklist_candidate`). A silently
truncated tracklist returned as "success" would be indistinguishable from a genuinely short set
and would poison that distinction forever (it gets cached, `FOUND`, and never retried).

The threshold applied here is all-or-nothing: EVERY container matched by `TRACK_CONTAINER_SELECTOR`
must yield a row with an artist or a title (or both) -- even an "ID - ID" row clears this bar,
since "ID" is real, present text, not an extraction failure. If even one container fails to yield
either field, `parse_tracklist_tracks` raises `TracklistParseError` and returns NOTHING, rather
than handing back the rows it did manage to extract. This module never tries to guess "how much
truncation is acceptable" -- given the whole point is that partial truncation is caused by
selector drift, which affects rows unpredictably, there is no principled way to reason about
that threshold. Zero containers (a genuinely empty tracklist) is not a partial-parse failure: it
vacuously satisfies "every container yielded usable data" and returns an empty list.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

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


def _parse_is_mashup(container: Tag) -> bool:
    """Return True if the row carries the site's own `mashupTrack` marker class.

    UNVERIFIED against real markup -- see the module docstring's "Mashup" paragraph. `class` is a
    multi-valued attribute; bs4's lxml backend always parses it to `list[str]` (or `None` when
    absent), never a bare string, so no `str(...)` fallback is needed here.
    """
    return _MASHUP_CLASS in (container.get("class") or [])
