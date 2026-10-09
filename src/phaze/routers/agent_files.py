"""POST /api/internal/agent/files -- chunked, idempotent file upsert (phase-25 D-20..D-22).

Idempotent on the composite natural key `(agent_id, original_path)` via
`INSERT ... ON CONFLICT DO UPDATE`.

Phase 35 (D-06): this handler NO LONGER auto-enqueues the metadata-extraction task.
Metadata extraction is operator-triggered ONLY (MANUAL-META) -- discovery just persists
rows. The `enqueued` field of the response is retained for schema stability and is always 0.

Per AUTH-01: `agent_id` comes from `Depends(get_authenticated_agent)` -- the
request schema has no agent_id field, so accidental body forgery returns
422 `extra_forbidden`.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, cast
import unicodedata
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import Executable, Row, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_analysis import PresignDownloadMetadata, PresignDownloadResponse
from phaze.schemas.agent_files import FileMoveRequest, FileMoveResponse, FileUpsertChunk, FileUpsertResponse
from phaze.services import backend_breaker, s3_staging
from phaze.services.agent_upsert import file_row, repoint_file
from phaze.services.companion import MEDIA_TYPES
from phaze.services.companion_autolink import request_association
from phaze.services.live_sentinel import ensure_live_sentinel
from phaze.services.scan_deletion import delete_file_cascade, invalidate_content_state, retention_blockers


if TYPE_CHECKING:
    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


# Bug 260706-vqz (first live k8s cloud-burst E2E, 2026-07-07, image 2026.7.3): the staged object
# lives in the bucket from UPLOADED through RUNNING until post-success cleanup, so all three are
# downloadable. ``submit_cloud_job`` stamps SUBMITTED at Kueue Job creation, BEFORE the analyze
# pod runs and calls presign-download, so a live pod NEVER observes UPLOADED -- an UPLOADED-only
# guard was unreachable for the pod and cloud analysis could never complete. UPLOADING is not yet
# fully staged, and terminal SUCCEEDED/FAILED may already be cleaned up -- all three 409. This is
# deliberately NOT the services/backends.py IN_FLIGHT tuple (that includes UPLOADING).
_PRESIGN_DOWNLOADABLE_STATUSES: frozenset[str] = frozenset(
    {
        CloudJobStatus.UPLOADED.value,
        CloudJobStatus.SUBMITTED.value,
        CloudJobStatus.RUNNING.value,
    }
)

router = APIRouter(prefix="/api/internal/agent/files", tags=["agent-internal"])


async def _resolve_batch_id(session: AsyncSession, agent: Agent, batch_id: uuid.UUID | None) -> uuid.UUID:
    """Resolve the scan batch new rows bind to: the body's ``batch_id`` after the tenancy check, else the LIVE sentinel."""
    # Phase 27 D-09 + D-18 + D-21: resolve batch_id BEFORE the records loop.
    # Cross-tenant guard returns 403 BEFORE any FileRecord insert -- mirrors the
    # D-08 authorization-first placement in ``agent_proposals.patch_proposal_state`` and
    # ``agent_scan_batches.patch_scan_batch``. T-27-02: a leaked
    # batch_id cannot be probed by attempting an upsert, because the 403
    # rejection precedes the records loop.
    if batch_id is not None:
        batch = await session.get(ScanBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan batch not found")
        if batch.agent_id != agent.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="scan batch does not belong to authenticated agent",
            )
        return batch.id
    # D-18: batch_id omitted -> resolve the calling agent's LIVE sentinel
    # batch from the bearer-token-derived agent_id. The partial unique index
    # `uq_scan_batches_agent_id_live` only guarantees AT MOST one LIVE row per
    # agent -- it does NOT guarantee one exists. A sentinel is normally
    # created at agent-registration time (`phaze agents add` / the dev seed,
    # both via `services.live_sentinel.ensure_live_sentinel`), but an agent
    # registered another way (e.g. a raw `INSERT INTO agents`) has none.
    # phaze-tvdu1: self-heal instead of crashing -- a missing sentinel used
    # to raise `NoResultFound` here (an uncaught 500 that dropped the whole
    # chunk); it is now created on demand via the same shared helper so the
    # only user-visible effect is a one-time WARNING log.
    stmt = select(ScanBatch.id).where(
        ScanBatch.agent_id == agent.id,
        ScanBatch.status == ScanStatus.LIVE.value,
    )
    existing_batch_id = (await session.execute(stmt)).scalar_one_or_none()
    if existing_batch_id is not None:
        return existing_batch_id
    logger.warning(
        "upsert_files: agent has no LIVE sentinel scan batch; self-healing by creating one",
        agent_id=agent.id,
    )
    return await ensure_live_sentinel(session, agent.id)


