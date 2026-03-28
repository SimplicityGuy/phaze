"""Tests for the health check endpoint."""

from httpx import AsyncClient


async def test_health_endpoint_returns_ok(client: AsyncClient) -> None:
    """Health endpoint should return 200 with status ok."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
