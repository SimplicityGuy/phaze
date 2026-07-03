"""Unit tests for phaze.agent_watcher.debouncer (Phase 27 D-01, D-02, Pitfall 2).

Five behaviors mirror 27-PATTERNS.md lines 1204-1209:

1. touch() inserts a new entry when path is unseen.
2. touch() resets last_change_at (NOT first_seen_at) on a re-touch.
3. sweep() returns ready paths once settle_period has elapsed since last_change_at.
4. sweep() evicts entries older than max_pending without posting (D-02 cap).
5. sweep() leaves un-settled entries in the pending set.

All time movement is driven by the ``fake_clock`` fixture from
``tests/test_agent_watcher/conftest.py`` (monkeypatched ``time.monotonic``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from phaze.agent_watcher.debouncer import Debouncer


if TYPE_CHECKING:
    from collections.abc import Callable


def test_touch_inserts_new_entry(fake_clock: Callable[[float], None]) -> None:
    """A fresh path touch creates a new pending entry."""
    fake_clock(0.0)
    d = Debouncer()

    d.touch("/a.mp3")

    assert d.pending_count() == 1


def test_touch_resets_last_change_at(fake_clock: Callable[[float], None]) -> None:
    """Re-touching a known path updates last_change_at, leaves first_seen_at."""
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/a.mp3")

    fake_clock(5.0)
    d.touch("/a.mp3")

    # Reach into the pending dict to confirm both timestamps.
    entry = d._pending["/a.mp3"]
    assert entry.first_seen_at == 0.0
    assert entry.last_change_at == 5.0
    assert d.pending_count() == 1


def test_sweep_returns_ready_after_settle(fake_clock: Callable[[float], None]) -> None:
    """Settle period elapsed -> entry returned in `ready` list, evicted from pending."""
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/a.mp3")

    fake_clock(10.5)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    assert ready == ["/a.mp3"]
    assert evicted == []
    assert d.pending_count() == 0


def test_sweep_evicts_stuck_entries(fake_clock: Callable[[float], None]) -> None:
    """An entry older than max_pending is evicted (no post) -- D-02 stuck-file cap."""
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/a.mp3")

    fake_clock(3601.0)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    assert ready == []
    assert evicted == ["/a.mp3"]
    assert d.pending_count() == 0


def test_sweep_does_not_return_unsettled_entry(fake_clock: Callable[[float], None]) -> None:
    """If now - last_change_at < settle_period, the entry stays pending."""
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/a.mp3")

    fake_clock(5.0)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    assert ready == []
    assert evicted == []
    assert d.pending_count() == 1
