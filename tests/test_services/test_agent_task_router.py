"""Integration tests for AgentTaskRouter (Phase 26 D-19..D-21, D-30).

Phase 36: the broker is PostgresQueue now, so the router takes ``(queue_url, cache_redis_url)``.
The Postgres broker DSN comes from ``PHAZE_QUEUE_URL`` (or is derived from the integration
harness' ``TEST_DATABASE_URL`` by stripping the SQLAlchemy ``+asyncpg`` dialect suffix to the
raw libpq form psycopg3 needs); the cache-Redis DSN comes from ``PHAZE_REDIS_URL``. Marked
``@pytest.mark.integration`` because the enqueue paths reach the live Postgres broker.

Assertions:
1. enqueue_for_agent with two distinct agent IDs writes jobs to two distinct
   per-agent queues (no cross-talk).
2. _queue_for() returns the SAME Queue instance on repeated calls for the
   same agent_id (cache identity invariant).
3. close() disconnects every cached Queue and empties the cache.
4. enqueue_for_file delegates correctly using FileRecord.agent_id.
5. _queue_for() registers apply_project_job_defaults as a before_enqueue hook
   on every per-agent Queue (quick-260609-f96 regression guard against the
   10s scan_directory TimeoutError) -- the factory now owns this registration.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
import uuid

import pytest

from phaze.schemas.agent_tasks import ExtractMetadataPayload
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.tasks._shared.queue_defaults import apply_project_job_defaults


_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6379/0")
"""Cache-Redis DSN. Override via `PHAZE_REDIS_URL=redis://...:6379/0 uv run pytest ...`."""

_QUEUE_URL = os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze").replace(
    "postgresql+asyncpg://", "postgresql://"
)
"""Postgres broker DSN (raw libpq form). Derived from TEST_DATABASE_URL in the harness."""


@pytest.fixture
async def router():  # type: ignore[no-untyped-def]
    """Fresh AgentTaskRouter; teardown calls .close()."""
    r = AgentTaskRouter(queue_url=_QUEUE_URL, cache_redis_url=_REDIS_URL)
    yield r
    await r.close()


def _make_payload(agent_id: str) -> ExtractMetadataPayload:
    return ExtractMetadataPayload(
        file_id=uuid.uuid4(),
        original_path="/test/file.mp3",
        file_type="mp3",
        agent_id=agent_id,
    )


@pytest.mark.integration
async def test_enqueue_for_two_agents_isolated(router) -> None:  # type: ignore[no-untyped-def]
    """Two agent IDs -> two distinct SAQ queues, jobs do not cross-contaminate."""
    payload_a = _make_payload("agent-a")
    payload_b = _make_payload("agent-b")
    await router.enqueue_for_agent(agent_id="agent-a", task_name="extract_file_metadata", payload=payload_a)
    await router.enqueue_for_agent(agent_id="agent-b", task_name="extract_file_metadata", payload=payload_b)

    # Both queues should be in the cache with the canonical name format.
    assert "agent-a" in router._queues
    assert "agent-b" in router._queues
    queue_a = router._queues["agent-a"]
    queue_b = router._queues["agent-b"]
    # SAQ Queue stores its name in .name attribute
    assert queue_a.name == "phaze-agent-agent-a"
    assert queue_b.name == "phaze-agent-agent-b"


@pytest.mark.integration
async def test_lazy_queue_cache_reuses_instance(router) -> None:  # type: ignore[no-untyped-def]
    """Second call to _queue_for for the same agent_id returns the SAME Queue instance."""
    q1 = router._queue_for("agent-cache-test")
    q2 = router._queue_for("agent-cache-test")
    assert q1 is q2, "queue cache must return identity on repeated calls"


@pytest.mark.integration
async def test_queue_registers_job_defaults_hook(router) -> None:  # type: ignore[no-untyped-def]
    """Per-agent queues must register the timeout-bumping before_enqueue hook.

    Regression guard for quick-260609-f96: AgentTaskRouter._queue_for previously
    built per-agent queues without registering apply_project_job_defaults, so
    agent-dispatched jobs (notably scan_directory) inherited SAQ 0.26.3's 10s
    default timeout and were cancelled with asyncio.TimeoutError after 10s. The
    per-agent dispatch path must register the hook just like controller.py and
    agent_worker.py do. SAQ stores before_enqueue callbacks in the
    `_before_enqueues` dict keyed by id(callback) (verified against saq 0.26.3).
    """
    queue = router._queue_for("agent-timeout-test")
    assert apply_project_job_defaults in queue._before_enqueues.values()


@pytest.mark.integration
async def test_close_empties_cache(router) -> None:  # type: ignore[no-untyped-def]
    """After close(), the internal cache dict is empty."""
    router._queue_for("agent-d")
    router._queue_for("agent-e")
    assert len(router._queues) == 2
    await router.close()
    assert len(router._queues) == 0


@pytest.mark.integration
async def test_enqueue_forwards_timeout_and_retries_when_provided(router) -> None:  # type: ignore[no-untyped-def]
    """enqueue_for_agent forwards explicit timeout/retries to queue.enqueue.

    scan_directory disables its SAQ wall-clock timeout (timeout=0 -> unbounded)
    and retries (retries=0) so a long-running bulk archive walk is never killed
    mid-progress. SAQ applies any kwarg matching a Job dataclass field as a Job
    property, so the enqueued Job must carry timeout==0 / retries==0 verbatim.
    """
    payload = _make_payload("agent-timeout-fwd")
    job = await router.enqueue_for_agent(
        agent_id="agent-timeout-fwd",
        task_name="extract_file_metadata",
        payload=payload,
        timeout=0,
        retries=0,
    )
    # SAQ returns the Job; explicit timeout=0/retries=0 win over the
    # apply_project_job_defaults hook (the hook only overrides SAQ's 10/1
    # defaults, and 0 != 10, 0 != 1).
    assert job.timeout == 0
    assert job.retries == 0


@pytest.mark.integration
async def test_enqueue_omits_timeout_and_retries_when_not_provided(router) -> None:  # type: ignore[no-untyped-def]
    """When timeout/retries are not passed, neither key reaches queue.enqueue.

    Omitting the keys lets the per-agent queue's apply_project_job_defaults
    before_enqueue hook apply the role's policy defaults (worker_job_timeout /
    worker_max_retries) -- so the enqueued Job must NOT carry the SAQ raw
    defaults (10s / 1 retry) nor the explicit 0 used by scan_directory.
    """
    from phaze.config import get_settings

    cfg = get_settings()
    payload = _make_payload("agent-default-fwd")
    job = await router.enqueue_for_agent(
        agent_id="agent-default-fwd",
        task_name="extract_file_metadata",
        payload=payload,
    )
    # The before_enqueue hook bumped the SAQ defaults to the project policy.
    assert job.timeout == cfg.worker_job_timeout
    assert job.retries == cfg.worker_max_retries


@pytest.mark.integration
async def test_enqueue_for_file_derives_agent_id(router) -> None:  # type: ignore[no-untyped-def]
    """enqueue_for_file uses FileRecord.agent_id to pick the queue.

    We use SimpleNamespace as a stand-in for FileRecord because constructing
    a real FileRecord requires a session + DB schema; for this test we only
    need the .agent_id attribute.
    """
    fake_file = SimpleNamespace(agent_id="agent-c")
    payload = _make_payload("agent-c")
    await router.enqueue_for_file(file_record=fake_file, task_name="extract_file_metadata", payload=payload)
    assert "agent-c" in router._queues
    assert router._queues["agent-c"].name == "phaze-agent-agent-c"
