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
    weighted_score = 0.0
    weights_used = 0.0

    # Artist similarity
    artist_sim = 0.0
    artist_contributed = False
    if tracklist_artist and file_artist:
        artist_sim = fuzz.token_set_ratio(tracklist_artist.lower(), file_artist.lower())
        weighted_score += artist_sim * _ARTIST_WEIGHT
        weights_used += _ARTIST_WEIGHT
        artist_contributed = True

    # Event similarity
    event_sim = 0.0
    if tracklist_event and file_event:
        event_sim = fuzz.token_set_ratio(tracklist_event.lower(), file_event.lower())
        weighted_score += event_sim * _EVENT_WEIGHT
        weights_used += _EVENT_WEIGHT

    # Date proximity scoring
    date_score = 0.0
    date_diff_days: int | None = None
    if tracklist_date and file_date:
        date_diff_days = abs((tracklist_date - file_date).days)
        if date_diff_days == 0:
            date_score = 100.0
        elif date_diff_days <= 3:
            date_score = 80.0
        elif date_diff_days <= 30:
            date_score = 50.0
        else:
            date_score = 0.0
        weighted_score += date_score * _DATE_WEIGHT
        weights_used += _DATE_WEIGHT

    if weights_used == 0:
        return 0

    score = round(weighted_score / weights_used)

    # CRITICAL: Cap at 89 if artist+event > 80 but date differs > 3 days
    if artist_sim > 80 and event_sim > 80 and date_diff_days is not None and date_diff_days > 3:
        score = min(score, 89)

    # CRITICAL (phaze-bsdu): never let a match reach the auto-link threshold without artist
    # corroboration -- see the docstring note above.
    if not artist_contributed:
        score = min(score, 89)

    return score


def should_auto_link(confidence: int) -> bool:
    """Determine whether a match confidence warrants automatic linking."""
    return confidence >= AUTO_LINK_THRESHOLD
