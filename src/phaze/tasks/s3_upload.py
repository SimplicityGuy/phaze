"""SAQ task: upload_file_s3 -- httpx multipart-PUT upload of a media file to presigned URLs (Phase 53).

The file-server agent (which owns the media mount) uploads a cloud-routed file by PUTting each
multipart part to a presigned URL the control plane minted (control initiates the multipart upload,
presigns the part URLs, and completes it -- KSTAGE-02 / D-01). The agent holds NO S3 client SDK
and NO bucket credentials: the byte transfer is httpx-only. It collects each part's ETag from the PUT
response header and reports the ordered ``(part_number, etag)`` list through a control-side callback
(D-04); there are NO S3-side per-part checksums -- the pod's end-to-end sha256 is the single
integrity gate.

This module MUST NOT import the app ORM/async DB engine NOR the S3 client SDK -- the import
boundary is enforced by tests/test_task_split.py (D-25 + KSTAGE-02 boundary). It carries ONLY
stdlib (asyncio/pathlib), phaze.config (AgentSettings narrowing), phaze.schemas.agent_s3, httpx,
and references PhazeAgentClient via ctx["api_client"] at runtime.

Transport invariants (mirrors push.py D-06/D-07 timeout layering):
- parts are streamed one at a time (read one ``part_size_bytes`` chunk, PUT it, release it) so peak
  memory is bounded to a single part regardless of file size (T-53-12 DoS bound).
- the outer ``asyncio.wait_for`` guard sits ABOVE the per-request httpx timeout so a SAQ job-net
  cancellation (CancelledError, NOT TimeoutError) and an outer wedge both reap the in-flight request
  before propagating -- the transfer is never swallowed (no silent partial-success callback).
- a non-2xx part PUT is a retryable RuntimeError (SAQ retry path).
- a missing/unreadable ``original_path`` is a clear TERMINAL error -- the task NEVER falls back to a
  local copy and NEVER reports completion (KSTAGE-02 invariant).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from phaze.config import AgentSettings, get_settings
from phaze.schemas.agent_s3 import UploadedPart, UploadFileS3Payload


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


# Bound the error snippet that crosses into a RuntimeError / SAQ job error so a runaway response body
# cannot ship a multi-megabyte string into the logs (T-53-12 DoS bound; mirrors push._STDERR_SNIPPET_MAX).
_BODY_SNIPPET_MAX = 500

# The outer asyncio.wait_for bound sits ABOVE the per-request httpx timeout so the per-request read
# kill fires first on a stall; this outer layer is the belt-and-suspenders cap for the rare case a
# request wedges without honoring its own timeout (mirrors the push_file inner<outer pattern).
_OUTER_TIMEOUT_BUFFER_SEC = 30

# WR-03 (mirrored from push): the SAQ job-net timeout a producer MUST stamp on an upload_file_s3
# enqueue. It sits strictly ABOVE the asyncio outer guard (push_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC)
# so SAQ never cancels the coroutine before that guard fires. The upload leg reuses the agent's
# transport timeout (AgentSettings.push_timeout_sec, default 600) -- the file-server agent's generic
# byte-transfer I/O bound -- so no new config knob is introduced for the same deployment concern.
_SAQ_JOB_TIMEOUT_MARGIN_SEC = 30
UPLOAD_FILE_SAQ_TIMEOUT_SEC = 600 + _OUTER_TIMEOUT_BUFFER_SEC + _SAQ_JOB_TIMEOUT_MARGIN_SEC


def _agent_settings() -> AgentSettings:
    """Return the AgentSettings for this worker process (mirrors push._agent_settings).

    ``upload_file_s3`` is registered ONLY on the agent worker (``PHAZE_ROLE=agent``), so
    ``get_settings()`` returns an :class:`AgentSettings`. The module-level ``settings`` singleton is
    ``ControlSettings``-typed and intentionally lacks the agent-only ``push_timeout_sec`` field, so we
    MUST resolve via ``get_settings()`` and narrow.
    """
    cfg = get_settings()
    if not isinstance(cfg, AgentSettings):  # pragma: no cover - defensive; worker always agent-role
        msg = f"upload_file_s3 requires PHAZE_ROLE=agent; get_settings() returned {type(cfg).__name__}"
        raise RuntimeError(msg)
    return cfg


async def _transfer_parts(payload: UploadFileS3Payload, *, transport_timeout_sec: int) -> list[UploadedPart]:
    """PUT each part to its presigned URL and return the ordered (part_number, etag) list.

    Reads one ``part_size_bytes`` chunk at a time so peak memory stays bounded to a single part.
    A missing source is a TERMINAL RuntimeError; a non-2xx part PUT is a retryable RuntimeError.
    """
    src = Path(payload.original_path)
    try:
        fh = src.open("rb")
    except OSError as exc:
        # Missing/unreadable source. TERMINAL -- NEVER fall back to a local copy (KSTAGE-02).
        msg = f"upload_file_s3: cannot read original_path {payload.original_path!r} for file_id={payload.file_id}: {exc}"
        raise RuntimeError(msg) from exc

    parts: list[UploadedPart] = []
    try:
        async with httpx.AsyncClient(timeout=transport_timeout_sec) as http:
            for part_number, url in enumerate(payload.part_urls, start=1):
                chunk = fh.read(payload.part_size_bytes)
                if not chunk:
                    # Source exhausted before all presigned URLs were used (e.g. a shorter file than
                    # the presign assumed). Stop -- the parts collected so far are the real upload.
                    break
                response = await http.put(url, content=chunk)
                if response.status_code // 100 != 2:
                    snippet = response.text[:_BODY_SNIPPET_MAX]
                    msg = f"upload_file_s3: part {part_number} PUT returned {response.status_code} for file_id={payload.file_id}: {snippet}"
                    raise RuntimeError(msg)
                etag = response.headers.get("ETag", "").strip('"')
                parts.append(UploadedPart(part_number=part_number, etag=etag))
    finally:
        fh.close()
    return parts


async def upload_file_s3(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Upload a cloud-routed file to presigned S3 part URLs over httpx, then report ETags via HTTP.

    Every part PUTs a ``part_size_bytes`` slice to ``part_urls[N-1]`` and collects the response
    ETag (D-04). On success -> ``api.report_upload_complete(file_id, parts)`` (control completes the
    multipart upload and flips the file forward) and returns a status dict. A non-2xx part ->
    RuntimeError (SAQ retry). A missing source -> TERMINAL RuntimeError, NO callback, NO local
    fallback. A SAQ job-net cancellation (CancelledError) or an outer wedge (TimeoutError) reaps the
    in-flight request and re-raises -- the transfer is never swallowed into a partial-success report.
    """
    payload = UploadFileS3Payload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]
    cfg = _agent_settings()

    try:
        parts = await asyncio.wait_for(
            _transfer_parts(payload, transport_timeout_sec=cfg.push_timeout_sec),
            timeout=cfg.push_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC,
        )
    except (TimeoutError, asyncio.CancelledError):
        # Outer-layer kill (a request wedged past its own timeout) OR a SAQ job-net cancellation
        # (CancelledError, NOT TimeoutError -- WR-03). The httpx AsyncClient is closed by its
        # ``async with`` on the way out, so the in-flight request is reaped before we re-raise. We do
        # NOT report completion on a cancelled transfer (no partial-success callback).
        raise

    await api.report_upload_complete(payload.file_id, parts)
    return {"file_id": str(payload.file_id), "status": "uploaded"}
