"""Host inventory remains per-container and visible through the Agents refresh."""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.deployment import Deployment
from phaze.routers import admin_agents, deployments


_API_ID = "a" * 64
_WORKER_ID = "b" * 64
_ANALYZE_ID = "c" * 64
_META_ID = "d" * 64
_DIGEST = "sha256:" + "e" * 64


def _app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.include_router(deployments.router)
    app.include_router(admin_agents.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest.mark.asyncio
async def test_mixed_versions_and_unknown_metadata_survive_refresh(session: AsyncSession) -> None:
    """Each lane has its own immutable identity and older report shapes remain accepted."""
    async with AsyncClient(transport=ASGITransport(app=_app(session)), base_url="http://test") as client:
        first = await client.post(
            "/api/internal/deployments",
            json={
                "host": "host-prod",
                "containers": [
                    {"container_id": _API_ID, "service": "api", "app_version": "2026.9.7", "image_ref": "phaze:current", "image_digest": _DIGEST},
                    {"container_id": _WORKER_ID, "service": "worker", "app_version": "2026.9.7"},
                    {"container_id": _ANALYZE_ID, "service": "worker-analyze", "app_version": "2026.9.6", "future_field": "ignored"},
                    {"container_id": _META_ID, "service": "worker-meta", "app_version": "2026.9.7", "image_digest": _DIGEST},
                ],
            },
        )
        assert first.status_code == 204
        rows = (await session.execute(select(Deployment).order_by(Deployment.container_id))).scalars().all()
        assert len(rows) == 4
        assert {row.app_version for row in rows} == {"2026.9.6", "2026.9.7"}
        assert next(row for row in rows if row.container_id == _ANALYZE_ID).lane == "analyze"
        assert next(row for row in rows if row.container_id == _WORKER_ID).image_digest is None

        html = (await client.get("/admin/agents/_table")).text
        assert "Deployments" in html
        assert "host-prod" in html
        assert "2026.9.6" in html
        assert "Unknown" in html
        assert _DIGEST in html
        assert "Current" in html

        await session.execute(
            Deployment.__table__.update().where(Deployment.host == "host-prod").values(observed_at=datetime.now(UTC) - timedelta(minutes=3))
        )
        await session.commit()
        session.expire_all()
        stale_html = (await client.get("/admin/agents/_table")).text
        assert "Stale" in stale_html


@pytest.mark.asyncio
async def test_snapshot_removes_only_stopped_containers_on_reporting_host(session: AsyncSession) -> None:
    """A new report removes a stopped lane, without touching another host's containers."""
    async with AsyncClient(transport=ASGITransport(app=_app(session)), base_url="http://test") as client:
        for host, container_id in (("host-prod", _API_ID), ("host-store", _ANALYZE_ID)):
            assert (
                await client.post("/api/internal/deployments", json={"host": host, "containers": [{"container_id": container_id, "service": "api"}]})
            ).status_code == 204
        assert (await client.post("/api/internal/deployments", json={"host": "host-prod", "containers": []})).status_code == 204
        rows = (await session.execute(select(Deployment))).scalars().all()
        assert [row.container_id for row in rows] == [_ANALYZE_ID]


@pytest.mark.asyncio
async def test_inventory_rejects_unbounded_and_non_phaze_data(session: AsyncSession) -> None:
    """Host reports cannot smuggle archive paths, credentials, or foreign services."""
    async with AsyncClient(transport=ASGITransport(app=_app(session)), base_url="http://test") as client:
        for report in (
            {"host": "../../archive", "containers": []},
            {"host": "host-prod", "containers": [{"container_id": _API_ID, "service": "postgres"}]},
            {"host": "host-prod", "containers": [{"container_id": _API_ID, "service": "api", "image_ref": "user:password@registry/phaze:tag"}]},
        ):
            assert (await client.post("/api/internal/deployments", json=report)).status_code == 422
