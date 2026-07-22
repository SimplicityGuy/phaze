"""SAQ task: ``upload_file_s3`` (registered under the SAQ function name ``s3_upload``) --
httpx multipart-PUT upload of a media file to presigned URLs (Phase 53).

The file-server agent (which owns the media mount) uploads a cloud-routed file by PUTting each
multipart part to a presigned URL the control plane minted (control initiates the multipart upload,
presigns the part URLs, and completes it -- KSTAGE-02 / D-01). The agent holds NO S3 client SDK
and NO bucket credentials: the byte transfer is httpx-only. It collects each part's ETag from the PUT
response header and reports the ordered ``(part_number, etag)`` list through a control-side callback
(D-04); there are NO S3-side per-part checksums -- the pod's end-to-end sha256 is the single
integrity gate.

This module MUST NOT import the app ORM/async DB engine NOR the S3 client SDK -- the import
boundary is enforced by tests/shared/core/test_task_split.py (D-25 + KSTAGE-02 boundary). It carries ONLY
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
import contextlib
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

# phaze-g37f: the asyncio.wait_for guard is applied PER PART (inside the transfer loop) so it sits
# ABOVE each individual PUT's httpx timeout -- the per-request read kill fires first on a stall and
# this per-part layer is the belt-and-suspenders cap for the rare case one request wedges without
# honoring its own timeout. It is deliberately NOT wrapped around the whole multi-part loop: doing so
# bounded the ENTIRE transfer to one fixed budget, so every file larger than one part deterministically
# timed out (a multi-GB concert recording shares no single 630s wall-clock cap now).
_OUTER_TIMEOUT_BUFFER_SEC = 30

# WR-03 (mirrored from push): the SAQ job-net timeout a producer MUST stamp on an upload_file_s3
# enqueue. It sits strictly ABOVE the summed per-part asyncio guards so SAQ never cancels the
# coroutine before those guards fire. The upload leg reuses the agent's transport timeout
# (AgentSettings.push_timeout_sec, default 600) -- the file-server agent's generic byte-transfer I/O
# bound -- so no new config knob is introduced for the same deployment concern.
_SAQ_JOB_TIMEOUT_MARGIN_SEC = 30

# Per-part transport budget default: the control plane cannot see the agent's
# AgentSettings.push_timeout_sec, so the producer derives the SAQ net from the documented default.
_DEFAULT_PER_PART_TIMEOUT_SEC = 600


def upload_file_saq_timeout_sec(part_count: int, *, per_part_timeout_sec: int = _DEFAULT_PER_PART_TIMEOUT_SEC) -> int:
    """SAQ job-net timeout a producer MUST stamp on an ``s3_upload`` enqueue, SCALED by part count.

    Each part gets its OWN ``per_part_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC`` asyncio guard inside the
    transfer loop, so a multi-part upload's total wall-clock budget is the SUM of the per-part budgets.
    The SAQ net sits strictly ABOVE that sum (plus a fixed margin) so SAQ never cancels the coroutine
    before the in-loop guards fire (WR-03). A single fixed cap here (the pre-phaze-g37f behaviour)
    deterministically timed out every multi-GB upload because N parts shared one 660s budget.
    """
    parts = max(1, part_count)
    return (per_part_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC) * parts + _SAQ_JOB_TIMEOUT_MARGIN_SEC


# Single-part baseline, retained for callers/tests that reference a nominal value. Multi-part producers
# MUST call ``upload_file_saq_timeout_sec(part_count)`` so the SAQ net scales with the transfer.
UPLOAD_FILE_SAQ_TIMEOUT_SEC = upload_file_saq_timeout_sec(1)

# phaze-oj7x: the EXPLICIT SAQ retries a producer MUST stamp on an ``s3_upload`` enqueue -- ZERO. The
# control plane owns the sole re-drive vehicle (``/failed`` -> ``cloud_staging.redrive_upload``: abort the
# prior multipart + re-stage a FRESH one, and the age/liveness-bounded stranded-staging reaper). SAQ's own
# retry path MUST NOT independently replay the job: the agent calls ``report_upload_failed`` (which aborts
# the prior multipart and re-stages) BEFORE it re-raises, so a SAQ retry would run the ORIGINAL payload
# whose presigned part URLs point at the now-ABORTED multipart -- a guaranteed ``NoSuchUpload`` per part,
# burning the bounded re-drive budget on dead-URL replays and orphaning a fresh multipart each cycle. With
# ``retries=0`` the failing job settles ``failed`` (terminal) on the re-raise, releasing its deterministic
# ``s3_upload:<file_id>`` key so the control re-drive's / reaper's next enqueue can actually LAND (SAQ's
# ``_enqueue`` ON CONFLICT only overwrites a key whose status is in ('aborted','complete','failed')) instead
# of being shadowed by a still-active self-retrying job. Must be passed EXPLICITLY: the
# ``apply_project_job_defaults`` before_enqueue hook clobbers an unset ``retries`` (SAQ default 1) up to
# ``worker_max_retries`` (=4), so omitting it re-arms exactly the multi-retry replay this closes. Mirrors
# push.py's explicit ``PUSH_FILE_SAQ_RETRIES``.
S3_UPLOAD_SAQ_RETRIES = 0


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
                # phaze-1lvp: read each 64 MiB (default) part off-loop. The source is the
                # media mount (typically NFS/SMB on this deployment), so a plain fh.read()
                # blocks the agent worker's event loop for the read's duration -- and a hard
                # NFS stall blocks it indefinitely. That loop also runs the Phase-46 liveness
                # heartbeat, so an on-loop read starves the heartbeat (its own asyncio.wait_for
                # deadline cannot even fire on a blocked loop) and risks a false DEAD. Reads are
                # sequential and the handle is not shared across coroutines, so offloading only
                # fh.read to a thread is safe. Mirrors scan.py's to_thread discipline.
                chunk = await asyncio.to_thread(fh.read, payload.part_size_bytes)
                if not chunk:
                    # Source exhausted before all presigned URLs were used (e.g. a shorter file than
                    # the presign assumed). Stop -- the parts collected so far are the real upload.
                    break
                # phaze-g37f: per-PART wait_for. Each PUT gets its own budget (the transport timeout
                # plus the belt-and-suspenders buffer) so the total wall-clock scales with the number
                # of parts. A wedged single request is reaped here without capping the whole transfer.
                response = await asyncio.wait_for(
                    http.put(url, content=chunk),
                    timeout=transport_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC,
                )
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
    multipart upload and flips the file forward) and returns a status dict. A terminal/transfer
    failure -- a non-2xx part PUT, a missing/unreadable source, or a wedged per-part wait_for
    (RuntimeError/TimeoutError) -- calls ``api.report_upload_failed(file_id, detail)`` before
    re-raising, so control runs its bounded re-drive / at-cap spill instead of stranding the
    cloud_job UPLOADING (phaze-lssv). A SAQ job-net cancellation (CancelledError) reaps the in-flight
    request and re-raises WITHOUT a callback (SAQ owns the re-drive; no premature terminal report).
    NO local fallback ever.
    """
    payload = UploadFileS3Payload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]
    cfg = _agent_settings()

    try:
        parts = await _transfer_parts(payload, transport_timeout_sec=cfg.push_timeout_sec)
    except asyncio.CancelledError:
        # A SAQ job-net cancellation (CancelledError, NOT TimeoutError -- WR-03). The httpx
        # AsyncClient is closed by its ``async with`` on the way out, so the in-flight request is
        # reaped before we re-raise. SAQ owns the re-drive; we do NOT report completion or failure on
        # a cancelled transfer (no partial-success callback, no premature terminal report).
        raise
    except (RuntimeError, TimeoutError, httpx.HTTPError, OSError) as exc:
        # phaze-lssv: a terminal/transfer failure (unreadable source, non-2xx part PUT, or a wedged
        # per-part wait_for). The control plane's bounded re-drive / at-cap spill machinery lives
        # behind report_upload_failed; without this callback the cloud_job stays UPLOADING forever
        # (leaking a kueue in-flight cap slot and a staged S3 object -- SAQ's default retries=1 means
        # the first raise is terminal). Notify control, then re-raise so SAQ still marks the job
        # failed. The notify is best-effort: a failure here must not mask the original error.
        #
        # phaze-7lxp: catch httpx.HTTPError and OSError alongside the two synthetic errors
        # _transfer_parts raises itself. A real transport failure (connection reset, DNS blip, TLS
        # error, ConnectError, ReadTimeout, RemoteProtocolError) surfaces as an httpx.HTTPError
        # subclass -- NOT builtins.TimeoutError/RuntimeError -- and fires BEFORE the wait_for budget
        # (httpx's own timeout is 30s tighter). A mid-loop fh.read() failure surfaces as OSError.
        # Both previously bypassed this handler and propagated WITHOUT report_upload_failed, stranding
        # the cloud_job in UPLOADING until the 6h reaper backstop. The narrow tuple only covered the
        # transfer failures _transfer_parts raises by hand, missing the far more common transport
        # class the callback exists for. asyncio.CancelledError (a BaseException on 3.14) is caught by
        # the clause above and re-raised, so cancellation still never triggers a premature callback.
        with contextlib.suppress(Exception):
            await api.report_upload_failed(payload.file_id, detail=str(exc)[:_BODY_SNIPPET_MAX])
        raise

    await api.report_upload_complete(payload.file_id, parts)
    return {"file_id": str(payload.file_id), "status": "uploaded"}
