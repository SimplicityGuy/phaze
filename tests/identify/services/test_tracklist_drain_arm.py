"""The durable ARM/DISARM state machine for the continuous drain (phaze-6nrrf).

Every case here is a plain read-modify-write against the singleton
``TracklistDrainArmState`` row -- no network, no browser, no live requests. What is asserted:

* DEFAULT OFF: a freshly-created row (the get-or-create fallback ``get_arm_state`` uses when a
  test database has not run migration 059's seed) reads ``armed=False`` -- the safety invariant
  the whole feature rests on.
* ``arm_drain`` is the ONLY path that ever sets ``armed=True``, and it resets the failure streak
  and any stale disarm bookkeeping from a PRIOR pass.
* ``disarm_drain`` is idempotent -- a second disarm never overwrites the first's recorded reason.
* ``mark_slice_finished`` implements the two auto-disarm rules from the bead's acceptance
  criteria: the 3-consecutive-failure streak (a clean slice resets it), and (via ``disarm_drain``
  called directly, exercised in the task-layer suite) the queue-empty case.
* ``clear_stale_in_flight`` un-wedges an ``in_flight`` row WITHOUT touching the failure streak --
  the outcome of an abandoned slice is unknown, so it must not manufacture a false failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from phaze.services.tracklist_drain_arm import (
    arm_drain,
    clear_stale_in_flight,
    disarm_drain,
    get_arm_state,
    mark_slice_enqueued,
    mark_slice_finished,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class TestDefaultState:
    async def test_a_fresh_row_is_disarmed_with_no_history(self, session: AsyncSession) -> None:
        """DEFAULT OFF: nothing before an explicit arm() call may read armed=True."""
        state = await get_arm_state(session)
        assert state.armed is False
        assert state.armed_at is None
        assert state.disarmed_reason is None
        assert state.consecutive_failures == 0
        assert state.in_flight is False

    async def test_get_arm_state_is_idempotent_and_returns_the_same_row(self, session: AsyncSession) -> None:
        """Two reads must never create two rows -- the CHECK constraint would reject a second id
        anyway, but the get-or-create must not even attempt one."""
        first = await get_arm_state(session)
        await session.commit()
        second = await get_arm_state(session)
        assert first.id == second.id


class TestArmAndDisarm:
    async def test_arm_sets_armed_true_and_stamps_armed_at(self, session: AsyncSession) -> None:
        state = await arm_drain(session, now=NOW)
        await session.commit()
        assert state.armed is True
        assert state.armed_at == NOW

    async def test_arm_resets_a_failure_streak_and_prior_disarm_bookkeeping_from_the_last_pass(self, session: AsyncSession) -> None:
        """A fresh arm is a fresh pass -- it must not inherit the streak or reason a PRIOR
        armed pass ended with."""
        await mark_slice_finished(session, success=False, cooldown_seconds=600, max_consecutive_failures=3, now=NOW)
        await disarm_drain(session, reason="operator", now=NOW)
        await session.commit()

        state = await arm_drain(session, now=NOW + timedelta(hours=1))
        await session.commit()

        assert state.consecutive_failures == 0
        assert state.disarmed_reason is None
        assert state.disarmed_at is None
        assert state.next_eligible_at is None

    async def test_disarm_records_the_reason_and_timestamp(self, session: AsyncSession) -> None:
        await arm_drain(session, now=NOW)
        await session.commit()

        state = await disarm_drain(session, reason="operator", now=NOW + timedelta(minutes=5))
        await session.commit()

        assert state.armed is False
        assert state.disarmed_reason == "operator"
        assert state.disarmed_at == NOW + timedelta(minutes=5)

    async def test_disarming_an_already_disarmed_row_is_a_no_op_and_keeps_the_first_reason(self, session: AsyncSession) -> None:
        """A double-click, or a queue-empty auto-disarm racing an operator's own click, must never
        overwrite the reason the FIRST disarm already recorded."""
        await arm_drain(session, now=NOW)
        await disarm_drain(session, reason="queue_empty", now=NOW + timedelta(minutes=1))
        await session.commit()

        state = await disarm_drain(session, reason="operator", now=NOW + timedelta(minutes=2))
        await session.commit()

        assert state.disarmed_reason == "queue_empty"
        assert state.disarmed_at == NOW + timedelta(minutes=1)


class TestSliceLifecycle:
    async def test_mark_slice_enqueued_sets_in_flight_and_the_enqueue_timestamp(self, session: AsyncSession) -> None:
        state = await mark_slice_enqueued(session, now=NOW)
        await session.commit()
        assert state.in_flight is True
        assert state.slice_enqueued_at == NOW

    async def test_a_successful_slice_clears_in_flight_resets_failures_and_arms_the_cooldown(self, session: AsyncSession) -> None:
        await arm_drain(session, now=NOW)
        await mark_slice_enqueued(session, now=NOW)
        await mark_slice_finished(session, success=False, cooldown_seconds=600, max_consecutive_failures=3, now=NOW)
        await session.commit()

        state = await mark_slice_finished(session, success=True, cooldown_seconds=600, max_consecutive_failures=3, now=NOW + timedelta(minutes=5))
        await session.commit()

        assert state.in_flight is False
        assert state.slice_enqueued_at is None
        assert state.consecutive_failures == 0
        assert state.next_eligible_at == NOW + timedelta(minutes=5, seconds=600)
        assert state.armed is True  # a success never disarms

    async def test_three_consecutive_failures_auto_disarm_with_reason_failures(self, session: AsyncSession) -> None:
        await arm_drain(session, now=NOW)
        await session.commit()

        for i in range(2):
            await mark_slice_enqueued(session, now=NOW)
            state = await mark_slice_finished(session, success=False, cooldown_seconds=600, max_consecutive_failures=3, now=NOW)
            await session.commit()
            assert state.armed is True, f"must stay armed after failure #{i + 1}"
            assert state.consecutive_failures == i + 1

        await mark_slice_enqueued(session, now=NOW)
        state = await mark_slice_finished(session, success=False, cooldown_seconds=600, max_consecutive_failures=3, now=NOW)
        await session.commit()

        assert state.consecutive_failures == 3
        assert state.armed is False
        assert state.disarmed_reason == "failures"

    async def test_a_success_between_two_failures_resets_the_streak(self, session: AsyncSession) -> None:
        await arm_drain(session, now=NOW)
        await mark_slice_enqueued(session, now=NOW)
        await mark_slice_finished(session, success=False, cooldown_seconds=600, max_consecutive_failures=3, now=NOW)
        await mark_slice_enqueued(session, now=NOW)
        await mark_slice_finished(session, success=True, cooldown_seconds=600, max_consecutive_failures=3, now=NOW)
        await session.commit()

        await mark_slice_enqueued(session, now=NOW)
        state = await mark_slice_finished(session, success=False, cooldown_seconds=600, max_consecutive_failures=3, now=NOW)
        await session.commit()

        assert state.consecutive_failures == 1
        assert state.armed is True

    async def test_failure_streak_never_auto_disarms_an_already_disarmed_row(self, session: AsyncSession) -> None:
        """A manual slice (never armed) failing repeatedly must not flip armed=True by accident --
        the guard is ``row.armed and ...``, not just the count."""
        await mark_slice_enqueued(session, now=NOW)
        for _ in range(5):
            state = await mark_slice_finished(session, success=False, cooldown_seconds=600, max_consecutive_failures=3, now=NOW)
            await mark_slice_enqueued(session, now=NOW)
        await session.commit()

        assert state.armed is False
        assert state.disarmed_reason is None  # never touched -- it was never armed to begin with


class TestStaleInFlightSelfHeal:
    async def test_clears_in_flight_without_touching_the_failure_streak(self, session: AsyncSession) -> None:
        await arm_drain(session, now=NOW)
        await mark_slice_enqueued(session, now=NOW)
        await mark_slice_finished(session, success=False, cooldown_seconds=600, max_consecutive_failures=3, now=NOW)
        await mark_slice_enqueued(session, now=NOW + timedelta(hours=1))
        await session.commit()

        state = await clear_stale_in_flight(session, cooldown_seconds=600, now=NOW + timedelta(hours=5))
        await session.commit()

        assert state.in_flight is False
        assert state.slice_enqueued_at is None
        assert state.consecutive_failures == 1  # unchanged -- unknown outcome is not a failure
        assert state.next_eligible_at == NOW + timedelta(hours=5, seconds=600)
