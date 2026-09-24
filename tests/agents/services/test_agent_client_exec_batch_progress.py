"""Respx tests for PhazeAgentClient.post_exec_batch_progress (Phase 28 D-05, D-16).

Mirrors tests/test_services/test_agent_client_endpoints.py patterns. Targets 28-V-25.

Behavior under test:
- POST to the correct URL `/api/internal/agent/exec-batches/{batch_id}/progress`.
- Request body matches `payload.model_dump(mode="json")`.
- 4xx surfaces immediately as AgentApiClientError (no retry — D-11/D-12).
- 5xx retries 3x then raises AgentApiServerError (D-11/D-12).
- Successful 200 returns None (no response model — heartbeat-style).
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload
from phaze.services.agent_client import (
    AgentApiClientError,
    AgentApiServerError,
    PhazeAgentClient,
)


_BASE_URL = "http://app.test"
_TOKEN = "phaze_agent_test-token-1234567890abcdef"


def _make_payload(batch_id: uuid.UUID, *, terminal_step: str = "deleted") -> ExecBatchProgressPayload:
    """Return a valid ExecBatchProgressPayload for the given batch_id."""
    kwargs: dict[str, object] = {
        "request_id": uuid.uuid4(),
        "batch_id": batch_id,
        "agent_id": "test-agent-01",
        "sub_batch_index": 0,
        "proposal_id": uuid.uuid4(),
        "terminal_step": terminal_step,
    }
    if terminal_step == "failed":
        kwargs["failed_at_step"] = "verify"
    return ExecBatchProgressPayload(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
def retry_delays() -> list[float]:
    """The delays tenacity's ``wait_exponential_jitter`` requests on ``client``'s retries.

    See ``tests/agents/services/test_agent_client.py``'s identical fixture (phaze-vh38w):
    the delay is still computed, only the real sleep is skipped.
    """
    return []


@pytest.fixture
async def client(retry_delays: list[float]):  # type: ignore[no-untyped-def]
    """Fresh PhazeAgentClient; closes underlying AsyncClient on teardown.

    ``_retry_sleep`` is test-only injection (phaze-vh38w): records the requested backoff
    delay and returns immediately instead of really sleeping. Production never passes it.
    """

    async def _no_wait(delay: float) -> None:
        retry_delays.append(delay)

    c = PhazeAgentClient(base_url=_BASE_URL, token=_TOKEN, timeout=5.0, _retry_sleep=_no_wait)
    yield c
    await c.close()


@respx.mock
async def test_post_exec_batch_progress_posts_to_correct_url(client):  # type: ignore[no-untyped-def]
    """post_exec_batch_progress -> POST /api/internal/agent/exec-batches/{batch_id}/progress, returns None."""
    batch_id = uuid.uuid4()
    payload = _make_payload(batch_id)

    route = respx.post(f"{_BASE_URL}/api/internal/agent/exec-batches/{batch_id}/progress").mock(
        return_value=httpx.Response(200),
    )

    result = await client.post_exec_batch_progress(batch_id, payload)

    assert route.called
    assert route.call_count == 1
    assert result is None, f"post_exec_batch_progress() should return None, got {result!r}"

    # Request body matches payload.model_dump(mode="json").
    sent_body = json.loads(route.calls.last.request.content)
    expected_body = payload.model_dump(mode="json")
    assert sent_body == expected_body


@respx.mock
async def test_post_exec_batch_progress_sends_failed_terminal_step(client):  # type: ignore[no-untyped-def]
    """A failed terminal_step payload serializes failed_at_step alongside terminal_step."""
    batch_id = uuid.uuid4()
    payload = _make_payload(batch_id, terminal_step="failed")

    route = respx.post(f"{_BASE_URL}/api/internal/agent/exec-batches/{batch_id}/progress").mock(
        return_value=httpx.Response(200),
    )

    await client.post_exec_batch_progress(batch_id, payload)

    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["terminal_step"] == "failed"
    assert sent_body["failed_at_step"] == "verify"


@respx.mock
async def test_post_exec_batch_progress_4xx_does_not_retry(client):  # type: ignore[no-untyped-def]
    """422 -> AgentApiClientError, route called exactly once (no retry on 4xx)."""
    batch_id = uuid.uuid4()
    payload = _make_payload(batch_id)

    route = respx.post(f"{_BASE_URL}/api/internal/agent/exec-batches/{batch_id}/progress").mock(
        return_value=httpx.Response(422, json={"detail": [{"msg": "invalid"}]}),
    )

    with pytest.raises(AgentApiClientError):
        await client.post_exec_batch_progress(batch_id, payload)

    assert route.call_count == 1, "4xx must NOT be retried"


@respx.mock
async def test_post_exec_batch_progress_404_does_not_retry(client):  # type: ignore[no-untyped-def]
    """404 -> AgentApiClientError, no retry."""
    batch_id = uuid.uuid4()
    payload = _make_payload(batch_id)

    route = respx.post(f"{_BASE_URL}/api/internal/agent/exec-batches/{batch_id}/progress").mock(
        return_value=httpx.Response(404, json={"detail": "batch not found"}),
    )

    with pytest.raises(AgentApiClientError):
        await client.post_exec_batch_progress(batch_id, payload)

    assert route.call_count == 1


@respx.mock
async def test_post_exec_batch_progress_5xx_retries_three_times_then_raises(client, retry_delays):  # type: ignore[no-untyped-def]
    """500 -> 3 retries then AgentApiServerError."""
    batch_id = uuid.uuid4()
    payload = _make_payload(batch_id)

    route = respx.post(f"{_BASE_URL}/api/internal/agent/exec-batches/{batch_id}/progress").mock(
        return_value=httpx.Response(500),
    )

    with pytest.raises(AgentApiServerError):
        await client.post_exec_batch_progress(batch_id, payload)

    assert route.call_count == 3, "5xx must be retried 3x (tenacity stop_after_attempt(3))"
    assert len(retry_delays) == 2
    assert all(d >= 0 for d in retry_delays)


@respx.mock
async def test_post_exec_batch_progress_500_then_200_succeeds_on_retry(client, retry_delays):  # type: ignore[no-untyped-def]
    """500 then 200 succeeds on retry; route called twice total."""
    batch_id = uuid.uuid4()
    payload = _make_payload(batch_id)

    route = respx.post(f"{_BASE_URL}/api/internal/agent/exec-batches/{batch_id}/progress").mock(
        side_effect=[httpx.Response(500), httpx.Response(200)],
    )

    result = await client.post_exec_batch_progress(batch_id, payload)
    assert result is None
    assert route.call_count == 2
    assert len(retry_delays) == 1
    assert retry_delays[0] >= 0


@respx.mock
async def test_post_exec_batch_progress_connect_error_retries(client, retry_delays):  # type: ignore[no-untyped-def]
    """ConnectError is retried like 5xx; persistent failure -> AgentApiServerError."""
    batch_id = uuid.uuid4()
    payload = _make_payload(batch_id)

    route = respx.post(f"{_BASE_URL}/api/internal/agent/exec-batches/{batch_id}/progress").mock(
        side_effect=httpx.ConnectError("simulated connection refused"),
    )

    with pytest.raises(AgentApiServerError):
        await client.post_exec_batch_progress(batch_id, payload)

    assert route.call_count == 3
    assert len(retry_delays) == 2
    assert all(d >= 0 for d in retry_delays)
