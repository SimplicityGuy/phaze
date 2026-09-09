"""The record page's eight-fact sidebar list (``phaze-x1qr3.8``).

The record used to open with a four-tile grid -- format, duration, sha256, lane -- laid across
the top of the content it was describing. The full page's final layout moves those facts into
the right sidebar and adds the four the set projection made available: how much of the file was
actually analyzed, its median tempo, its modal key, and what it mostly sounds like.

Built here rather than in the template for the ordinary reason: the four new values are
DERIVATIONS (a median, two duration-weighted modes, a code named back to a key), and a
derivation in Jinja is a derivation with no test. The template's remaining job is a loop.

Every fact is present in every render. A fact with nothing behind it renders an em dash rather
than vanishing, so the list's shape does not change with the data -- an absent row reads as a
layout difference, while a dashed row reads as "not measured", which is what is true.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import TYPE_CHECKING, Final

from phaze.services.analysis_timeline import format_elapsed_time
from phaze.services.set_projection import key_name_for_camelot


if TYPE_CHECKING:
    from collections.abc import Sequence

    from phaze.models.analysis import AnalysisWindow


# What an unmeasured fact renders as. One constant so the sidebar, and any test asserting the
# "not measured" branch, name the same character.
ABSENT: Final[str] = "—"

# How many leading hex characters of the digest the row shows. The full value stays on the
# row's ``title``; the truncation is display only and the same length the four-tile grid used.
SHA_PREFIX_LEN: Final[int] = 12

# Lane kind -> (glyph, tone). The SAME mapping the Analyze matrix renders from
# ``f.lane_kind`` (COMPUTE-03), moved out of the record template unchanged by phaze-x1qr3.8:
# local=local machine, compute=a registered cloud agent, kueue=a burst-to-cluster job. An
# unrecognised or deregistered kind gets the neutral marker rather than silently asserting
# local, which is the defect phaze-lljfx fixed and this must not undo.
_LANE_PRESENTATION: Final[dict[str, tuple[str, str]]] = {
    "local": ("\U0001f5a5️", "ok"),
    "compute": ("☁️", "info"),
    "kueue": ("⎈", "warn"),
}
_LANE_FALLBACK: Final[tuple[str, str]] = ("▪", "unknown")


@dataclass(frozen=True)
class RecordFact:
    """One labelled row of the sidebar's facts list.

    ``tone`` is an INTENT name ("ok" / "info" / "warn" / "unknown" / "neutral"), never a
    colour: the template owns the mapping onto its semantic utility classes, so the class
    strings stay literal in the markup where Tailwind's source scan can see them and the
    a11y guard's semantic-token rule applies to them.
    """

    label: str
    value: str
    title: str = ""
    """The untruncated value, for a row whose display form is abbreviated. Empty otherwise."""
    glyph: str = ""
    tone: str = "neutral"
    mono: bool = False
    """True for a value read character by character (the digest), which wants a mono face."""


def median_bpm(fine_windows: Sequence[AnalysisWindow]) -> float | None:
    """Median of the fine windows' measured BPM, or ``None`` when none carries one.

    Median, not mean, for the same reason ``services/track_segments.py`` takes one per track: a
    single half- or double-time detection would drag a mean well off the set's real tempo, and
    a multi-hour set has plenty of windows for one to hide in.
    """
    values = [window.bpm for window in fine_windows if window.bpm is not None and math.isfinite(window.bpm) and window.bpm > 0]
    if not values:
        return None
    return statistics.median(values)


def dominant_label(windows: Sequence[AnalysisWindow], attribute: str) -> str | None:
    """Duration-weighted modal value of ``attribute`` across ``windows``, or ``None``.

    Duration-weighted rather than counted, and for the same reason
    :func:`phaze.services.set_projection.modal_camelot` is: windows are not all the same
    length once a file's last window is short, and "what this set mostly is" is a question
    about TIME, not about row counts. Ties resolve to whichever value ``max`` sees first,
    which is stable for a given input ordering.
    """
    weights: dict[str, float] = {}
    for window in windows:
        value = getattr(window, attribute, None)
        if not value or not math.isfinite(window.start_sec) or not math.isfinite(window.end_sec):
            continue
        weights[value] = weights.get(value, 0.0) + max(0.0, window.end_sec - window.start_sec)
    if not weights:
        return None
    return max(weights, key=lambda label: weights[label])


def _joined(*parts: str | None) -> str:
    """Join the present parts with a middle dot, or :data:`ABSENT` when none is present."""
    present = [part for part in parts if part]
    return " · ".join(present) if present else ABSENT


def build_record_facts(
    *,
    file_type: str | None,
    sha256_hash: str | None,
    total_sec: float,
    lane: str,
    lane_kind: str | None,
    coverage_text: str | None,
    windows: Sequence[AnalysisWindow],
    camelot_modal: str | None,
) -> list[RecordFact]:
    """The eight facts, in the sidebar's own order, every one always present.

    ``total_sec`` is the analyzed extent the timeline already derived (the largest finite window
    end), not a separately-read duration -- the sidebar must not be able to claim a length the
    picture beside it does not cover. ``coverage_text`` is
    :func:`phaze.services.analysis_timeline.coverage_chip`'s own sentence, reused verbatim
    rather than recomposed, so "N coarse - M fine - no gaps" reads identically in both places.
    """
    fine = [window for window in windows if window.tier == "fine"]
    coarse = [window for window in windows if window.tier == "coarse"]
    glyph, tone = _LANE_PRESENTATION.get(lane_kind or "", _LANE_FALLBACK)
    digest = sha256_hash or ""
    tempo = median_bpm(fine)
    return [
        RecordFact(label="Format", value=file_type or ABSENT),
        RecordFact(label="Duration", value=format_elapsed_time(total_sec) if total_sec > 0 else ABSENT),
        RecordFact(
            label="sha256",
            value=f"{digest[:SHA_PREFIX_LEN]}…" if digest else ABSENT,
            title=digest,
            mono=True,
        ),
        RecordFact(label="Lane", value=lane, glyph=glyph, tone=tone),
        RecordFact(label="Windows", value=coverage_text or ABSENT),
        RecordFact(label="Median BPM", value=str(round(tempo)) if tempo is not None else ABSENT),
        RecordFact(label="Modal key", value=_joined(camelot_modal, key_name_for_camelot(camelot_modal))),
        RecordFact(label="Mood · style", value=_joined(dominant_label(coarse, "mood"), dominant_label(coarse, "style"))),
    ]


__all__ = [
    "ABSENT",
    "RecordFact",
    "build_record_facts",
    "dominant_label",
    "median_bpm",
]
