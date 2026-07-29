"""Asyncio-owned debouncer for the always-on watcher (Phase 27 D-01, D-02).

State machine:
    touch(path)    -- insert or refresh ``_PendingEntry`` keyed by absolute path
    sweep(...)     -- emit ready paths (settle_period elapsed) and evict
                      stuck paths (older than max_pending)
    pending_count() -- observability hook (debug logging / metrics)

THREAD-SAFETY INVARIANT (RESEARCH Pitfall 2):
    The internal ``_pending`` dict is **asyncio-owned**. It MUST be mutated only
    from the asyncio event-loop thread. The watchdog Observer thread reaches
    ``touch`` exclusively via ``loop.call_soon_threadsafe(...)``, scheduled by
    :class:`phaze.agent_watcher.observer.WatcherEventHandler`. Never call
    ``touch`` from the watchdog thread directly; never access ``_pending``
    from outside the asyncio loop.

Time source:
    ``time.monotonic()`` -- guaranteed non-decreasing, immune to wall-clock
    adjustments (NTP, DST). Tests use the ``fake_clock`` fixture in
    ``tests/discovery/agent_watcher/conftest.py`` to drive deterministic time.
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

    Capacity (D-02): the pending dict has NO hard cap. Adversarial filesystem
    activity (e.g., a rename loop) is contained by the stuck-file eviction in
    ``sweep`` -- any entry that fails to settle within TWO consecutive
    ``max_pending`` windows (phaze-kw36's one-shot grace extension, see
    ``sweep``) is dropped from the dict WITHOUT being posted, yielding bounded
    memory growth in the worst case (T-27-05 mitigation).
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingEntry] = {}

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

    def sweep(self, settle_period: float, max_pending: float) -> tuple[list[str], list[str]]:
        """Emit settled paths and evict stuck paths in a single pass.

        Returns ``(ready, evicted)``:
            ready    -- paths whose ``last_change_at`` is >= ``settle_period``
                        seconds in the past (caller MUST post them).
            evicted  -- paths whose ``first_seen_at`` is > ``max_pending`` seconds
                        in the past, that have not settled, AND that have
                        already used their one-shot cap grace extension (D-02
                        stuck-file cap; NEVER posted).

        Settledness is checked BEFORE the stuck-file cap (phaze-w27e). A path
        whose ``last_change_at`` is already >= ``settle_period`` in the past is
        always ``ready``, even if its ``first_seen_at`` is also older than
        ``max_pending`` -- it is quiet, not stuck. The cap's documented intent
        (module docstring, class docstring) is to contain a path whose mtime
        keeps changing (e.g. a rename loop) and therefore NEVER satisfies the
        settle check; checking eviction first misfired on entries that had
        simply settled but were not yet swept (e.g. a sweep loop stalled for
        over an hour behind serial multi-GB hashing elsewhere in the pipeline
        -- see ``_sweep_loop`` in ``agent_watcher/__main__.py``), silently
        dropping ready files at the cap instead of posting them.

        phaze-kw36: "not settled" does not imply "still changing forever" -- it
        also matches an entry whose LAST write landed inside the final
        ``settle_period`` before the ``max_pending`` crossing (e.g. a multi-hour
        file copy that finishes seconds before the cap). Hard-dropping on the
        very first cap crossing evicted that legitimately-completed write and,
        since no further filesystem event ever re-inserts a completed path, it
        was silently never posted. The cap branch now gives each entry ONE
        grace extension: the first time it is found not-settled-but-capped,
        ``first_seen_at`` is reset to ``now`` (via ``cap_grace_used``) instead
        of evicting, buying it a full fresh ``max_pending`` window -- vastly
        more than any realistic ``settle_period`` -- to either go quiet and
        settle on a later sweep, or keep churning. A path that is genuinely
        still changing (e.g. a rename loop) never satisfies the settle check
        and is evicted the SECOND time it crosses the cap; a completed write
        settles well within its grace window and is posted, never evicted.

        Both buckets are removed from the pending dict before return. The
        list-snapshot iteration pattern is the canonical safe-mutation idiom
        -- Pitfall 2 mitigation against dict-size-changed RuntimeError on
        Python 3.13.
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
                else:
                    entry.cap_grace_used = True
                    entry.first_seen_at = now
        return ready, evicted

    def pending_count(self) -> int:
        """Number of entries currently awaiting settlement (observability hook)."""
        return len(self._pending)
