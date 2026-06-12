"""Unit + drift-guard tests for the central deterministic-key hook (Phase 35 Plan 01).

Two concerns:

1. The per-function key behaviors of ``apply_deterministic_key`` / ``increment_completed``
   (key format, batch-hash order-independence, unregistered-function passthrough,
   unconditional override, best-effort counter folding).
2. The DRIFT-GUARD: every routable task name (``CONTROLLER_TASKS`` union ``AGENT_TASKS``) must
   either have a ``_KEY_BUILDERS`` entry OR be in the documented :data:`_UNKEYED_TASKS`
   allow-list. A new routable task absent from both FAILS this test loud, so it can never
   silently revert to a random-uuid key.
"""

from __future__ import annotations

from types import SimpleNamespace
import uuid

from saq.job import Job, Status

from phaze.services.enqueue_router import AGENT_TASKS, CONTROLLER_TASKS
from phaze.tasks._shared.deterministic_key import (
    _KEY_BUILDERS,
    apply_deterministic_key,
    increment_completed,
)
from tests._queue_fakes import FakeRedis


# ---------------------------------------------------------------------------
# Drift-guard allow-list
# ---------------------------------------------------------------------------

# Routable tasks intentionally left UNKEYED (random-uuid default). Each entry documents
# WHY a deterministic key is wrong for it. A routable task in NEITHER this set nor
# ``_KEY_BUILDERS`` fails ``test_every_routable_task_is_keyed_or_exempt`` loud.
_UNKEYED_TASKS: frozenset[str] = frozenset(
    {
        # cron-only periodic refresh; never operator-enqueued, so repeated runs are
        # intentionally distinct jobs (no dedup target).
        "refresh_tracklists",
        # repeated directory scans are intentionally distinct (a re-scan of the same
        # root is a NEW unit of work, not a dedup no-op).
        "scan_directory",
        # keyed elsewhere / optional per 35-RESEARCH Q4 (batch execution carries its own
        # idempotency at the approved-batch layer, not the job key).
        "execute_approved_batch",
    }
)


# ---------------------------------------------------------------------------
# Per-function key behaviors
# ---------------------------------------------------------------------------


async def test_process_file_key_matches_legacy_template() -> None:
    fid = uuid.uuid4()
    job = Job(function="process_file", kwargs={"file_id": fid})
    await apply_deterministic_key(job)
    assert job.key == f"process_file:{fid}"


async def test_extract_file_metadata_key() -> None:
    fid = uuid.uuid4()
    job = Job(function="extract_file_metadata", kwargs={"file_id": fid})
    await apply_deterministic_key(job)
    assert job.key == f"extract_file_metadata:{fid}"


async def test_tracklist_keyed_by_tracklist_id() -> None:
    tid = uuid.uuid4()
    job = Job(function="scrape_and_store_tracklist", kwargs={"tracklist_id": tid})
    await apply_deterministic_key(job)
    assert job.key == f"scrape_and_store_tracklist:{tid}"


async def test_generate_proposals_batch_hash_is_order_independent() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    job1 = Job(function="generate_proposals", kwargs={"file_ids": [b, a, c]})
    job2 = Job(function="generate_proposals", kwargs={"file_ids": [c, b, a]})
    await apply_deterministic_key(job1)
    await apply_deterministic_key(job2)
    assert job1.key == job2.key
    assert job1.key.startswith("generate_proposals:")
    # A different batch produces a different key.
    job3 = Job(function="generate_proposals", kwargs={"file_ids": [a, b]})
    await apply_deterministic_key(job3)
    assert job3.key != job1.key


async def test_unregistered_function_leaves_key_unchanged() -> None:
    job = Job(function="heartbeat_tick", kwargs={})
    original_key = job.key
    await apply_deterministic_key(job)
    assert job.key == original_key


async def test_key_set_unconditionally_overriding_caller_key() -> None:
    fid = uuid.uuid4()
    job = Job(function="process_file", kwargs={"file_id": fid}, key="caller-supplied-key")
    await apply_deterministic_key(job)
    assert job.key == f"process_file:{fid}"


async def test_enqueued_counter_folded_into_key_hook() -> None:
    redis = FakeRedis()
    fid = uuid.uuid4()
    job = Job(function="process_file", kwargs={"file_id": fid})
    job.queue = SimpleNamespace(redis=redis)  # type: ignore[assignment]
    await apply_deterministic_key(job)
    assert redis.store["phaze:pipeline:enqueued:process_file"] == 1


