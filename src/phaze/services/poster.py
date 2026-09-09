"""Poster export: one printable, self-contained SVG per set (``phaze-x1qr3.13``).

The record page already builds everything a poster needs -- ``services/analysis_timeline.py``'s
exhaustive energy area and tracklist ticks (``phaze-x1qr3.5``), ``services/harmonic_journey.py``'s
Camelot wheel (``phaze-x1qr3.8``), and ``services/track_segments.py``'s per-track join
(``phaze-x1qr3.6``) -- through :func:`phaze.routers.record.build_file_record_context`. This module
composes those three, unchanged, into ONE printable document rather than re-deriving any of their
geometry: the energy polygons and the wheel's sectors/nodes/edges are embedded verbatim inside
nested ``<svg>`` elements, each keeping its own builder's native coordinate space (``timeline_w`` x
``lane_h`` for the arc, ``harmonic_journey.canvas`` square for the wheel) and scaled into the poster
purely by that nested element's own ``width``/``height``/``viewBox`` -- no transform matrix or
re-computed coordinate is written here.

This module owns only what the drawer/full-page HTML partials do not need: the poster's OWN fixed
layout (:func:`build_poster_layout`, every row and section a plain float in one coordinate system
the template places elements at), the title line (:func:`build_poster_title`), and the tracklist's
printable rows (:func:`poster_track_rows`, formatting :class:`~phaze.services.track_segments.
TrackSegment`'s already-computed fields -- never a second BPM median or modal key of its own).

The poster's height GROWS with the number of tracklist rows rather than truncating -- the same
"never sample, cap, or stride" rule ``services/analysis_timeline.py`` follows for windows applies
here to tracks, so a printable poster never silently drops the tail of a long set's tracklist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from phaze.services.analysis_timeline import format_elapsed_time
from phaze.services.record_facts import ABSENT


if TYPE_CHECKING:
    from collections.abc import Sequence

    from phaze.models.tracklist import Tracklist
    from phaze.services.track_segments import TrackSegment


# The poster's own fixed canvas width -- a portrait sheet wide enough to read the energy arc and
# the tracklist without the wheel dominating either. Height is DERIVED (see
# :func:`build_poster_layout`), never fixed, because a long set's tracklist must never be cropped.
POSTER_W: Final[float] = 900.0
MARGIN: Final[float] = 48.0

# Section block heights, in the poster's own units. `TITLE_H` reserves room for the title line
# AND the filename line beneath it; `ARC_H` and `WHEEL_SIZE` are picked so the wheel's own square
# canvas is legible without crowding the arc above it.
TITLE_H: Final[float] = 96.0
ARC_H: Final[float] = 160.0
WHEEL_SIZE: Final[float] = 260.0
SECTION_GAP: Final[float] = 32.0

# One printable line per timestamped track, and the height of the single line of text that
# replaces the whole section when there is no tracklist at all (`phaze-x1qr3.13`'s "missing
# section stated in text, not blank" acceptance).
TRACK_ROW_H: Final[float] = 20.0
EMPTY_SECTION_H: Final[float] = 28.0
FOOTER_H: Final[float] = 32.0


@dataclass(frozen=True)
class PosterLayout:
    """Every absolute y (and the two x offsets that need one) the poster template places at.

    Computed ONCE, in ``build_poster_layout``, so the template never re-derives a coordinate --
    the risk a hand-duplicated formula in Jinja would silently drift from the canvas height below
    it and either clip the tracklist or leave dead space under it.
    """

    width: float
    height: float
    margin: float
    title_y: float
    filename_y: float
    arc_label_y: float
    arc_y: float
    arc_w: float
    arc_h: float
    arc_note_y: float
    wheel_label_y: float
    wheel_x: float
    wheel_y: float
    wheel_size: float
    wheel_caption_y: float
    tracklist_label_y: float
    tracklist_y: float
    track_row_h: float


def build_poster_layout(track_row_count: int) -> PosterLayout:
    """Lay out the poster's fixed header/arc/wheel block, then grow the canvas by the track count."""
    margin = MARGIN
    arc_y = margin + TITLE_H
    wheel_y = arc_y + ARC_H + SECTION_GAP
    wheel_caption_y = wheel_y + WHEEL_SIZE + 16.0
    tracklist_y = wheel_caption_y + SECTION_GAP
    row_block_h = (track_row_count * TRACK_ROW_H) if track_row_count else EMPTY_SECTION_H
    height = tracklist_y + row_block_h + FOOTER_H
    return PosterLayout(
        width=POSTER_W,
        height=height,
        margin=margin,
        title_y=margin + 34.0,
        filename_y=margin + 58.0,
        arc_label_y=arc_y - 8.0,
        arc_y=arc_y,
        arc_w=POSTER_W - 2.0 * margin,
        arc_h=ARC_H,
        arc_note_y=arc_y + ARC_H - 14.0,
        wheel_label_y=wheel_y - 8.0,
        wheel_x=(POSTER_W - WHEEL_SIZE) / 2.0,
        wheel_y=wheel_y,
        wheel_size=WHEEL_SIZE,
        wheel_caption_y=wheel_caption_y,
        tracklist_label_y=tracklist_y - 12.0,
        tracklist_y=tracklist_y + 12.0,
        track_row_h=TRACK_ROW_H,
    )


def build_poster_title(display_filename: str, tracklist: Tracklist | None) -> str:
    """The poster's title line: ``"artist · event · date"`` when a tracklist exists, the filename otherwise.

    A canonical tracklist that carries none of the three fields (a bare scraped stub) falls back
    to the filename too -- an empty title line would be a worse answer than the one thing always
    known about the file, and ``display_filename`` is already the caller's best-known-clean name
    (``COALESCE(original_filename_repaired, original_filename)``, the convention every other
    filename display in this app follows).
    """
    if tracklist is not None:
        parts = [tracklist.artist, tracklist.event, tracklist.date.isoformat() if tracklist.date else None]
        joined = " · ".join(part for part in parts if part)
        if joined:
            return joined
    return display_filename


def poster_track_rows(segments: Sequence[TrackSegment]) -> list[dict[str, object]]:
    """One printable row per timestamped track, reusing the segment's own already-joined fields.

    No metric is recomputed here: ``bpm``/``key``/``mood`` are ``TrackSegment``'s own median,
    modal-Camelot-derived name, and argmax mood label -- the identical values the tracklist
    fragment's table renders, so a poster and the record page can never disagree about one
    track's tempo. Only display formatting happens here: the elapsed-time string
    (:func:`~phaze.services.analysis_timeline.format_elapsed_time`, the one formatter every
    surface in this module uses for a duration) and the honest :data:`~phaze.services.
    record_facts.ABSENT` dash for a field a segment did not measure.
    """
    return [
        {
            "position": segment.position,
            "title": segment.title or "Untitled",
            "start": format_elapsed_time(segment.start_sec),
            "bpm": f"{segment.bpm:g}" if segment.bpm is not None else ABSENT,
            "key": segment.key or ABSENT,
            "mood": segment.mood_label or ABSENT,
        }
        for segment in segments
    ]


__all__ = [
    "ARC_H",
    "MARGIN",
    "POSTER_W",
    "SECTION_GAP",
    "TITLE_H",
    "TRACK_ROW_H",
    "WHEEL_SIZE",
    "PosterLayout",
    "build_poster_layout",
    "build_poster_title",
    "poster_track_rows",
]
