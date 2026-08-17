"""Metadata extraction triggers + metadata failure retries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

# The suppression below is deliberate (runtime import, NOT type-only): this module carries
# `from __future__ import annotations`, so ruff offers to move `uuid` into the TYPE_CHECKING block.
# FastAPI resolves route annotations at RUNTIME via get_type_hints, so a `file_id: uuid.UUID` path
# param would raise NameError on import. (Before phaze-0jpe this import also had a plain runtime
# use -- `uuid.uuid4()` for the scan_live_set nonce -- which masked the rule; the annotation
# requirement is the real reason it must stay here.)
import uuid  # noqa: TC003

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import exists, select

from phaze.database import get_session
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.routers.pipeline._common import _NO_ACTIVE_AGENT_MESSAGE, _background_tasks, _stage_pill_oob, logger, router, templates
from phaze.schemas.agent_tasks import ExtractMetadataPayload
from phaze.services import enqueue_router
from phaze.services.pipeline import get_file_stage_buckets, get_metadata_failed_files, get_metadata_pending_files


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _enqueue_extraction_jobs(queue: Any, files: list[FileRecord], agent_id: str) -> None:
    """Background coroutine to enqueue extract_file_metadata jobs with the COMPLETE payload.

    The agent worker validates ``ExtractMetadataPayload`` with ``extra="forbid"`` and four
    required fields (file_id, original_path, file_type, agent_id). A ``file_id``-only enqueue
    therefore fails validation and dead-letters EVERY job -- the same defect that bit the
    pre-Phase-30 ``process_file`` path (see ``analysis_enqueue.enqueue_process_file``) and the
    v4.0.8 payload incident. D-06 removed the only other producer (the agent file-upsert
    auto-enqueue), making this manual trigger the SOLE metadata producer, so the full payload
    MUST be built here. ``model_dump(mode="json")`` serializes the UUID as a string so the
    worker's ``model_validate`` accepts it. The deterministic key
    (``extract_file_metadata:<file_id>``) is applied centrally by the ``before_enqueue`` hook
    (35-01), so no explicit ``key=`` is set here.

    phaze-ysz16: each file's enqueue is individually contained, mirroring phaze-4ter's
    ``_enqueue_analysis_jobs`` containment. Pre-fix a bare loop with no per-item try/except meant
    the FIRST transient broker/pool error aborted every remaining file in the group, surfacing
    only as asyncio's uncorrelated GC-time "Task exception was never retrieved" log (every caller
    detaches this via ``asyncio.create_task`` + a bare ``_background_tasks.discard`` done-callback
    that never calls ``task.result()``) while the response had already reported the full count.
    Nothing here mutates durable state before the enqueue, so a dropped file stays in the derived
    pending set for an idempotent re-click; this fix makes the drop visible in a correlated log
    instead of losing every remaining file to one failure.
    """
    dropped = 0
    for f in files:
        payload = ExtractMetadataPayload(
            file_id=f.id,
            original_path=f.original_path,
            file_type=f.file_type,
            agent_id=agent_id,
        )
        try:
            await queue.enqueue("extract_file_metadata", **payload.model_dump(mode="json"))
        except Exception:
            dropped += 1
            logger.exception("_enqueue_extraction_jobs: failed to enqueue extract_file_metadata job", file_id=str(f.id))
    if dropped:
        logger.warning(
            "_enqueue_extraction_jobs: files dropped from this run -- pending set unaffected, re-click will retry",
            dropped=dropped,
            total=len(files),
        )


@router.post("/api/v1/extract-metadata")
async def trigger_metadata_extraction(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue extract_file_metadata jobs for eligible music/video files (READ-01 cutover).

    Per D-04: originally queued every music/video file regardless of status, for backfill. READ-01
    replaced that state-agnostic selector with the DERIVED pending set (see
    :func:`get_metadata_pending_files`, ``eligible_clause(Stage.METADATA)``): a file whose metadata
    is genuinely done is excluded, while a not-started or failed one stays eligible (auto-retry).
    Per D-09: manual API endpoint for re-extraction.
    """
    files = await get_metadata_pending_files(session)

    if not files:
        return {"enqueued": 0, "message": "No music/video files found"}

    try:
        # phaze-c9w9: group by each file's OWNING agent -- never one most-recently-seen pick
        # for the whole set (owner-offline files are skipped, not rerouted).
        routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files("extract_file_metadata", request.app.state, session, files)
    except enqueue_router.NoActiveAgentError:
        return {"enqueued": 0, "message": _NO_ACTIVE_AGENT_MESSAGE}

    for routed, group in routed_groups:
        task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, group, cast("str", routed.agent_id)))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    enqueued = sum(len(group) for _, group in routed_groups)
    message = f"Enqueued {enqueued} files for metadata extraction"
    if skipped_files:
        message += f" ({len(skipped_files)} skipped: owning agent offline)"
    return {"enqueued": enqueued, "message": message}


