"""Database queries that materialize recovery classification inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import ARRAY, Select, bindparam, func, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from phaze.enums.stage import Stage
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.backends import IN_FLIGHT
from phaze.services.stage_status import cloud_lane_completed_clause, domain_completed_clause, skipped_clause
from phaze.tasks.recovery_policy import _DoneSets


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


def _fids_scope(fids: list[uuid.UUID], name: str) -> Any:
    """Build one ``uuid[]`` ANY bind, avoiding asyncpg's scalar-parameter ceiling."""
    return FileRecord.id == func.any(bindparam(name, value=fids, type_=ARRAY(PGUUID(as_uuid=True))))


def _select_done_analyze_ids(fids: list[uuid.UUID]) -> Select[tuple[uuid.UUID]]:
    """Select ledger-scoped file ids whose analyze domain is terminal."""
    return select(FileRecord.id).where(_fids_scope(fids, "a_ids"), domain_completed_clause(Stage.ANALYZE))


def _select_cloud_lane_done_ids(fids: list[uuid.UUID]) -> Select[tuple[uuid.UUID]]:
    """Select ledger-scoped file ids whose cloud lane has completed."""
    return select(FileRecord.id).where(_fids_scope(fids, "p_ids"), cloud_lane_completed_clause())


async def _build_done_sets(session: AsyncSession, fids: list[uuid.UUID]) -> _DoneSets:
    """Materialize all ledger-scoped completion facts once per recovery run."""
    if not fids:
        return _DoneSets(set(), set(), {}, set(), set())

    analyze_done = {str(fid) for fid in (await session.scalars(_select_done_analyze_ids(fids))).all()}
    metadata_domain_completed = {
        str(fid)
        for fid in (await session.scalars(select(FileRecord.id).where(_fids_scope(fids, "m_ids"), domain_completed_clause(Stage.METADATA)))).all()
    }
    metadata_failed_at = {
        str(file_id): failed_at
        for file_id, failed_at in (
            await session.execute(
                select(FileMetadata.file_id, FileMetadata.failed_at).where(
                    FileMetadata.file_id == func.any(bindparam("mf_ids", value=fids, type_=ARRAY(PGUUID(as_uuid=True)))),
                    FileMetadata.failed_at.isnot(None),
                )
            )
        ).all()
    }
    metadata_skipped = {
        str(fid) for fid in (await session.scalars(select(FileRecord.id).where(_fids_scope(fids, "ms_ids"), skipped_clause(Stage.METADATA)))).all()
    }
    cloud_lane_done = {str(fid) for fid in (await session.scalars(_select_cloud_lane_done_ids(fids))).all()}
    return _DoneSets(
        analyze_done=analyze_done,
        metadata_domain_completed=metadata_domain_completed,
        metadata_failed_at=metadata_failed_at,
        metadata_skipped=metadata_skipped,
        cloud_lane_done=cloud_lane_done,
    )


async def _awaiting_cloud_job_ids(session: AsyncSession) -> set[str]:
    """Return files held for the awaiting-cloud drain, recovery's exclusive co-owner."""
    return {str(fid) for fid in (await session.scalars(select(CloudJob.file_id).where(CloudJob.status == CloudJobStatus.AWAITING.value))).all()}


async def _in_flight_cloud_job_ids(session: AsyncSession) -> set[str]:
    """Return files currently owned by backend reconciliation or the pushed callback."""
    return {str(fid) for fid in (await session.scalars(select(CloudJob.file_id).where(CloudJob.status.in_([s.value for s in IN_FLIGHT])))).all()}
