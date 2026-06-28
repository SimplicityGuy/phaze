"""Tests for the Phase 54 (D-06) degrade-safe ``get_inadmissible_count`` dashboard reader.

The reader drives the Inadmissible operator alert: it returns
``COUNT(cloud_job WHERE inadmissible = true)`` and, mirroring
:func:`get_awaiting_cloud_count`, degrades to 0 on any DB error so the hot 5s
``/pipeline/stats`` poll never 500s (T-54-10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.services.pipeline import get_inadmissible_count


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _file(i: int) -> FileRecord:
    """Build a minimal FileRecord seed (CloudJob.file_id is a unique FK to files.id)."""
    return FileRecord(
        id=uuid.uuid4(),
        sha256_hash=f"c{i:063d}"[:64],
        original_path=f"/music/cloud{i}.mp3",
        original_filename=f"cloud{i}.mp3",
        current_path=f"/music/cloud{i}.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.PUSHED,
    )


def _cloud_job(file_id: uuid.UUID, *, inadmissible: bool, status: str = CloudJobStatus.SUBMITTED.value) -> CloudJob:
    """Build a CloudJob seed flagged inadmissible (or not) for the given file_id."""
    return CloudJob(
        id=uuid.uuid4(),
        file_id=file_id,
        s3_key=f"phaze-staging/{file_id}",
        status=status,
        inadmissible=inadmissible,
    )


@pytest.mark.asyncio
async def test_get_inadmissible_count_happy_path(session: AsyncSession) -> None:
    """Counts exactly the cloud_job rows with inadmissible=true; admissible rows are excluded."""
    files = [_file(i) for i in range(3)]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            _cloud_job(files[0].id, inadmissible=True),
            _cloud_job(files[1].id, inadmissible=True),
            _cloud_job(files[2].id, inadmissible=False),
        ]
    )
    await session.commit()

    assert await get_inadmissible_count(session) == 2


@pytest.mark.asyncio
async def test_get_inadmissible_count_excludes_terminal_rows(session: AsyncSession) -> None:
    """CR-01: a terminal row (SUCCEEDED/FAILED) that was transiently inadmissible never inflates the alert.

    cloud_job rows are never deleted, so a row flagged inadmissible that later succeeds would keep the
    banner lit if the count weren't scoped to in-flight (SUBMITTED/RUNNING) rows."""
    files = [_file(i) for i in range(3)]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            _cloud_job(files[0].id, inadmissible=True, status=CloudJobStatus.SUBMITTED.value),  # counted
            _cloud_job(files[1].id, inadmissible=True, status=CloudJobStatus.SUCCEEDED.value),  # terminal -> excluded
            _cloud_job(files[2].id, inadmissible=True, status=CloudJobStatus.FAILED.value),  # terminal -> excluded
        ]
    )
    await session.commit()

    assert await get_inadmissible_count(session) == 1


@pytest.mark.asyncio
async def test_get_inadmissible_count_zero_when_all_admissible(session: AsyncSession) -> None:
    """Healthy Pending workloads (inadmissible=false) count 0 — the alert stays silent (D-06)."""
    file = _file(0)
    session.add(file)
    await session.flush()
    session.add(_cloud_job(file.id, inadmissible=False))
    await session.commit()

    assert await get_inadmissible_count(session) == 0


@pytest.mark.asyncio
async def test_get_inadmissible_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the count to 0 (poll-safe via _safe_count), never raising.

    Mirrors the :func:`get_awaiting_cloud_count` degrade discipline: the hot 5s
    ``/pipeline/stats`` poll must keep serving instead of 500ing when the cloud_job read fails.
    """

    class _ExplodingSession:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("cloud_job table unavailable")

        async def rollback(self) -> None:
            return None

    assert await get_inadmissible_count(_ExplodingSession()) == 0  # type: ignore[arg-type]