@router.post("/pipeline/extract-metadata", response_class=HTMLResponse)
async def trigger_extraction_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger metadata extraction and return response fragment."""
    files = await get_metadata_pending_files(session)
    count = 0
    no_active_agent = False

    if files:
        try:
            # phaze-c9w9: group by each file's OWNING agent -- never one most-recently-seen pick.
            routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files(
                "extract_file_metadata", request.app.state, session, files
            )
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True
        else:
            if skipped_files:
                logger.warning("trigger_extraction_ui: owning agent offline -- files skipped", skipped=len(skipped_files))
            for routed, group in routed_groups:
                count += len(group)
                task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, group, cast("str", routed.agent_id)))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "metadata extraction", "count": count, "no_active_agent": no_active_agent},
    )


@router.post("/pipeline/metadata-failed/retry", response_class=HTMLResponse)
async def retry_metadata_failed(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated BULK retry of every terminally-failed metadata file (FAIL-03).

    Closes gap G-01 (SC#3): a metadata failure persisted by the 81-03 writer (``metadata`` row
    with ``failed_at`` set, payload NULL) derives FAILED and would otherwise be a permanent
    dead-end blocking the file from ever reaching ``propose``. This is the operator-gated retry:
    it re-drives EVERY ``metadata.failed_at IS NOT NULL`` file through the SAME guarded funnel the
    manual metadata triggers use -- per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`_enqueue_extraction_jobs` (the COMPLETE ``ExtractMetadataPayload``, not a
    dead-lettering file_id-only enqueue) + the central deterministic ``extract_file_metadata:<id>``
    dedup key.

    It mirrors :func:`retry_analysis_failed`'s Phase-30-hardened ordering, MINUS the state flip:
    - Resolve the per-agent queue ONCE. ``extract_file_metadata`` is an AGENT_TASK; if no agent is
      online ``NoActiveAgentError`` is caught and the endpoint returns a fragment WITHOUT enqueuing
      or mutating any state -- it never falls through to the consumer-less default queue (Phase-30).
    - D-11: NO ``f.state`` flip. Metadata has no terminal FileState -- the failure lives only in the
      ``metadata`` failure row. The row is LEFT in place; clearing ``failed_at`` here would make a
      zero-metadata file read DONE forever. ``put_metadata``'s clear-on-success (81-03) wipes the
      marker only when real metadata lands, or ``report_metadata_failed`` re-stamps it on another
      failure. With no state mutation there is nothing to commit before the enqueue.
    - The deterministic key dedups any file with a live in-flight job to a no-op, so re-enqueuing
      the WHOLE failed set is safe (dedup-safe; no silent cap).
    - phaze-zecg: ``_enqueue_extraction_jobs`` runs as a BACKGROUND task here too (matching every
      other caller of it), so a large failed set can no longer time out the HTTP request/proxy.
    """
    files = await get_metadata_failed_files(session)
    if not files:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        # phaze-c9w9: group the failed set by each file's OWNING agent -- never one
        # most-recently-seen pick for the whole set (owner-offline files are skipped, not rerouted).
        routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files("extract_file_metadata", request.app.state, session, files)
    except enqueue_router.NoActiveAgentError:
        # Do NOT enqueue, do NOT mutate state, do NOT fall through to the default queue (Phase-30).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    if skipped_files:
        logger.warning("retry_metadata_failed: owning agent offline -- files left failed", skipped=len(skipped_files))
    routed_count = sum(len(group) for _, group in routed_groups)

    # D-11: no state flip, so no pre-enqueue commit. Build the COMPLETE payload via the shared
    # producer (a file_id-only enqueue dead-letters every job) and rely on the central
    # extract_file_metadata:<file_id> key for in-flight dedup.
    #
    # phaze-zecg: BACKGROUND the enqueue loop instead of awaiting it inline -- `_enqueue_extraction_
    # jobs` is docstring-labeled "Background coroutine" and every OTHER caller (`trigger_metadata_
    # extraction`, `trigger_extraction_ui`) already wraps it in `asyncio.create_task` + the
    # `_background_tasks` discipline specifically "to avoid HTTP timeout on large file counts" -- this
    # bulk retry was the one caller that awaited it inline, so a large failed set could time out the
    # HTTP request/proxy mid-loop.
    for routed, group in routed_groups:
        task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, group, cast("str", routed.agent_id)))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    logger.info("retry_metadata_failed re-queued files", count=routed_count)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/metadata_retry_response.html",
        context={"request": request, "count": routed_count, "no_active_agent": False},
    )


