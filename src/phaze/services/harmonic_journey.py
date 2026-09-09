"""The harmonic journey wheel: the set's key path drawn on the Camelot wheel (``phaze-x1qr3.8``).

The record page's right sidebar answers "what did this set DO harmonically" with a picture the
timeline's key ribbons cannot give: the ribbons are laid out in TIME, so two keys a semitone
apart and two keys on opposite sides of the wheel look identical there. Drawn on the wheel, a
disciplined set is a short walk around neighbouring sectors and an undisciplined one is a star
of chords across the middle -- the shape IS the reading.

Everything here is pure geometry over already-stored rows, in the same spirit as
``services/set_projection.py`` and ``services/analysis_timeline.py``: no I/O, no templates, no
session. Three sharing rules the shape exists to keep:

1. **The sector tints come from :func:`phaze.services.set_glyph_colors.camelot_hue`** -- the
   same function the set glyph and its legend call, so Camelot position 8 is one colour
   everywhere on the page. This module never computes a hue of its own.
2. **The nodes are :func:`phaze.services.set_projection.flicker_filtered_key_runs`' runs** --
   the same filtered sequence :func:`phaze.services.set_projection.harmonic_discipline`
   counts, so the node count and the percentage captioned beside it describe one sequence.
   A second filter here would let the picture and its own caption disagree.
3. **An edge is a jump exactly when
   :func:`phaze.services.set_projection.wheel_adjacent` says so** -- the identical predicate
   the discipline figure is computed from, never a re-derivation.

Coordinates are plain rounded floats written into numeric SVG attributes, never label text
interpolated into path or style syntax (the injection-safety convention
``services/analysis_timeline.py`` follows for the timeline's own SVG).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import TYPE_CHECKING, Final

from phaze.services.analysis_timeline import format_elapsed_time
from phaze.services.set_glyph_colors import camelot_hue
from phaze.services.set_projection import flicker_filtered_key_runs, placeable_key_runs, wheel_adjacent


if TYPE_CHECKING:
    from collections.abc import Sequence

    from phaze.models.analysis import AnalysisWindow
    from phaze.services.set_projection import KeyRun


# The wheel's own square canvas. A viewBox, not pixels: the sidebar scales it with CSS, so
# these are the only numbers any coordinate below is relative to.
CANVAS: Final[float] = 240.0
CENTRE: Final[float] = CANVAS / 2.0

# The tinted annulus of 12 sectors, and the two node rings inside it. Camelot's own layout:
# minor keys ("...A") ride the INNER ring and their relative majors ("...B") the outer one, so
# a relative-major/minor move -- adjacent, and the one adjacency that is not a step around the
# circle -- draws as a short radial hop rather than a chord across the middle.
SECTOR_OUTER_R: Final[float] = 108.0
SECTOR_INNER_R: Final[float] = 44.0
MINOR_RING_R: Final[float] = 62.0
MAJOR_RING_R: Final[float] = 92.0

# Twelve sectors, so each spans 30 degrees, centred on its position's own angle.
SECTOR_ARC_DEG: Final[float] = 360.0 / 12.0

# Node radius range. The AREA of a node is proportional to its dwell (hence the square root):
# radius-proportional sizing over-reads a long run by the square of its dwell, which is the
# classic bubble-chart exaggeration and would make one long ambient passage swamp a set.
NODE_R_MIN: Final[float] = 4.0
NODE_R_MAX: Final[float] = 11.0

# Coordinates are rounded before they reach an SVG attribute: two decimals is far finer than a
# 240-unit canvas can show, and it keeps the rendered markup (and its diffs) readable.
_PRECISION: Final[int] = 2


def _polar(radius: float, angle_deg: float) -> tuple[float, float]:
    """Canvas coordinates ``radius`` from the centre at ``angle_deg`` CLOCKWISE from 12 o'clock.

    Clockwise-from-top is the Camelot wheel's own convention (position 1 at the top, ascending
    clockwise), not SVG's zero-at-3-o'clock, so the sine/cosine pair is deliberately swapped
    and the y term subtracted.
    """
    theta = math.radians(angle_deg)
    return (
        round(CENTRE + radius * math.sin(theta), _PRECISION),
        round(CENTRE - radius * math.cos(theta), _PRECISION),
    )


def _sector_angle(number: int) -> float:
    """The centre angle of Camelot position ``number`` -- position 1 at the top, ascending clockwise."""
    return (number - 1) * SECTOR_ARC_DEG


def _ring_radius(letter: str) -> float:
    """Minor ("A") keys ride the inner ring, majors ("B") the outer one."""
    return MINOR_RING_R if letter.upper() == "A" else MAJOR_RING_R


@dataclass(frozen=True)
class WheelSector:
    """One of the twelve tinted wheel positions: an annulus wedge plus its hue and label."""

    number: int
    hue: int
    path: str
    """The wedge as an SVG path: outer arc clockwise, radial line in, inner arc back."""
    label_x: float
    label_y: float


@dataclass(frozen=True)
class JourneyNode:
    """One surviving key run, placed on the wheel and sized by how long the set stayed there."""

    index: int
    code: str
    number: int
    letter: str
    cx: float
    cy: float
    r: float
    hue: int
    dwell_sec: float
    dwell_label: str
    start_label: str


@dataclass(frozen=True)
class JourneyEdge:
    """One transition between consecutive runs, classified as an adjacent move or a jump."""

    from_code: str
    to_code: str
    x1: float
    y1: float
    x2: float
    y2: float
    adjacent: bool
    at_sec: float
    at_label: str

    @property
    def kind(self) -> str:
        """``"adjacent"`` or ``"jump"`` -- the value the rendered edge carries as a data hook."""
        return "adjacent" if self.adjacent else "jump"

    @property
    def label(self) -> str:
        """The move as the caption names it: ``"10B → 5A"``."""
        return f"{self.from_code} → {self.to_code}"


@dataclass(frozen=True)
class HarmonicJourney:
    """Everything the sidebar's wheel renders, or the honest empty answer for a file with no keys.

    ``sectors`` is always the full twelve -- the wheel is the frame of reference and is drawn
    even when nothing is plotted on it, so an empty wheel reads as "no key path here" rather
    than as a missing component. ``nodes`` empty is what ``has_journey`` reports, and the
    template pairs that with a text alternative rather than an empty graphic.
    """

    sectors: list[WheelSector]
    nodes: list[JourneyNode] = field(default_factory=list)
    edges: list[JourneyEdge] = field(default_factory=list)
    adjacent_share: float | None = None
    caption: str = ""
    canvas: float = CANVAS

    @property
    def has_journey(self) -> bool:
        """True when at least one key run survived the flicker filter and could be placed."""
        return bool(self.nodes)

    @property
    def jumps(self) -> list[JourneyEdge]:
        """The non-adjacent moves, in time order -- the ones the caption names individually."""
        return [edge for edge in self.edges if not edge.adjacent]


def _sectors() -> list[WheelSector]:
    """The twelve tinted wedges, built once per render from :func:`camelot_hue`."""
    sectors: list[WheelSector] = []
    for number in range(1, 13):
        centre = _sector_angle(number)
        start, end = centre - SECTOR_ARC_DEG / 2.0, centre + SECTOR_ARC_DEG / 2.0
        ox1, oy1 = _polar(SECTOR_OUTER_R, start)
        ox2, oy2 = _polar(SECTOR_OUTER_R, end)
        ix2, iy2 = _polar(SECTOR_INNER_R, end)
        ix1, iy1 = _polar(SECTOR_INNER_R, start)
        label_x, label_y = _polar((SECTOR_OUTER_R + MAJOR_RING_R) / 2.0, centre)
        hue = camelot_hue(number)
        sectors.append(
            WheelSector(
                number=number,
                # `camelot_hue` is total over 1..12, so this branch is defensive only; the
                # wheel is drawn from a fixed range, never from stored data.
                hue=hue if hue is not None else 0,
                path=(
                    f"M {ox1} {oy1} "
                    f"A {SECTOR_OUTER_R} {SECTOR_OUTER_R} 0 0 1 {ox2} {oy2} "
                    f"L {ix2} {iy2} "
                    f"A {SECTOR_INNER_R} {SECTOR_INNER_R} 0 0 0 {ix1} {iy1} Z"
                ),
                label_x=label_x,
                label_y=label_y,
            )
        )
    return sectors


def _nodes(placed: Sequence[tuple[KeyRun, int]]) -> list[JourneyNode]:
    """Place every run on its ring, sized so node AREA tracks dwell."""
    longest = max((run.dwell_sec for run, _ in placed), default=0.0)
    nodes: list[JourneyNode] = []
    for index, (run, number) in enumerate(placed):
        letter = run.code[-1].upper()
        cx, cy = _polar(_ring_radius(letter), _sector_angle(number))
        # sqrt so AREA, not radius, is proportional to dwell. A file whose runs all have zero
        # extent (degenerate bounds) gets the minimum radius rather than a division by zero.
        fraction = math.sqrt(run.dwell_sec / longest) if longest > 0 else 0.0
        hue = camelot_hue(number)
        nodes.append(
            JourneyNode(
                index=index,
                code=run.code,
                number=number,
                letter=letter,
                cx=cx,
                cy=cy,
                r=round(NODE_R_MIN + fraction * (NODE_R_MAX - NODE_R_MIN), _PRECISION),
                hue=hue if hue is not None else 0,
                dwell_sec=run.dwell_sec,
                dwell_label=format_elapsed_time(run.dwell_sec),
                start_label=format_elapsed_time(run.start_sec),
            )
        )
    return nodes


def _edges(placed: Sequence[tuple[KeyRun, int]], nodes: Sequence[JourneyNode]) -> list[JourneyEdge]:
    """One edge per consecutive pair, classified by :func:`wheel_adjacent`.

    A move's TIME is when it lands -- the start of the run being moved INTO -- which is the
    moment an operator scrubbing the timeline would hear the change.
    """
    edges: list[JourneyEdge] = []
    for index in range(len(nodes) - 1):
        source, target = nodes[index], nodes[index + 1]
        arrival = placed[index + 1][0].start_sec
        edges.append(
            JourneyEdge(
                from_code=source.code,
                to_code=target.code,
                x1=source.cx,
                y1=source.cy,
                x2=target.cx,
                y2=target.cy,
                adjacent=wheel_adjacent(source.code, target.code),
                at_sec=arrival,
                at_label=format_elapsed_time(arrival),
            )
        )
    return edges


def _caption(nodes: Sequence[JourneyNode], edges: Sequence[JourneyEdge], adjacent_share: float | None) -> str:
    """The wheel's text alternative: the run count, the adjacent share, and every jump by name.

    The jumps are named individually and with their elapsed time because that is the only part
    of the picture an operator has to ACT on -- "82% adjacent" says the set is disciplined, but
    "10B → 5A at 1:02:30" says where to listen.

    Only ever called with at least one node: :func:`build_harmonic_journey` returns the empty
    journey -- whose caption is the empty string -- before it reaches here. A guard for the
    empty case would be a branch no test could close through the public API.
    """
    runs = f"{len(nodes)} key run{'' if len(nodes) == 1 else 's'}"
    if not edges:
        return f"{runs}, no key changes."
    share = f"{round((adjacent_share or 0.0) * 100)}% of {len(edges)} move{'' if len(edges) == 1 else 's'} wheel-adjacent"
    jumps = [edge for edge in edges if not edge.adjacent]
    if not jumps:
        return f"{runs}, {share}. No jumps."
    named = "; ".join(f"{edge.label} at {edge.at_label}" for edge in jumps)
    return f"{runs}, {share}. Jump{'' if len(jumps) == 1 else 's'}: {named}."


def build_harmonic_journey(fine_windows: Sequence[AnalysisWindow], *, stored_discipline: float | None = None) -> HarmonicJourney:
    """The set's flicker-filtered key path, placed on the twelve-sector Camelot wheel.

    A file with no usable ``camelot`` data at all -- never analyzed, analyzed before the
    projection existed, or genuinely atonal -- returns the wheel with no nodes and an empty
    caption. That is a real answer and the template renders it as one; it is never an
    exception and never a wheel claiming a path nothing measured.

    ``stored_discipline`` -- the caller's ``SetProfile.harmonic_discipline`` -- is THE caption
    figure when given (phaze-0zx26): every other fact on this page (``camelot_modal``, the set
    glyph, the arc, the peak) already reads the stored profile snapshot rather than a fresh
    recompute, and the operator's phaze-x1qr3.12 blind check judges that same stored snapshot,
    so the wheel disagreeing with it in isolation is the actual inconsistency, not the fix for
    one. The nodes and edges are always built live from ``fine_windows`` regardless -- the
    wheel has to draw the keys actually in front of the reader -- so a caller with a current
    profile gets a picture and a caption both describing that profile; a caller with none (never
    analyzed, or predating the backfill) falls back to computing the share from THIS wheel's own
    edges, exactly as before. Both are derived from the same filter and predicate
    (:func:`phaze.services.set_projection.flicker_filtered_key_runs` /
    :func:`phaze.services.set_projection.wheel_adjacent`), so they agree whenever the stored
    profile is current.
    """
    placed = placeable_key_runs(flicker_filtered_key_runs(fine_windows))
    sectors = _sectors()
    if not placed:
        return HarmonicJourney(sectors=sectors)
    nodes = _nodes(placed)
    edges = _edges(placed, nodes)
    computed_share = (sum(1 for edge in edges if edge.adjacent) / len(edges)) if edges else 1.0
    adjacent_share = stored_discipline if stored_discipline is not None else computed_share
    return HarmonicJourney(
        sectors=sectors,
        nodes=nodes,
        edges=edges,
        adjacent_share=adjacent_share,
        caption=_caption(nodes, edges, adjacent_share),
    )


__all__ = [
    "CANVAS",
    "NODE_R_MAX",
    "NODE_R_MIN",
    "HarmonicJourney",
    "JourneyEdge",
    "JourneyNode",
    "WheelSector",
    "build_harmonic_journey",
]
