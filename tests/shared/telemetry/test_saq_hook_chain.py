"""The SAQ ``after_process`` hook CHAIN, driven by the installed saq package's own dispatch.

phaze-24dl8. ``saq.worker.Worker._after_process`` is a single bare loop::

    for ap in self.after_process:
        await ap(ctx)

and ``Worker.process`` wraps the whole loop in one ``try`` that merely logs. With the
telemetry hook registered LAST in that list, ANY raise from a hook ahead of it abandoned the
rest of the loop: the job's span was never ``__exit__``'d (never ended, never exported) and
its duration and outcome were never recorded. The jobs that vanished from the panels were
exactly the abnormally-ended ones the metrics exist to show.

WHY THESE TESTS BUILD A REAL ``saq.Worker`` (ADR-0012 rule 3). The claim under test is a
property of SAQ's hook dispatch, so a hand-rolled ``for hook in hooks`` loop in the test
would be the producer vouching for itself -- it would pass against the pre-fix list shape
too, because the loop a test writes is the loop the test expects. Every test here registers
the hooks through ``saq.Worker(**settings)`` -- the PRODUCTION settings dicts wherever the
wiring is what is being asserted -- and drives ``worker._before_process`` /
``worker._after_process``, the same methods ``Worker.process`` calls. That also exercises
``ensure_coroutine_function_many``, which is not incidental: it gates on
``asyncio.iscoroutinefunction`` and would have silently thread-pooled (and never awaited) a
chain implemented as a callable object rather than an ``async def``.

``test_control_group_the_pre_fix_list_shape_still_loses_the_span`` is the harness's own
proof: the identical scenario, hooks registered the pre-fix way, loses the span and the
metrics. Without it a green run here would say nothing about whether the defect is
reachable at all.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
import saq
from saq.job import Status

from phaze.tasks._shared import stage_control as stage_control_module
from phaze.tasks._shared.deterministic_key import increment_completed
from phaze.tasks._shared.stage_control import repark_if_stage_paused
from phaze.tasks.controller import settings as controller_settings
from phaze.tasks.tracklist_drain_control import record_drain_slice_completion
from phaze.telemetry import saq as telemetry_saq


if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.shared.telemetry.conftest import TelemetrySink


class _Redis:
    """The one call ``_bump_completed_counter`` makes -- ``INCR``."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.keys: list[str] = []

    async def incr(self, key: str) -> int:
        if self.raises is not None:
            raise self.raises
        self.keys.append(key)
        return 1


class _Queue:
    """The queue attributes the after-process hooks read off ``job.queue``."""

    def __init__(self, cache_redis: _Redis | None = None) -> None:
        self.cache_redis = cache_redis


class _Job:
    """The subset of ``saq.job.Job`` the hooks under test read and write."""

    def __init__(self, function: str, status: Status, *, queue: _Queue | None = None) -> None:
        self.function = function
        self.status = status
        self.key = f"{function}:a-file-id"
        self.attempts = 1
        self.kwargs = {"file_id": "a-file-id"}
        self.queue = queue if queue is not None else _Queue()
        self.updates: list[dict[str, Any]] = []

    async def update(self, **fields: Any) -> None:
        self.updates.append(fields)


def _session_factory_that_fails() -> Any:
    """A transient DB error out of ``record_drain_slice_completion``'s very first statement.

    The bead's own example of a raising neighbour. The hook has no ``except`` of its own --
    deliberately, it is a correctness-critical write -- so this is a real, un-swallowed raise
    from a real production hook, not a stand-in for one.
    """
    msg = "connection reset by peer"
    raise OSError(msg)


async def _unpaused(_queue: Any, _stage: str) -> tuple[bool, int]:
    return False, 50


@pytest.fixture
def agent_worker_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real agent worker settings module.

    ``AgentSettings`` fail-fasts on a missing token / queue name and the module builds its
    Queue at import time, so the env has to be in place first -- the same prelude
    ``tests/analyze/tasks/test_queue_defaults.py`` uses to construct a real Worker from these
    settings. The autouse env-isolation fixture in ``tests/conftest.py`` clears these vars per
    test, so this cannot leak into a later one.
    """
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://app.test:8000")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-TOKEN-1234567890ab")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test")
    monkeypatch.setenv("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@app-server.example:5432/phaze")
    from phaze.config import get_settings

    get_settings.cache_clear()

    from phaze.tasks import agent_worker

    return agent_worker


@pytest.fixture
def unpaused_stage_control(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Report every stage unpaused, so the agent's pause check is a deliberate no-op.

    Patched at the control READ rather than at the hook, so ``enforce_stage_pause_on_process``
    itself -- including its raise -- is always the real one.
    """
    monkeypatch.setattr(stage_control_module, "_read_stage_control", _unpaused)
    yield


@pytest.mark.asyncio
async def test_a_raising_controller_hook_no_longer_abandons_the_jobs_span_and_metrics(telemetry_sink: TelemetrySink) -> None:
    """Acceptance 1, on the controller's PRODUCTION wiring.

    ``record_drain_slice_completion`` raising is one of the three cases the bead names. The
    job itself completed normally: the whole point is that a failure in a bookkeeping hook
    used to erase the record of a job that was fine.
    """
    worker = saq.Worker(**controller_settings)
    job = _Job("drain_tracklists", Status.COMPLETE)
    ctx: dict[str, Any] = {"job": job, "async_session": _session_factory_that_fails}

    await worker._before_process(ctx)
    with pytest.raises(OSError, match="connection reset"):
        await worker._after_process(ctx)

    assert telemetry_sink.span_names() == ["saq.job"], "the job's span was not ended and exported"
    assert telemetry_sink.attribute_sets("phaze.saq.jobs") == [{"saq_function": "drain_tracklists", "outcome": "ok"}]
    assert telemetry_sink.count("phaze.saq.job.duration") == 1


