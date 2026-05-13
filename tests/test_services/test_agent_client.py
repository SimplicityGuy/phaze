"""Contract tests for PhazeAgentClient (Phase 26 D-09..D-13, D-31, D-32).

Asserts the four critical invariants:
1. 4xx NEVER retried -- call_count == 1 on 401/403/404/422 (D-32).
2. 5xx retried exactly 3 times -- call_count == 3 on persistent 500.
3. Auth header injected: `Authorization: Bearer <token>` on every request (D-09).
4. Exception classes match status code class: 401/403 -> AgentApiAuthError;
   other 4xx -> AgentApiClientError; 5xx after retries -> AgentApiServerError (D-12).
"""

from __future__ import annotations

from datetime import UTC
import uuid

import httpx
import pytest
import respx

from phaze.services.agent_client import (
    AgentApiAuthError,
    AgentApiClientError,
    AgentApiServerError,
    PhazeAgentClient,
)


_BASE_URL = "http://app.test"
_TOKEN = "phaze_agent_test-token-1234567890abcdef"


@pytest.fixture
async def client():  # type: ignore[no-untyped-def]
    """Fresh PhazeAgentClient; closes underlying AsyncClient on teardown."""
    c = PhazeAgentClient(base_url=_BASE_URL, token=_TOKEN, timeout=5.0)
    yield c
    await c.close()


@respx.mock
async def test_put_analysis_happy_path_injects_auth_header(client):  # type: ignore[no-untyped-def]
    from phaze.schemas.agent_analysis import AnalysisWritePayload

    file_id = uuid.uuid4()
    route = respx.put(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
    )
    await client.put_analysis(file_id, AnalysisWritePayload(bpm=120.0))
    assert route.called
    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {_TOKEN}"


@respx.mock
async def test_401_raises_auth_error_without_retry(client):  # type: ignore[no-untyped-def]
    from phaze.schemas.agent_analysis import AnalysisWritePayload

    file_id = uuid.uuid4()
    route = respx.put(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(401, json={"detail": "Forbidden"}),
    )
    with pytest.raises(AgentApiAuthError):
        await client.put_analysis(file_id, AnalysisWritePayload(bpm=120.0))
    assert route.call_count == 1


@respx.mock
async def test_403_raises_auth_error_without_retry(client):  # type: ignore[no-untyped-def]
    from phaze.schemas.agent_analysis import AnalysisWritePayload

    file_id = uuid.uuid4()
    route = respx.put(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"}),
    )
    with pytest.raises(AgentApiAuthError):
        await client.put_analysis(file_id, AnalysisWritePayload(bpm=120.0))
    assert route.call_count == 1


@respx.mock
async def test_404_raises_client_error_without_retry(client):  # type: ignore[no-untyped-def]
    from phaze.schemas.agent_analysis import AnalysisWritePayload

    file_id = uuid.uuid4()
    route = respx.put(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(404, json={"detail": "not found"}),
    )
    with pytest.raises(AgentApiClientError):
        await client.put_analysis(file_id, AnalysisWritePayload(bpm=120.0))
    assert route.call_count == 1


@respx.mock
async def test_422_raises_client_error_without_retry(client):  # type: ignore[no-untyped-def]
    from phaze.schemas.agent_analysis import AnalysisWritePayload

    file_id = uuid.uuid4()
    route = respx.put(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(422, json={"detail": [{"msg": "extra forbidden"}]}),
    )
    with pytest.raises(AgentApiClientError):
        await client.put_analysis(file_id, AnalysisWritePayload(bpm=120.0))
    assert route.call_count == 1


@respx.mock
async def test_500_retries_three_times_then_raises_server_error(client):  # type: ignore[no-untyped-def]
    from phaze.schemas.agent_analysis import AnalysisWritePayload

    file_id = uuid.uuid4()
    route = respx.put(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(500),
    )
    with pytest.raises(AgentApiServerError):
        await client.put_analysis(file_id, AnalysisWritePayload(bpm=120.0))
    assert route.call_count == 3


@respx.mock
async def test_500_then_200_succeeds_on_retry(client):  # type: ignore[no-untyped-def]
    from phaze.schemas.agent_analysis import AnalysisWritePayload

    file_id = uuid.uuid4()
    route = respx.put(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"agent_id": "test-agent-01", "file_id": str(file_id)}),
        ],
    )
    await client.put_analysis(file_id, AnalysisWritePayload(bpm=120.0))
    assert route.call_count == 2


@respx.mock
async def test_connect_error_retries_then_raises_server_error(client):  # type: ignore[no-untyped-def]
    from phaze.schemas.agent_analysis import AnalysisWritePayload

    file_id = uuid.uuid4()
    route = respx.put(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}").mock(
        side_effect=httpx.ConnectError("simulated connection refused"),
    )
    with pytest.raises(AgentApiServerError):
        await client.put_analysis(file_id, AnalysisWritePayload(bpm=120.0))
    assert route.call_count == 3


@respx.mock
async def test_bearer_token_absent_from_warning_logs_on_500(client, caplog):  # type: ignore[no-untyped-def]
    """D-13: bearer token must never appear in WARNING logs emitted by _request() on HTTP failure.

    A 500 triggers the WARNING path in _request(). Capture caplog at WARNING level and assert
    the token string does NOT appear in any log record message.
    """
    import logging

    from phaze.schemas.agent_analysis import AnalysisWritePayload

    file_id = uuid.uuid4()
    respx.put(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}").mock(
        return_value=httpx.Response(500),
    )

    with caplog.at_level(logging.WARNING, logger="phaze.services.agent_client"), pytest.raises(AgentApiServerError):
        await client.put_analysis(file_id, AnalysisWritePayload(bpm=120.0))

    warning_text = "\n".join(rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING)
    assert _TOKEN not in warning_text, f"D-13 violation: bearer token appeared in WARNING log output: {warning_text!r}"


@respx.mock
async def test_whoami_returns_agent_identity_model(client):  # type: ignore[no-untyped-def]
    from datetime import datetime

    from phaze.schemas.agent_identity import AgentIdentity

    expected_created = datetime(2026, 5, 12, 10, 0, 0, tzinfo=UTC)
    route = respx.get(f"{_BASE_URL}/api/internal/agent/whoami").mock(
        return_value=httpx.Response(
            200,
            json={
                "agent_id": "fileserver-01",
                "name": "File Server 01",
                "scan_roots": ["/data/music"],
                "created_at": expected_created.isoformat(),
            },
        ),
    )
    identity = await client.whoami()
    assert isinstance(identity, AgentIdentity)
    assert identity.agent_id == "fileserver-01"
    assert identity.name == "File Server 01"
    assert identity.scan_roots == ["/data/music"]
    assert route.call_count == 1
