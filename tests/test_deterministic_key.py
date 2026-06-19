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
    job.queue = SimpleNamespace(cache_redis=redis)  # type: ignore[assignment]
    await apply_deterministic_key(job)
    assert redis.store["phaze:pipeline:enqueued:process_file"] == 1


async def test_enqueued_counter_skipped_when_no_redis() -> None:
    # job.queue is None by default -> getattr(None, "cache_redis", None) is None -> no INCR,
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
    job.queue = SimpleNamespace(cache_redis=_BoomRedis())  # type: ignore[assignment]
    await apply_deterministic_key(job)  # must not raise
    assert job.key == f"process_file:{fid}"


# ---------------------------------------------------------------------------
# increment_completed (after_process)
# ---------------------------------------------------------------------------


async def test_increment_completed_bumps_on_complete_status() -> None:
    redis = FakeRedis()
    job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
    job.queue = SimpleNamespace(cache_redis=redis)  # type: ignore[assignment]
    job.status = Status.COMPLETE
    await increment_completed({"job": job})
    assert redis.store["phaze:pipeline:completed:process_file"] == 1


async def test_increment_completed_noop_on_non_complete_status() -> None:
    redis = FakeRedis()
    job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
    job.queue = SimpleNamespace(cache_redis=redis)  # type: ignore[assignment]
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
    job.queue = SimpleNamespace(cache_redis=redis)  # type: ignore[assignment]
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
    job.queue = SimpleNamespace(cache_redis=_BoomRedis())  # type: ignore[assignment]
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


