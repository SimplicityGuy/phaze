"""Behavior 1 (metadata): a force-skipped file LEAVES the metadata pending set (Phase 87, D-08).

``get_metadata_pending_files`` reads ``eligible_clause(Stage.METADATA)``, into which Plan 02 threaded a
``not_(skipped_clause(stage))`` conjunct. This test proves that conjunct propagates with ZERO per-caller
edits: seed a music file that WOULD be metadata-pending (a positive control asserts it IS present), add a
``stage_skip(metadata)`` marker, and assert the file is now ABSENT from ``get_metadata_pending_files``.

Mutation discipline (project memory -- a green guard proves nothing): temporarily drop the ``~skipped``
conjunct from ``eligible_clause`` and this test goes RED. Recorded in the 87-03 SUMMARY.

Uses the shared real-PG ``session`` fixture (``tests/conftest.py``); run via ``just test-bucket metadata``
with ``TEST_DATABASE_URL`` at :5433.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from phaze.models.file import FileRecord, FileState
from phaze.models.stage_skip import StageSkip
from phaze.services.pipeline import get_metadata_pending_files


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _music_file() -> FileRecord:
    """Build a discovered music FileRecord that (absent a skip marker) is metadata-pending."""
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
        state=FileState.DISCOVERED,
    )


async def test_unskipped_music_file_is_metadata_pending(session: AsyncSession) -> None:
    """Positive control: a not-done, not-skipped music file IS in the metadata pending set."""
    f = _music_file()
    session.add(f)
    await session.commit()

    pending = {r.id for r in await get_metadata_pending_files(session)}
    assert f.id in pending


async def test_skipped_file_leaves_metadata_pending_set(session: AsyncSession) -> None:
    """Behavior 1: adding a ``stage_skip(metadata)`` marker removes the file from the pending set."""
    f = _music_file()
    session.add(f)
    await session.commit()
    # Sanity: present before the marker (proves the seed WOULD be pending).
    assert f.id in {r.id for r in await get_metadata_pending_files(session)}

    session.add(StageSkip(file_id=f.id, stage="metadata", reason="operator force-skip"))
    await session.commit()

    pending = {r.id for r in await get_metadata_pending_files(session)}
    assert f.id not in pending
