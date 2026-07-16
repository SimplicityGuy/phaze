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

from typing import TYPE_CHECKING, Annotated, Any, cast
import unicodedata
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Executable, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_analysis import PresignDownloadMetadata, PresignDownloadResponse
from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertResponse
from phaze.services import s3_staging


if TYPE_CHECKING:
    from phaze.config import ControlSettings


# Bug 260706-vqz (first live k8s cloud-burst E2E, 2026-07-07, image 2026.7.3): the staged object
# lives in the bucket from UPLOADED through RUNNING until post-success cleanup, so all three are
# downloadable. submit_cloud_job.py:117 stamps SUBMITTED at Kueue Job creation, BEFORE the analyze
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


@router.post("", status_code=status.HTTP_200_OK, response_model=FileUpsertResponse)
async def upsert_files(
    body: FileUpsertChunk,
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
    """
    # Phase 27 D-09 + D-18 + D-21: resolve batch_id BEFORE the records loop.
    # Cross-tenant guard returns 403 BEFORE any FileRecord insert -- mirrors the
    # Phase 26 D-08 placement in agent_proposals.py:62-76 (and the new
    # agent_scan_batches.py PATCH handler). T-27-02 mitigation: a leaked
    # batch_id cannot be probed by attempting an upsert, because the 403
    # rejection precedes the records loop.
    if body.batch_id is not None:
        batch = await session.get(ScanBatch, body.batch_id)
        if batch is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scan batch not found")
        if batch.agent_id != agent.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="scan batch does not belong to authenticated agent",
            )
        resolved_batch_id = batch.id
    else:
        # D-18: batch_id omitted -> resolve the calling agent's LIVE sentinel
        # batch from the bearer-token-derived agent_id. The partial unique index
        # `uq_scan_batches_agent_id_live` guarantees exactly one row exists for
        # any registered agent (Phase 24 D-11 + D-12), so `.scalar_one()` is
        # safe -- a missing sentinel is an operator-actionable invariant
        # violation (500-level), not a 4xx contract failure.
        stmt = select(ScanBatch.id).where(
            ScanBatch.agent_id == agent.id,
            ScanBatch.status == ScanStatus.LIVE.value,
        )
        resolved_batch_id = (await session.execute(stmt)).scalar_one()

    # 1. Build raw record dicts with agent_id stamped from auth dep (NEVER from body)
    raw_records: list[dict[str, Any]] = []
    for r in body.files:
        data = r.model_dump()
        # RESEARCH Pitfall 7: NFC-normalize defensively
        data["original_path"] = unicodedata.normalize("NFC", data["original_path"])
        data["agent_id"] = agent.id  # AUTH-01 -- stamped from auth, NEVER from body
        data["id"] = uuid.uuid4()  # server-generates new id; ON CONFLICT preserves existing id
        data["batch_id"] = resolved_batch_id  # Phase 27 D-09/D-18 -- server resolves; never from body
        raw_records.append(data)

    # 2. RESEARCH Pitfall 4: same-chunk dedup on (original_path) -- last write wins.
    # Postgres rejects multiple rows targeting the same conflict-target within one stmt.
    deduped: dict[str, dict[str, Any]] = {}
    for rec in raw_records:
        deduped[rec["original_path"]] = rec
    records = list(deduped.values())

    # 3. UPSERT with insert-detection (RESEARCH Pattern 2; D-12 + D-21).
    # Mirrors services/ingestion.py:103-117. `inserted` (xmax = 0) is retained so the
    # response can report how many rows were newly INSERTed vs updated.
    base_stmt = pg_insert(FileRecord).values(records)
    upsert_stmt: Executable = base_stmt.on_conflict_do_update(
        index_elements=["agent_id", "original_path"],  # composite UQ from models/file.py:61
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
        },
    ).returning(
        FileRecord.id,
        FileRecord.file_type,
        FileRecord.original_path,
        literal_column("(xmax = 0)").label("inserted"),
    )
    result = await session.execute(upsert_stmt)
    rows = result.all()
    await session.commit()

    # Phase 35 (D-06): NO auto-enqueue of the metadata-extraction task. Discovery persists
    # rows; metadata extraction is operator-triggered only (MANUAL-META). `enqueued` is
    # always 0, kept on the response for schema stability.
    return FileUpsertResponse(
        agent_id=agent.id,
        upserted=len(rows),
        inserted=sum(1 for r in rows if r.inserted),
        enqueued=0,
    )


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
    return PresignDownloadResponse(
        download_url=download_url,
        expected_sha256=file.sha256_hash,
        audio_ext=file.file_type,
        metadata=display_metadata,
    )