async def _upsert_rows(session: AsyncSession, raw_records: list[dict[str, Any]]) -> Sequence[Row[Any]]:
    """``INSERT ... ON CONFLICT DO UPDATE`` the rows on ``(agent_id, original_path)``; returns ``(id, file_type, original_path, inserted)``."""
    # RESEARCH Pitfall 4: same-chunk dedup on (original_path) -- last write wins.
    # Postgres rejects multiple rows targeting the same conflict-target within one stmt.
    deduped: dict[str, dict[str, Any]] = {}
    for rec in raw_records:
        deduped[rec["original_path"]] = rec

    # phaze-zfxy6: sort the deduped VALUES rows by `original_path` before the multi-row
    # `ON CONFLICT DO UPDATE` below. Postgres locks each conflicting pre-existing row in
    # VALUES order, which was previously the agent's directory-walk order -- an order this
    # statement shares no column with `delete_scan_cascade`'s FOR UPDATE sweep
    # (services/scan_deletion.py), which locks the same batch's file rows in a different
    # order. Two multi-row lockers over an overlapping row set (a rescan reassigning a
    # completed batch's files to the live batch, concurrent with an operator deleting that
    # completed batch) that acquire locks in different orders is a classic ABBA deadlock.
    # `original_path` is the only column both sides can sort on (this upsert's natural key
    # is `(agent_id, original_path)`; the cascade only has `batch_id`), so both now use it,
    # giving the two lockers one global acquisition order and making the cycle impossible.
    records = sorted(deduped.values(), key=lambda rec: rec["original_path"])

    # UPSERT with insert-detection (RESEARCH Pattern 2; D-12 + D-21).
    # Mirrors ``services.ingestion``. `inserted` (xmax = 0) is retained so the
    # response can report how many rows were newly INSERTed vs updated.
    base_stmt = pg_insert(FileRecord).values(records)
    upsert_stmt: Executable = base_stmt.on_conflict_do_update(
        index_elements=["agent_id", "original_path"],  # composite FileRecord natural key
        set_={
            "sha256_hash": base_stmt.excluded.sha256_hash,
            "file_size": base_stmt.excluded.file_size,
            # An agent rescan of an existing file refreshes only its content facts
            # (hash/size/batch/file_type); identity columns are never touched -- the
            # conflict target (agent_id, original_path) and the server-generated `id`
            # are preserved, so a rescan can never re-key or duplicate a known file.
            # AUTH-01 unchanged (agent_id still stamped from the auth dep, never body).
            "batch_id": base_stmt.excluded.batch_id,
            "file_type": base_stmt.excluded.file_type,
            # TimestampMixin.updated_at's ORM onupdate=func.now() never fires on this Core ON
            # CONFLICT DO UPDATE path -- stamp it explicitly so a rescan bumps updated_at instead
            # of freezing it at first discovery (phaze-c8nz). created_at stays pinned.
            "updated_at": func.now(),
            # phaze-5rfev: a file reported at this path again is, by definition, not missing.
            "missing_at": None,
        },
    ).returning(
        FileRecord.id,
        FileRecord.file_type,
        FileRecord.original_path,
        literal_column("(xmax = 0)").label("inserted"),
    )
    result = await session.execute(upsert_stmt)
    return result.all()


