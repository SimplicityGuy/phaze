"""HYG-01 orphan-cache unit contract (Phase 91, Plan 01 -- Wave-0 scaffold, DB-free).

These are the failing tests plan 91-02 implements against (Nyquist: test-first). They encode the
process-local cache contract for the per-enrich-stage orphan count that plan 91-02 will add to
``phaze.services.pipeline``:

* **O(1) copy-on-read (D-04)** -- ``get_cached_stage_orphan_counts()`` returns a dict *equal to* the
  module cache but a *distinct object*, so a caller mutating the return can never corrupt the cache.
* **refresh updates the value** -- ``await refresh_stage_orphan_counts()`` recomputes off-request (via
  the raising ``_compute_stage_orphan_counts``) and rebinds the cache to the fresh result.
* **degrade never poisons (D-03)** -- when the compute core raises, ``refresh_stage_orphan_counts``
  propagates the error (the background loop, not this function, swallows it) AND the cache keeps its
  last-good value -- it is NEVER stamped back to all-zeros.
* **fresh seed** -- before any successful refresh, the cache reads ``{metadata,analyze}: 0``.

The module is imported at top level so collection succeeds, but every new symbol
(``get_cached_stage_orphan_counts`` / ``refresh_stage_orphan_counts`` / ``_orphan_cache`` /
``_compute_stage_orphan_counts``) is touched only INSIDE test bodies / fixtures. Pre-91-02 runs raise
``AttributeError`` (clean RED); they go GREEN once 91-02 lands the symbols. DB-free: the refresh path's
``phaze.database.async_session`` is monkeypatched to a no-op async context manager, and the compute core
is monkeypatched, so no real session or PostgreSQL is ever touched (``shared`` bucket).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest

from phaze.services.pipeline import orphans as pipeline


if TYPE_CHECKING:
    from collections.abc import Iterator


_ALL_ZEROS = {"metadata": 0, "analyze": 0}


class _FakeSession:
    """A no-op async context manager standing in for a real AsyncSession (never queried)."""

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _fake_async_session(*_args: object, **_kwargs: object) -> _FakeSession:
    return _FakeSession()


@pytest.fixture(autouse=True)
def _isolate_orphan_cache() -> Iterator[None]:
    """Snapshot + restore the module-scope cache around each test so refresh rebinds never leak.

    Also the point where a pre-91-02 run first touches the not-yet-existing ``_orphan_cache`` symbol,
    turning the whole module RED with a clean ``AttributeError`` (never a collection/import error).
    """
    snapshot = dict(pipeline._orphan_cache)
    expires = pipeline._orphan_cache_expires_at
    try:
        yield
    finally:
        pipeline._orphan_cache = snapshot
        pipeline._orphan_cache_expires_at = expires


def test_cached_read_returns_a_distinct_copy() -> None:
    """``get_cached_stage_orphan_counts`` returns an equal-but-distinct copy; mutating it never leaks (D-04)."""
    seed = {"metadata": 3, "analyze": 5}
    pipeline._orphan_cache = dict(seed)

    first = pipeline.get_cached_stage_orphan_counts()
    assert first == seed
    assert first is not pipeline._orphan_cache  # a COPY, not the live module dict

    first["metadata"] = 999
    assert pipeline.get_cached_stage_orphan_counts() == seed  # the write did not reach the cache


async def test_refresh_updates_the_cached_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful ``refresh_stage_orphan_counts`` rebinds the cache to the freshly computed dict."""
    pipeline._orphan_cache = dict(_ALL_ZEROS)
    monkeypatch.setattr("phaze.database.async_session", _fake_async_session)

    computed = {"metadata": 1, "analyze": 2}

    async def _fake_compute(_session: object) -> dict[str, int]:
        return dict(computed)

    monkeypatch.setattr(pipeline, "_compute_stage_orphan_counts", _fake_compute)

    await pipeline.refresh_stage_orphan_counts()

    assert pipeline.get_cached_stage_orphan_counts() == computed


async def test_degraded_refresh_keeps_last_good_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising compute core propagates the error AND leaves the last-good cache intact -- never all-zeros (D-03)."""
    good = {"metadata": 2, "analyze": 0}
    pipeline._orphan_cache = dict(good)
    monkeypatch.setattr("phaze.database.async_session", _fake_async_session)

    async def _boom(_session: object) -> dict[str, int]:
        raise RuntimeError("forced compute failure")

    monkeypatch.setattr(pipeline, "_compute_stage_orphan_counts", _boom)

    with pytest.raises(RuntimeError, match="forced compute failure"):
        await pipeline.refresh_stage_orphan_counts()

    # No poison: the cache still holds the prior known-good value, not {0, 0, 0}.
    assert pipeline.get_cached_stage_orphan_counts() == good


async def test_orphan_refresh_loop_warns_on_a_failed_iteration_and_keeps_polling(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The ACTUAL app loop survives a failed refresh: named WARNING, last-good kept, polling continues.

    Iterations are gated by the compute calls themselves (no timing-only sleep): the first raises,
    the second succeeds, and the third parks until cancellation, which proves the loop polled again
    after both outcomes. The cache value seen DURING the second iteration proves the failure left the
    last-good value in place; the value after it proves the next refresh really landed.
    """
    import phaze.main as main_module

    good = {"metadata": 4, "analyze": 1}
    fresh = {"metadata": 0, "analyze": 3}
    pipeline._orphan_cache = dict(good)
    monkeypatch.setattr("phaze.database.async_session", _fake_async_session)
    monkeypatch.setattr(main_module, "_ORPHAN_TTL_SECONDS", 0)
    calls: list[dict[str, int]] = []
    third_started = asyncio.Event()

    async def _compute(_session: object) -> dict[str, int]:
        calls.append(pipeline.get_cached_stage_orphan_counts())
        if len(calls) == 1:
            raise RuntimeError("forced first-iteration failure")
        if len(calls) == 2:
            return dict(fresh)
        third_started.set()
        await asyncio.Event().wait()  # park the third iteration until cancellation
        raise AssertionError("unreachable")

    monkeypatch.setattr(pipeline, "_compute_stage_orphan_counts", _compute)
    with caplog.at_level("WARNING", logger="phaze.main"):
        task = asyncio.create_task(main_module._orphan_refresh_loop())
        try:
            async with asyncio.timeout(5):
                await third_started.wait()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert task.cancelled()
    assert calls == [good, good, fresh]
    assert pipeline.get_cached_stage_orphan_counts() == fresh
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "orphan refresh loop iteration failed; keeping last-good" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    # structlog renders the traceback into the record itself: the named exception must be in it.
    assert "RuntimeError: forced first-iteration failure" in warnings[0].getMessage()


def test_fresh_cache_seeds_all_zeros() -> None:
    """Before any successful refresh, the module cache reads all-zeros (safe default until first success)."""
    assert pipeline._orphan_cache == _ALL_ZEROS
