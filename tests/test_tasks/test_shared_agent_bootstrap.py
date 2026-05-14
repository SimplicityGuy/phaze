"""Unit tests for phaze.tasks._shared.agent_bootstrap (Phase 27 D-17).

Covers:
1. construct_agent_client(cfg) builds a PhazeAgentClient with base_url / token / timeout
   pulled from AgentSettings.
2. whoami_with_retry returns identity on success (no sleep, no retry).
3. whoami_with_retry short-circuits on AgentApiAuthError WITHOUT consuming any
   backoff entries (RESEARCH Pitfall 7); log captured contains the operator-
   actionable "auth invalid" hint.
4. whoami_with_retry exhausts the backoff budget on persistent AgentApiServerError
   and raises RuntimeError; call_count == 7 (6 backoff entries + 1 final attempt).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

from pydantic import SecretStr
import pytest

from phaze.config import AgentSettings
from phaze.services.agent_client import AgentApiAuthError, AgentApiServerError, PhazeAgentClient
from phaze.tasks._shared import agent_bootstrap as ab


def _build_agent_settings(monkeypatch: pytest.MonkeyPatch) -> AgentSettings:
    """Build an AgentSettings instance bypassing env-var resolution."""
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://app.test:8000")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-xyz")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music")
    return AgentSettings()


def test_construct_agent_client_uses_cfg_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """construct_agent_client(cfg) returns a PhazeAgentClient with base_url/token from cfg."""
    cfg = _build_agent_settings(monkeypatch)

    client = ab.construct_agent_client(cfg)

    try:
        assert isinstance(client, PhazeAgentClient)
        assert client.base_url == "http://app.test:8000"
        # The bearer is stashed inside the underlying httpx.AsyncClient default
        # headers -- D-13 / T-26-02-I hardening means it MUST NOT be an instance
        # attribute. Verify both invariants.
        assert client._client.headers.get("Authorization") == "Bearer phaze_agent_test-token-xyz"
        assert not hasattr(client, "token"), "T-26-02-I violation: token must not be instance attribute"
        assert client._client.timeout.connect == 30.0
    finally:
        import asyncio

        asyncio.run(client.close())


def test_construct_agent_client_does_not_log_secret(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-27-04 mitigation: construct_agent_client must not emit any log line that
    includes the cleartext bearer token. Verified by sweeping caplog for the
    secret bytes."""
    cfg = _build_agent_settings(monkeypatch)
    # Override the token with a synthetic secret bytes pattern we can grep for.
    cfg = cfg.model_copy(update={"agent_token": SecretStr("phaze_agent_BYTES-1234-ABCDEF")})

    with caplog.at_level(logging.DEBUG, logger="phaze.tasks._shared.agent_bootstrap"):
        client = ab.construct_agent_client(cfg)

    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "BYTES-1234-ABCDEF" not in text, f"T-27-04 violation: token leaked into logs: {text!r}"

    import asyncio

    asyncio.run(client.close())


@pytest.mark.asyncio
async def test_whoami_with_retry_returns_identity_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """First successful whoami() returns the identity without sleeping."""
    fake_identity = object()
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=fake_identity)

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(ab.asyncio, "sleep", _fake_sleep)

    result = await ab.whoami_with_retry(fake_client)

    assert result is fake_identity
    assert fake_client.whoami.call_count == 1
    assert sleep_calls == [], "no backoff should be consumed on success"


