"""The record page's nine-fact sidebar list (``phaze-x1qr3.8``, mood chips added ``phaze-4ye5i``).

The record used to open with a four-tile grid -- format, duration, sha256, lane -- laid across
the top of the content it was describing. The full page's final layout moves those facts into
the right sidebar and adds the five the set projection made available: how much of the file was
actually analyzed, its median tempo, its modal key, what it mostly sounds like (as coloured mood
chips), and its dominant style.

Built here rather than in the template for the ordinary reason: "Modal key" is a DERIVATION (a
code named back to a key), and a derivation in Jinja is a derivation with no test. The template's
remaining job is a loop, plus one small conditional for the Mood row's chips (see
:class:`RecordFact.chips`).

**Median BPM and dominant style are read off the file's ``AnalysisResult`` row, never re-derived
from windows (phaze-duyyw).** This module used to recompute a median over the fine windows'
``bpm`` and a duration-weighted mode over the coarse windows' ``mood``/``style`` -- a second
implementation of exactly what ``analysis_windows.aggregate_bpm`` / ``aggregate_dominant`` already
computed once, at analysis completion, into ``AnalysisResult.bpm`` / ``.dominant_style``. The two
implementations could disagree, and did: ``aggregate_bpm`` excludes windows with
``confidence == 0.0`` (unreliable BPM on short/silent audio), but ``AnalysisWindow`` carries no
``confidence`` column and ``FineWindow.as_payload_dict`` never persists it, so the
window-re-derivation here could not reproduce that gate and fell back to a weaker ``bpm > 0``
filter -- on a file whose only fine window was short enough to be gated at write time,
``AnalysisResult.bpm`` was ``None`` (the similarity line, which reads ``AnalysisResult.bpm``
directly -- see ``services/set_similarity.py`` -- printed no BPM segment) while this module's
re-derivation still produced a number, and on a multi-window file the two medians could simply
differ. Reading the stored aggregate directly means the sidebar and the similarity term can never
show two different numbers for the same file again -- there is only one place that computes them.
This rule still holds for ``dominant_style``; it deliberately no longer holds for mood -- see the
next paragraph.

**Mood is intentionally NOT read off ``AnalysisResult.mood`` any more (phaze-4ye5i).** That column
is a ``String(50)``: essentia's top-3 positive-class scores from ONE representative coarse window,
formatted ``"k=v.2f"`` and truncated to 50 characters --
``routers/agent_analysis.py``'s ``_summarize_dict_to_string`` -- which is exactly the operator
complaint this bead fixes: ``"electronic=0.88,party=0.72..."``, CSS-ellipsised, from a single
window rather than the whole set. The Mood row now sources SET-WIDE mood chips from
``SetProfile.mean_vector`` (the file's mean 11-d positive-class vector, positional in
``set_projection.MOOD_ORDER``), falling back to an average of the coarse windows' own
``mood_scores`` -- see :func:`_mood_fractions_from_mean_vector` /
:func:`_mood_fractions_from_windows`. Both paths are normalised through
``analysis_timeline.normalize_mood_shares``, the SAME function :func:`~phaze.services.
analysis_timeline.mood_stack` uses for the mood river, so a chip's percentage always means "this
mood's share of the set's total mood signal" -- never a standalone per-classifier confidence.
That is a deliberate choice, not the only possible one: the 7 stored mood values are independent
per-classifier probabilities (a file can score 0.88 on "electronic" AND 0.72 on "party" at once,
because two different binary classifiers answered two different questions), so a percentage read
straight off them would not sum to anything meaningful and would invite exactly the wrong
comparison against the mood river's "Electronic 31%", which IS a share of a whole. Normalising
the chips the same way as the river keeps every "N%" on this page meaning the same thing.

Every fact is present in every render. A fact with nothing behind it renders an em dash rather
than vanishing, so the list's shape does not change with the data -- an absent row reads as a
layout difference, while a dashed row reads as "not measured", which is what is true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from phaze.services.analysis_timeline import MOOD_HUES, MOOD_LABELS, MOOD_NAMES, format_elapsed_time, mood_stack, normalize_mood_shares
from phaze.services.set_projection import key_name_for_camelot


if TYPE_CHECKING:
    from collections.abc import Sequence

    from phaze.models.analysis import AnalysisResult, AnalysisWindow


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
class MoodChip:
    """One coloured chip in the Mood row: a mood name, its display label, its hue, and its
    share of the set's total mood signal as a whole-number percentage.

    ``hue`` reads :data:`phaze.services.analysis_timeline.MOOD_HUES` -- the one place mood
    colour is declared -- so a chip's swatch is always the same colour the mood river paints
    for the same mood, never a colour this module invents of its own.
    """

    name: str
    label: str
    hue: int
    pct: int


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
    """The untruncated value, for a row whose display form is abbreviated. Empty otherwise.
    Doubles as the Mood row's hover text (phaze-4ye5i): the full ranked mood list, not just
    the top 3 the chips show."""
    glyph: str = ""
    tone: str = "neutral"
    mono: bool = False
    """True for a value read character by character (the digest), which wants a mono face."""
    chips: tuple[MoodChip, ...] = ()
    """Non-empty only for the Mood row. When present, the template renders these coloured
    chips instead of ``value`` as plain text; ``value`` and ``title`` stay populated
    regardless, so the row still degrades to readable text for anything that only reads
    ``dd``'s text content (a screen reader, a text-only test)."""


