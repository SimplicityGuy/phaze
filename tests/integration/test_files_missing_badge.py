"""phaze-5rfev: a row a reconcile found gone from disk reads "Missing" on Files, on both responsive surfaces.

``files.missing_at`` takes the row out of every enrich pending set, so its stage buckets stop saying
what is wrong with it (a failed metadata row, say, no longer counts as owed work). The current-status
cell is where the operator looks for that, so the marker replaces the stage summary there. A row
without the marker keeps the stage summary unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _file(name: str, *, missing: bool) -> FileRecord:
    uid = uuid.uuid4()
    path = f"/music/{name}-{uid.hex}.mp3"
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex * 2,
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="mp3",
        file_size=1000,
        missing_at=datetime(2026, 10, 8, tzinfo=UTC) if missing else None,
    )


def _status_cells(body: str, file_id: uuid.UUID) -> list[str]:
    return [match.group(1) for match in re.finditer(rf'id="files(?:-mobile)?-current-status-{file_id}">(.*?)</span>\s*</', body, re.DOTALL)]


async def test_a_missing_row_reads_missing_and_a_present_row_keeps_its_stage_summary(client: AsyncClient, session: AsyncSession) -> None:
    gone, here = _file("gone", missing=True), _file("here", missing=False)
    session.add_all([gone, here])
    await session.commit()

    body = (await client.get("/pipeline/files?page_size=50", headers={"HX-Request": "true"})).text

    gone_cells = _status_cells(body, gone.id)
    here_cells = _status_cells(body, here.id)
    assert len(gone_cells) == len(here_cells) == 2, "the desktop row and the mobile card both carry the cell"
    for cell in gone_cells:
        assert 'aria-label="File missing on disk since 2026-10-08"' in cell
        assert "<span>missing" in cell
    for cell in here_cells:
        assert "missing" not in cell
        assert "Metadata" in cell, "a present, never-extracted row still names its first open stage"
