"""Phase 87 (87-06, UI-04 / D-08/D-09/D-10): the force-skip writer endpoint.

``POST /pipeline/files/{file_id}/skip/{stage}`` writes a distinct ``stage_skip`` marker for an ENRICH
stage so the ``failed`` bucket can converge for genuinely-unprocessable files -- honestly (``skipped``,
never counterfeit ``done``). This suite locks the five correctness behaviors the writer must hold:

1. A non-enrich stage (``propose``/``review``/``apply``) returns 422 with NO row written (D-10,
   T-87-18 -- the approval-bypass hazard; backstopped by the Plan-01 DB CHECK).
2. A blank / whitespace-only reason returns the inline "A reason is required." validation fragment
   with NO row written (D-09, T-87-22).
3. A valid reason commits a ``StageSkip(file_id, stage, reason)`` row that is readable from an
   INDEPENDENT session (Pitfall 7 -- ``get_session`` NEVER auto-commits; a flush-only writer would
   pass a same-session read but fail this).
4. A NUL byte in the reason is sanitized before persist (T-87-19 -- a NUL passes pydantic then aborts
   the PG txn) and the sanitized text round-trips.
5. The writer is ADDITIVE-ONLY (T-87-20 -- behavior 6): a terminally-failed analyze keeps its
   ``analysis.failed_at`` marker after a skip, so the Phase-79 shadow-compare gate stays green.

Every DB assertion reads from an INDEPENDENT session (the ``client`` fixture overrides ``get_session``
with the shared test session, which sees UNCOMMITTED rows -- so a same-session read cannot prove the
writer committed). Must pass in the ``analyze`` bucket in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.stage_skip import StageSkip


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Seed a FileRecord so the StageSkip.file_id FK (files.id) is satisfied, then COMMIT it."""
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


async def _read_skip(async_engine: AsyncEngine, file_id: uuid.UUID, stage: str) -> StageSkip | None:
    """Read the stage_skip marker from an INDEPENDENT session (proves the writer COMMITTED)."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as independent:
        return (await independent.execute(select(StageSkip).where(StageSkip.file_id == file_id, StageSkip.stage == stage))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_non_enrich_stage_returns_422_and_writes_nothing(client: AsyncClient, session: AsyncSession, async_engine: AsyncEngine) -> None:
    """A ``propose`` force-skip is rejected 422 before any write (D-10 enrich-only, T-87-18)."""
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/propose", data={"reason": "trying to bypass approval"})

    assert response.status_code == 422
    assert response.json()["detail"] == "stage not force-skippable"
    assert await _read_skip(async_engine, file_id, "propose") is None


@pytest.mark.asyncio
async def test_empty_reason_returns_validation_fragment_and_writes_nothing(
    client: AsyncClient, session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """A whitespace-only reason returns the inline validation fragment with NO write (D-09, T-87-22)."""
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/analyze", data={"reason": "   "})

    assert response.status_code == 422
    assert "A reason is required." in response.text
    assert await _read_skip(async_engine, file_id, "analyze") is None


@pytest.mark.asyncio
async def test_valid_skip_is_committed_and_readable_from_independent_session(
    client: AsyncClient, session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """A valid reason COMMITS the marker (readable from an independent session, Pitfall 7)."""
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/metadata", data={"reason": "corrupt source file"})

    assert response.status_code == 200
    marker = await _read_skip(async_engine, file_id, "metadata")
    assert marker is not None
    assert marker.reason == "corrupt source file"


@pytest.mark.asyncio
async def test_duplicate_force_skip_is_idempotent_not_500(client: AsyncClient, session: AsyncSession, async_engine: AsyncEngine) -> None:
    """CR-01: re-submitting a force-skip for the same (file, stage) is a no-op success, never a 500.

    ``_force_skip_dialog.html`` is not hidden after a successful skip, so a re-submit is a NORMAL path.
    A bare INSERT would hit UNIQUE(file_id, stage) and raise an unhandled IntegrityError → HTTP 500;
    ``on_conflict_do_nothing`` makes it idempotent. The first-writer's reason is preserved (do-nothing).
    """
    file_id = await _seed_file(session)

    first = await client.post(f"/pipeline/files/{file_id}/skip/fingerprint", data={"reason": "first reason"})
    second = await client.post(f"/pipeline/files/{file_id}/skip/fingerprint", data={"reason": "second reason"})

    assert first.status_code == 200
    assert second.status_code == 200  # would be 500 with a bare INSERT

    # Exactly one row survives (scalar_one_or_none raises MultipleResultsFound if the conflict duplicated).
    marker = await _read_skip(async_engine, file_id, "fingerprint")
    assert marker is not None
    assert marker.reason == "first reason"  # do-nothing keeps the original, does not overwrite


@pytest.mark.asyncio
async def test_nul_only_reason_returns_422_and_writes_nothing(client: AsyncClient, session: AsyncSession, async_engine: AsyncEngine) -> None:
    """WR-01: a NUL/control-only reason is empty AFTER sanitize, so it must fail the D-09 gate with NO write.

    ``str.strip()`` does not remove NUL, so a raw-input blank check would let ``"\\x00"`` through and then
    persist ``""``. The gate now validates the SANITIZED value, so a NUL-only reason returns 422.
    """
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/metadata", data={"reason": "\x00\x00"})

    assert response.status_code == 422
    assert "A reason is required." in response.text
    assert await _read_skip(async_engine, file_id, "metadata") is None


@pytest.mark.asyncio
async def test_nul_in_reason_is_sanitized_and_round_trips(client: AsyncClient, session: AsyncSession, async_engine: AsyncEngine) -> None:
    """A NUL byte is stripped before persist (no PG txn abort) and the sanitized text round-trips (T-87-19)."""
    file_id = await _seed_file(session)

    response = await client.post(f"/pipeline/files/{file_id}/skip/fingerprint", data={"reason": "corrupt\x00source"})

    assert response.status_code == 200
    marker = await _read_skip(async_engine, file_id, "fingerprint")
    assert marker is not None
    assert marker.reason == "corruptsource"  # NUL removed; no lost text around it


@pytest.mark.asyncio
async def test_skip_never_clears_analysis_failed_at(client: AsyncClient, session: AsyncSession, async_engine: AsyncEngine) -> None:
    """ADDITIVE-ONLY (behavior 6, T-87-20): a terminally-failed analyze keeps ``failed_at`` after a skip."""
    file_id = await _seed_file(session)
    failed_at = datetime.now(UTC)
    session.add(AnalysisResult(file_id=file_id, failed_at=failed_at, error_message="analyze crashed on this set"))
    await session.commit()

    response = await client.post(f"/pipeline/files/{file_id}/skip/analyze", data={"reason": "analyze crashes on this set"})
    assert response.status_code == 200

    # The skip marker exists AND the failure marker is untouched -- read both from an independent session.
    assert await _read_skip(async_engine, file_id, "analyze") is not None
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as independent:
        row = (await independent.execute(select(AnalysisResult.failed_at).where(AnalysisResult.file_id == file_id))).first()
    assert row is not None
    assert row[0] is not None  # failed_at was NOT cleared by the additive writer