# ---------------------------------------------------------------------------
# Phase 45: ledger WRITE hook + controller-stage CLEAR hook
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal async-context-manager session double the ledger hooks open via ``sm()``.

    Records ``commit`` calls so a test can assert the hook committed its own short-lived
    session. ``execute`` is unused here because the tests patch the ledger service
    functions (``upsert_ledger_entry`` / ``clear_ledger_entry``) to capture the call args
    directly -- the hook's contract is "open a session, call the service, commit".
    """

    def __init__(self) -> None:
        self.committed = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionmaker:
    """A callable that returns a fresh :class:`_FakeSession` (mirrors ``async_sessionmaker``)."""

    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        s = _FakeSession()
        self.sessions.append(s)
        return s


async def test_write_hook_upserts_ledger_when_sessionmaker_present(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: list[dict[str, object]] = []

    async def _fake_upsert(session: object, *, key: str, function: str, kwargs: dict[str, object]) -> None:
        captured.append({"key": key, "function": function, "kwargs": kwargs})

    monkeypatch.setattr("phaze.services.scheduling_ledger.upsert_ledger_entry", _fake_upsert)

    sm = _FakeSessionmaker()
    fid = uuid.uuid4()
    job = Job(function="process_file", kwargs={"file_id": fid})
    job.queue = SimpleNamespace(cache_redis=FakeRedis(), ledger_sessionmaker=sm)  # type: ignore[assignment]
    await apply_deterministic_key(job)

    assert job.key == f"process_file:{fid}"
    assert len(captured) == 1
    assert captured[0]["key"] == f"process_file:{fid}"
    assert captured[0]["function"] == "process_file"
    assert captured[0]["kwargs"] == {"file_id": fid}
    assert sm.sessions[0].committed is True


async def test_write_hook_noop_without_sessionmaker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # No ledger_sessionmaker on the queue (the agent queue / a test fake): the hook must NOT
    # call the ledger service and must NOT raise -- identical to the cache_redis-absent path.
    called = False

    async def _fake_upsert(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("phaze.services.scheduling_ledger.upsert_ledger_entry", _fake_upsert)

    fid = uuid.uuid4()
    job = Job(function="process_file", kwargs={"file_id": fid})
    job.queue = SimpleNamespace(cache_redis=FakeRedis())  # type: ignore[assignment]
    await apply_deterministic_key(job)

    assert job.key == f"process_file:{fid}"
    assert called is False


async def test_write_hook_skips_non_keyed_function(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A function NOT in _KEY_BUILDERS returns early (random-uuid key) -- NO ledger write even
    # when a sessionmaker is present.
    called = False

    async def _fake_upsert(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("phaze.services.scheduling_ledger.upsert_ledger_entry", _fake_upsert)

    job = Job(function="heartbeat_tick", kwargs={})
    job.queue = SimpleNamespace(ledger_sessionmaker=_FakeSessionmaker())  # type: ignore[assignment]
    await apply_deterministic_key(job)

    assert called is False


async def test_write_hook_ledger_failure_does_not_block_enqueue(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A ledger hiccup must be swallowed -- the key is still set and the hook returns normally.
    async def _boom_upsert(*_a: object, **_k: object) -> None:
        raise RuntimeError("ledger down")

    monkeypatch.setattr("phaze.services.scheduling_ledger.upsert_ledger_entry", _boom_upsert)

    fid = uuid.uuid4()
    job = Job(function="process_file", kwargs={"file_id": fid})
    job.queue = SimpleNamespace(ledger_sessionmaker=_FakeSessionmaker())  # type: ignore[assignment]
    await apply_deterministic_key(job)  # must not raise
    assert job.key == f"process_file:{fid}"


async def test_clear_hook_clears_on_terminal_status(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for status in (Status.COMPLETE, Status.FAILED, Status.ABORTED):
        cleared: list[str] = []

        async def _fake_clear(session: object, key: str, _sink: list[str] = cleared) -> None:
            _sink.append(key)

        monkeypatch.setattr("phaze.services.scheduling_ledger.clear_ledger_entry", _fake_clear)

        sm = _FakeSessionmaker()
        job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
        job.key = "process_file:k"
        job.queue = SimpleNamespace(cache_redis=FakeRedis(), ledger_sessionmaker=sm)  # type: ignore[assignment]
        job.status = status
        await increment_completed({"job": job})

        assert cleared == ["process_file:k"], f"status {status} must clear the ledger row"
        assert sm.sessions[0].committed is True


async def test_clear_hook_does_not_clear_on_retry_queued(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A retry sets job.status = Status.QUEUED (NOT terminal): the row must survive.
    cleared: list[str] = []

    async def _fake_clear(session: object, key: str) -> None:
        cleared.append(key)

    monkeypatch.setattr("phaze.services.scheduling_ledger.clear_ledger_entry", _fake_clear)

    job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
    job.key = "process_file:k"
    job.queue = SimpleNamespace(cache_redis=FakeRedis(), ledger_sessionmaker=_FakeSessionmaker())  # type: ignore[assignment]
    job.status = Status.QUEUED
    await increment_completed({"job": job})

    assert cleared == [], "a QUEUED (retry) job must NOT clear its ledger row"


async def test_clear_hook_noop_without_sessionmaker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # On the agent worker (no ledger_sessionmaker) the clear is a logged no-op -- agent-stage
    # clears are Plan 02's job (callback handlers). No call, no raise.
    called = False

    async def _fake_clear(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("phaze.services.scheduling_ledger.clear_ledger_entry", _fake_clear)

    job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
    job.key = "process_file:k"
    job.queue = SimpleNamespace(cache_redis=FakeRedis())  # type: ignore[assignment]
    job.status = Status.COMPLETE
    await increment_completed({"job": job})

    assert called is False


async def test_clear_hook_skips_non_keyed_function(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    called = False

    async def _fake_clear(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("phaze.services.scheduling_ledger.clear_ledger_entry", _fake_clear)

    job = Job(function="some_unregistered_task", kwargs={})
    job.key = "some_unregistered_task:k"
    job.queue = SimpleNamespace(ledger_sessionmaker=_FakeSessionmaker())  # type: ignore[assignment]
    job.status = Status.COMPLETE
    await increment_completed({"job": job})

    assert called is False


async def test_clear_hook_completed_counter_still_fires_on_complete(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The existing completed-counter INCR on COMPLETE must be preserved alongside the new clear.
    async def _fake_clear(session: object, key: str) -> None:
        return None

    monkeypatch.setattr("phaze.services.scheduling_ledger.clear_ledger_entry", _fake_clear)

    redis = FakeRedis()
    job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
    job.key = "process_file:k"
    job.queue = SimpleNamespace(cache_redis=redis, ledger_sessionmaker=_FakeSessionmaker())  # type: ignore[assignment]
    job.status = Status.COMPLETE
    await increment_completed({"job": job})

    assert redis.store["phaze:pipeline:completed:process_file"] == 1


async def test_clear_hook_ledger_failure_is_swallowed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def _boom_clear(*_a: object, **_k: object) -> None:
        raise RuntimeError("ledger down")

    monkeypatch.setattr("phaze.services.scheduling_ledger.clear_ledger_entry", _boom_clear)

    job = Job(function="process_file", kwargs={"file_id": uuid.uuid4()})
    job.key = "process_file:k"
    job.queue = SimpleNamespace(cache_redis=FakeRedis(), ledger_sessionmaker=_FakeSessionmaker())  # type: ignore[assignment]
    job.status = Status.FAILED
    await increment_completed({"job": job})  # must not raise
