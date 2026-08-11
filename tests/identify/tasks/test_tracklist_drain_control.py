"""The continuous-drain CronJob + its after_process hook (phaze-6nrrf). No network, no browser.

Mirrors ``tests/identify/tasks/test_tracklist_drain.py``'s shape: the task SHELL is what's under
test here, not the drain's own lookup logic (covered end to end in
``tests/identify/services/test_tracklist_drain.py``) or the arm-state transitions themselves
(covered in ``tests/identify/services/test_tracklist_drain_arm.py``).

What is asserted, mapped directly to the bead's acceptance criteria:

* the cron enqueues the next slice ONLY while armed, not in-flight, and past the cooldown --
  every other tick is an intentional no-op, not a partial state;
* it auto-disarms on an empty queue WITHOUT spending a request (the queue-empty check reuses
  ``tracklist_drain_status``, never a live lookup);
* a controller restart is modeled directly: a row that already reads armed=True with no other
  in-memory state produces the SAME single enqueue a live restart would (there is nothing else to
  restore -- the row already IS the whole state);
* nothing here EVER sets ``armed=True`` -- the no-self-arm-on-boot invariant;
* a slice whose ``after_process`` never ran self-heals rather than wedging the pass forever;
* the after_process hook is scoped to CRON-driven slices (``in_flight``) and to terminal
  statuses, and applies the exact cooldown / failure-streak rules the acceptance criteria specify.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from saq import Job
from saq.job import Status

from phaze.config import get_settings
from phaze.services.enqueue_router import CONTROLLER_TASKS
from phaze.services.tracklist_drain_arm import arm_drain, disarm_drain, get_arm_state, mark_slice_enqueued
from phaze.tasks import tracklist_drain_control as control_module
from phaze.tasks.controller import settings as controller_settings
from phaze.tasks.tracklist_drain_control import (
    MAX_CONSECUTIVE_SLICE_FAILURES,
    continue_armed_tracklist_drain,
    record_drain_slice_completion,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class FakeQueue:
    """Records every ``enqueue`` call; nothing else. No SAQ, no Postgres broker."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any]]] = []

    async def enqueue(self, task: str, **kwargs: Any) -> None:
        self.enqueued.append((task, kwargs))


class _FixedDatetime:
    """Stand-in for the module's ``datetime`` name, exposing only ``.now()``, fixed to one instant.

    ``continue_armed_tracklist_drain`` calls only ``datetime.now(UTC)``, so a stand-in with just
    that bound method is enough to control "now" without a freeze-time dependency.
    """

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self, tz: Any = None) -> datetime:  # noqa: ARG002 -- matches datetime.now's signature
        return self._moment


def ctx_for(session: AsyncSession, *, queue: FakeQueue | None = None) -> dict[str, Any]:
    """A SAQ-shaped ctx over the test's own savepoint-nested session (mirrors
    ``test_tracklist_drain.ctx_for``)."""

    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return {"async_session": _factory, "queue": queue or FakeQueue()}


class TestDisarmedIsAlwaysANoOp:
    async def test_a_fresh_never_armed_row_enqueues_nothing(self, session: AsyncSession) -> None:
        """The no-self-arm-on-boot invariant: nothing sets armed=True, so a controller that has
        never seen an Arm click must never enqueue -- including on the very first tick after a
        fresh migration."""
        queue = FakeQueue()
        await continue_armed_tracklist_drain(ctx_for(session, queue=queue))
        assert queue.enqueued == []

    async def test_a_row_disarmed_by_the_operator_enqueues_nothing(self, session: AsyncSession) -> None:
        await arm_drain(session, now=NOW)
        await disarm_drain(session, reason="operator", now=NOW)
        await session.commit()

        queue = FakeQueue()
        await continue_armed_tracklist_drain(ctx_for(session, queue=queue))
        assert queue.enqueued == []


