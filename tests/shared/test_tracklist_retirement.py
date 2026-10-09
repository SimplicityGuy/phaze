"""Retired provider work cannot be routed, scheduled or offered by the application."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

import pytest
import saq

from phaze.config import ControlSettings
from phaze.enums.stage import Stage, Status, resolve_status
from phaze.services.enqueue_router import resolve_queue_for_task
from phaze.services.tracklist_review import get_file_tracklist_review
from phaze.tasks.controller import settings


RETIRED_TASKS = (
    "search_tracklist",
    "scrape_and_store_tracklist",
    "refresh_tracklists",
    "drain_tracklists",
    "tracklist_drain_status",
    "continue_armed_tracklist_drain",
)


@pytest.mark.parametrize("task", RETIRED_TASKS)
async def test_retired_task_has_no_worker_handler_or_route(task: str) -> None:
    worker = saq.Worker(**settings)
    assert task not in worker.functions
    assert all(job.function not in RETIRED_TASKS for job in settings["cron_jobs"])
    with pytest.raises(ValueError):
        await resolve_queue_for_task(task, SimpleNamespace(), None)


@pytest.mark.parametrize(
    "path",
    [
        "/pipeline/run-tracklist-drain",
        "/pipeline/disarm-tracklist-drain",
        "/pipeline/arm-tracklist-drain",
        "/pipeline/search-tracklists",
        "/pipeline/scrape-tracklists",
        f"/pipeline/tracklists/{uuid.UUID(int=1)}/prioritize",
        f"/pipeline/tracklists/{uuid.UUID(int=1)}/refresh",
        f"/pipeline/tracklists/{uuid.UUID(int=1)}/unprioritize",
    ],
)
async def test_retired_actions_return_404_and_enqueue_nothing(client: Any, path: str) -> None:
    from tests.shared.routers.pipeline._shared import wire_fakes

    capture = wire_fakes(client)
    assert (await client.post(path)).status_code == 404
    assert capture == []


async def test_record_and_workspace_show_stored_data_without_lookup_actions(client: Any, session: Any, make_file: Any) -> None:
    from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion

    file = await make_file(original_filename="set.mp3")
    tracklist = Tracklist(external_id="synthetic-set", source_url="https://example.invalid/set", file_id=file.id, source="manual")
    session.add(tracklist)
    await session.flush()
    version = TracklistVersion(tracklist_id=tracklist.id, version_number=1)
    session.add(version)
    await session.flush()
    tracklist.latest_version_id = version.id
    session.add(TracklistTrack(version_id=version.id, position=1, artist="Synthetic Artist", title="Opening", timestamp="00:00"))
    await session.commit()

    review = await get_file_tracklist_review(session, file.id)
    assert review is not None and review.tracks[0].title == "Opening"
    for path in (f"/record/{file.id}", "/s/tracklist"):
        response = await client.get(path)
        assert response.status_code == 200
        assert "run-tracklist-drain" not in response.text
        assert "/prioritize" not in response.text
        assert "/refresh" not in response.text
    assert "Opening" in (await client.get(f"/record/{file.id}")).text
    assert (await client.get("/pipeline/tracklist-drain-status")).status_code == 404
    assert await get_file_tracklist_review(session, uuid.uuid4()) is None


@pytest.mark.parametrize("outcome", ["queued", "retry_pending", "not_found", "low_confidence"])
def test_old_lookup_outcome_does_not_imply_active_work(outcome: str) -> None:
    assert resolve_status(Stage.TRACKLIST, {"lookup_outcome": outcome}) is Status.NOT_STARTED
    assert resolve_status(Stage.TRACKLIST, {"lookup_outcome": outcome, "row_present": True}) is Status.DONE


def test_runtime_and_image_have_no_acquisition_configuration() -> None:
    assert not any(key.startswith(("scraper_", "tracklist_render_", "tracklist_drain_")) for key in ControlSettings.model_fields)
    root = Path(__file__).resolve().parents[2]
    image = (root / "Dockerfile").read_text().lower()
    assert not any(name in image for name in ("patchright", "xvfb", "ms-playwright"))
    assert '"patchright' not in (root / "pyproject.toml").read_text()