@pytest.mark.asyncio
async def test_whoami_with_retry_short_circuits_on_auth_error(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RESEARCH Pitfall 7: AgentApiAuthError on the FIRST attempt raises immediately.

    Asserts:
    - client.whoami is called EXACTLY ONCE (no retries consumed)
    - RuntimeError is raised with the operator-facing "auth invalid" hint
    - ERROR-level log captured contains "auth invalid"
    - The raw bearer token is NEVER in the log line (T-27-04)
    """
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(side_effect=AgentApiAuthError("GET /api/internal/agent/whoami -> 401"))

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(ab.asyncio, "sleep", _fake_sleep)

    with (
        caplog.at_level(logging.ERROR, logger="phaze.tasks._shared.agent_bootstrap"),
        pytest.raises(RuntimeError, match="auth invalid"),
    ):
        await ab.whoami_with_retry(fake_client)

    assert fake_client.whoami.call_count == 1, "Pitfall 7: must short-circuit on first AgentApiAuthError"
    assert sleep_calls == [], "no backoff entries should be consumed before short-circuit"

    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "auth invalid" in text, f"expected operator-actionable hint in log: {text!r}"

    # T-27-04: no bearer token leakage. The exception message includes
    # "GET /api/internal/agent/whoami -> 401" -- a status mapping, not the
    # token itself. Verify no token-looking byte sequences appear.
    assert "phaze_agent_" not in text, f"T-27-04 violation: token prefix in log: {text!r}"


@pytest.mark.asyncio
async def test_whoami_with_retry_exhausts_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistent AgentApiServerError exhausts the backoff budget and raises RuntimeError.

    With 6 backoff entries + 1 final no-sleep attempt, total calls = 7.
    """
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(side_effect=AgentApiServerError("GET /whoami -> 503 after retries"))

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(ab.asyncio, "sleep", _fake_sleep)

    with pytest.raises(RuntimeError, match="exhausted retry budget"):
        await ab.whoami_with_retry(fake_client)

    # 6 backoff entries (each followed by sleep) + 1 final attempt = 7 calls.
    assert fake_client.whoami.call_count == 7, f"expected 7 whoami calls, got {fake_client.whoami.call_count}"
    # The 6 sleep calls correspond to the entries in _WHOAMI_BACKOFF_S.
    assert sleep_calls == list(ab._WHOAMI_BACKOFF_S), f"sleep budget mismatch: {sleep_calls}"


# ---------------------------------------------------------------------------
# Coverage gap fill (Codecov PR #59): agent_bootstrap.py:105-107
# Pitfall 7 — the existing short-circuit test covers AgentApiAuthError on the
# FIRST attempt (inside the for-loop). Lines 105-107 are the FINAL no-delay
# attempt's AgentApiAuthError branch — only reachable when 401/403 surfaces
# *after* all 6 backoff entries have been spent on transient AgentApiServer-
# Errors. This regression-pins that path: an admin who rotates the token
# mid-bootstrap (turning a transient 503 into a permanent 401 on the final
# retry) still surfaces the Pitfall-7 hint rather than the generic
# "exhausted retry budget" message.
# ---------------------------------------------------------------------------


async def test_whoami_with_retry_short_circuits_on_auth_error_in_final_attempt(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AgentApiAuthError on the FINAL no-delay attempt raises RuntimeError
    with the Pitfall-7 hint (agent_bootstrap.py:105-107)."""
    fake_client = AsyncMock()
    # First 6 attempts: AgentApiServerError (so we consume the backoff budget
    # and reach the final no-delay attempt). 7th attempt: AgentApiAuthError.
    fake_client.whoami = AsyncMock(
        side_effect=[
            *(AgentApiServerError(f"GET /whoami -> 503 attempt {i}") for i in range(6)),
            AgentApiAuthError("GET /whoami -> 401 after token rotation"),
        ]
    )

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(ab.asyncio, "sleep", _fake_sleep)

    with caplog.at_level(logging.ERROR, logger="phaze.tasks._shared.agent_bootstrap"), pytest.raises(RuntimeError, match="rejected by server"):
        await ab.whoami_with_retry(fake_client)

    assert fake_client.whoami.call_count == 7, "must invoke final attempt before raising"
    text = "\n".join(r.getMessage() for r in caplog.records)
    # Pitfall-7 hint surfaces the actionable env-var name (not just "401").
    assert "PHAZE_AGENT_TOKEN" in text or "auth invalid" in text, f"missing Pitfall-7 hint in log: {text!r}"