def _joined(*parts: str | None) -> str:
    """Join the present parts with a middle dot, or :data:`ABSENT` when none is present."""
    present = [part for part in parts if part]
    return " · ".join(present) if present else ABSENT


def _mood_fractions_from_mean_vector(mean_vector: Sequence[float] | None) -> tuple[float, ...] | None:
    """Set-wide mood fractions from ``SetProfile.mean_vector``, the primary source.

    ``mean_vector`` is positional in ``set_projection.MOOD_ORDER`` (11 dims); only the leading
    7 ``mood_*`` dims are moods (:data:`~phaze.services.analysis_timeline.MOOD_NAMES` is that
    same prefix, in the same order). ``None`` when there is no vector, or too short a one to
    index safely -- a file with no coarse windows at all (``set_profile.mean_vector`` is NULL
    for ~661 fine-only files, phaze-hia9z) or no profile row yet.
    """
    if mean_vector is None or len(mean_vector) < len(MOOD_NAMES):
        return None
    return normalize_mood_shares(mean_vector[: len(MOOD_NAMES)])


def _mood_fractions_from_windows(coarse_windows: Sequence[AnalysisWindow]) -> tuple[float, ...] | None:
    """Set-wide mood fractions averaged from the coarse windows' own :func:`mood_stack`.

    Used only when :func:`_mood_fractions_from_mean_vector` returns ``None`` -- an older file,
    or one predating the projection backfill. Each window's raw ``mood_scores`` is normalised
    exactly as the mood river normalises it, then the resulting per-window fractions are
    averaged; a window with no mood scores contributes nothing, and a file with no scored
    coarse windows at all returns ``None`` -- the honest absent state, never a manufactured
    zero vector.
    """
    stacks = [stack for window in coarse_windows if (stack := mood_stack(window)) is not None]
    if not stacks:
        return None
    return tuple(sum(values) / len(stacks) for values in zip(*stacks, strict=True))


def _ranked_mood_chips(fractions: tuple[float, ...]) -> list[MoodChip]:
    """The 7 moods as :class:`MoodChip`, strongest first.

    Sorted by ``-share`` rather than ``reverse=True`` so ties keep :data:`MOOD_NAMES` order --
    the same tie-break :func:`~phaze.services.analysis_timeline.top_mood` uses, kept here by
    the same stable-sort argument rather than restated as a separate rule.
    """
    ranked = sorted(zip(fractions, MOOD_NAMES, strict=True), key=lambda pair: -pair[0])
    return [MoodChip(name=name, label=MOOD_LABELS[name], hue=MOOD_HUES[name], pct=round(share * 100)) for share, name in ranked]


