"""PhazeAgentClient -- internal-agent HTTP wrapper (Phase 26 D-09..D-13).

Single httpx.AsyncClient wrapper every file-bound SAQ task on the agent uses
to POST/PUT/PATCH state changes back to the application server.

The client funnels every call through a tenacity retry loop that retries 5xx
and transient network errors three times with exponential-jitter backoff but
**never retries 4xx** (auth or validation errors must surface immediately).

Decisions:
- D-09: mirrors DiscogsographyClient pattern (one httpx.AsyncClient per instance).
- D-10: one method per endpoint, Pydantic models in/out.
- D-11: tenacity funnel, ``stop_after_attempt(3)``, ``wait_exponential_jitter``,
  4xx never retried, 5xx + ConnectError/Timeout retried.
- D-12: 4-class exception hierarchy -- ``AgentApiError`` base + ``AgentApiAuthError``
  (401/403, no retry) + ``AgentApiClientError`` (other 4xx, no retry) +
  ``AgentApiServerError`` (5xx + network, after retry exhaustion).
- D-13: DEBUG on success, WARNING on failure; bearer token NEVER logged.

Schemas referenced in TYPE_CHECKING block live in ``phaze.schemas.agent_*`` --
Phase 25 modules (files/metadata/fingerprint/execution/heartbeat) already exist;
Phase 26 Plan 03 modules (identity/analysis/tracklists/proposals) land in
parallel. Endpoint methods import response schemas lazily inside the method
body so this module loads independent of Plan 03's merge order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter


if TYPE_CHECKING:
    import ssl
    import uuid

    # Phase 26 Plan 03 schemas (now merged; type: ignore tripwires retired).
    from phaze.schemas.agent_analysis import (
        AnalysisFailurePayload,
        AnalysisFailureResponse,
        AnalysisWritePayload,
        AnalysisWriteResponse,
    )

    # Phase 28 schema (D-06).
    from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload

    # Phase 25 schemas (already exist).
    from phaze.schemas.agent_execution import (
        ExecutionLogCreate,
        ExecutionLogCreateResponse,
        ExecutionLogPatch,
        ExecutionLogPatchResponse,
    )
    from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertResponse
    from phaze.schemas.agent_fingerprint import FingerprintWriteRequest, FingerprintWriteResponse
    from phaze.schemas.agent_heartbeat import HeartbeatRequest
    from phaze.schemas.agent_identity import AgentIdentity
    from phaze.schemas.agent_metadata import MetadataWriteRequest, MetadataWriteResponse
    from phaze.schemas.agent_proposals import (
        ProposalStatePatch,
        ProposalStateResponse,
    )
    from phaze.schemas.agent_scan_batches import ScanBatchPatch, ScanBatchPatchResponse
    from phaze.schemas.agent_tracklists import (
        TracklistCreatePayload,
        TracklistCreateResponse,
    )


logger = structlog.get_logger(__name__)


class AgentApiError(Exception):
    """Base for all PhazeAgentClient errors."""


class AgentApiAuthError(AgentApiError):
    """401 / 403 from the server. NEVER retried (D-12)."""


class AgentApiClientError(AgentApiError):
    """Any 4xx that is not auth. NEVER retried (D-12)."""


class AgentApiServerError(AgentApiError):
    """5xx after retries exhausted, or persistent ConnectError/Timeout (D-12)."""


def _should_retry(exc: BaseException) -> bool:
    """Retry only on transient network errors and 5xx HTTP responses.

    NEVER retry on 4xx -- auth/validation errors must surface immediately
    (D-11, D-32). Tested in ``tests/test_services/test_agent_client.py`` via
    ``route.call_count`` assertions.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


