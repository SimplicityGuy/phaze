"""Pick and validate the RIGHT 1001Tracklists search result -- never just the first (phaze-fq9h.6).

The spike this epic grew out of rendered a wrong, empty set by taking the top search row blindly.
This module exists to make that impossible: it scores EVERY row against the file's derived
artist/event/date, ranks them, and either returns a winner with a confidence the caller can gate
on or returns an explicit "no confident match" verdict.

## Why "take the top row" is not merely lazy but actively wrong

Measured against the six real search captures in ``tests/identify/fixtures/tracklist_search/``:
**1001Tracklists orders results CHRONOLOGICALLY, not by relevance.** Each captured page decomposes
into at most two contiguous date-descending runs -- two of the six are descending throughout, and
the other four contain exactly one inversion, where a second, lower-relevance block begins. Within
a block there is no relevance component whatsoever. So "the top row" means "the most RECENT row
that matched some keywords", which is a completely different thing from "the row that is this
file". Two of the captures show the consequence directly:

* ``time-warp-2024.html`` -- searching an event-shaped query puts ``Miss Monique @ MiMo Radio 009
  (Floor 1, Time Warp, Maimarkthalle Mannheim)`` (2024-11-05) at rank 1 and the correct
  ``Sven Väth @ Time Warp, Maimarkthalle Mannheim`` (2024-10-25) at rank **4**. Same festival,
  same venue, same year, wrong artist. This scorer ranks the correct row 84 and the top row 32.
* ``no-such-set.html`` -- a query for a set that does not exist returns **30 rows anyway**, topped
  by ``Sunburn Festival - Official Aftermovie 2019``. There is no empty-page signal to detect: an
  unmatchable query and a matchable one are byte-indistinguishable at the row-count level. A blind
  top-row take renders an aftermovie video page, which parses to zero tracks -- precisely the
  "wrong, empty set" the spike produced. This scorer's best candidate there is 35, far below
  threshold, and the verdict is a rejection.

## Why the cost asymmetry justifies being this conservative

A wrong match is not a wasted request; it is a poisoned one. It renders a wrong detail page,
parses a wrong tracklist, and -- via phaze-fq9h.7 -- propagates that wrong tracklist to **every
duplicate of the set**. A false negative costs one re-query against a budget of ~10,800
requests/day. A false positive silently corrupts an arbitrary number of rows and nothing
downstream will ever notice. Every threshold here is therefore set to fail closed, and the
rejection verdicts are deliberately routed so that "we could not tell" ends at operator attention
rather than at a cached negative.

## The three gates, and why score alone is not enough

1. **Absolute confidence** (:data:`SELECTION_THRESHOLD`) -- reuses
   ``tracklist_matcher.compute_match_confidence`` rather than writing a third scorer, so the
   Pitfall-3 date cap and the phaze-bsdu missing-artist cap keep applying.
2. **The year gate** (:attr:`Disqualifier.YEAR_MISMATCH`) -- score alone does NOT separate a recurring
   festival's editions. Measured: a same-artist, same-event, WRONG-YEAR row scores **64** against
   the correct row's 84. Six points of headroom under a threshold of 70 is luck, not a guard, and
   Time Warp/Awakenings/Space-residency rows are the single most common shape in this corpus. A
   confirmed year disagreement therefore disqualifies a candidate outright, independent of score.
3. **The margin gate** (:data:`MIN_SELECTION_MARGIN`) -- a high score is still unsafe when the
   runner-up scores nearly the same, which is exactly what two nights of the same festival by the
   same artist look like. Selecting the higher of two indistinguishable candidates is a coin flip,
   and a coin flip is not something to propagate to every duplicate. Too close to call is refused.

## Feeding the LookupOutcome vocabulary

Rejections map onto ``enums.tracklist_candidate.LookupOutcome`` rather than a parallel vocabulary,
and the choice between its two relevant members is load-bearing:

* :attr:`~phaze.enums.tracklist_candidate.LookupOutcome.NOT_FOUND` -- the search ran cleanly, we
  read every row, and none of them is this set. A statement about the WORLD, and the only
  cacheable negative.
* :attr:`~phaze.enums.tracklist_candidate.LookupOutcome.SEARCH_FAILED` -- we could not TELL. Used
  when two candidates cleared the bar too close together, or when the file's own derived signal
  was too thin to score anything against. A statement about US, so it stays transient and is never
  cached as a negative. Its backoff ladder ends at ``CacheDecision.TRANSIENT_EXHAUSTED`` --
  "parked for operator attention" -- which is the correct destination for a genuinely ambiguous
  match. Recording either of these cases as ``NOT_FOUND`` would suppress a set that IS on the site
  for the whole negative TTL, which is the exact confusion ``LookupOutcome`` was shaped to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
import enum
import re
from typing import TYPE_CHECKING

from phaze.enums.tracklist_candidate import LookupOutcome
from phaze.services.tracklist_matcher import compute_match_confidence


if TYPE_CHECKING:
    from collections.abc import Sequence

    from phaze.services.tracklist_query import DerivedQuery
    from phaze.services.tracklist_scraper import TracklistSearchResult


__all__ = [
    "MIN_SELECTION_MARGIN",
    "SELECTION_THRESHOLD",
    "Disqualifier",
    "ResultSelection",
    "ScoredResult",
    "score_results",
    "select_result",
]


# --------------------------------------------------------------------------------------------
# Calibration -- every number below is set against the six real captures, not invented
# --------------------------------------------------------------------------------------------

SELECTION_THRESHOLD = 70
"""Minimum confidence for a candidate to be selectable at all.

