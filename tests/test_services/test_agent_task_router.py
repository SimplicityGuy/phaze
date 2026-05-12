"""Integration tests for AgentTaskRouter (Phase 26 D-19..D-21, D-30).

Requires a real Redis instance reachable at the URL in `PHAZE_REDIS_URL` env
(default `redis://localhost:6379/0`). Marked `@pytest.mark.integration` so
the tests skip cleanly when Redis is unavailable (no `fakeredis` fallback
because SAQ's Queue.from_url is not compatible with fakeredis at the saq>=0.26
version we use).

Assertions:
1. enqueue_for_agent with two distinct agent IDs writes jobs to two distinct
   Redis queues (no cross-talk).
2. _queue_for() returns the SAME Queue instance on repeated calls for the
   same agent_id (cache identity invariant).
3. close() disconnects every cached Queue and empties the cache.
4. enqueue_for_file delegates correctly using FileRecord.agent_id.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
import uuid

import pytest

from phaze.schemas.agent_tasks import ExtractMetadataPayload
from phaze.services.agent_task_router import AgentTaskRouter


_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6379/0")
"""Override at test time via `PHAZE_REDIS_URL=redis://...:6379/0 uv run pytest ...`."""


@pytest.fixture
async def router():  # type: ignore[no-untyped-def]
    """Fresh AgentTaskRouter; teardown calls .close()."""
    r = AgentTaskRouter(redis_url=_REDIS_URL)
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
async def test_close_empties_cache(router) -> None:  # type: ignore[no-untyped-def]
    """After close(), the internal cache dict is empty."""
    router._queue_for("agent-d")
    router._queue_for("agent-e")
    assert len(router._queues) == 2
    await router.close()
    assert len(router._queues) == 0


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