class PhazeAgentClient:
    """HTTP client adapter for the internal agent API on the application server.

    Mirrors the ``DiscogsographyClient`` pattern (``services/discogs_matcher.py``):
    construct with ``base_url`` + ``token``, call async methods, ``close()`` when
    done. The bearer token is injected as a default ``Authorization`` header on
    the underlying ``httpx.AsyncClient`` so every request inherits it; the token
    is **never** stored as an instance attribute (D-13 hardening; see threat
    model T-26-02-I).

    The ``_client`` constructor parameter exists for respx test injection only
    (leading underscore = private). Production code never passes it.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        verify: ssl.SSLContext | str | bool = True,
        _client: httpx.AsyncClient | None = None,
    ) -> None:
        """Construct the client.

        Phase 29 D-03/D-04: ``verify`` is threaded through to
        ``httpx.AsyncClient(verify=...)``. Accepts an ``ssl.SSLContext``,
        a file path string pointing at a CA bundle, or a bool. Default
        ``True`` preserves backwards compatibility with all existing
        respx-based tests (RESEARCH Pitfall 10) -- respx mocks below
        the TLS layer so cert validation is bypassed there.

        Production callers (``construct_agent_client`` in
        ``phaze.tasks._shared.agent_bootstrap``) pass
        ``verify=cfg.agent_ca_file`` so the agent's httpx client trusts
        the operator-distributed internal CA and rejects any other.
        """
        self.base_url = base_url
        self._client = _client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=verify,
        )

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient (releases connection pool)."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Retry funnel -- every endpoint method routes through here (D-11).
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute one HTTP call with tenacity retry policy.

        Retry policy (D-11):
        - 3 attempts total; ``wait_exponential_jitter(initial=0.5, max=4.0)``.
        - Retry on ``ConnectError``, ``ReadTimeout``, ``WriteTimeout``, and 5xx.
        - 4xx surfaces immediately (no retry) via ``_should_retry``.

        Exception mapping (D-12):
        - 401 / 403 -> ``AgentApiAuthError``.
        - Other 4xx -> ``AgentApiClientError``.
        - 5xx after retries / persistent network -> ``AgentApiServerError``.
        """
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential_jitter(initial=0.5, max=4.0),
                retry=retry_if_exception(_should_retry),
                reraise=True,
            ):
                with attempt:
                    response = await self._client.request(method, path, **kwargs)
                    response.raise_for_status()
                    logger.debug("agent_api method=%s path=%s status=%d", method, path, response.status_code)
                    return response
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            logger.warning(
                "agent_api method=%s path=%s status=%d error=HTTPStatusError",
                method,
                path,
                status_code,
            )
            if status_code in (401, 403):
                raise AgentApiAuthError(f"{method} {path} -> {status_code}") from e
            if 400 <= status_code < 500:
                raise AgentApiClientError(f"{method} {path} -> {status_code}: {e.response.text}") from e
            raise AgentApiServerError(f"{method} {path} -> {status_code} after retries") from e
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
            logger.warning("agent_api method=%s path=%s error=%s", method, path, type(e).__name__)
            raise AgentApiServerError(f"{method} {path} network failure after retries") from e
        # Defensive: tenacity AsyncRetrying with reraise=True always either returns
        # via ``return response`` above or re-raises. This line should be
        # unreachable; keep it as a tripwire if tenacity behavior changes.
        raise AssertionError("AsyncRetrying loop exited without returning or raising")

    # ------------------------------------------------------------------
    # Endpoint methods (D-10 -- one per /api/internal/agent/* resource).
    # ------------------------------------------------------------------

    async def whoami(self) -> AgentIdentity:
        """GET /api/internal/agent/whoami -- resolve token -> agent identity."""
        from phaze.schemas.agent_identity import AgentIdentity  # noqa: PLC0415

        response = await self._request("GET", "/api/internal/agent/whoami")
        return AgentIdentity.model_validate(response.json())

    async def upsert_files(self, payload: FileUpsertChunk) -> FileUpsertResponse:
        """POST /api/internal/agent/files -- chunked file-record upsert."""
        from phaze.schemas.agent_files import FileUpsertResponse  # noqa: PLC0415

        response = await self._request(
            "POST",
            "/api/internal/agent/files",
            json=payload.model_dump(mode="json"),
        )
        return FileUpsertResponse.model_validate(response.json())

    async def put_metadata(self, file_id: uuid.UUID, payload: MetadataWriteRequest) -> MetadataWriteResponse:
        """PUT /api/internal/agent/metadata/{file_id} -- partial metadata upsert (CR-01)."""
        from phaze.schemas.agent_metadata import MetadataWriteResponse  # noqa: PLC0415

        response = await self._request(
            "PUT",
            f"/api/internal/agent/metadata/{file_id}",
            json=payload.model_dump(mode="json", exclude_unset=True),
        )
        return MetadataWriteResponse.model_validate(response.json())

    async def put_fingerprint(
        self,
        file_id: uuid.UUID,
        engine: str,
        payload: FingerprintWriteRequest,
    ) -> FingerprintWriteResponse:
        """PUT /api/internal/agent/fingerprints/{file_id}/{engine} -- engine result."""
        from phaze.schemas.agent_fingerprint import FingerprintWriteResponse  # noqa: PLC0415

        response = await self._request(
            "PUT",
            f"/api/internal/agent/fingerprints/{file_id}/{engine}",
            json=payload.model_dump(mode="json", exclude_unset=True),
        )
        return FingerprintWriteResponse.model_validate(response.json())

    async def put_analysis(self, file_id: uuid.UUID, payload: AnalysisWritePayload) -> AnalysisWriteResponse:
        """PUT /api/internal/agent/analysis/{file_id} -- essentia analysis upsert (D-26)."""
        from phaze.schemas.agent_analysis import AnalysisWriteResponse  # noqa: PLC0415

        response = await self._request(
            "PUT",
            f"/api/internal/agent/analysis/{file_id}",
            json=payload.model_dump(mode="json", exclude_unset=True),
        )
        return AnalysisWriteResponse.model_validate(response.json())

    async def report_analysis_failed(self, file_id: uuid.UUID, payload: AnalysisFailurePayload) -> AnalysisFailureResponse:
        """POST /api/internal/agent/analysis/{file_id}/failed -- terminal-failure report (Phase 43).

        Marks the file ``ANALYSIS_FAILED`` on the control plane. Inherits the
        tenacity retry policy (D-11) + exception hierarchy (D-12) via the
        ``_request`` funnel -- 5xx retries, 4xx (e.g. a 422 on a bad body) surface
        immediately. ``file_id`` rides the path only (AUTH-01); the body carries
        ``reason``/``error``."""
        from phaze.schemas.agent_analysis import AnalysisFailureResponse  # noqa: PLC0415

        response = await self._request(
            "POST",
            f"/api/internal/agent/analysis/{file_id}/failed",
            json=payload.model_dump(mode="json"),
        )
        return AnalysisFailureResponse.model_validate(response.json())

    async def create_tracklist(self, payload: TracklistCreatePayload) -> TracklistCreateResponse:
        """POST /api/internal/agent/tracklists -- atomic tracklist insert (D-27)."""
        from phaze.schemas.agent_tracklists import TracklistCreateResponse  # noqa: PLC0415

        response = await self._request(
            "POST",
            "/api/internal/agent/tracklists",
            json=payload.model_dump(mode="json"),
        )
        return TracklistCreateResponse.model_validate(response.json())

    async def post_execution_log(self, payload: ExecutionLogCreate) -> ExecutionLogCreateResponse:
        """POST /api/internal/agent/execution-log -- INSERT-on-conflict-do-nothing."""
        from phaze.schemas.agent_execution import ExecutionLogCreateResponse  # noqa: PLC0415

        response = await self._request(
            "POST",
            "/api/internal/agent/execution-log",
            json=payload.model_dump(mode="json"),
        )
        return ExecutionLogCreateResponse.model_validate(response.json())

    async def patch_execution_log(
        self,
        execution_log_id: uuid.UUID,
        payload: ExecutionLogPatch,
    ) -> ExecutionLogPatchResponse:
        """PATCH /api/internal/agent/execution-log/{id} -- monotonic status update."""
        from phaze.schemas.agent_execution import ExecutionLogPatchResponse  # noqa: PLC0415

        response = await self._request(
            "PATCH",
            f"/api/internal/agent/execution-log/{execution_log_id}",
            json=payload.model_dump(mode="json", exclude_unset=True),
        )
        return ExecutionLogPatchResponse.model_validate(response.json())

    async def patch_proposal_state(
        self,
        proposal_id: uuid.UUID,
        payload: ProposalStatePatch,
    ) -> ProposalStateResponse:
        """PATCH /api/internal/agent/proposals/{id}/state -- joint Proposal + FileRecord (D-28)."""
        from phaze.schemas.agent_proposals import ProposalStateResponse  # noqa: PLC0415

        response = await self._request(
            "PATCH",
            f"/api/internal/agent/proposals/{proposal_id}/state",
            json=payload.model_dump(mode="json", exclude_unset=True),
        )
        return ProposalStateResponse.model_validate(response.json())

    async def patch_scan_batch(
        self,
        batch_id: uuid.UUID,
        payload: ScanBatchPatch,
    ) -> ScanBatchPatchResponse:
        """PATCH /api/internal/agent/scan-batches/{batch_id} -- update batch status/counts (Phase 27 D-10).

        Inherits the tenacity retry policy (D-11) + exception hierarchy (D-12)
        via the `_request` funnel -- 5xx retries, 4xx surface immediately.
        """
        from phaze.schemas.agent_scan_batches import ScanBatchPatchResponse  # noqa: PLC0415

        response = await self._request(
            "PATCH",
            f"/api/internal/agent/scan-batches/{batch_id}",
            json=payload.model_dump(mode="json", exclude_unset=True),
        )
        return ScanBatchPatchResponse.model_validate(response.json())

    async def post_exec_batch_progress(
        self,
        batch_id: uuid.UUID,
        payload: ExecBatchProgressPayload,
    ) -> None:
        """POST /api/internal/agent/exec-batches/{batch_id}/progress -- per-proposal terminal progress (Phase 28 D-05).

        Inherits the tenacity retry policy (D-11) + exception hierarchy (D-12)
        via the ``_request`` funnel -- 5xx retries, 4xx surface immediately.
        Caller in ``tasks/execution._execute_one`` (Plan 28-05) should swallow
        ``AgentApiError`` after retries (D-16); the underlying file ops are
        already committed and the per-proposal PATCH has already landed via
        ``patch_proposal_state``. Returns ``None`` (no response body -- the
        endpoint returns 200 with empty Response per D-05).
        """
        await self._request(
            "POST",
            f"/api/internal/agent/exec-batches/{batch_id}/progress",
            json=payload.model_dump(mode="json"),
        )
        return None

    async def heartbeat(self, payload: HeartbeatRequest) -> None:
        """POST /api/internal/agent/heartbeat -- agent liveness ping (204 No Content)."""
        await self._request(
            "POST",
            "/api/internal/agent/heartbeat",
            json=payload.model_dump(mode="json"),
        )
        return None
