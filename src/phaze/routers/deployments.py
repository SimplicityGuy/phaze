"""Receive a host-side inventory without exposing Docker to application containers."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.deployment import Deployment
from phaze.schemas.deployment import DeploymentReport


router = APIRouter(prefix="/api/internal/deployments", tags=["deployment"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def report_deployments(body: DeploymentReport, session: Annotated[AsyncSession, Depends(get_session)]) -> Response:
    """Replace one host's running inventory; absence from a new report removes a stopped container.

    The endpoint follows the existing private-LAN admin API posture. The host reporter only sends
    allowlisted Phaze Compose services and never sends Docker environment or mount fields.
    """
    ids = {container.container_id for container in body.containers}
    await session.execute(delete(Deployment).where(Deployment.host == body.host, Deployment.container_id.not_in(ids)))
    for container in body.containers:
        statement = insert(Deployment).values(
            container_id=container.container_id,
            host=body.host,
            service=container.service,
            lane=container.service.removeprefix("worker-") if container.service.startswith("worker-") else None,
            app_version=container.app_version,
            image_ref=container.image_ref,
            image_digest=container.image_digest,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[Deployment.container_id],
                set_={
                    "host": body.host,
                    "service": container.service,
                    "lane": statement.excluded.lane,
                    "app_version": container.app_version,
                    "image_ref": container.image_ref,
                    "image_digest": container.image_digest,
                    "observed_at": statement.excluded.observed_at,
                },
            )
        )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
