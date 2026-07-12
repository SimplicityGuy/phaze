"""Tests for fingerprint pipeline endpoints."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.schemas.agent_tasks import FingerprintFilePayload
from tests._queue_fakes import seed_active_agent, wire_fakes


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _drain_background() -> None:
    """Yield until the router's background enqueue tasks have drained."""
    import phaze.routers.pipeline as pipeline_mod

    for _ in range(500):
        if not pipeline_mod._background_tasks:
            return
        await asyncio.sleep(0)


def _make_file(*, state: str = FileState.METADATA_EXTRACTED) -> FileRecord:
    """Create a FileRecord with the given state."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )


@pytest.mark.asyncio
async def test_trigger_fingerprint_enqueues_eligible(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/fingerprint enqueues fingerprint_file onto phaze-agent-nox-fingerprint (not default)."""
    session.add_all([_make_file(state=FileState.METADATA_EXTRACTED) for _ in range(3)])
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/fingerprint")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 3

    await _drain_background()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-nox-fingerprint", "fingerprint_file")}
    assert all(q != "default" for q, _, _ in capture)


@pytest.mark.asyncio
async def test_trigger_fingerprint_enqueues_complete_payload(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (35-REVIEW CR-02): /api/v1/fingerprint must enqueue a COMPLETE FingerprintFilePayload.

    The trigger passed only ``file_id``; the agent worker's
    ``FingerprintFilePayload.model_validate(kwargs)`` (``extra="forbid"``) requires
    file_id + original_path + agent_id and would otherwise dead-letter every job. This
    pins all three required fields and that the exact kwargs validate cleanly.
    """
    file_rec = _make_file(state=FileState.METADATA_EXTRACTED)
    session.add(file_rec)
    await session.commit()
    expected_id = str(file_rec.id)
    expected_path = file_rec.original_path
    agent = await seed_active_agent(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/fingerprint")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 1

    await _drain_background()
    assert len(capture) == 1
    queue_name, task_name, kwargs = capture[0]
    assert (queue_name, task_name) == ("phaze-agent-nox-fingerprint", "fingerprint_file")

    # All three required fields present -- not just file_id (the CR-02 bug).
    assert set(kwargs) == {"file_id", "original_path", "agent_id"}
    assert kwargs["file_id"] == expected_id
    assert kwargs["original_path"] == expected_path
    assert kwargs["agent_id"] == agent.id

    # The exact kwargs the agent worker receives validate against FingerprintFilePayload.
    validated = FingerprintFilePayload.model_validate(kwargs)
    assert str(validated.file_id) == expected_id


@pytest.mark.asyncio
async def test_trigger_fingerprint_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/fingerprint with files but no active agent surfaces a visible empty-state."""
    session.add_all([_make_file(state=FileState.METADATA_EXTRACTED) for _ in range(3)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/api/v1/fingerprint")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0
    assert "no active agent" in data["message"].lower()

    await _drain_background()
    assert capture == []


@pytest.mark.asyncio
async def test_trigger_fingerprint_no_eligible(client: AsyncClient) -> None:
    """POST /api/v1/fingerprint returns 0 when no eligible files exist."""
    response = await client.post("/api/v1/fingerprint")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0


@pytest.mark.asyncio
async def test_fingerprint_progress_returns_counts(client: AsyncClient, session: AsyncSession) -> None:
    """GET /api/v1/fingerprint/progress derives counts from fingerprint_results, not FileRecord.state.

    The two `state` values below are deliberately misleading: `f3` is FINGERPRINTED with no
    engine row, and `f4` is only METADATA_EXTRACTED but has a success row. `completed` must
    count `f4` and ignore `f3` (D-11 / READ-04). Reverting the endpoint to `state ==
    FINGERPRINTED` inverts both assertions.
    """
    f1 = _make_file(state=FileState.METADATA_EXTRACTED)  # failed engine only -> failed
    f2 = _make_file(state=FileState.METADATA_EXTRACTED)  # no engine rows -> total only
    f3 = _make_file(state=FileState.FINGERPRINTED)  # state says done, no row -> NOT completed
    f4 = _make_file(state=FileState.METADATA_EXTRACTED)  # success row -> completed
    session.add_all([f1, f2, f3, f4])
    await session.flush()

    session.add(FingerprintResult(file_id=f1.id, engine="audfprint", status="failed", error_message="timeout"))
    session.add(FingerprintResult(file_id=f4.id, engine="audfprint", status="success"))
    await session.commit()

    response = await client.get("/api/v1/fingerprint/progress")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 4
    assert data["completed"] == 1
    assert data["failed"] == 1


@pytest.mark.asyncio
async def test_pipeline_stats_include_fingerprinted(client: AsyncClient, session: AsyncSession) -> None:
    """Pipeline stats include FINGERPRINTED stage."""
    session.add(_make_file(state=FileState.FINGERPRINTED))
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "Fingerprinted" in response.text
