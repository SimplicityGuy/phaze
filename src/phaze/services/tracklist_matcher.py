"""Fuzzy matching service for linking tracklists to files."""

from __future__ import annotations

from datetime import date
import re

from rapidfuzz import fuzz


# Auto-link threshold per D-14: confidence >= 90 means auto-link
AUTO_LINK_THRESHOLD = 90

# Weight distribution per D-12
_ARTIST_WEIGHT = 0.5
_EVENT_WEIGHT = 0.3
_DATE_WEIGHT = 0.2

# Filename pattern for v1.0 live set naming format
_LIVE_SET_PATTERN = re.compile(r"^(?P<artist>.+?) - Live @ (?P<event>.+?) (?P<year>\d{4})\.(?P<month>\d{2})\.(?P<day>\d{2})\.\w+$")


def parse_live_set_filename(filename: str) -> tuple[str, str, date] | None:
    """Parse a v1.0 live set filename into (artist, event, date).

    Expected format: "{Artist} - Live @ {Event} {YYYY.MM.DD}.{ext}"
    Returns None if the filename doesn't match the pattern.
    """
    match = _LIVE_SET_PATTERN.match(filename)
    if match is None:
        return None

    artist = match.group("artist")
    event = match.group("event")
    try:
        d = date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None

    return (artist, event, d)


def _artist_similarity(tracklist_artist: str | None, file_artist: str | None) -> tuple[float, float, bool]:
    """Score artist-name similarity, if both sides supply an artist.

    Returns (similarity, weight contributed, whether the artist signal contributed at all). The
    third element is load-bearing on its own per phaze-bsdu: a caller must be able to tell "no
    artist data" apart from "artist data scored zero similarity".
    """
    if tracklist_artist and file_artist:
        return fuzz.token_set_ratio(tracklist_artist.lower(), file_artist.lower()), _ARTIST_WEIGHT, True
    return 0.0, 0.0, False


def _event_similarity(tracklist_event: str | None, file_event: str | None) -> tuple[float, float]:
    """Score event-name similarity, if both sides supply an event."""
    if tracklist_event and file_event:
        return fuzz.token_set_ratio(tracklist_event.lower(), file_event.lower()), _EVENT_WEIGHT
    return 0.0, 0.0


def _date_proximity_score(date_diff_days: int) -> float:
    """Score how close two dates are: exact match highest, decaying to 0 past a month apart."""
    if date_diff_days == 0:
        return 100.0
    if date_diff_days <= 3:
        return 80.0
    if date_diff_days <= 30:
        return 50.0
    return 0.0


def _date_similarity(tracklist_date: date | None, file_date: date | None) -> tuple[float, float, int | None]:
    """Score date proximity, if both sides supply a date.

    Returns (proximity score, weight contributed, the day gap itself -- the gap is also needed
    by the Pitfall-3 cap below, so it is surfaced rather than recomputed).
    """
    if tracklist_date and file_date:
        date_diff_days = abs((tracklist_date - file_date).days)
        return _date_proximity_score(date_diff_days), _DATE_WEIGHT, date_diff_days
    return 0.0, 0.0, None


def _date_conflicts_with_strong_artist_event_match(artist_sim: float, event_sim: float, date_diff_days: int | None) -> bool:
    """Whether artist+event look like a real match but the dates disagree enough to be risky.

    Per D-14 Pitfall 3: artist+event similarity above 80 looks like a genuine match, but a date
    gap of more than 3 days is more likely two different nights of the same tour than a scrape
    error -- that pairing must not be allowed to reach the auto-link confidence band.
    """
    return artist_sim > 80 and event_sim > 80 and date_diff_days is not None and date_diff_days > 3


def compute_match_confidence(
    tracklist_artist: str | None,
    tracklist_event: str | None,
    tracklist_date: date | None,
    file_artist: str | None,
    file_event: str | None,
    file_date: date | None,
) -> int:
    """Compute a weighted confidence score (0-100) for a tracklist-file match.

    Weight distribution: artist 0.5, event 0.3, date 0.2.
    Only signals where both sides have data contribute to the score.
    If no signals overlap, returns 0.

    CRITICAL (Pitfall 3): If artist+event similarity > 80 but date differs
    by more than 3 days, cap at 89 to prevent false auto-links.

    NOTE: this cap only fires when BOTH sides supply a date. phaze-2akf retired the caller that
    used to guard the remaining holes itself (``tasks/tracklist.search_tracklist``, whose rule was
    phaze-rkxy: an auto-link requires a CONFIRMED same-window date, so artist+event alone can never
    auto-link a wrong-date tracklist). That rule is not lost -- it moved and got stricter. The
    drain's selector, ``services/tracklist_result_scorer.select_result``, calls this function and
    then applies its own disqualifiers: a date mismatch DISQUALIFIES a row outright rather than
    merely capping it below a threshold, and an ambiguous/absent date drops the candidate to a
    transient refusal instead of a silent accept. This function's score is left intact for the
    manual "find better match" panel, which displays it for operator review rather than gating.

    CRITICAL (phaze-bsdu): normalizing by ``weights_used`` (only the signals present on BOTH
    sides) lets an INCOMPLETE scrape reach full confidence. With no artist on either side, a bare
    event+date match -- or even a bare date match -- divides out to 100, because the missing
    artist term is dropped from the denominator instead of counting as a miss. That silently
    defeats the whole point of the artist weight (0.5, the largest of the three) and the Pitfall-3
    cap above, which requires both sides to have already computed ``artist_sim`` to fire at all.
    So: whenever the artist term did not contribute (either side lacks an artist), the score is
    capped at 89 -- below ``AUTO_LINK_THRESHOLD`` -- the same way the Pitfall-3 date mismatch is
    capped. The raw normalized score is otherwise left intact for the manual review panel.
    """
    artist_sim, artist_weight, artist_contributed = _artist_similarity(tracklist_artist, file_artist)
    event_sim, event_weight = _event_similarity(tracklist_event, file_event)
    date_score, date_weight, date_diff_days = _date_similarity(tracklist_date, file_date)

    weighted_score = artist_sim * artist_weight + event_sim * event_weight + date_score * date_weight
    weights_used = artist_weight + event_weight + date_weight

    if weights_used == 0:
        return 0

    score = round(weighted_score / weights_used)

    # CRITICAL: Cap at 89 if artist+event > 80 but date differs > 3 days
    if _date_conflicts_with_strong_artist_event_match(artist_sim, event_sim, date_diff_days):
        score = min(score, 89)

    # CRITICAL (phaze-bsdu): never let a match reach the auto-link threshold without artist
    # corroboration -- see the docstring note above.
    if not artist_contributed:
        score = min(score, 89)

    return score


def should_auto_link(confidence: int) -> bool:
    """Determine whether a match confidence warrants automatic linking."""
    return confidence >= AUTO_LINK_THRESHOLD
