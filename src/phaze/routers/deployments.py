"""Receive a host-side inventory without exposing Docker to application containers."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.deployment import Deployment
from phaze.schemas.deployment import ContainerReport, DeploymentReport


router = APIRouter(prefix="/api/internal/deployments", tags=["deployment"])


def _role_and_lane(container: ContainerReport) -> tuple[str | None, str | None]:
    """Fixed Compose services imply a role; only `worker` needs a reported role."""
    if container.service == "api":
        return "api", None
    if container.service == "worker":
        role = container.role if container.role in {"control", "agent"} else None
        return role, container.lane if role == "agent" else None
    if container.service.startswith("worker-"):
        return "agent", container.service.removeprefix("worker-")
    return "agent", None


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def report_deployments(body: DeploymentReport, session: Annotated[AsyncSession, Depends(get_session)]) -> Response:
    """Replace one host's running inventory; absence from a new report removes a stopped container.

    The endpoint follows the existing private-LAN admin API posture. The host reporter only sends
    allowlisted Phaze Compose services and never sends Docker environment or mount fields.
    """
    ids = {container.container_id for container in body.containers}
    await session.execute(delete(Deployment).where(Deployment.host == body.host, Deployment.container_id.not_in(ids)))
    for container in body.containers:
        role, lane = _role_and_lane(container)
        statement = insert(Deployment).values(
            container_id=container.container_id,
            host=body.host,
            service=container.service,
            role=role,
            lane=lane,
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
                    "role": statement.excluded.role,
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
