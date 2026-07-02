"""Tests for phaze.utils.humanize.relative_time per UI-SPEC §Relative-Time Helper LOCKED.

Output table (UI-SPEC LOCKED):
    None               → "never"
    delta < 0          → "just now"
    0 <= d < 60        → "{int(d)}s ago"
    60 <= d < 3600     → "{int(d/60)}m ago"
    3600 <= d < 86400  → "{int(d/3600)}h ago"
    d >= 86400         → "{int(d/86400)}d ago"

Truncation rule: int() truncates toward zero, NOT round.
UI-SPEC line 248 explicit: 89.7s → "89s ago", NOT "1m ago".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from phaze.utils.humanize import relative_time


NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Special cases: None, negative delta (future)
# ---------------------------------------------------------------------------


def test_relative_time_none_returns_never() -> None:
    """None dt → 'never' (matches the 'never' pill state)."""
    assert relative_time(None, now=NOW) == "never"


def test_relative_time_negative_delta_returns_just_now() -> None:
    """Future timestamp (clock skew) → 'just now'."""
    future = NOW + timedelta(seconds=10)
    assert relative_time(future, now=NOW) == "just now"


def test_relative_time_zero_delta_returns_zero_seconds() -> None:
    """delta == 0 → '0s ago' (NOT 'just now' which is only for future)."""
    assert relative_time(NOW, now=NOW) == "0s ago"


# ---------------------------------------------------------------------------
# Boundary table — locked output rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta_seconds", "expected"),
    [
        # seconds bucket: 0 <= d < 60
        (0, "0s ago"),
        (1, "1s ago"),
        (5, "5s ago"),
        (23, "23s ago"),
        (59, "59s ago"),
        # 60s boundary — first 'm' result
        (60, "1m ago"),
        (61, "1m ago"),
        (119, "1m ago"),
        (120, "2m ago"),
        # 3599s boundary — last 'm' result
        (3599, "59m ago"),
        # 3600s boundary — first 'h' result
        (3600, "1h ago"),
        (3601, "1h ago"),
        (7200, "2h ago"),
        # 86399s boundary — last 'h' result
        (86399, "23h ago"),
        # 86400s boundary — first 'd' result
        (86400, "1d ago"),
        (86401, "1d ago"),
        (172800, "2d ago"),
        (259200, "3d ago"),
    ],
)
def test_relative_time_boundaries(delta_seconds: int, expected: str) -> None:
    """All output bucket boundaries (UI-SPEC LOCKED)."""
    dt = NOW - timedelta(seconds=delta_seconds)
    assert relative_time(dt, now=NOW) == expected


# ---------------------------------------------------------------------------
# Truncation rule: int() truncates toward zero, NOT round
# ---------------------------------------------------------------------------


def test_relative_time_truncates_not_rounds_within_seconds_bucket() -> None:
    """UI-SPEC truncation rule: int() truncates toward zero, NOT round.

    UI-SPEC LOCKED line 248 spells the rule with a "89.7s → '89s ago'" example,
    but 89.7s lies in the [60, 3600) minutes bucket per the LOCKED output
    table on lines 232-241 (the table is the authoritative contract). The
    truncation rule itself is verified here with a value INSIDE the seconds
    bucket so both LOCKED invariants hold: ``int()`` truncates (59.7 → 59,
    not round to 60 which would cross the bucket boundary), AND the
    [0, 60) seconds bucket is respected. This is the Rule-1 reconciliation
    of the UI-SPEC documentation defect (the 89.7 prose example is
    internally inconsistent with its own bucket table — see plan 29-07
    SUMMARY deviation log).
    """
    dt = NOW - timedelta(seconds=59.7)
    assert relative_time(dt, now=NOW) == "59s ago"


def test_relative_time_truncates_fractional_minutes() -> None:
    """61.9s → '1m ago' (truncated; 61.9/60 = 1.03 → 1)."""
    dt = NOW - timedelta(seconds=61.9)
    assert relative_time(dt, now=NOW) == "1m ago"


def test_relative_time_truncates_fractional_hours() -> None:
    """3600 + 1800 = 5400s = 1.5h → '1h ago' (truncated, not rounded to 2h)."""
    dt = NOW - timedelta(seconds=5400)
    assert relative_time(dt, now=NOW) == "1h ago"


def test_relative_time_truncates_fractional_days() -> None:
    """1.5d = 129600s → '1d ago' (truncated, not rounded to 2d)."""
    dt = NOW - timedelta(seconds=129600)
    assert relative_time(dt, now=NOW) == "1d ago"


# ---------------------------------------------------------------------------
# Default now=None branch (uses datetime.now(UTC))
# ---------------------------------------------------------------------------


def test_relative_time_default_now_returns_just_now_for_recent_dt() -> None:
    """When `now=None`, uses datetime.now(UTC). A very recent dt is < 60s away."""
    # Construct a dt that is "just before" the current wall clock so the test
    # works regardless of when it runs. Use 1 second ago.
    from datetime import datetime as _datetime_for_now

    dt = _datetime_for_now.now(UTC) - timedelta(seconds=1)
    out = relative_time(dt)
    # Allow either '1s ago' or '0s ago' / '2s ago' depending on wall-clock slack.
    assert out.endswith("s ago")


# ---------------------------------------------------------------------------
# Format invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delta", [1, 60, 3600, 86400])
def test_relative_time_no_plural_s_suffix(delta: int) -> None:
    """No 'seconds'/'minutes'/'hours'/'days' word — compact unit char only."""
    dt = NOW - timedelta(seconds=delta)
    out = relative_time(dt, now=NOW)
    for word in ("seconds", "minutes", "hours", "days", "second", "minute", "hour", "day"):
        assert word not in out, f"output {out!r} should not contain {word!r}"


def test_relative_time_unit_char_is_single_letter() -> None:
    """Every non-special output ends with 's ago', 'm ago', 'h ago', or 'd ago'."""
    for delta in (5, 60, 3600, 86400):
        out = relative_time(NOW - timedelta(seconds=delta), now=NOW)
        assert out.endswith(" ago")
        unit = out.split(" ")[0][-1]
        assert unit in {"s", "m", "h", "d"}