@router.post("/pipeline/files/{file_id}/metadata-failed/retry", response_class=HTMLResponse)
async def retry_metadata_failed_file(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated PER-FILE retry of ONE terminally-failed metadata file (87-07, UI-02 / D-04).

    The scoped twin of :func:`retry_metadata_failed`: it re-drives EXACTLY ONE file through the
    identical guarded funnel (per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`_enqueue_extraction_jobs` with the COMPLETE ``ExtractMetadataPayload`` + the central
    ``extract_file_metadata:<id>`` dedup key), scoped by ``id == file_id AND EXISTS(a metadata row
    with failed_at)`` so a non-failed (or unknown) file is a safe no-op ack (T-87-27).

    D-11: NO ``f.state`` flip and NO ``failed_at`` clear — metadata has no terminal FileState and the
    failure lives only in the ``metadata`` row; clearing it here would make a zero-metadata file read
    DONE forever. ``put_metadata``'s clear-on-success wipes the marker only when real metadata lands.
    With no state mutation there is nothing to commit before the enqueue. The deterministic key dedups
    a live in-flight job to a no-op (T-87-26). The ack is count/bool-only (T-d79-04).
    """
    file = (
        await session.execute(
            select(FileRecord).where(
                FileRecord.id == file_id,
                exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.isnot(None))),
            ),
        )
    ).scalar_one_or_none()
    if file is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        # phaze-c9w9: route to the FILE's owning agent (agent_id=file.agent_id), never the
        # most-recently-seen fileserver -- an owner-offline file surfaces no_active_agent.
        routed = await enqueue_router.resolve_queue_for_task("extract_file_metadata", request.app.state, session, agent_id=file.agent_id)
    except enqueue_router.NoActiveAgentError:
        # Do NOT enqueue, do NOT mutate state, do NOT fall through to the default queue (Phase-30, T-87-25).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    # extract_file_metadata is an AGENT_TASK -- resolve always returns a non-None agent_id.
    agent_id = cast("str", routed.agent_id)

    # D-11: no state flip, so no pre-enqueue commit. The shared producer builds the COMPLETE payload
    # and the central extract_file_metadata:<file_id> key dedups an in-flight job.
    await _enqueue_extraction_jobs(routed.queue, [file], agent_id)

    logger.info("retry_metadata_failed_file re-queued", file_id=str(file_id))
    # phaze-bgz26: same shape as retry_analysis_failed_file -- OOB-push the Files-matrix pill this
    # write invalidated. D-11 means `failed_at` is NOT cleared here, so the re-derived bucket
    # stays "failed" until `put_metadata`'s clear-on-success later lands real metadata; pushing it
    # anyway keeps the pill's id fresh in the DOM rather than claiming a bucket flip that has not
    # happened yet (it will land on the next per-file retry / force-skip / poll-driven action).
    buckets = await get_file_stage_buckets(session, file_id)
    ack = templates.get_template("pipeline/partials/metadata_retry_response.html").render(count=1, no_active_agent=False)
    pill_oob = _stage_pill_oob(file_id, "metadata", buckets.get("metadata", "failed"), id_prefix="files-stage-pill")
    return HTMLResponse(ack + pill_oob)
