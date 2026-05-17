"""Relative-time formatter: '23s ago', '4m ago', '2h ago', '3d ago'.

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
"""

from __future__ import annotations

from datetime import UTC, datetime


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