Measured on the captured fixtures: the correct row scores **84**; the best wrong-artist row on the
same event and date scores **50**; the best row for a set that does not exist scores **35**. 70
sits in the wide empty band between 50 and 84 rather than being tuned to just clear one example.

Deliberately BELOW ``tracklist_matcher.AUTO_LINK_THRESHOLD`` (90) and not the same knob: that
constant governs whether a scraped tracklist may be auto-LINKED to a file without review, a
decision made after the detail page has been read. This one governs whether a search row is worth
SPENDING A RENDER on at all. Requiring 90 here would refuse to render the correct row in every one
of the captured cases (its 84 is the ceiling this signal can reach, since a search row carries no
track data to corroborate with), so the two thresholds must not be collapsed.
"""

MIN_SELECTION_MARGIN = 10
"""Minimum confidence gap between the winner and the best qualifying rival, in points.

Measured: the correct row leads its nearest surviving rival by 18 points in the wrong-match
capture. Two nights of the same festival by the same artist -- distinguishable only by a date the
file may not carry -- land within a point or two of each other, and picking the higher of those is
a coin flip that phaze-fq9h.7 would then propagate to every duplicate of the set.
"""

_YEAR_ONLY_CONFIDENCE_CAP = 85
"""Cap applied when the file has a trustworthy YEAR but no trustworthy exact DATE.

This is the same defect ``compute_match_confidence``'s phaze-bsdu note describes, arriving through
a different door. That function normalizes by the weights that ACTUALLY contributed, so when the
file supplies no date the 0.2 date weight is dropped from the denominator instead of counting as a
miss: a same-artist, same-event row then divides out to a flat **100** -- on every edition of the
festival at once, having never checked a day. Capping below 100 keeps such a match selectable (it
still clears :data:`SELECTION_THRESHOLD`, and the year gate plus the margin gate are what actually
make it safe) while recording honestly that the day was never confirmed.
"""

_NEAR_DATE_DAYS = 3
"""Date window within which a YEAR disagreement is forgiven.

Exists for the new-year boundary, which is not hypothetical in this corpus: a set played on the
night of 2023-12-31 is routinely filed by an uploader as 2024-01-01, and vice versa. Those two
dates disagree on year while being one day apart, and a naive year-equality gate would disqualify
the correct row. The forgiveness requires BOTH exact dates to be known -- when the file's date is
year-only or unresolved there is nothing to measure the window against, so the gate stands.
"""

_YEAR_PATTERN = re.compile(r"\b(19[7-9]\d|20[0-3]\d)\b")


class Disqualifier(enum.StrEnum):
    """Why a candidate is barred from selection regardless of its score.

    Kept as a distinct axis from the score (rather than folded in as a penalty) so the operator
    panel can show "scored 64 but disqualified: wrong year" instead of an unexplained low number.
    A disqualified candidate is still ranked and still reported -- suppressing it entirely would
    hide from a human the very row they need to see to correct a bad verdict.
    """

    YEAR_MISMATCH = "year_mismatch"
    """The row's year and the file's year disagree, and the exact dates are not within
    :data:`_NEAR_DATE_DAYS` (or are not both known). The recurring-festival guard."""

    NO_ARTIST = "no_artist"
    """The row carries no artist -- a promo/aftermovie video entry rather than a set. 32 of the
    180 captured rows are this shape, and one of them is the top row of the no-match capture.
    These have no tracklist to render, so they are never selectable no matter how well the
    remaining text happens to match."""


@dataclass(frozen=True, slots=True)
class ScoredResult:
    """One search row, scored against the file's derived query."""

    result: TracklistSearchResult
    confidence: int
    row_date: _date | None
    row_year: int | None
    disqualifiers: tuple[Disqualifier, ...]

    @property
    def is_qualified(self) -> bool:
        """True when nothing bars this candidate from selection (its score may still be too low)."""
        return not self.disqualifiers

    @property
    def is_selectable(self) -> bool:
        """True when this candidate is qualified AND clears the absolute confidence bar."""
        return self.is_qualified and self.confidence >= SELECTION_THRESHOLD


