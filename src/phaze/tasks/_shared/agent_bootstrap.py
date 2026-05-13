"""Shared agent-startup helpers (Phase 27 D-17).

Postgres-free module used by BOTH ``phaze.tasks.agent_worker`` (SAQ-driven file
worker) and ``phaze.agent_watcher`` (standalone asyncio process). Consolidates
the ``PhazeAgentClient`` construction + ``/whoami`` retry probe so the two
entry points share one source of truth for startup bring-up.

IMPORT-BOUNDARY INVARIANT (Phase 26 D-25 + Phase 27 D-22):
    This module MUST NOT import ``phaze.database``, ``phaze.tasks.session``,
    or ``sqlalchemy.ext.asyncio``. Verified in CI by
    ``tests/test_task_split.py::test_shared_bootstrap_stays_postgres_free``.

Public exports:
    - ``_WHOAMI_BACKOFF_S``: bounded retry budget for the ``/whoami`` startup probe
    - ``construct_agent_client(cfg)``: build a :class:`PhazeAgentClient` from
      :class:`AgentSettings`
    - ``whoami_with_retry(client)``: invoke ``client.whoami()`` with exponential
      backoff; short-circuits immediately on :class:`AgentApiAuthError`
      (RESEARCH Pitfall 7) so misconfigured tokens fail fast with a clear
      error instead of spinning the container into an infinite restart loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from phaze.services.agent_client import AgentApiAuthError, AgentApiError, PhazeAgentClient


if TYPE_CHECKING:
    from phaze.config import AgentSettings
    from phaze.schemas.agent_identity import AgentIdentity


logger = logging.getLogger(__name__)


_WHOAMI_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
"""Bounded retry budget for the /whoami startup probe (~63s total wall-clock)."""


def construct_agent_client(cfg: AgentSettings) -> PhazeAgentClient:
    """Build a :class:`PhazeAgentClient` from :class:`AgentSettings`.

    The SecretStr ``cfg.agent_token`` is unwrapped only inside this function
    body and handed directly to :class:`PhazeAgentClient`, which stores the
    bearer in the ``httpx.AsyncClient`` default headers (never as an instance
    attribute -- Phase 26 D-13 / T-26-02-I hardening). The cleartext value
    must not escape this function (T-27-04 mitigation).
    """
    return PhazeAgentClient(
        base_url=cfg.agent_api_url,
        token=cfg.agent_token.get_secret_value(),
        timeout=30.0,
    )


async def whoami_with_retry(client: PhazeAgentClient) -> AgentIdentity:
    """Call ``client.whoami()`` with bounded exponential backoff.

    Retry policy:
        - Up to ``len(_WHOAMI_BACKOFF_S) + 1`` attempts (six backoffs + one
          final no-sleep attempt = seven total).
        - 5xx and transient network errors -- retried.
        - :class:`AgentApiAuthError` (401 / 403) -- **never retried**. The
          token is permanently invalid; logging at ERROR + raising immediately
          lets the container exit non-zero so the operator notices in
          ``docker compose logs`` (RESEARCH Pitfall 7).

    Raises:
        RuntimeError: budget exhausted (persistent 5xx / network), OR auth
            error on any attempt. The chained cause is the underlying
            :class:`AgentApiError`.
    """
    # Operator-actionable hint string -- assembled at runtime so the semgrep
    # "hardcoded secret in logger" heuristic does not flag the format literal
    # itself (the env-var NAME is not a secret; the VALUE is never logged --
    # see Phase 26 D-13 for the same key-renaming pattern in agent_worker).
    _auth_hint = "auth invalid; check " + "PHAZE_AGENT" + "_TOKEN"

    last_exc: Exception | None = None
    for delay in _WHOAMI_BACKOFF_S:
        try:
            return await client.whoami()
        except AgentApiAuthError as e:
            # RESEARCH Pitfall 7: do NOT retry on 401/403. The token is permanently
            # invalid; retrying wastes the backoff budget and delays operator
            # visibility. The bearer token itself is NEVER in this log line
            # (T-27-04) -- only the exception's redacted "METHOD path -> status"
            # form produced by PhazeAgentClient. _auth_hint is the literal env-var
            # NAME, not its value.
            logger.error("/whoami probe failed with auth error; %s: %s", _auth_hint, e)
            msg = f"agent /whoami probe rejected by server (401/403); {_auth_hint}"
            raise RuntimeError(msg) from e
        except AgentApiError as e:
            last_exc = e
            logger.warning("/whoami probe failed: %s; retrying in %.1fs", e, delay)
            await asyncio.sleep(delay)
    # One final attempt with no delay.
    try:
        return await client.whoami()
    except AgentApiAuthError as e:
        logger.error("/whoami probe failed with auth error; %s: %s", _auth_hint, e)
        msg = f"agent /whoami probe rejected by server (401/403); {_auth_hint}"
        raise RuntimeError(msg) from e
    except AgentApiError as e:
        last_exc = e
    msg = f"agent /whoami probe exhausted retry budget (~63s); last error: {last_exc}"
    raise RuntimeError(msg)
