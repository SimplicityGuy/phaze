"""Controller-side tests for `routers/pipeline/extraction.py` (split from test_pipeline.py, phaze-7l8jh).

POST /api/v1/extract-metadata, /pipeline/extract-metadata, and the METADATA_FAILED retry endpoints -- `routers/pipeline/extraction.py`.
"""

from __future__ import annotations

from tests.shared.routers.pipeline._shared import *


@pytest.mark.asyncio
async def test_extract_metadata_enqueues_complete_payload(client: AsyncClient, session: AsyncSession) -> None:
    """Regression (35-REVIEW CR-01): /api/v1/extract-metadata must enqueue a COMPLETE ExtractMetadataPayload.

    D-06 removed the agent file-upsert auto-enqueue -- the only producer that built the full
    payload -- making this manual trigger the SOLE metadata producer. The surviving path passed
    only ``file_id``; the agent worker's ``ExtractMetadataPayload.model_validate(kwargs)``
    (``extra="forbid"``) then raised "Field required" and dead-lettered every job (the same
    class as the v4.0.8 payload incident). This pins all four required fields and that the
    exact kwargs validate cleanly.
    """
    file_rec = _make_file()
    session.add(file_rec)
    await session.commit()
    expected_id = str(file_rec.id)
    expected_path = file_rec.original_path
    expected_type = file_rec.file_type
    await make_agent_live(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/extract-metadata")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 1

    await drain_router_background_tasks()
    assert len(capture) == 1
    queue_name, task_name, kwargs = capture[0]
    assert (queue_name, task_name) == ("phaze-agent-test-fileserver-meta", "extract_file_metadata")

    # All four required fields present -- not just file_id (the CR-01 bug).
    assert set(kwargs) == {"file_id", "original_path", "file_type", "agent_id"}
    assert kwargs["file_id"] == expected_id
    assert kwargs["original_path"] == expected_path
    assert kwargs["file_type"] == expected_type
    assert kwargs["agent_id"] == "test-fileserver"

    # The exact kwargs the agent worker receives validate against ExtractMetadataPayload.
    validated = ExtractMetadataPayload.model_validate(kwargs)
    assert str(validated.file_id) == expected_id


@pytest.mark.asyncio
async def test_extract_metadata_enqueues(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/extract-metadata enqueues extract_file_metadata onto phaze-agent-nox."""
    session.add_all([_make_file() for _ in range(3)])
    await session.commit()
    await make_agent_live(session)
    capture = wire_fakes(client)

    response = await client.post("/api/v1/extract-metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 3

    await drain_router_background_tasks()
    assert len(capture) == 3
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-test-fileserver-meta", "extract_file_metadata")}


@pytest.mark.asyncio
async def test_extract_metadata_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/extract-metadata with files but no active agent surfaces empty-state."""
    session.add_all([_make_file() for _ in range(3)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/api/v1/extract-metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0
    assert "no active agent" in data["message"].lower()

    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_extract_metadata_no_files(client: AsyncClient) -> None:
    """POST /api/v1/extract-metadata with no music files returns enqueued=0."""
    response = await client.post("/api/v1/extract-metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0


@pytest.mark.asyncio
async def test_trigger_extraction_ui_with_files(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/extract-metadata enqueues extract_file_metadata onto phaze-agent-nox."""
    session.add_all([_make_file() for _ in range(2)])
    await session.commit()
    await make_agent_live(session)
    capture = wire_fakes(client)

    response = await client.post("/pipeline/extract-metadata")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "metadata extraction" in response.text

    await drain_router_background_tasks()
    assert len(capture) == 2
    assert {(q, t) for q, t, _ in capture} == {("phaze-agent-test-fileserver-meta", "extract_file_metadata")}


@pytest.mark.asyncio
async def test_trigger_extraction_ui_no_active_agent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /pipeline/extract-metadata with files but no active agent renders the empty-state."""
    session.add_all([_make_file() for _ in range(2)])
    await session.commit()
    capture = wire_fakes(client)  # no active agent seeded

    response = await client.post("/pipeline/extract-metadata")
    assert response.status_code == 200
    assert "No active agent available" in response.text

    await drain_router_background_tasks()
    assert capture == []


@pytest.mark.asyncio
async def test_trigger_extraction_ui_no_files(client: AsyncClient) -> None:
    """POST /pipeline/extract-metadata with no music files returns HTML with zero count."""
    response = await client.post("/pipeline/extract-metadata")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_extract_metadata_routes_each_file_to_its_owning_agent(client: AsyncClient, session: AsyncSession) -> None:
    """THE phaze-c9w9 endpoint regression: EXTRACT ALL with two live fileservers routes per OWNER.

    ``fileserver-west`` is seeded LAST (most recently seen) -- the pre-fix single
    ``select_active_agent`` pick would land BOTH files on west's meta lane, including east's file
    (whose path exists only on east's mount). Post-fix each file lands on its owner's queue.
    """
    await seed_active_agent(session, "fileserver-east")
    await seed_active_agent(session, "fileserver-west")  # most recently seen -> the pre-fix winner
    east_file = _make_file_owned_by("fileserver-east")
    west_file = _make_file_owned_by("fileserver-west")
    session.add_all([east_file, west_file])
    await session.commit()
    capture = wire_fakes(client)

    response = await client.post("/api/v1/extract-metadata")
    assert response.status_code == 200
    assert response.json()["enqueued"] == 2

    await drain_router_background_tasks()
    destinations = {kwargs["file_id"]: q for q, _t, kwargs in capture}
    assert destinations == {
        str(east_file.id): "phaze-agent-fileserver-east-meta",
        str(west_file.id): "phaze-agent-fileserver-west-meta",
    }