@router.post("", status_code=status.HTTP_200_OK, response_model=FileUpsertResponse)
async def upsert_files(
    body: FileUpsertChunk,
    request: Request,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FileUpsertResponse:
    """Idempotently upsert a chunk of FileRecord rows for the calling agent.

    - Stamps `agent_id` from auth dep (NEVER from body -- AUTH-01).
    - NFC-normalizes `original_path` on receive (RESEARCH Pitfall 7).
    - Server-side dedups same-chunk records on `original_path` (RESEARCH Pitfall 4)
      to avoid Postgres "cannot affect row a second time" errors on duplicate
      natural keys within one statement.
    - Phase 35 (D-06): does NOT auto-enqueue the metadata-extraction task. The
      `enqueued` count is always 0 (metadata extraction is operator-triggered only).
    - Returns `(upserted, inserted, enqueued)` counts.
    - phaze-spd83: a chunk that INSERTED a media row requests the agent's automatic companion
      association run, after the commit: a new media path may be what a waiting companion
      references, or the stem, folder, twin or close name it links by. A re-upsert of a known
      path changes nothing association reads (it reads paths only), so it requests nothing.
    """
    resolved_batch_id = await _resolve_batch_id(session, agent, body.batch_id)
    rows = await _upsert_rows(session, [file_row(r, agent.id, resolved_batch_id) for r in body.files])
    await session.commit()
    if any(row.inserted and row.file_type in MEDIA_TYPES for row in rows):
        await request_association(request.app.state, agent.id)

    # Phase 35 (D-06): NO auto-enqueue of the metadata-extraction task. Discovery persists
    # rows; metadata extraction is operator-triggered only (MANUAL-META). `enqueued` is
    # always 0, kept on the response for schema stability.
    return FileUpsertResponse(
        agent_id=agent.id,
        upserted=len(rows),
        inserted=sum(1 for r in rows if r.inserted),
        enqueued=0,
    )


@router.post("/move", status_code=status.HTTP_200_OK, response_model=FileMoveResponse)
async def move_file(
    body: FileMoveRequest,
    request: Request,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FileMoveResponse:
    """Record a settled file that arrived by an in-tree move: keep ONE row for it, at its new path (phaze-oxn2m).

    The watcher posts here instead of to the plain upsert when the settled path was reached by a
    paired ``FileMovedEvent``. Rows are looked up on ``(agent_id, original_path)`` for the new path
    and every previous path, and locked ``FOR UPDATE`` in ``original_path`` order -- the one global
    order ``upsert_files`` and ``delete_scan_cascade`` already share (phaze-zfxy6).

    - **No row at any previous path** (rsync's temp name, or a file never posted under its old
      name): ``file`` is upserted exactly as ``upsert_files`` would. Outcome ``upserted``.
    - **A row at a previous path and none at the new one:** the newest such row is re-pointed --
      ``original_path``/``current_path``, the filename columns, the hash, size and type all take the
      new record's values, its id and every child row stay. If the hash changed (the row was
      ingested mid-write), the state computed from the old bytes is invalidated
      (``scan_deletion.invalidate_content_state``). Outcome ``moved``.
    - **A row already at the new path** (a manual scan got there first): that row is upserted, and
      the previous-path rows are stale.
    - Every other previous-path row is stale -- its path was moved away from -- and is retired with
      ``scan_deletion.delete_file_cascade``, each retirement logged with both paths.

    Only rows at EXACTLY the listed paths are considered (no prefix or wildcard), at most
    ``MOVE_LINEAGE_MAX`` of them, and only those plausibly the same file
    (:func:`_plausibly_same_file`); any other row named is logged and left untouched.

    A row carrying operator-reviewed state (``scan_deletion.retention_blockers``) is never
    re-pointed or retired: it is logged and left in place. When it is the row that would have been
    re-pointed, ``file`` is upserted as its own row -- what every watcher move did before this
    endpoint existed -- and the outcome is ``kept_reviewed``.

    Every outcome leaves a file at a new path, which can change what a companion links to, so each
    requests the agent's automatic companion association run after the commit (phaze-spd83).
    """
    resolved_batch_id = await _resolve_batch_id(session, agent, None)
    row = file_row(body.file, agent.id, resolved_batch_id)
    new_path: str = row["original_path"]
    previous_paths = list(dict.fromkeys(p for p in (unicodedata.normalize("NFC", p) for p in body.previous_paths) if p != new_path))

    locked = (
        (
            await session.execute(
                select(FileRecord)
                .where(FileRecord.agent_id == agent.id, FileRecord.original_path.in_([*previous_paths, new_path]))
                .order_by(FileRecord.original_path)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_path = {record.original_path: record for record in locked}
    stale = []
    for path in previous_paths:
        if path not in by_path:
            continue
        if _plausibly_same_file(by_path[path], row):
            stale.append(by_path[path])
        else:
            logger.warning(
                "move_file: ignoring a previous path whose row is not this file", file_id=str(by_path[path].id), previous_path=path, new_path=new_path
            )

    outcome = "upserted"
    content_changed = False
    file_id: uuid.UUID | None = None
    if stale and new_path not in by_path:
        keeper = stale.pop()  # the newest name a row was posted under
        content_changed = keeper.sha256_hash != row["sha256_hash"]
        blockers = await retention_blockers(session, keeper, content_changed=content_changed)
        if blockers:
            logger.warning("move_file: previous row carries reviewed state; keeping it", file_id=str(keeper.id), blockers=blockers)
            outcome = "kept_reviewed"
        else:
            logger.info(
                "move_file: re-pointing row",
                agent_id=agent.id,
                file_id=str(keeper.id),
                old_path=keeper.original_path,
                new_path=new_path,
                content_changed=content_changed,
            )
            repoint_file(keeper, row)
            await session.flush()
            if content_changed:
                await invalidate_content_state(session, keeper.id)
            outcome, file_id = "moved", keeper.id
    if file_id is None:
        file_id = (await _upsert_rows(session, [row]))[0].id

    retired = 0
    for record in stale:
        blockers = await retention_blockers(session, record, content_changed=True, retiring=True)
        if blockers:
            logger.warning("move_file: stale previous-path row carries reviewed state; keeping it", file_id=str(record.id), blockers=blockers)
            continue
        logger.info(
            "move_file: retiring stale row",
            agent_id=agent.id,
            file_id=str(record.id),
            old_path=record.original_path,
            new_path=new_path,
            kept_file_id=str(file_id),
        )
        await delete_file_cascade(session, record.id)
        retired += 1
    await session.commit()
    await request_association(request.app.state, agent.id)
    logger.info("move_file", agent_id=agent.id, file_id=str(file_id), outcome=outcome, content_changed=content_changed, retired=retired)
    return FileMoveResponse(agent_id=agent.id, file_id=file_id, outcome=outcome, content_changed=content_changed, retired=retired)


def _plausibly_same_file(record: FileRecord, row: dict[str, Any]) -> bool:
    """Whether the row at a previous path can be the file that moved: same name, same size, or same hash.

    The controller cannot see the agent's disk, so a previous path is the agent's claim. This keeps a
    bad claim from re-pointing or retiring an unrelated row: a move keeps the content (same hash or,
    for a file posted mid-download into a preallocated file, the same size -- all 62 measured pairs),
    and a directory move keeps the name. A file both renamed AND posted mid-download with a different
    size fails all three; it gets its own row, which is what every move did before phaze-oxn2m.
    """
    return bool(
        record.original_filename == row["original_filename"] or record.file_size == row["file_size"] or record.sha256_hash == row["sha256_hash"]
    )


async def _close_breaker_on_presign(session: AsyncSession, backend_id: str) -> None:
    """Close ``backend_id``'s control-plane-unreachable breaker, if open (phaze-j0ixx). Never raises.

    Best-effort by design: the presign has already been minted and the pod is waiting for it, so a
    failure here must not turn a working request into a 500. The breaker stays open and the drain's next
    probe gets another chance to close it.
    """
    try:
        closed = await backend_breaker.close_breaker(session, backend_id, datetime.now(UTC), evidence="presign succeeded")
        if closed:
            await session.commit()
    except Exception:
        logger.warning("presign_download: could not close the backend breaker", backend_id=backend_id, exc_info=True)
        await session.rollback()


@router.post("/{file_id}/presign-download", status_code=status.HTTP_200_OK, response_model=PresignDownloadResponse)
async def presign_download(
    file_id: uuid.UUID,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PresignDownloadResponse:
    """Mint a just-in-time presigned GET URL for a file's staged bytes (Phase 53, KSTAGE-03).

    Completes the SERVER side of the Phase 52 pod client ``request_download_url``: the DB-less
    one-shot pod POSTs here at startup and downloads the bytes from the returned short-TTL URL,
    verifying them against ``expected_sha256``.

    AUTH-01: ``file_id`` rides the URL PATH only; the agent identity comes from the token
    dependency, and no request body is accepted. The presign is minted FRESH per call
    (KSTAGE-03 -- never at submit time, so it never expires during a Kueue wait). The
    ``expected_sha256`` is read SERVER-side from ``FileRecord.sha256_hash`` (T-53-06 / the
    single integrity gate, D-04) -- never echoed from the request -- and the
    ``Field(pattern=...)`` on the response catches any format skew at the wire boundary.

    An unknown ``file_id`` is a clean 404, never a 500.

    Phase 100 (phaze-sfbx.1): also returns an optional ``metadata`` display-identity block
    (``PresignDownloadMetadata``) built from the SAME ``FileRecord``/``CloudJob`` rows this
    handler already loads for the readiness gate above, plus one narrow extra
    ``FileMetadata.duration`` select (OBS-02 wants duration in the pod banner; it lives on a
    separate 1:1 table this handler otherwise never touches) -- no change to the auth gating or
    the 404/409 readiness paths, which are both fully resolved before that extra select runs.
    Consumed by the pod's console banner (phaze-sfbx.3).
    """
    # Touch ``agent`` so ARG001 doesn't fire; the binding's real role is auth-gating (AUTH-01).
    _ = agent.id

    file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
    if file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")

    # Readiness guard (WR-03): the presign is purely computational and always succeeds, so without
    # this check we could hand back a well-formed but DEAD URL for an object that was never staged or
    # was already evicted (inline cleanup / Phase 54 eviction / lifecycle TTL). Require the cloud_job
    # to be in a downloadable/staged status (_PRESIGN_DOWNLOADABLE_STATUSES = {UPLOADED, SUBMITTED,
    # RUNNING}); otherwise 409 so the pod (or Phase 54 reconcile) sees "not ready" at the control plane
    # instead of taking an opaque 403/404 from S3 mid-download. Single-user system: NO per-agent
    # ownership predicate -- cross-agent access is by design (file_id is path-only, AUTH-01), not an IDOR.
    # Phase 100 (phaze-sfbx.1): the select is extended with `backend_id` (over the pre-existing
    # `status`/`staging_bucket` pair) so the display-metadata block below is populated from THIS
    # row -- no second CloudJob query. Purely additive to the readiness gating above/below.
    cloud_job_row = (
        await session.execute(select(CloudJob.status, CloudJob.staging_bucket, CloudJob.backend_id).where(CloudJob.file_id == file_id))
    ).first()
    cloud_job_status = cloud_job_row.status if cloud_job_row is not None else None
    if cloud_job_row is None or cloud_job_status not in _PRESIGN_DOWNLOADABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"staged object not ready (cloud_job status={cloud_job_status!r})",
        )

    # MKUE-02 (Pitfall 4): presign against the RECORDED staging bucket -- resolve the id stamped at stage
    # time, never re-derive via pick_bucket (a config-set change would then mis-point the presign). An
    # UPLOADED row with no resolvable bucket is a corrupt state -> 409 rather than a dead URL from S3.
    bucket = s3_staging.resolve_bucket_config(cast("ControlSettings", get_settings()), cloud_job_row.staging_bucket)
    if bucket is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="staged object has no resolvable staging bucket recorded",
        )
    download_url = await s3_staging.presign_get(file_id, bucket)
    # Phase 100 (phaze-sfbx.1): display-metadata block for the pod's console banner
    # (phaze-sfbx.3), populated from the FileRecord + CloudJob rows already loaded above (no
    # new query, no effect on the gating raised earlier in this handler) PLUS one narrow extra
    # select for `duration` -- FileMetadata is a separate 1:1-with-files table this handler
    # otherwise never touches. `scalar_one_or_none()` tolerates a file with no metadata row yet
    # (extraction is operator-triggered, MANUAL-META) -> duration_sec stays None rather than 500ing.
    duration = (await session.execute(select(FileMetadata.duration).where(FileMetadata.file_id == file_id))).scalar_one_or_none()
    display_metadata = PresignDownloadMetadata(
        original_filename=file.original_filename,
        current_path=file.current_path,
        source_agent_id=file.agent_id,
        duration_sec=duration,
        file_size=file.file_size,
        staging_bucket=cloud_job_row.staging_bucket,
        backend_id=cloud_job_row.backend_id,
    )
    # Thread the file's real audio extension so the DB-less pod can name its temp file
    # <file_id>.<ext> (essentia detects format by extension). The staged S3 key carries
    # no extension, so without this the pod falls back to `.audio` -> essentia decodes 0
    # duration -> 0 windows -> a silent empty-but-"successful" analysis (cloud-analyze-
    # empty-no-ext). `file_type` is the dotless extension (e.g. "mp3"), the same value
    # agent_push/push use to name their scratch copies.
    response = PresignDownloadResponse(
        download_url=download_url,
        expected_sha256=file.sha256_hash,
        audio_ext=file.file_type,
        metadata=display_metadata,
    )
    # phaze-j0ixx: a pod on this backend has just reached the control plane -- the one thing an open
    # control-plane-unreachable breaker is waiting to see -- so close it here, on proof rather than a timer.
    # Last, after every ORM attribute the response needs has been read, because the close commits.
    if cloud_job_row.backend_id is not None:
        await _close_breaker_on_presign(session, cloud_job_row.backend_id)
    return response
