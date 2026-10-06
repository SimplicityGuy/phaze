"""Relative-time and duration formatters: '23s ago', '4m ago', '2h ago', '3d ago'.

UI-SPEC §Relative-Time Helper LOCKS this signature. Pure Python, no deps.

Output table (LOCKED):

    None               → "never"
    delta < 0          → "just now"
    0 <= d < 60        → "{int(d)}s ago"
    60 <= d < 3600     → "{int(d/60)}m ago"
    3600 <= d < 86400  → "{int(d/3600)}h ago"
    d >= 86400         → "{int(d/86400)}d ago"

Format invariants:
- No leading zero ("5s ago" not "05s ago").
- No plural-s suffix ("1s ago" not "1 second ago").
- Single-character unit suffix (s/m/h/d), space before "ago".
- ``int()`` truncates toward zero, NOT round. 89.7s → "89s ago", NOT "1m ago"
  (UI-SPEC line 248 LOCKED).

``format_duration`` (phaze-nwmsu) is the ONE duration format for every surface: ``h:mm:ss`` from an
hour up, ``m:ss`` under one, ``—`` for no value. It is the ``duration`` Jinja filter on every
template environment (``phaze.web.template_globals.register_format_filters``), and the analysis
timeline's axis labels delegate to it so the two cannot drift.

``format_count`` (phaze-dwevc) is the ONE count format: thousands separators (``145,057``). It is the ``thousands``
Jinja filter on every template environment, and ``static/js/format_count.js`` is its byte-for-byte JS twin
(``window.formatCount``) for every Alpine ``x-text`` that renders a live-updated count;
``tests/browser/test_format_count_parity.py`` runs both over one case table.
"""

from __future__ import annotations

from datetime import UTC, datetime
import math
import re


_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def relative_time(dt: datetime | None, *, now: datetime | None = None) -> str:
    """Return a glanceable 'N{s,m,h,d} ago' label for ``dt`` (or 'never' / 'just now').

    The ``now`` kwarg is optional so unit tests pin a deterministic clock; in
    production callers pass ``now=datetime.now(UTC)`` once per render.

    See module docstring for the full LOCKED output table.
    """
    if dt is None:
        return "never"
    reference = now if now is not None else datetime.now(UTC)
    delta_seconds = (reference - dt).total_seconds()
    if delta_seconds < 0:
        return "just now"
    if delta_seconds < _SECONDS_PER_MINUTE:
        return f"{int(delta_seconds)}s ago"
    if delta_seconds < _SECONDS_PER_HOUR:
        return f"{int(delta_seconds // _SECONDS_PER_MINUTE)}m ago"
    if delta_seconds < _SECONDS_PER_DAY:
        return f"{int(delta_seconds // _SECONDS_PER_HOUR)}h ago"
    return f"{int(delta_seconds // _SECONDS_PER_DAY)}d ago"


# What a metric with no data renders as: an em dash, never ``0`` / ``0%``.
NO_DATA = "—"


def format_duration(seconds: float | int | str | None) -> str:
    """``h:mm:ss`` (an hour or more) or ``m:ss`` (under one), rounded to the whole second.

    ``None``, a non-numeric value, ``NaN`` and infinities render as :data:`NO_DATA`. A negative
    value clamps to ``0:00`` (a clock skew must not print a minus sign into a duration column).
    Hours are never rolled into days: 25 h renders ``25:00:00``.
    """
    if seconds is None or isinstance(seconds, bool):
        return NO_DATA
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return NO_DATA
    if not math.isfinite(value):
        return NO_DATA
    whole = max(0, round(value))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


_INTEGER_TEXT = re.compile(r"[+-]?\d+")


def format_count(value: object) -> str:
    """An integer count with ``,`` thousands separators; ``NO_DATA`` for no value.

    * ``None``, a bool, ``NaN`` and infinities render as :data:`NO_DATA` -- a count with no data is never ``0``.
    * An int, an integral float (``1000.0``) and a string of digits (``"1000"``) are grouped: ``1,000``.
    * Anything else is returned as its own text, ungrouped: a non-integral float (``12.5``) and a string that is not
      an integer (``"12+"``, the "at least" marker) keep their text.

    Only COUNTS go through this. Years, ids, ports, versions, codec bitrates, percentages, page numbers, byte sizes,
    durations and paths are not counts and are rendered as they are.
    """
    if value is None or isinstance(value, bool):
        return NO_DATA
    number: int | float
    if isinstance(value, str):
        text = value.strip()
        if not _INTEGER_TEXT.fullmatch(text):
            return value
        number = int(text)
    elif isinstance(value, (int, float)):
        number = value
    else:
        return str(value)
    if isinstance(number, float):
        if not math.isfinite(number):
            return NO_DATA
        if not number.is_integer():
            return str(number)
        number = int(number)
    return f"{number:,}"