class TestArmedEnqueuing:
    async def test_armed_and_eligible_with_a_nonempty_queue_enqueues_exactly_one_slice(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        from phaze.models.metadata import FileMetadata

        file = await make_file(original_filename="Artist - Live @ Some Event 2024-04-12.mp3")
        session.add(FileMetadata(file_id=file.id, duration=3600.0))
        await arm_drain(session, now=NOW)
        await session.flush()
        await session.commit()

        queue = FakeQueue()
        await continue_armed_tracklist_drain(ctx_for(session, queue=queue))

        assert queue.enqueued == [("drain_tracklists", {})]
        state = await get_arm_state(session)
        assert state.in_flight is True
        assert state.slice_enqueued_at is not None

    async def test_a_slice_already_in_flight_is_not_stacked_on(self, session: AsyncSession, make_file, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        from phaze.models.metadata import FileMetadata

        file = await make_file(original_filename="Artist - Live @ Some Event 2024-04-12.mp3")
        session.add(FileMetadata(file_id=file.id, duration=3600.0))
        await arm_drain(session, now=NOW)
        await mark_slice_enqueued(session, now=NOW)
        await session.flush()
        await session.commit()

        # Pin "now" to just after the enqueue -- this is the ORDINARY in-progress case, not the
        # stale self-heal one (covered separately in TestStaleInFlightSelfHeal).
        monkeypatch.setattr(control_module, "datetime", _FixedDatetime(NOW + timedelta(seconds=30)))

        queue = FakeQueue()
        await continue_armed_tracklist_drain(ctx_for(session, queue=queue))
        assert queue.enqueued == []
        state = await get_arm_state(session)
        assert state.in_flight is True  # ordinary skip, not a self-heal

    async def test_still_inside_the_cooldown_window_enqueues_nothing(self, session: AsyncSession, make_file, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        from phaze.models.metadata import FileMetadata

        file = await make_file(original_filename="Artist - Live @ Some Event 2024-04-12.mp3")
        session.add(FileMetadata(file_id=file.id, duration=3600.0))
        state = await arm_drain(session, now=NOW)
        state.next_eligible_at = NOW + timedelta(minutes=9)
        await session.flush()
        await session.commit()

        # "now" must be pinned relative to NOW, not left at the real wall clock -- the real clock
        # is years past this fixture's 2026-08-11 NOW and would read the cooldown as long elapsed.
        monkeypatch.setattr(control_module, "datetime", _FixedDatetime(NOW + timedelta(minutes=1)))

        queue = FakeQueue()
        await continue_armed_tracklist_drain(ctx_for(session, queue=queue))
        assert queue.enqueued == []

    async def test_an_empty_queue_auto_disarms_and_spends_no_slice(self, session: AsyncSession) -> None:
        """No files exist at all -- the queue is genuinely empty. Must disarm, not enqueue an
        empty slice."""
        await arm_drain(session, now=NOW)
        await session.commit()

        queue = FakeQueue()
        await continue_armed_tracklist_drain(ctx_for(session, queue=queue))

        assert queue.enqueued == []
        state = await get_arm_state(session)
        assert state.armed is False
        assert state.disarmed_reason == "queue_empty"

    async def test_restart_resumes_an_already_armed_pass_with_nothing_else_to_restore(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """Models a controller restart directly: the row already reads armed=True with a cleared
        in_flight/next_eligible_at (exactly what a prior clean pass, or a fresh arm, leaves
        behind) and NO in-memory state anywhere else. The very first post-restart tick must
        enqueue -- there is nothing left to reconstruct."""
        from phaze.models.metadata import FileMetadata

        file = await make_file(original_filename="Artist - Live @ Some Event 2024-04-12.mp3")
        session.add(FileMetadata(file_id=file.id, duration=3600.0))
        await arm_drain(session, now=NOW - timedelta(days=1))
        await session.flush()
        await session.commit()

        queue = FakeQueue()
        await continue_armed_tracklist_drain(ctx_for(session, queue=queue))
        assert queue.enqueued == [("drain_tracklists", {})]


class TestStaleInFlightSelfHeal:
    async def test_a_slice_enqueued_long_ago_self_heals_instead_of_wedging_forever(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await arm_drain(session, now=NOW)
        await mark_slice_enqueued(session, now=NOW)
        await session.commit()

        cfg = get_settings()
        stale_after_seconds = cfg.worker_job_timeout * (cfg.worker_max_retries + 1) + 300
        far_future = NOW + timedelta(seconds=stale_after_seconds + 60)
        monkeypatch.setattr(control_module, "datetime", _FixedDatetime(far_future))

        queue = FakeQueue()
        await continue_armed_tracklist_drain(ctx_for(session, queue=queue))

        # Self-heals THIS tick (clears the wedge) but does not enqueue on the SAME tick -- the
        # next tick, past the cooldown this heal sets, is what actually resumes the pass.
        assert queue.enqueued == []
        state = await get_arm_state(session)
        assert state.in_flight is False
        assert state.slice_enqueued_at is None
        assert state.consecutive_failures == 0  # unknown outcome is never counted as a failure
        assert state.armed is True  # still armed -- self-heal is not a disarm

    async def test_a_recently_enqueued_slice_is_not_treated_as_stale(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ordinary in-progress case: an in-flight slice enqueued moments ago must be left
        alone, not self-healed."""
        await arm_drain(session, now=NOW)
        await mark_slice_enqueued(session, now=NOW)
        await session.commit()

        # Pin "now" to just after the enqueue -- well inside the stale threshold -- rather than
        # the real wall clock, which is years past this fixture's 2026-08-11 NOW.
        monkeypatch.setattr(control_module, "datetime", _FixedDatetime(NOW + timedelta(seconds=10)))

        queue = FakeQueue()
        await continue_armed_tracklist_drain(ctx_for(session, queue=queue))

        assert queue.enqueued == []
        state = await get_arm_state(session)
        assert state.in_flight is True  # untouched -- this is an ordinary running slice


class TestRecordSliceCompletion:
    async def test_ignores_jobs_that_are_not_drain_tracklists(self, session: AsyncSession) -> None:
        await mark_slice_enqueued(session, now=NOW)
        await session.commit()
        job = Job(function="generate_proposals", kwargs={})
        job.status = Status.COMPLETE
        await record_drain_slice_completion(ctx_for(session) | {"job": job})
        state = await get_arm_state(session)
        assert state.in_flight is True  # untouched

    async def test_ignores_non_terminal_statuses_mid_retry(self, session: AsyncSession) -> None:
        await mark_slice_enqueued(session, now=NOW)
        await session.commit()
        job = Job(function="drain_tracklists", kwargs={})
        job.status = Status.QUEUED  # SAQ's own retry re-queue, not a terminal outcome
        await record_drain_slice_completion(ctx_for(session) | {"job": job})
        state = await get_arm_state(session)
        assert state.in_flight is True  # still waiting for a terminal outcome

    async def test_a_manual_slice_not_in_flight_is_left_untouched(self, session: AsyncSession) -> None:
        """A manual "Run tracklist lookups" click never set in_flight -- its completion must not
        perturb the cooldown clock or the failure streak."""
        await arm_drain(session, now=NOW)
        await session.commit()
        job = Job(function="drain_tracklists", kwargs={})
        job.status = Status.FAILED
        await record_drain_slice_completion(ctx_for(session) | {"job": job})
        state = await get_arm_state(session)
        assert state.consecutive_failures == 0
        assert state.armed is True

    async def test_a_completed_cron_slice_clears_in_flight_and_arms_the_cooldown(self, session: AsyncSession) -> None:
        await arm_drain(session, now=NOW)
        await mark_slice_enqueued(session, now=NOW)
        await session.commit()

        job = Job(function="drain_tracklists", kwargs={})
        job.status = Status.COMPLETE
        await record_drain_slice_completion(ctx_for(session) | {"job": job})

        state = await get_arm_state(session)
        assert state.in_flight is False
        assert state.consecutive_failures == 0
        assert state.next_eligible_at is not None

    async def test_three_consecutive_cron_slice_failures_auto_disarm(self, session: AsyncSession) -> None:
        await arm_drain(session, now=NOW)
        await session.commit()

        for _ in range(MAX_CONSECUTIVE_SLICE_FAILURES):
            await mark_slice_enqueued(session, now=NOW)
            await session.commit()
            job = Job(function="drain_tracklists", kwargs={})
            job.status = Status.FAILED
            await record_drain_slice_completion(ctx_for(session) | {"job": job})
            await session.commit()

        state = await get_arm_state(session)
        assert state.armed is False
        assert state.disarmed_reason == "failures"


class TestRegistration:
    """Mirrors test_tracklist_drain.TestRegistration -- a task registered in only one of the two
    places is unroutable or unconsumable."""

    def test_the_cron_function_is_registered_on_the_controller_worker(self) -> None:
        registered = {fn.__name__ for fn in controller_settings["functions"]}
        assert "continue_armed_tracklist_drain" in registered

    def test_the_cron_function_has_a_cronjob(self) -> None:
        cron_functions = {job.function.__name__ for job in controller_settings["cron_jobs"]}
        assert "continue_armed_tracklist_drain" in cron_functions

    def test_drain_tracklists_itself_still_has_no_cronjob(self) -> None:
        """Unchanged invariant -- the ethics bound is on drain_tracklists, not relaxed by this
        bead. See test_tracklist_drain.TestRegistration.test_the_drain_has_no_cron_job."""
        cron_functions = {job.function.__name__ for job in controller_settings["cron_jobs"]}
        assert "drain_tracklists" not in cron_functions

    def test_the_cron_function_is_cron_only_not_operator_routable(self) -> None:
        """Mirrors reap_stalled_scans and friends -- never operator/API-enqueued directly."""
        assert "continue_armed_tracklist_drain" not in CONTROLLER_TASKS

    def test_after_process_runs_both_the_counter_hook_and_the_drain_completion_hook(self) -> None:
        after_process = controller_settings["after_process"]
        names = {fn.__name__ for fn in after_process}
        assert {"increment_completed", "record_drain_slice_completion"} <= names
