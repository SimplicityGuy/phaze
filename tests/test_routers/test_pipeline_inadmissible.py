"""Tests for the Phase 54 (D-06) Inadmissible operator alert on the pipeline dashboard.

The dashboard surfaces a warning banner ("K8s Jobs not admitting — check LocalQueue config") ONLY
when one or more ``cloud_job`` rows are flagged ``inadmissible``. A healthy ``Pending`` workload
never sets the flag, so the all-zero path renders the card carrier with NO banner copy (D-06:
healthy Pending stays invisible). The card is also re-pushed OOB on the /pipeline/stats poll.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_WARNING_COPY = "K8s Jobs not admitting"


def _file(i: int) -> FileRecord:
    """Build a minimal FileRecord seed (CloudJob.file_id is a unique FK to files.id)."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.PUSHED,
    )


async def _seed_cloud_job(session: AsyncSession, *, inadmissible: bool) -> None:
    """Seed one file + its cloud_job row flagged inadmissible (or not) and commit."""
    file = _file(0)
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            s3_key=f"phaze-staging/{file.id}",
            status=CloudJobStatus.SUBMITTED.value,
            inadmissible=inadmissible,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_dashboard_shows_inadmissible_alert_when_flagged(client: AsyncClient, session: AsyncSession) -> None:
    """With an inadmissible cloud_job row the dashboard renders the warning copy + card id."""
    await _seed_cloud_job(session, inadmissible=True)

    response = await client.get("/pipeline/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="inadmissible-card"' in response.text
    assert _WARNING_COPY in response.text


@pytest.mark.asyncio
async def test_dashboard_hides_inadmissible_alert_when_none(client: AsyncClient, session: AsyncSession) -> None:
    """With only admissible (healthy Pending) rows the warning copy is ABSENT — alert stays silent.

    The card carrier (#inadmissible-card) still renders (stable OOB target) but the banner body is
    gated behind ``{% if inadmissible_count %}`` so no warning text appears (D-06).
    """
    await _seed_cloud_job(session, inadmissible=False)

    response = await client.get("/pipeline/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    # The empty carrier is always present; the warning banner is NOT.
    assert 'id="inadmissible-card"' in response.text
    assert _WARNING_COPY not in response.text


@pytest.mark.asyncio
async def test_dashboard_renders_on_all_zero_path(client: AsyncClient) -> None:
    """With NO cloud_job rows at all the dashboard still renders (no template error), banner absent."""
    response = await client.get("/pipeline/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="inadmissible-card"' in response.text
    assert _WARNING_COPY not in response.text


@pytest.mark.asyncio
async def test_stats_poll_repushes_inadmissible_card_oob(client: AsyncClient, session: AsyncSession) -> None:
    """The 5s /pipeline/stats poll re-pushes the Inadmissible alert OOB (hx-swap-oob + warning copy)."""
    await _seed_cloud_job(session, inadmissible=True)

    response = await client.get("/pipeline/stats")

    assert response.status_code == 200
    assert 'id="inadmissible-card"' in response.text
    assert 'hx-swap-oob="true"' in response.text
    assert _WARNING_COPY in response.text
