"""phaze-iyqhg: the record's enrich stage control is "Skip stage…", offered only where a skip can matter.

Operator report 2026-09-27, verbatim: '"force complete / skip" is a single button. is this meant to be
2 buttons?'. It was one control whose label named two actions, the first of which it never performed
(a skip writes a ``stage_skip`` marker; the stage reads ``skipped``, never ``done``), and it was
rendered on every enrich stage regardless of state -- including a DONE one, where the marker is a
no-op the ``done ≻ skipped`` precedence hides while the toast still says "Skipped".

Asserted on BOTH presentations of the record, because both render the one shared
``record/_record_content.html``: the standalone page ``GET /files/{id}`` and the drawer
``GET /record/{id}``. A seed per bucket: offered on failed, not started and ORPHANED (a ledger row
nothing is running); absent on done, skipped and genuinely in flight.

``saq_jobs`` is SAQ-owned and absent from ``Base.metadata``; the orphan / live cells pin the same
module-controlled minimal table ``tests/shared/test_record_why_lines.py`` does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_PRESENTATIONS = ("page", "drawer")


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


async def _pin_saq_jobs(session: AsyncSession, *live_keys: str) -> None:
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.execute(text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"))
    for key in live_keys:
        await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES (:k, 'active')"), {"k": key})
    await session.commit()


def _ledger(file_id: uuid.UUID) -> SchedulingLedger:
    return SchedulingLedger(key=f"process_file:{file_id}", function="process_file", routing="agent", payload={"file_id": str(file_id)})


async def _get(client: AsyncClient, presentation: str, file_id: uuid.UUID) -> str:
    url = f"/files/{file_id}" if presentation == "page" else f"/record/{file_id}"
    response = await client.get(url, headers={"HX-Request": "true"} if presentation == "drawer" else None)
    assert response.status_code == 200
    return response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", _PRESENTATIONS)
async def test_the_control_is_labelled_skip_stage_and_never_force_complete(client: AsyncClient, session: AsyncSession, presentation: str) -> None:
    file_id = await _seed_file(session)  # both enrich stages not started -> both offered

    body = await _get(client, presentation, file_id)

    assert body.count("Skip stage…") == 2, "one trigger per not-started enrich stage"
    assert "Skip metadata for this file?" in body
    assert "Skip analyze for this file?" in body
    assert "Force complete" not in body
    assert "Force metadata" not in body
    assert "Force analyze" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", _PRESENTATIONS)
async def test_absent_on_done_stages(client: AsyncClient, session: AsyncSession, presentation: str) -> None:
    file_id = await _seed_file(session)
    session.add(AnalysisResult(file_id=file_id, analysis_completed_at=datetime.now(UTC)))
    session.add(FileMetadata(file_id=file_id, failed_at=None, artist="Artist"))
    await session.commit()

    body = await _get(client, presentation, file_id)

    assert "/skip/" not in body, "a done stage must offer no skip -- the marker would be a no-op"
    assert "Skip stage…" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", _PRESENTATIONS)
async def test_offered_on_failed_stages(client: AsyncClient, session: AsyncSession, presentation: str) -> None:
    file_id = await _seed_file(session)
    session.add(AnalysisResult(file_id=file_id, failed_at=datetime.now(UTC)))
    session.add(FileMetadata(file_id=file_id, failed_at=datetime.now(UTC)))
    await session.commit()

    body = await _get(client, presentation, file_id)

    assert f"/pipeline/files/{file_id}/skip/analyze" in body
    assert f"/pipeline/files/{file_id}/skip/metadata" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", _PRESENTATIONS)
async def test_absent_on_an_already_skipped_stage(client: AsyncClient, session: AsyncSession, presentation: str) -> None:
    file_id = await _seed_file(session)
    session.add(AnalysisResult(file_id=file_id, failed_at=datetime.now(UTC)))
    session.add(StageSkip(file_id=file_id, stage="analyze", reason="corrupt source"))
    await session.commit()

    body = await _get(client, presentation, file_id)

    assert "/skip/analyze" not in body
    assert "/skip/metadata" in body  # metadata is still not started


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", _PRESENTATIONS)
async def test_offered_on_an_orphaned_stage(client: AsyncClient, session: AsyncSession, presentation: str) -> None:
    await _pin_saq_jobs(session)  # no live job: the ledger row is an orphan
    file_id = await _seed_file(session)
    session.add(_ledger(file_id))
    await session.commit()

    body = await _get(client, presentation, file_id)

    assert "Orphaned — scheduled" in body  # the same fact the control's condition reads
    assert "/skip/analyze" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", _PRESENTATIONS)
async def test_absent_on_a_genuinely_running_stage(client: AsyncClient, session: AsyncSession, presentation: str) -> None:
    file_id = await _seed_file(session)
    await _pin_saq_jobs(session, f"process_file:{file_id}")  # live job: really running
    session.add(_ledger(file_id))
    await session.commit()

    body = await _get(client, presentation, file_id)

    assert "Orphaned — scheduled" not in body
    assert "/skip/analyze" not in body, "a skip on a running stage would race the work it skips"
