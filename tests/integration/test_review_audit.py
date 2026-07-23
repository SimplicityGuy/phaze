"""REVIEW-05 audit integration tests: one audit row per apply + reversibility.

Runs against the real integration-test Postgres -- the whole ``tests/integration/`` package is
auto-marked ``integration`` by the ``tests/conftest.py`` path rule, and these tests additionally
consume the DB-backed ``client`` + ``session`` fixtures (which build tables on
``TEST_DATABASE_URL``). The mutagen file writes are patched (there are no audio files on disk) so
the DB audit trail -- the subject under test -- is exercised end-to-end while the on-disk write is
stubbed to a COMPLETED result.

Proves REVIEW-05 over the EXISTING apply endpoints (no new audit/undo logic, D-04):
  (a) a single ``POST /tags/{id}/write`` produces exactly ONE ``TagWriteLog`` row;
  (b) ``POST /tags/{id}/undo`` re-applies ``before_tags`` via ``execute_tag_write`` (append-only);
  (c) a ``POST /duplicates/{hash}/resolve`` writes exactly one resolution and its undo round-trips
      the ``file_states`` blob.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch
import uuid

import pytest
from sqlalchemy import func, select

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


async def _executed_file(
    session: AsyncSession,
    *,
    artist: str | None = "Old Artist",
    filename: str = "Old Artist - Old Title.mp3",
    sha256: str | None = None,
    file_size: int = 5_000_000,
) -> FileRecord:
    """Insert an APPLIED FileRecord + FileMetadata (the tag-apply target).

    READ-05 / D-01: the tag-apply guard now reads ``applied()`` — an ``executed`` ``RenameProposal``
    exists — NOT ``file.state == 'executed'`` (no ``src/`` writer produces that value). So this seeds an
    ``executed`` proposal and sets the file's own ``state`` to ``MOVED`` (the real apply-path outcome).
    The ``MOVED`` state makes the fixture mutation-sensitive: a guard reverted to ``state == EXECUTED``
    would reject it, so the audit genuinely exercises the ``proposals.status`` predicate.
    """
    file = FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=sha256 or (uuid.uuid4().hex + uuid.uuid4().hex),
        original_path=f"/dest/{uuid.uuid4().hex}/{filename}",
        original_filename=filename,
        current_path=f"/dest/{filename}",
        file_type="mp3",
        file_size=file_size,
    )
    session.add(file)
    await session.flush()
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file.id, artist=artist, title="Old Title"))
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file.id,
            proposed_filename=filename,
            status=ProposalStatus.EXECUTED.value,
        )
    )
    await session.commit()
    await session.refresh(file)
    return file


async def _tag_log_count(session: AsyncSession, file_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_id)
    return (await session.execute(stmt)).scalar_one()


@pytest.mark.asyncio
async def test_tag_write_produces_exactly_one_audit_row(client: AsyncClient, session: AsyncSession) -> None:
    """(a) A single ``POST /tags/{id}/write`` appends exactly one ``TagWriteLog`` row."""
    file = await _executed_file(session, artist="Old Artist")
    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Old Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        resp = await client.post(f"/tags/{file.id}/write", data={"artist": "New Artist"})
    assert resp.status_code == 200
    assert await _tag_log_count(session, file.id) == 1


@pytest.mark.asyncio
async def test_tag_undo_reapplies_before_tags(client: AsyncClient, session: AsyncSession) -> None:
    """(b) Undo re-applies the snapshot captured before the write via ``execute_tag_write``.

    The first write's ``before_tags`` snapshot ({"artist": "Old Artist"}) is what the undo must
    re-apply. Undo appends a second audit row (append-only trail), proving reversibility over the
    existing mutagen path -- no new undo logic.
    """
    file = await _executed_file(session, artist="Old Artist")
    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Old Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        write = await client.post(f"/tags/{file.id}/write", data={"artist": "New Artist"})
    assert write.status_code == 200

    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "New Artist"}),
        patch("phaze.services.tag_writer.write_tags") as mock_write,
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
        undo = await client.post(f"/tags/{file.id}/undo")
    assert undo.status_code == 200

    # Undo re-applied the pre-write snapshot ("Old Artist"), reusing execute_tag_write.
    mock_write.assert_called_once()
    reapplied = mock_write.call_args[0][1]
    assert reapplied.get("artist") == "Old Artist"
    # Append-only: write + undo = two audit rows.
    assert await _tag_log_count(session, file.id) == 2


@pytest.mark.asyncio
async def test_tag_undo_missing_log_returns_404(client: AsyncClient, session: AsyncSession) -> None:
    """Undo on a file with no prior write log redraws the pending row with a toast (nothing to reverse).

    phaze-y4s6: routers/tags.py's write/undo routes always return the v7 _diff_row.html shape now
    (the legacy non-v7 bare-404 fallback had no live caller left and was removed), so this is a
    200 + toast, not a bare 404.
    """
    file = await _executed_file(session)
    resp = await client.post(f"/tags/{file.id}/undo")
    assert resp.status_code == 200
    assert "No prior tag write to undo." in resp.text


@pytest.mark.asyncio
async def test_dedupe_resolve_one_resolution_and_undo_round_trips(client: AsyncClient, session: AsyncSession) -> None:
    """(c) Resolve marks exactly one non-canonical file; undo round-trips the file_states blob.

    The dedupe "audit row" is the durable ``DedupResolution`` marker (D-07 / Phase 90 D-09: the
    ``FileRecord.state`` dual-write was removed, so the marker is the sole authority): exactly one
    non-canonical file gets a marker per resolve, and undo DELETEs it from the round-tripped
    ``file_states`` JSON -- reversibility with zero new logic.
    """
    from phaze.models.dedup_resolution import DedupResolution

    shared = uuid.uuid4().hex + uuid.uuid4().hex
    canonical = await _executed_file(session, filename="keep.mp3", sha256=shared, file_size=1000)
    other = await _executed_file(session, filename="dupe.mp3", sha256=shared, file_size=2000)

    resolve = await client.post(f"/duplicates/{shared}/resolve", data={"canonical_id": str(canonical.id)})
    assert resolve.status_code == 200

    resolved_stmt = select(func.count()).select_from(DedupResolution).where(DedupResolution.file_id == other.id)
    assert (await session.execute(resolved_stmt)).scalar_one() == 1, "exactly one resolution marker per resolve"

    # Undo round-trips the file_states blob the resolve response carries. phaze-btix: the CAS DELETE
    # now requires both the file id and the canonical_id the marker was written with, not id alone.
    file_states = json.dumps([{"id": str(other.id), "canonical_id": str(canonical.id)}])
    undo = await client.post(f"/duplicates/{shared}/undo", data={"file_states": file_states})
    assert undo.status_code == 200

    # The marker is gone -> the file derives ~dedup_resolved_clause() again (Phase 90 D-09).
    remaining = (await session.execute(select(func.count()).select_from(DedupResolution).where(DedupResolution.file_id == other.id))).scalar_one()
    assert remaining == 0, "undo DELETEs the resolution marker"
