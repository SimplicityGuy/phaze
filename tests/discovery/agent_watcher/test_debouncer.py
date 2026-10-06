"""Unit tests for phaze.agent_watcher.debouncer (Phase 27 D-01, D-02, Pitfall 2).

Five behaviors mirror 27-PATTERNS.md lines 1204-1209:

1. touch() inserts a new entry when path is unseen.
2. touch() resets last_change_at (NOT first_seen_at) on a re-touch.
3. sweep() returns ready paths once settle_period has elapsed since last_change_at.
4. sweep() evicts entries older than max_pending without posting (D-02 cap) --
   but only entries that have NOT also settled (phaze-w27e: settledness is
   checked first, so a quiet-but-unswept entry is always posted).
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
    """An entry that never settles across TWO consecutive max_pending crossings is evicted.

    Simulates a genuinely stuck path (e.g. a rename loop): it is re-touched
    so ``last_change_at`` stays recent (never satisfies ``settle_period``)
    while ``first_seen_at`` keeps getting reset by the phaze-kw36 one-shot
    grace extension. The first cap crossing only arms the grace flag and
    resets ``first_seen_at``; eviction requires still-not-settled at a
    SECOND cap crossing after the reset (see test_sweep_grace_extension_*
    below for the single-crossing, settles-in-time case this distinguishes
    from).
    """
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/a.mp3")

    fake_clock(3600.5)
    d.touch("/a.mp3")  # keeps last_change_at recent; first_seen_at stays 0.0

    fake_clock(3601.0)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    # First crossing: grace extension armed, NOT evicted yet.
    assert ready == []
    assert evicted == []
    assert d.pending_count() == 1

    # Keep churning across a second full max_pending window from the reset first_seen_at (3601.0).
    fake_clock(7200.5)
    d.touch("/a.mp3")  # keeps last_change_at recent; still never settles

    fake_clock(7201.5)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    assert ready == []
    assert evicted == ["/a.mp3"]
    assert d.pending_count() == 0


def test_sweep_grace_extension_saves_completed_write_in_final_settle_window(
    fake_clock: Callable[[float], None],
) -> None:
    """phaze-kw36 regression: a write whose LAST event lands just inside the cap is not lost.

    Reproduces the exact failure scenario: a long-running copy's final Modified event
    lands 6s before the entry's age crosses max_pending (settle_period=10s). The first
    sweep after the crossing must NOT evict the path -- no further events arrive (the
    write is complete), so a later sweep must find it settled and post it.
    """
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/a.mp3")

    fake_clock(3600.5)
    d.touch("/a.mp3")  # the copy's final write landed here -- write is now complete

    fake_clock(3601.0)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    # Would have been wrongly evicted before the fix: now - last_change_at = 0.5 < 10
    # (not ready) and now - first_seen_at = 3601 > 3600 (capped). The grace extension
    # keeps it pending instead of dropping it.
    assert ready == []
    assert evicted == []
    assert d.pending_count() == 1

    # No further filesystem events -- the write is done. Once settle_period has
    # elapsed since the last (and only remaining) change, the entry must be posted.
    fake_clock(3611.5)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    assert ready == ["/a.mp3"]
    assert evicted == []
    assert d.pending_count() == 0


def test_sweep_grace_extension_is_one_shot_per_entry(fake_clock: Callable[[float], None]) -> None:
    """cap_grace_used does not reset just because a touch refreshed last_change_at.

    Once an entry has used its one grace extension, a still-churning entry must be
    evicted on its NEXT cap crossing rather than being granted unlimited extensions
    (which would defeat the D-02 bounded-memory containment goal).
    """
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/a.mp3")

    fake_clock(3600.5)
    d.touch("/a.mp3")  # keeps last_change_at recent so the first crossing does not settle

    fake_clock(3601.0)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)
    assert ready == []
    assert evicted == []  # grace extension #1: first_seen_at reset to 3601.0

    # Keep churning right up to (and past) the second cap crossing so the entry never settles.
    fake_clock(7200.5)
    d.touch("/a.mp3")  # keeps last_change_at recent; first_seen_at stays 3601.0

    fake_clock(7201.5)  # > 3601.0 + 3600.0
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    assert ready == []
    assert evicted == ["/a.mp3"]
    assert d.pending_count() == 0


def test_sweep_returns_settled_entry_even_past_max_pending(fake_clock: Callable[[float], None]) -> None:
    """phaze-w27e regression: a settled-but-unswept entry is posted, not evicted.

    An entry can be quiet (``last_change_at`` far in the past, satisfying
    ``settle_period``) yet also have a ``first_seen_at`` older than
    ``max_pending`` -- this happens when a sweep iteration stalls behind
    something slow elsewhere in the pipeline (e.g. serial multi-GB sha256
    hashing) for longer than ``max_pending``. Settledness must win: the file
    is genuinely ready, not stuck, and must never be silently dropped.
    """
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/a.mp3")

    fake_clock(5.0)
    # File goes quiet after this -- no further touches.

    fake_clock(3700.0)  # sweep stalled well past max_pending before running
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    assert ready == ["/a.mp3"]
    assert evicted == []
    assert d.pending_count() == 0


def test_sweep_settled_check_takes_precedence_at_exact_boundary(fake_clock: Callable[[float], None]) -> None:
    """When both conditions are simultaneously true, settled (ready) wins over stuck (evicted)."""
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/a.mp3")

    # now - first_seen_at > max_pending AND now - last_change_at >= settle_period,
    # since the path was never re-touched.
    fake_clock(3601.0)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)

    assert ready == ["/a.mp3"]
    assert evicted == []


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


# phaze-oxn2m: Debouncer.move carries the names a file was moved away from, so the poster can ask
# the controller to re-point the row it was already posted under instead of adding a second one.


def test_move_of_a_long_settled_path_debounces_dest_with_src_as_lineage(fake_clock: Callable[[float], None]) -> None:
    fake_clock(0.0)
    d = Debouncer()

    d.move("/in/incomplete/rel/a.mp3", "/in/rel/a.mp3")

    assert d.pending_count() == 1
    assert d.lineage("/in/rel/a.mp3") == ["/in/incomplete/rel/a.mp3"]
    fake_clock(10.5)
    assert d.sweep(settle_period=10.0, max_pending=3600.0) == (["/in/rel/a.mp3"], [])


def test_move_while_src_is_still_pending_drops_src_so_only_dest_is_ever_posted(fake_clock: Callable[[float], None]) -> None:
    """Race 1: the download is still being written when the client moves it. ``src`` was never
    posted, so the only post is ``dest`` -- carrying ``src`` in case a row exists under it anyway."""
    fake_clock(0.0)
    d = Debouncer()
    d.touch("/in/incomplete/a.mp3")

    fake_clock(3.0)
    d.move("/in/incomplete/a.mp3", "/in/a.mp3")

    fake_clock(20.0)
    ready, evicted = d.sweep(settle_period=10.0, max_pending=3600.0)
    assert ready == ["/in/a.mp3"]
    assert evicted == []
    assert d.lineage("/in/a.mp3") == ["/in/incomplete/a.mp3"]
    assert d.lineage("/in/incomplete/a.mp3") == []


def test_chained_moves_before_settling_accumulate_the_lineage_oldest_first(fake_clock: Callable[[float], None]) -> None:
    fake_clock(0.0)
    d = Debouncer()
    d.move("/in/a.mp3", "/in/b.mp3")
    d.move("/in/b.mp3", "/in/c.mp3")

    assert d.pending_count() == 1
    assert d.lineage("/in/c.mp3") == ["/in/a.mp3", "/in/b.mp3"]
    assert d.lineage("/in/b.mp3") == []


def test_move_while_src_post_is_in_flight_inherits_its_lineage(fake_clock: Callable[[float], None]) -> None:
    """Race 2: ``b`` was swept and its POST is in flight when ``b -> c`` arrives. ``b``'s lineage is
    still held, so ``c`` carries both names: the controller finds the row whether ``b``'s post
    re-pointed it to ``b`` or ``b`` vanished before it was hashed and the row is still at ``a``."""
    fake_clock(0.0)
    d = Debouncer()
    d.move("/in/a.mp3", "/in/b.mp3")
    fake_clock(10.5)
    assert d.sweep(settle_period=10.0, max_pending=3600.0)[0] == ["/in/b.mp3"]

    d.move("/in/b.mp3", "/in/c.mp3")  # arrives while b's post is in flight
    assert d.lineage("/in/b.mp3") == ["/in/a.mp3"], "the in-flight post of b still needs its own lineage"
    d.forget("/in/b.mp3")  # b's post finished

    assert d.lineage("/in/c.mp3") == ["/in/a.mp3", "/in/b.mp3"]
    assert d.lineage("/in/b.mp3") == []


def test_forget_and_eviction_release_lineage(fake_clock: Callable[[float], None]) -> None:
    fake_clock(0.0)
    d = Debouncer()
    d.move("/in/a.mp3", "/in/b.mp3")
    d.forget("/in/b.mp3")
    assert d.lineage("/in/b.mp3") == []

    d.move("/in/x.mp3", "/in/y.mp3")
    for t in (1.0, 3601.0, 7202.5):  # keeps changing: the first cap crossing grants grace, the second evicts
        fake_clock(t)
        d.touch("/in/y.mp3")
        d.sweep(settle_period=10.0, max_pending=3600.0)
    assert d.pending_count() == 0
    assert d.lineage("/in/y.mp3") == []
