"""phaze-cavai: the record slide-in's per-stage "why" lines — failure reasons and orphan diagnostics.

Before this bead the stored failure reasons (``FileMetadata.error_message`` /
``AnalysisResult.error_message``) were written on failure and surfaced NOWHERE in the UI, and an
orphaned file was visible only as an aggregate count (amber badge / agents matrix). The record
slide-in (``GET /record/{id}`` -> ``record_body.html``) now renders, under the corresponding
Stage-Eligibility row:

* FAILED (metadata / analyze): the stored ``error_message``, or the explicit
  "no error detail recorded" fallback — never a bare red pill with no explanation;
* ORPHANED (the two enrich stages): the scheduling-ledger facts recovery will replay —
  scheduled-when, the timeout/retries replay budget, the re-drive count — plus
  started-vs-never-started evidence for analyze.

Mirrors ``test_record_stage_pills.py``'s composition-level idiom (real endpoint, seeded markers).
``saq_jobs`` is SAQ-owned and absent from ``Base.metadata``, so the orphan tests pin the same
module-controlled minimal table ``tests/integration/test_orphan_count.py`` documents.

Must pass in the ``shared`` bucket in isolation (consumes the DB fixtures -> auto-marked integration).
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


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Seed a committed FileRecord (FK anchor for the per-stage marker rows)."""
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


async def _pin_empty_saq_jobs(session: AsyncSession) -> None:
    """Pin a module-controlled, empty ``saq_jobs`` table so the live-keys probe has a real relation."""
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.execute(text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"))
    await session.commit()


@pytest.mark.asyncio
async def test_failed_analyze_shows_the_stored_error_message(client: AsyncClient, session: AsyncSession) -> None:
    """A failed analyze row's stored error_message renders under the Analyze stage row."""
    file_id = await _seed_file(session)
    session.add(AnalysisResult(file_id=file_id, failed_at=datetime.now(UTC), error_message="essentia exploded: tensor shape mismatch"))
    await session.commit()

    body = (await client.get(f"/record/{file_id}")).text
    assert "essentia exploded: tensor shape mismatch" in body


@pytest.mark.asyncio
async def test_failed_analyze_without_message_shows_the_explicit_fallback(client: AsyncClient, session: AsyncSession) -> None:
    """A failed analyze with NO stored message says so explicitly — never a bare unexplained red pill."""
    file_id = await _seed_file(session)
    session.add(AnalysisResult(file_id=file_id, failed_at=datetime.now(UTC)))
    await session.commit()

    body = (await client.get(f"/record/{file_id}")).text
    assert "no error detail recorded" in body


@pytest.mark.asyncio
async def test_failed_metadata_shows_the_stored_error_message(client: AsyncClient, session: AsyncSession) -> None:
    """A failed metadata row's stored error_message renders under the Meta stage row."""
    file_id = await _seed_file(session)
    session.add(FileMetadata(file_id=file_id, failed_at=datetime.now(UTC), error_message="mutagen could not parse the header"))
    await session.commit()

    body = (await client.get(f"/record/{file_id}")).text
    assert "mutagen could not parse the header" in body


@pytest.mark.asyncio
async def test_orphaned_analyze_shows_ledger_diagnostics(client: AsyncClient, session: AsyncSession) -> None:
    """An orphaned analyze (ledger row, no live key, no analysis) renders the amber diagnostics line."""
    await _pin_empty_saq_jobs(session)
    file_id = await _seed_file(session)
    session.add(
        SchedulingLedger(
            key=f"process_file:{file_id}",
            function="process_file",
            routing="agent",
            payload={"file_id": str(file_id)},
            timeout=7200,
            retries=2,
        )
    )
    await session.commit()

    body = (await client.get(f"/record/{file_id}")).text
    assert "Orphaned — scheduled" in body
    assert "The analysis never started." in body
    assert "timeout 7200s" in body
    assert "retries 2" in body


@pytest.mark.asyncio
async def test_non_orphaned_file_renders_no_orphan_line(client: AsyncClient, session: AsyncSession) -> None:
    """A file with no ledger row renders no orphan diagnostics at all."""
    await _pin_empty_saq_jobs(session)
    file_id = await _seed_file(session)

    body = (await client.get(f"/record/{file_id}")).text
    assert "Orphaned — scheduled" not in body
