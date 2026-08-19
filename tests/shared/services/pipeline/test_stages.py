"""Tests for `services/pipeline/stages.py` (split from test_pipeline.py, phaze-7l8jh).

get_stage_busy_counts's activity split, the `_stats_fanout` process-global-semaphore cache -- `services/pipeline/stages.py`.
"""

from __future__ import annotations

from tests.shared.services.pipeline._shared import (
    _NullSavepoint,
    asyncio,
    get_stage_activity_counts,
    pipeline_stages_mod,
    pytest,
)


@pytest.mark.asyncio
async def test_get_stage_activity_counts_separates_queued_and_active() -> None:
    """The workspace can explain waiting and running jobs without changing the combined gate."""

    class _FakeResult:
        def all(self) -> list[tuple[str, str, int]]:
            return [
                ("extract_file_metadata", "queued", 4),
                ("extract_file_metadata", "active", 2),
                ("process_file", "active", 3),
                ("generate_proposals", "queued", 8),
            ]

    class _FakeSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
            return _FakeResult()

    counts = await get_stage_activity_counts(_FakeSession())  # type: ignore[arg-type]

    assert counts == {
        "metadata": {"queued": 4, "active": 2},
        "analyze": {"queued": 0, "active": 3},
    }


@pytest.mark.asyncio
async def test_stats_fanout_is_process_global_within_a_loop() -> None:
    """phaze-28wi: two "polls" on the SAME loop must share ONE semaphore, not one each.

    Deliberately does NOT use the ``session`` fixture -- that fixture's ``_route_stats_fanout``
    monkeypatches ``_STATS_FANOUT`` to a test override, which would short-circuit the very cache
    this test exercises. Simulates two independent ``/pipeline/stats`` polls landing on the SAME
    running loop (the real production shape: one uvicorn worker, one loop, many concurrent
    requests) by calling :func:`pipeline_stages_mod._stats_fanout` twice with nothing in between.
    """
    assert pipeline_stages_mod._STATS_FANOUT is None  # guard: no test override is active here
    first_poll = pipeline_stages_mod._stats_fanout()
    second_poll = pipeline_stages_mod._stats_fanout()
    assert first_poll is second_poll
    assert isinstance(first_poll, asyncio.Semaphore)


@pytest.mark.asyncio
async def test_stats_fanout_reuses_the_cached_semaphore_across_many_calls() -> None:
    """A long run of calls on one loop never allocates a new Semaphore past the first."""
    assert pipeline_stages_mod._STATS_FANOUT is None
    fanouts = [pipeline_stages_mod._stats_fanout() for _ in range(5)]
    assert len({id(f) for f in fanouts}) == 1


def test_stats_fanout_gives_different_loops_different_semaphores() -> None:
    """A fresh loop still gets its OWN semaphore (preserves the original loop-binding fix).

    An ``asyncio.Semaphore`` binds to the event loop of its first use, so two loops MUST NOT
    share one -- only concurrently in-flight polls on the SAME loop should. Runs two throwaway
    loops sequentially (each a stand-in for e.g. two separate pytest test-loops) and asserts the
    cache hands back a DIFFERENT object per loop.
    """
    assert pipeline_stages_mod._STATS_FANOUT is None

    async def _call() -> asyncio.Semaphore:
        return pipeline_stages_mod._stats_fanout()

    def _get_fanout_on_a_fresh_loop() -> asyncio.Semaphore:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_call())
        finally:
            loop.close()

    first = _get_fanout_on_a_fresh_loop()
    second = _get_fanout_on_a_fresh_loop()
    assert first is not second


@pytest.mark.asyncio
async def test_stats_fanout_test_override_wins_over_the_loop_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``_STATS_FANOUT`` patchable seam (92-03 Task 2) still takes priority, unchanged by phaze-28wi."""
    override = asyncio.Semaphore(1)
    monkeypatch.setattr(pipeline_stages_mod, "_STATS_FANOUT", override)
    assert pipeline_stages_mod._stats_fanout() is override
