"""A resizable, FIFO async capacity limiter (``phaze-mvq8z.7``).

``asyncio.Semaphore`` has no way to change its ceiling once constructed, and the runtime-config
hot-reload epic (``docs/design/0019-runtime-config-hot-reload.md`` §5/§7) needs exactly that: an
operator raising or lowering ``worker_process_pool_size`` on a running agent worker must take
effect for new work without restarting the process and without touching a file already in flight.

**Semantics mirror trio/anyio's ``CapacityLimiter.total_tokens`` setter** (the ADR's own
vocabulary), reimplemented on plain ``asyncio`` rather than by adding ``anyio``/``trio`` as a
direct dependency: this repo already carries ``anyio`` transitively (via httpx/starlette), never
imports it directly, and CLAUDE.md's technology-stack section treats an added dependency as never
free -- reimplementing ~80 lines against the stdlib's own ``asyncio.Semaphore`` (whose FIFO-waiter
bookkeeping this class deliberately follows) costs less than a new pin would.

* **Grow** (:meth:`resize` to a larger ``total_tokens``) wakes as many FIFO-queued waiters as the
  new ceiling admits, immediately -- new work starts without waiting for anything to release.
* **Shrink** (:meth:`resize` to a smaller ``total_tokens``) only lowers the ceiling. It NEVER
  revokes a token already held: a holder finishes and calls :meth:`release` on its own schedule,
  same as always. No new acquire is admitted until :attr:`borrowed_tokens` drops back under the
  new ceiling -- draining by attrition, never a forced kill or cancellation of in-flight work.
  This is what ADR §5 means by "shrink happens by attrition".

Used as an async context manager, exactly like ``asyncio.Semaphore``::

    limiter = ResizableLimiter(cfg.worker_process_pool_size)
    async with limiter:
        ...  # bounded work
    limiter.resize(new_total)  # applier-driven, on a reload (phaze-mvq8z.4's register_applier)
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Self


if TYPE_CHECKING:
    from types import TracebackType


class ResizableLimiter:
    """A FIFO capacity limiter whose ``total_tokens`` ceiling can change live.

    Not thread-safe (nothing here is -- like ``asyncio.Semaphore``, every method must run on the
    loop that owns it) and not reentrant. FIFO ordering follows ``asyncio.Semaphore.acquire``'s
    own fast-path rule: even when tokens are available, a new acquire queues behind any waiter
    already ahead of it rather than jumping the line.
    """

    def __init__(self, total_tokens: int) -> None:
        if total_tokens < 1:
            raise ValueError(f"total_tokens must be >= 1, got {total_tokens}")
        self._total = total_tokens
        self._borrowed = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    def __repr__(self) -> str:
        state = "locked" if self.locked() else f"unlocked, available:{self.available_tokens}"
        extra = f"{state}, total:{self._total}, borrowed:{self._borrowed}"
        if self._waiters:
            extra = f"{extra}, waiters:{len(self._waiters)}"
        return f"<{type(self).__name__} [{extra}]>"

    @property
    def total_tokens(self) -> int:
        """The current ceiling. Changed live only through :meth:`resize`."""
        return self._total

    @property
    def borrowed_tokens(self) -> int:
        """How many tokens are currently held (in-flight acquires not yet released)."""
        return self._borrowed

    @property
    def waiting(self) -> int:
        """How many acquires are currently queued behind the ceiling. Test/introspection surface."""
        return len(self._waiters)

    @property
    def available_tokens(self) -> int:
        """Tokens free to hand out right now. Never negative -- a mid-shrink deficit reads as 0."""
        return max(0, self._total - self._borrowed)

    def locked(self) -> bool:
        """Whether an immediate :meth:`acquire` would have to wait.

        True whenever no token is free, OR a non-cancelled waiter is already queued (FIFO: a
        fresh acquire must not overtake one already waiting, even if a token just freed up).
        """
        return self.available_tokens <= 0 or any(not waiter.cancelled() for waiter in self._waiters)

    def resize(self, total_tokens: int) -> None:
        """Change the ceiling. Growing wakes waiters now; shrinking only lowers the bar.

        A shrink never touches a token already borrowed -- the excess drains as each holder
        calls :meth:`release` on its own. See the module docstring for the full contract this
        implements (docs/design/0019-runtime-config-hot-reload.md §5/§7).
        """
        if total_tokens < 1:
            raise ValueError(f"total_tokens must be >= 1, got {total_tokens}")
        self._total = total_tokens
        self._wake_eligible_waiters()

    async def acquire(self) -> None:
        """Borrow one token, waiting FIFO-fair if none is free under the current ceiling."""
        if not self.locked():
            # Maintain FIFO: no waiters ahead, and a token is free under the current ceiling.
            self._borrowed += 1
            return

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append(fut)
        try:
            try:
                await fut
            finally:
                # A cancelled wait may already have been removed by _wake_eligible_waiters
                # granting it a token before the cancellation was delivered; tolerate that.
                if fut in self._waiters:
                    self._waiters.remove(fut)
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # We were granted a token but are not actually taking it (cancelled after the
                # grant raced delivery) -- give it back and try to wake someone else.
                self._borrowed -= 1
            raise
        finally:
            # New waiters may have queued behind this one while it was pending; let resize()
            # or a release() that landed mid-wait wake as many of them as the ceiling allows.
            self._wake_eligible_waiters()

    def release(self) -> None:
        """Return a borrowed token. Wakes the next FIFO waiter if the ceiling now allows it."""
        if self._borrowed <= 0:
            raise ValueError("release() called more times than acquire()")
        self._borrowed -= 1
        self._wake_eligible_waiters()

    def _wake_eligible_waiters(self) -> None:
        """Grant tokens to as many queued waiters as :attr:`available_tokens` now permits."""
        while self.available_tokens > 0 and self._wake_next():
            pass

    def _wake_next(self) -> bool:
        """Grant a token to the first non-done waiter, removing it from the queue immediately
        (rather than leaving a granted-but-not-yet-resumed future to linger there until its
        own ``acquire()`` call cleans up) -- so :attr:`waiting` always counts only futures
        that are genuinely still waiting. Returns whether one was found.
        """
        for fut in self._waiters:
            if not fut.done():
                self._waiters.remove(fut)
                self._borrowed += 1
                fut.set_result(None)
                return True
        return False

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