@pytest.mark.asyncio
async def test_control_group_the_pre_fix_list_shape_still_loses_the_span(telemetry_sink: TelemetrySink) -> None:
    """CONTROL: the same scenario with the hooks registered as a flat list -- the pre-fix shape.

    Proves the harness exercises the documented defect through real SAQ machinery rather than
    merely failing to trigger it. If this test ever goes green on the assertions below, the
    scenario has stopped reproducing and the test above has stopped meaning anything.
    """
    worker = saq.Worker(
        queue=_Queue(),
        functions=[],
        before_process=[telemetry_saq.before_process],
        after_process=[increment_completed, record_drain_slice_completion, telemetry_saq.after_process],
    )
    job = _Job("drain_tracklists", Status.COMPLETE)
    ctx: dict[str, Any] = {"job": job, "async_session": _session_factory_that_fails}

    await worker._before_process(ctx)
    with pytest.raises(OSError, match="connection reset"):
        await worker._after_process(ctx)

    assert telemetry_sink.span_names() == [], "the pre-fix shape no longer leaks the span; this control group is stale"
    assert "phaze.saq.jobs" not in telemetry_sink.metric_names()


@pytest.mark.asyncio
async def test_a_cancellederror_from_a_neighbour_hook_does_not_leak_the_span(
    telemetry_sink: TelemetrySink,
    agent_worker_module: Any,
    unpaused_stage_control: None,
) -> None:
    """Acceptance 2, on the agent's PRODUCTION wiring, through a real un-swallowed raise.

    ``increment_completed`` guards its Redis ``INCR`` with ``except Exception``, and
    ``CancelledError`` is a ``BaseException`` -- so a worker shutdown that cancels the task
    mid-``INCR`` escapes the neighbour and, before the fix, abandoned the telemetry hook.
    The cancellation itself must still propagate: SAQ's own handler is what logs it.
    """
    worker = saq.Worker(**agent_worker_module.settings)
    job = _Job("process_file", Status.COMPLETE, queue=_Queue(cache_redis=_Redis(raises=asyncio.CancelledError())))
    ctx: dict[str, Any] = {"job": job}

    await worker._before_process(ctx)
    with pytest.raises(asyncio.CancelledError):
        await worker._after_process(ctx)

    assert telemetry_sink.span_names() == ["saq.job"], "a shutdown cancellation leaked the job's span"
    assert telemetry_sink.attribute_sets("phaze.saq.jobs") == [{"saq_function": "process_file", "outcome": "ok"}]
    assert telemetry_sink.count("phaze.saq.job.duration") == 1


@pytest.mark.asyncio
async def test_the_chain_keeps_hook_order_and_abort_on_first_raise(telemetry_sink: TelemetrySink) -> None:
    """Acceptance 3: the neighbours' ORDER semantics are untouched.

    Both halves matter. The hooks still run in the order they were registered, and a raise
    from one still skips the ones after it -- on the agent worker that relationship is
    load-bearing, because ``repark_if_stage_paused`` is what stops ``increment_completed``
    reading a pause bounce as a genuine terminal outcome. The ONLY thing the chain adds is
    that telemetry closes the job afterwards either way.
    """
    calls: list[str] = []
    span_open_when_second_ran: list[bool] = []

    async def first(ctx: dict[str, Any]) -> None:
        calls.append("first")

    async def second(ctx: dict[str, Any]) -> None:
        calls.append("second")
        span_open_when_second_ran.append(telemetry_saq._SPAN_KEY in ctx)

    async def first_raising(ctx: dict[str, Any]) -> None:
        calls.append("first")
        msg = "boom"
        raise RuntimeError(msg)

    worker = saq.Worker(queue=_Queue(), functions=[], after_process=[telemetry_saq.after_process_chain(first, second)])
    ctx: dict[str, Any] = {"job": _Job("process_file", Status.COMPLETE)}
    await telemetry_saq.before_process(ctx)
    await worker._after_process(ctx)
    assert calls == ["first", "second"]
    assert span_open_when_second_ran == [True], "telemetry closed the job before the neighbours had run"
    assert telemetry_saq._SPAN_KEY not in ctx

    calls.clear()
    aborting = saq.Worker(queue=_Queue(), functions=[], after_process=[telemetry_saq.after_process_chain(first_raising, second)])
    ctx = {"job": _Job("process_file", Status.COMPLETE)}
    await telemetry_saq.before_process(ctx)
    with pytest.raises(RuntimeError, match="boom"):
        await aborting._after_process(ctx)
    assert calls == ["first"], "the chain changed SAQ's abort-on-first-raise between the neighbours"
    assert telemetry_sink.count("phaze.saq.job.duration") == 2, "the aborted chain still owed the job its duration"


def test_both_workers_register_the_telemetry_hook_through_the_chain(agent_worker_module: Any) -> None:
    """The wiring that makes the guarantee above production behaviour rather than a property
    of one test's Worker -- asserted against the real settings dicts, and in the documented
    neighbour order.

    ``chain`` is readable here because ``ensure_coroutine_function_many`` passes an
    ``async def`` through untouched; a callable object would have been replaced by a
    thread-pool wrapper, taking the attribute -- and the telemetry -- with it.
    """
    for settings, neighbours in (
        (agent_worker_module.settings, (repark_if_stage_paused, increment_completed)),
        (controller_settings, (increment_completed, record_drain_slice_completion)),
    ):
        worker = saq.Worker(**settings)
        assert worker.after_process is not None
        assert len(worker.after_process) == 1, "a second entry would run outside the chain's finally"
        assert worker.after_process[0].chain == (*neighbours, telemetry_saq.after_process)
