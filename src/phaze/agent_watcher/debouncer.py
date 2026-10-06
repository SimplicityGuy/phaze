"""Asyncio-owned debouncer for the always-on watcher.

Thread-safety invariant:
    The internal ``_pending`` dict is **asyncio-owned**. It MUST be mutated only
    from the asyncio event-loop thread. The watchdog Observer thread reaches
    ``touch`` exclusively via ``loop.call_soon_threadsafe(...)``, scheduled by
    :class:`phaze.agent_watcher.observer.WatcherEventHandler`. Never call
    ``touch`` from the watchdog thread directly; never access ``_pending``
    from outside the asyncio loop.

Time source:
    ``time.monotonic()`` -- guaranteed non-decreasing, immune to wall-clock
    adjustments such as NTP and DST.
"""

from __future__ import annotations

from dataclasses import dataclass
import time

import structlog


logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class _PendingEntry:
    """Per-path state captured at first touch and refreshed on each subsequent touch."""

    first_seen_at: float
    last_change_at: float
    cap_grace_used: bool = False
    """phaze-kw36: set once this entry has already been given one grace extension at the
    ``max_pending`` cap (see ``Debouncer.sweep``). ``touch`` never resets this flag -- only
    a genuinely fresh entry (post-eviction re-insertion) starts with it ``False`` again.
    """


class Debouncer:
    """Coalesce a stream of filesystem events into one post-per-settled-path.

    Backing store is a plain ``dict[str, _PendingEntry]``. ``sweep`` iterates a
    list-snapshot of the items view to permit safe in-loop deletion -- avoiding
    ``RuntimeError: dictionary changed size during iteration``.

    The dictionary has no count cap. A path that fails to settle across two
    consecutive ``max_pending`` windows is evicted, bounding growth by time.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingEntry] = {}
        # phaze-oxn2m: path -> the paths it was moved away from, oldest first. Held from the move
        # until the caller ``forget``s it after posting, so it outlives the pending entry for the
        # whole time that path's POST is in flight (see ``move``).
        self._lineage: dict[str, list[str]] = {}

    def touch(self, path: str) -> None:
        """Record a file-change event for ``path``.

        - First touch:        inserts an entry with ``first_seen_at = last_change_at = now``.
        - Subsequent touches: refreshes ``last_change_at`` only; ``first_seen_at``
                              is the anchor for the stuck-file cap (D-02).
        """
        now = time.monotonic()
        entry = self._pending.get(path)
        if entry is None:
            self._pending[path] = _PendingEntry(first_seen_at=now, last_change_at=now)
        else:
            entry.last_change_at = now

    def move(self, src: str, dest: str) -> None:
        """Record an in-tree rename ``src -> dest`` (phaze-oxn2m).

        ``dest`` is touched like any changed path, and remembers ``src`` -- plus whatever ``src``
        itself had been moved from -- so that when ``dest`` settles the controller can find the row
        the file was already posted under and re-point it instead of adding a second one.

        - ``src`` still pending (never posted under that name): its entry is dropped -- the name no
          longer exists, so posting it could only ENOENT -- and its own lineage passes to ``dest``.
        - ``src``'s POST already in flight: its lineage is still held (the caller only
          ``forget``s after the post), so ``dest`` inherits it too. The controller then finds the
          row whichever way that in-flight post ended: re-pointed to ``src``, or still at an older
          name because ``src`` vanished before it was hashed.
        - ``src`` posted long ago: ``dest`` carries just ``[src]``.

        Ordering against an in-flight post is structural, not timed: ``dest`` cannot be ready
        before a full settle period, and the sweep loop awaits each post before sweeping again.
        """
        lineage = [*self._lineage.get(src, ()), src]
        if self._pending.pop(src, None) is not None:
            self._lineage.pop(src, None)
        self.touch(dest)
        self._lineage[dest] = lineage

    def lineage(self, path: str) -> list[str]:
        """Paths ``path`` was moved away from since it last settled, oldest first; empty if none."""
        return list(self._lineage.get(path, ()))

    def forget(self, path: str) -> None:
        """Drop ``path``'s lineage once its post has finished, successfully or not."""
        self._lineage.pop(path, None)

    def sweep(self, settle_period: float, max_pending: float) -> tuple[list[str], list[str]]:
        """Emit settled paths and evict stuck paths in a single pass.

        Returns ``(ready, evicted)``:
            ready    -- paths whose ``last_change_at`` is >= ``settle_period``
                        seconds in the past (caller MUST post them).
            evicted  -- paths whose ``first_seen_at`` is > ``max_pending`` seconds
                        in the past, that have not settled, AND that have
                        already used their one-shot cap grace extension (D-02
                        stuck-file cap; NEVER posted).

        Settledness is checked before age: a quiet path is ready even after an
        unusually delayed sweep. The first age crossing grants one full grace
        window because a long copy may have finished only seconds earlier; only
        the second unsettled crossing evicts it. Both result buckets are removed
        while iterating a list snapshot, so dictionary mutation is safe.
        """
        now = time.monotonic()
        ready: list[str] = []
        evicted: list[str] = []
        for path, entry in list(self._pending.items()):
            if now - entry.last_change_at >= settle_period:
                ready.append(path)
                del self._pending[path]
            elif now - entry.first_seen_at > max_pending:
                if entry.cap_grace_used:
                    evicted.append(path)
                    del self._pending[path]
                    self._lineage.pop(path, None)
                else:
                    entry.cap_grace_used = True
                    entry.first_seen_at = now
        return ready, evicted

    def pending_count(self) -> int:
        """Number of entries currently awaiting settlement (observability hook)."""
        return len(self._pending)