@dataclass(frozen=True, slots=True)
class ResultSelection:
    """The verdict for one search: a winner, or an explicit refusal to pick one.

    Exactly one of ``selected`` / ``rejection`` is set. When ``selected`` is None the caller must
    NOT render anything -- ``rejection`` is the ``LookupOutcome`` to record, and its
    ``is_definitive_negative`` / ``is_transient`` properties already say whether it may be cached.
    """

    selected: ScoredResult | None
    rejection: LookupOutcome | None
    reason: str
    ranked: tuple[ScoredResult, ...]
    runner_up: ScoredResult | None = None

    @property
    def confidence(self) -> int:
        """The selected candidate's confidence, or 0 when nothing was selected."""
        return self.selected.confidence if self.selected is not None else 0


# --------------------------------------------------------------------------------------------
# Signal extraction
# --------------------------------------------------------------------------------------------


def _parse_row_date(raw: str | None) -> _date | None:
    """Parse a search row's ``YYYY-MM-DD`` date string, tolerating anything else by returning None."""
    if not raw:
        return None
    try:
        return _date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _row_year(result: TracklistSearchResult, row_date: _date | None) -> int | None:
    """Best available year for a row: its parsed date, else a 4-digit year in its raw text.

    The fallback scans the raw date string first and then the title. It matters for two shapes:
    rows with no date cell that still name a year in prose (``"Sunburn Festival - Official
    Aftermovie 2019"``), and dates in a format :func:`_parse_row_date` could not read at all -- if
    a redesign switched the cell to ``"25 October 2024"``, the exact day would be lost but the
    year is still plainly there. Recovering it lets the year gate keep disqualifying such a row,
    whereas treating the year as unknown would silently EXEMPT the row from the gate. Fail closed:
    a partially-readable date must not become a free pass.
    """
    if row_date is not None:
        return row_date.year
    for text in (result.date, result.title):
        match = _YEAR_PATTERN.search(text) if text else None
        if match is not None:
            return int(match.group(1))
    return None


def _file_year(derived: DerivedQuery) -> int | None:
    """The file's trustworthy year, or None.

    ``DerivedQuery.year`` is trustworthy even when ``date_ambiguous`` is set -- an unresolved
    DD-MM/MM-DD order leaves the YEAR perfectly well determined (see
    ``tracklist_query.resolve_ambiguous_date_order``). That is the whole reason the year gate can
    still fire on a file whose exact date could not be resolved.
    """
    if derived.year is not None:
        return derived.year
    return derived.date.year if derived.date is not None else None


def _trustworthy_file_date(derived: DerivedQuery) -> _date | None:
    """The file's exact date, but ONLY when it is actually settled.

    An ambiguous DD-MM/MM-DD filename must never be scored as though its day were known: comparing
    an unresolved date against a row's real one is scoring a coin flip, and it would produce a
    confident-looking number with nothing behind it. ``derive_query`` already leaves ``date`` None
    in that case; this is the explicit belt-and-braces assertion of the same rule, so a future
    change to the seam in ``resolve_ambiguous_date_order`` cannot quietly make ambiguous dates
    score as certain ones.
    """
    if derived.date_ambiguous:
        return None
    return derived.date


def _disqualify(
    *,
    result: TracklistSearchResult,
    row_date: _date | None,
    row_year: int | None,
    derived: DerivedQuery,
    file_date: _date | None,
) -> tuple[Disqualifier, ...]:
    """Return the affirmative reasons this candidate may never be selected."""
    reasons: list[Disqualifier] = []

    if not result.artist:
        reasons.append(Disqualifier.NO_ARTIST)

    file_year = _file_year(derived)
    if file_year is not None and row_year is not None and file_year != row_year:
        near = row_date is not None and file_date is not None and abs((row_date - file_date).days) <= _NEAR_DATE_DAYS
        if not near:
            reasons.append(Disqualifier.YEAR_MISMATCH)

    return tuple(reasons)


def _confidence(*, result: TracklistSearchResult, row_date: _date | None, derived: DerivedQuery, file_date: _date | None) -> int:
    """Score one row, reusing ``compute_match_confidence`` and capping year-only matches."""
    score = compute_match_confidence(result.artist, result.event, row_date, derived.artist, derived.event, file_date)
    if file_date is None or row_date is None:
        score = min(score, _YEAR_ONLY_CONFIDENCE_CAP)
    return score


# --------------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------------


