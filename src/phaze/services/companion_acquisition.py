"""Small capture-attempt evidence separate from immutable semantic observations."""

from collections.abc import Sequence
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.companion_import import ProviderAcquisitionAttempt
from phaze.schemas.agent_companion_capture import CaptureReport
from phaze.schemas.companion_import import LatestSourceAttempt
from phaze.services.provider_persistence import StoredObservation


async def record_capture_attempt(session: AsyncSession, agent_id: str, report: CaptureReport, stored: StoredObservation, freshness: str) -> None:
    """Called while capture retains its source bucket; ordinals describe received order."""
    if report.attempt_id is None:
        # Rolling-upgrade callbacks cannot distinguish transport retries. Keep history unknown.
        return
    envelope = {
        "target": report.target.model_dump(mode="json"),
        "budget": report.budget.model_dump(mode="json"),
        "attempted_at": report.read.retrieved_at.isoformat(),
    }
    previous = await session.get(ProviderAcquisitionAttempt, report.attempt_id)
    if previous is not None:
        if previous.agent_id != agent_id or previous.observation_id != stored.observation.id or previous.envelope != envelope:
            raise ValueError("Capture attempt UUID reused with different report")
        return
    ordinal = await session.scalar(
        select(func.coalesce(func.max(ProviderAcquisitionAttempt.ordinal), 0)).where(ProviderAcquisitionAttempt.source_object_id == stored.source.id)
    )
    session.add(
        ProviderAcquisitionAttempt(
            attempt_id=report.attempt_id,
            source_object_id=stored.source.id,
            observation_id=stored.observation.id,
            ordinal=int(ordinal or 0) + 1,
            agent_id=agent_id,
            attempted_at=report.read.retrieved_at,
            status=stored.observation.status,
            code=stored.observation.code,
            revision=stored.observation.revision,
            revision_scope=stored.observation.revision_scope,
            truncated=stored.observation.truncated,
            freshness=freshness,
            origin="capture",
            envelope=envelope,
        )
    )
    await session.flush()


async def get_latest_source_attempts(session: AsyncSession, object_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, LatestSourceAttempt]:
    """At most fifty narrow rows; never load observation JSON/text for an attempt listing."""
    if len(object_ids) > 50:
        raise ValueError("Attempt summaries accept at most 50 source objects")
    if not object_ids:
        return {}
    columns = [getattr(ProviderAcquisitionAttempt, name) for name in LatestSourceAttempt.model_fields]
    rows = (
        (
            await session.execute(
                select(*columns)
                .where(ProviderAcquisitionAttempt.source_object_id.in_(object_ids))
                .distinct(ProviderAcquisitionAttempt.source_object_id)
                .order_by(ProviderAcquisitionAttempt.source_object_id, ProviderAcquisitionAttempt.ordinal.desc())
            )
        )
        .mappings()
        .all()
    )
    return {row["source_object_id"]: LatestSourceAttempt.model_validate(dict(row)) for row in rows}