async def test_enqueued_counter_skipped_when_no_redis() -> None:
    # job.queue is None by default -> getattr(None, "redis", None) is None -> no INCR,
    # and crucially no raise (best-effort).
    fid = uuid.uuid4()
    job = Job(function="process_file", kwargs={"file_id": fid})
    await apply_deterministic_key(job)
    assert job.key == f"process_file:{fid}"


async def test_enqueued_counter_failure_does_not_block_enqueue() -> None:
    # A Redis hiccup during the counter INCR must be swallowed -- the key is still set and
    # the hook returns normally (the enqueue must never be blocked by a counter cache).
    class _BoomRedis:
        async def incr(self, _key: str) -> int:
            raise RuntimeError("redis down")

    fid = uuid.uuid4()
    job = Job(function="process_file", kwargs={"file_id": fid})
    job.queue = SimpleNamespace(redis=_BoomRedis())  # type: ignore[assignment]
    await apply_deterministic_key(job)  # must not raise
    assert job.key == f"process_file:{fid}"


# ---------------------------------------------------------------------------
# increment_completed (after_process)
# ---------------------------------------------------------------------------


async def test_increment_completed_bumps_on_complete_status() -> None:
    redis = FakeRedis()
    job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
    job.queue = SimpleNamespace(redis=redis)  # type: ignore[assignment]
    job.status = Status.COMPLETE
    await increment_completed({"job": job})
    assert redis.store["phaze:pipeline:completed:process_file"] == 1


async def test_increment_completed_noop_on_non_complete_status() -> None:
    redis = FakeRedis()
    job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
    job.queue = SimpleNamespace(redis=redis)  # type: ignore[assignment]
    job.status = Status.FAILED
    await increment_completed({"job": job})
    assert "phaze:pipeline:completed:process_file" not in redis.store


async def test_increment_completed_noop_without_job() -> None:
    # An empty ctx (no "job") must not raise.
    await increment_completed({})


async def test_increment_completed_noop_for_unregistered_function() -> None:
    # A COMPLETE job whose function is NOT in _KEY_BUILDERS bumps no counter (the maintained
    # counters only track the keyed pipeline functions).
    redis = FakeRedis()
    job = Job(function="some_unregistered_task", kwargs={})
    job.queue = SimpleNamespace(redis=redis)  # type: ignore[assignment]
    job.status = Status.COMPLETE
    assert "some_unregistered_task" not in _KEY_BUILDERS
    await increment_completed({"job": job})
    assert redis.store == {}


async def test_increment_completed_failure_is_swallowed() -> None:
    # A Redis hiccup during the completed INCR must be swallowed (best-effort).
    class _BoomRedis:
        async def incr(self, _key: str) -> int:
            raise RuntimeError("redis down")

    job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
    job.queue = SimpleNamespace(redis=_BoomRedis())  # type: ignore[assignment]
    job.status = Status.COMPLETE
    await increment_completed({"job": job})  # must not raise


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


def test_every_routable_task_is_keyed_or_exempt() -> None:
    routable = CONTROLLER_TASKS | AGENT_TASKS
    covered = set(_KEY_BUILDERS) | _UNKEYED_TASKS
    missing = routable - covered
    assert not missing, (
        f"routable task(s) {sorted(missing)} have no deterministic-key builder and no "
        f"documented _UNKEYED_TASKS exemption -- add a _KEY_BUILDERS entry or an explicit "
        f"exemption so the task cannot silently revert to a random-uuid key"
    )


def test_drift_guard_catches_a_fabricated_routable_task() -> None:
    # Simulate a future developer adding a routable task without keying it: the guard's
    # set-difference must flag it.
    fabricated = "totally_new_unkeyed_task"
    routable = CONTROLLER_TASKS | AGENT_TASKS | {fabricated}
    covered = set(_KEY_BUILDERS) | _UNKEYED_TASKS
    assert fabricated in (routable - covered)


def test_unkeyed_allow_list_has_no_stale_entries() -> None:
    # Every exemption must still be a real routable task (prevents the allow-list from
    # rotting into a list of names that no longer exist).
    routable = CONTROLLER_TASKS | AGENT_TASKS
    assert routable >= _UNKEYED_TASKS


def test_key_builders_and_unkeyed_are_disjoint() -> None:
    assert not (set(_KEY_BUILDERS) & _UNKEYED_TASKS)