def score_results(derived: DerivedQuery, results: Sequence[TracklistSearchResult]) -> tuple[ScoredResult, ...]:
    """Score every search row against *derived*, best first.

    Ranking is by confidence descending, with qualified candidates always ahead of disqualified
    ones at equal score, and the external id as a final tiebreak so the order is deterministic
    (the caller's decision must not depend on the site's row order, which is date-descending and
    therefore actively misleading -- see the module docstring).
    """
    file_date = _trustworthy_file_date(derived)
    scored: list[ScoredResult] = []

    for result in results:
        row_date = _parse_row_date(result.date)
        row_year = _row_year(result, row_date)
        scored.append(
            ScoredResult(
                result=result,
                confidence=_confidence(result=result, row_date=row_date, derived=derived, file_date=file_date),
                row_date=row_date,
                row_year=row_year,
                disqualifiers=_disqualify(result=result, row_date=row_date, row_year=row_year, derived=derived, file_date=file_date),
            )
        )

    scored.sort(key=lambda s: (-s.confidence, not s.is_qualified, s.result.external_id))
    return tuple(scored)


def _uniquely_dated(winner: ScoredResult, selectable: Sequence[ScoredResult], file_date: _date | None) -> bool:
    """True when *winner* is the ONLY selectable candidate whose date equals the file's exactly.

    The margin gate exists to catch candidates that are genuinely INDISTINGUISHABLE. This is the
    case where a small score gap does not mean that, and reading it as though it did costs real
    matches: ``compute_match_confidence`` scores an exact date 100 and a 3-day miss 80, and the
    date weight is only 0.2, so being right about the day is worth just **4 points**. Measured on
    the Carl Cox capture, the correct row (an exact 2016-09-13 date match) leads a radio broadcast
    three days later by exactly that 4 -- under the bare margin rule the scorer refused a row whose
    date matched perfectly, and residency sets shadowed by a near-dated broadcast are one of the
    most common shapes in this corpus.

    Uniqueness is the whole guard, and it is what keeps this from re-opening the hole the margin
    gate was built to close: two sets by the same artist on the SAME day (different stages of one
    festival, or a set plus its own broadcast) both match the date exactly, so neither is uniquely
    dated, the exemption does not apply, and the margin gate still refuses them. The exemption only
    ever fires when the date genuinely singles one row out.
    """
    if file_date is None or winner.row_date != file_date:
        return False
    return sum(1 for candidate in selectable if candidate.row_date == file_date) == 1


def select_result(derived: DerivedQuery, results: Sequence[TracklistSearchResult]) -> ResultSelection:
    """Choose the one search row worth rendering, or refuse and say why.

    Never raises and never returns a low-confidence winner: when the evidence does not clearly
    identify a single row, the verdict is a rejection carrying the ``LookupOutcome`` the drain
    should record. See the module docstring for why the two rejection outcomes are not
    interchangeable.
    """
    ranked = score_results(derived, results)

    if not ranked:
        return ResultSelection(
            selected=None,
            rejection=LookupOutcome.NOT_FOUND,
            reason="search returned no result rows",
            ranked=(),
        )

    # A file with neither artist nor event has nothing to score AGAINST -- every candidate would be
    # judged on a date alone, which on a date-descending results page means "pick the newest", the
    # exact defect this module exists to prevent. Refuse before scoring can launder that into a
    # number, and refuse TRANSIENTLY: the set may well be on the site, we just cannot ask for it.
    if not derived.artist and not derived.event:
        return ResultSelection(
            selected=None,
            rejection=LookupOutcome.SEARCH_FAILED,
            reason="derived query has neither artist nor event -- nothing to score candidates against",
            ranked=ranked,
        )

    file_date = _trustworthy_file_date(derived)
    selectable = [candidate for candidate in ranked if candidate.is_selectable]
    if not selectable:
        best = ranked[0]
        return ResultSelection(
            selected=None,
            rejection=LookupOutcome.NOT_FOUND,
            reason=(
                f"no candidate reached the selection threshold "
                f"(best {best.result.external_id} scored {best.confidence} < {SELECTION_THRESHOLD}"
                f"{', disqualified: ' + ', '.join(d.value for d in best.disqualifiers) if best.disqualifiers else ''})"
            ),
            ranked=ranked,
        )

    winner = selectable[0]
    runner_up = selectable[1] if len(selectable) > 1 else None

    if (
        runner_up is not None
        and winner.confidence - runner_up.confidence < MIN_SELECTION_MARGIN
        and not _uniquely_dated(winner, selectable, file_date)
    ):
        return ResultSelection(
            selected=None,
            rejection=LookupOutcome.SEARCH_FAILED,
            reason=(
                f"top candidates are too close to call: {winner.result.external_id} scored {winner.confidence} but "
                f"{runner_up.result.external_id} scored {runner_up.confidence} (margin < {MIN_SELECTION_MARGIN})"
            ),
            ranked=ranked,
            runner_up=runner_up,
        )

    return ResultSelection(
        selected=winner,
        rejection=None,
        reason=f"selected {winner.result.external_id} with confidence {winner.confidence}",
        ranked=ranked,
        runner_up=runner_up,
    )
