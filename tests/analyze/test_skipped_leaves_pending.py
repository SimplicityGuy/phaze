"""Behavior 1 (analyze): a force-skipped file LEAVES the analyze pending set (Phase 87, D-08).

``get_discovered_files_with_duration`` (the analyze pending set) reads ``eligible_clause(Stage.ANALYZE)``,
into which Plan 02 threaded a ``not_(skipped_clause(stage))`` conjunct. This test proves that conjunct
propagates with ZERO per-caller edits: seed a music file that WOULD be analyze-pending (a positive control
asserts it IS present), add a ``stage_skip(analyze)`` marker, and assert the file is now ABSENT from the
analyze pending set.

Analyze is the ELIG-03 carve-out stage (``ELIGIBLE_AFTER_FAILURE[ANALYZE] is False``), so its
``eligible_clause`` already carries a ``~failed`` conjunct; the ``~skipped`` conjunct is orthogonal and
this test seeds a NOT-STARTED file (no analysis row) so the drop is attributable to the skip marker alone.

Mutation discipline (project memory -- a green guard proves nothing): temporarily drop the ``~skipped``
conjunct from ``eligible_clause`` and this test goes RED. Recorded in the 87-03 SUMMARY.

Uses the shared real-PG ``session`` fixture (``tests/conftest.py``); run via ``just test-bucket analyze``
with ``TEST_DATABASE_URL`` at :5433.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from phaze.models.file import FileRecord, FileState
from phaze.models.stage_skip import StageSkip
from phaze.services.pipeline import get_discovered_files_with_duration


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _music_file() -> FileRecord:
    """Build a discovered music FileRecord that (absent a skip marker) is analyze-pending."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.DISCOVERED,
    )


async def _analyze_pending_ids(session: AsyncSession) -> set[uuid.UUID]:
    return {record.id for record, _duration in await get_discovered_files_with_duration(session)}


async def test_unskipped_music_file_is_analyze_pending(session: AsyncSession) -> None:
    """Positive control: a not-done, not-skipped music file IS in the analyze pending set."""
    f = _music_file()
    session.add(f)
    await session.commit()

    assert f.id in await _analyze_pending_ids(session)


async def test_skipped_file_leaves_analyze_pending_set(session: AsyncSession) -> None:
    """Behavior 1: adding a ``stage_skip(analyze)`` marker removes the file from the analyze pending set."""
    f = _music_file()
    session.add(f)
    await session.commit()
    # Sanity: present before the marker (proves the seed WOULD be pending).
    assert f.id in await _analyze_pending_ids(session)

    session.add(StageSkip(file_id=f.id, stage="analyze", reason="operator force-skip"))
    await session.commit()

    assert f.id not in await _analyze_pending_ids(session)
