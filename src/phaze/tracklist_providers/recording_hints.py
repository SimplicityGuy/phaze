"""Date and event facts inferred from a recording's file name and path. Pure: no I/O, no database, no clock.

Follows docs/design/tracklist-provider-matching.md (date kind, precision, ambiguity) and the recommendation of
docs/spikes/phaze-fi8rs-recording-facts-for-remote-search.md:

* A year-first full date (``2024-06-15``, ``2024_06_15``, compact ``20240615``) is one ``inferred`` day-precision date.
* A day-first or month-first date that reads two ways (``03-04-2024``) is NEVER collapsed to one reading. It is carried
  at reduced (year) precision, with both readings recorded as ``alt=`` evidence. A form that reads one way only
  (``15-06-2024``, ``05-05-2024``) is a single day-precision date.
* A year alone is year precision, never a day. Several different years, or several different full dates in one name
  (a series, a title containing a year), are ambiguous, so no date is derived at that precision.
* An event comes only from the documented ``Artist - Event - Date`` dash shape or a ``live at/from/@`` marker, stays
  ``inferred``, and records the source token as evidence. The album is not an input, so it cannot become an event.
* No hint means no facts. Nothing is guessed from unrelated tokens.

How often these hints are RIGHT on the real archive is unmeasured; the labelled synthetic set in
``tests/identify/services/test_recording_hints.py`` reports precision and abstention per pattern on invented names only.
"""

from dataclasses import dataclass, field
from datetime import date as CalendarDate
import re

from phaze.tracklist_providers.domain import Fact


_YEAR = r"(?:19|20)\d{2}"
_SEP = r"[-._]"
# A digit run is never entered mid-way: every numeric pattern is fenced by "not a digit" on both sides.
_YEAR_FIRST = re.compile(rf"(?<!\d)({_YEAR})({_SEP})(\d{{1,2}})\2(\d{{1,2}})(?!\d)")
_COMPACT = re.compile(rf"(?<!\d)({_YEAR})(\d{{2}})(\d{{2}})(?!\d)")
_TWO_WAY = re.compile(rf"(?<!\d)(\d{{1,2}})({_SEP})(\d{{1,2}})\2({_YEAR})(?!\d)")
_STANDALONE_YEAR = re.compile(rf"(?<![0-9A-Za-z])({_YEAR})(?![0-9A-Za-z])")
_YEAR_DIRECTORY = re.compile(rf"^{_YEAR}$")
_LIVE_MARKER = re.compile(r"\blive\s+(?:at|from)\s+|@\s*", re.IGNORECASE)
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_EVENT_STOP = re.compile(r"\s+-\s+|[(\[]|(?<!\d)" + _YEAR + r"(?!\d)")
_MAX_EVENT_CHARACTERS = 256


@dataclass(frozen=True)
class NameHints:
    """Facts a file name and path support, with the pattern that produced each (``None`` when it abstained)."""

    date: Fact = field(default_factory=Fact)
    event: Fact = field(default_factory=Fact)
    date_pattern: str | None = None
    event_pattern: str | None = None


def _valid(year: int, month: int, day: int) -> CalendarDate | None:
    try:
        return CalendarDate(year, month, day)
    except ValueError:
        return None


@dataclass(frozen=True)
class _Reading:
    """One date a name can mean: the readings that survive calendar validity, each a day-precision date."""

    pattern: str
    token: str
    days: tuple[CalendarDate, ...]


def _full_date_readings(stem: str) -> list[_Reading]:
    readings: list[_Reading] = []
    for match in _YEAR_FIRST.finditer(stem):
        day = _valid(int(match[1]), int(match[3]), int(match[4]))
        if day is not None:
            readings.append(_Reading("full_date_year_first", match[0], (day,)))
    for match in _COMPACT.finditer(stem):
        day = _valid(int(match[1]), int(match[2]), int(match[3]))
        if day is not None:
            readings.append(_Reading("full_date_compact", match[0], (day,)))
    for match in _TWO_WAY.finditer(stem):
        first, second, year = int(match[1]), int(match[3]), int(match[4])
        days = {d for d in (_valid(year, second, first), _valid(year, first, second)) if d is not None}
        if days:
            readings.append(_Reading("full_date_day_or_month_first", match[0], tuple(sorted(days))))
    return readings