def build_record_facts(
    *,
    file_type: str | None,
    sha256_hash: str | None,
    total_sec: float,
    lane: str,
    lane_kind: str | None,
    coverage_text: str | None,
    analysis: AnalysisResult | None,
    camelot_modal: str | None,
    metadata_duration: float | None = None,
    mean_vector: Sequence[float] | None = None,
    coarse_windows: Sequence[AnalysisWindow] = (),
) -> list[RecordFact]:
    """The nine facts, in the sidebar's own order, every one always present.

    ``total_sec`` is the analyzed extent the timeline already derived (the largest finite window
    end), not a separately-read duration -- the sidebar must not be able to claim a length the
    picture beside it does not cover. ``coverage_text`` is
    :func:`phaze.services.analysis_timeline.coverage_chip`'s own sentence, reused verbatim
    rather than recomposed, so "N coarse - M fine - no gaps" reads identically in both places.

    ``metadata_duration`` is the file's ``FileMetadata.duration`` (the tag-reported length),
    consulted ONLY when ``total_sec`` is zero -- a file with no analysis windows at all. It never
    overrides a real analyzed extent, which is what keeps the "must not overstate" intent above
    true: a file with windows always shows what was actually analyzed, never the tag value. The
    fallback is visibly marked "from tags" in the value itself (not merely a tooltip), so it can
    never be mistaken for an analysed extent at a glance.

    ``analysis`` is the file's (at most one) ``AnalysisResult`` row -- ``None`` for a file never
    analyzed to completion. Median BPM and Style read ``analysis.bpm`` / ``.dominant_style``
    verbatim (rounding BPM for display only); see the module docstring for why this module must
    not re-derive them from windows.

    ``mean_vector`` is ``SetProfile.mean_vector`` (``None`` for a file with no profile row, or
    one predating the coarse-window backfill) and ``coarse_windows`` is this file's own coarse
    ``AnalysisWindow`` rows -- the Mood row's primary source and its fallback, in that order.
    See the module docstring for why Mood, unlike BPM and Style, is no longer read off
    ``AnalysisResult`` at all.
    """
    glyph, tone = _LANE_PRESENTATION.get(lane_kind or "", _LANE_FALLBACK)
    digest = sha256_hash or ""
    tempo = analysis.bpm if analysis is not None else None
    style = analysis.dominant_style if analysis is not None else None
    if total_sec > 0:
        duration_value = format_elapsed_time(total_sec)
        duration_title = ""
    elif metadata_duration is not None and metadata_duration > 0:
        duration_value = f"{format_elapsed_time(metadata_duration)} (from tags)"
        duration_title = "No analysis windows yet -- this is the tag-reported length, not an analyzed extent."
    else:
        duration_value = ABSENT
        duration_title = ""

    mood_fractions = _mood_fractions_from_mean_vector(mean_vector)
    if mood_fractions is None:
        mood_fractions = _mood_fractions_from_windows(coarse_windows)
    ranked_chips = _ranked_mood_chips(mood_fractions) if mood_fractions is not None else []
    top_chips = tuple(ranked_chips[:3])
    mood_value = " · ".join(f"{chip.label} {chip.pct}%" for chip in top_chips) if top_chips else ABSENT
    mood_title = " · ".join(f"{chip.label} {chip.pct}%" for chip in ranked_chips)

    return [
        RecordFact(label="Format", value=file_type or ABSENT),
        RecordFact(label="Duration", value=duration_value, title=duration_title),
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
        RecordFact(label="Mood", value=mood_value, title=mood_title, chips=top_chips),
        RecordFact(label="Style", value=style or ABSENT),
    ]


__all__ = [
    "ABSENT",
    "MoodChip",
    "RecordFact",
    "build_record_facts",
]