def _date_shaped(stem: str) -> bool:
    return any(pattern.search(stem) for pattern in (_YEAR_FIRST, _COMPACT, _TWO_WAY))


def _date_fact(readings: list[_Reading]) -> tuple[Fact, str] | None:
    """One full date (certain or two-way), or the shared year of several; abstain when they disagree on the year."""
    distinct = {day for reading in readings for day in reading.days}
    if len(distinct) == 1 and all(len(reading.days) == 1 for reading in readings):
        reading = readings[0]
        return Fact(
            value=reading.days[0].isoformat(), certainty="inferred", evidence=(f"name:date={reading.token}", "precision=day")
        ), reading.pattern
    years = {day.year for reading in readings for day in reading.days}
    if len(years) != 1:
        return None
    alternatives = sorted({day.isoformat() for reading in readings for day in reading.days})
    evidence = (
        *(f"name:date={reading.token}" for reading in readings),
        *(f"alt={alternative}" for alternative in alternatives),
        "precision=year",
    )
    pattern = readings[0].pattern if len({reading.pattern for reading in readings}) == 1 else "full_date_ambiguous"
    return Fact(value=f"{years.pop():04d}", certainty="inferred", evidence=evidence[:64]), pattern


def _year_fact(stem: str, directories: list[str]) -> tuple[Fact, str] | None:
    in_name = {m[1] for m in _STANDALONE_YEAR.finditer(stem)}
    if len(in_name) == 1:
        year = in_name.pop()
        return Fact(value=year, certainty="inferred", evidence=(f"name:year={year}", "precision=year")), "year_in_name"
    if in_name:
        return None
    in_path = {part for part in directories if _YEAR_DIRECTORY.fullmatch(part)}
    if len(in_path) == 1:
        year = in_path.pop()
        return Fact(value=year, certainty="inferred", evidence=(f"path:year={year}", "precision=year")), "year_directory"
    return None


def _event_token(text: str) -> str | None:
    """A candidate event token must carry a letter; a bare date, year or number is not an event name."""
    token = text.strip(" -_.,")
    if not token or len(token) > _MAX_EVENT_CHARACTERS or not re.search(r"[^\W\d_]", token):
        return None
    return token


def _live_event(stem: str) -> Fact | None:
    marker = _LIVE_MARKER.search(stem)
    if marker is None:
        return None
    rest = stem[marker.end() :]
    stop = _EVENT_STOP.search(rest)
    token = _event_token(rest[: stop.start()] if stop else rest)
    if token is None:
        return None
    return Fact(value=token, certainty="inferred", evidence=(f"name:live={token}",))


def _dash_event(stem: str) -> Fact | None:
    parts = [part.strip() for part in stem.split(" - ")]
    if len(parts) < 3:
        return None
    token = _event_token(parts[1])
    if token is None or _full_date_readings(token) or _STANDALONE_YEAR.fullmatch(token):
        return None
    return Fact(value=token, certainty="inferred", evidence=(f"name:dash={token}",))


def hints_from_name(file_name: str | None, path: str | None = None) -> NameHints:
    """Infer date and event facts from a file name and its directory path; no hint yields no facts."""
    stem = _EXTENSION.sub("", (file_name or "").strip())
    directories = [part for part in re.split(r"[\\/]", path or "") if part]
    date: tuple[Fact, str] | None = None
    readings = _full_date_readings(stem)
    if readings:
        date = _date_fact(readings)
    # A date-shaped token that is no calendar date (``2024-13-45``) is a malformed date, not a year hint: abstain.
    if date is None and not _date_shaped(stem):
        date = _year_fact(stem, directories)
    live, dash = _live_event(stem), _dash_event(stem)
    event, event_pattern = (live, "live_hint") if live is not None else (dash, "dash_pattern")
    return NameHints(
        date=date[0] if date else Fact(),
        event=event if event is not None else Fact(),
        date_pattern=date[1] if date else None,
        event_pattern=event_pattern if event is not None else None,
    )
