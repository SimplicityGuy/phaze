"""Tests for the ordered transactional scan-deletion cascade (PR5).

The load-bearing invariant: ``delete_scan_cascade`` removes a ScanBatch and
EVERY row that transitively hangs off its files -- and NOTHING that belongs to
any other batch. These tests run against real Postgres (the ``session`` fixture)
because the cascade is built from set-based ``DELETE ... WHERE col IN (SELECT
...)`` subqueries that must execute against a real engine; deletes are visible
transaction-locally before commit, so per-table count assertions work inside the
same uncommitted transaction.

Coverage:
- Full-graph delete: a batch seeded with files + metadata + analysis +
  proposals + execution_log + tracklists -> versions ->
  tracks -> discogs_links + tag_write_log + file_companions is 100% removed, and
  the returned counts dict matches the seeded cardinality.
- No collateral deletion: a SECOND independent full-graph batch is 100% intact.
- Cross-batch companion: a file in batch A linked via file_companions to a file
  in batch B -- deleting A removes the JOIN row but the batch-B FILE survives.
- Nullable tracklists.file_id: a scraped tracklist with file_id=NULL is NOT
  touched when an unrelated batch is deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import func, select

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.models.tag_write_log import TagWriteLog
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.scan_deletion import delete_scan_cascade


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Per-table seeded cardinality for one ``_seed_full_graph`` call. Each batch gets
# two files (a media file carrying the full descendant chain + a companion file)
# linked by one file_companions row.
#
# RULING (phaze-bk9el.6, epic design rule 2): repowise reports this dict as an 18-line/40.9%
# duplication clone against services/scan_deletion.py's own ``ordered`` cascade list. That is the
# test deliberately restating the production shape, not debt -- a characterization test that
# asserts an exact per-table rowcount MUST enumerate the same tablenames the cascade deletes from,
# in the same order, or it cannot catch a step being silently dropped/reordered. Coupling this dict
# to the implementation (e.g. deriving it from ``ordered`` at import time) would make the test
# incapable of catching exactly the regression it exists to catch -- a table quietly falling out of
# the cascade. Left as-is; not fixed.
_EXPECTED_COUNTS = {
    "discogs_links": 1,
    "tracklist_tracks": 1,
    "tracklist_versions": 1,
    "tracklists": 1,
    "execution_log": 1,
    "proposals": 1,
    "analysis": 1,
    "metadata": 1,
    "tag_write_log": 1,
    "file_companions": 1,
    # phaze-6l74: a force-skip sidecar (fk_stage_skip_file_id_files, NO ON DELETE) on the media file.
    # Before the fix this blocked the files delete with a ForeignKeyViolation (batch undeletable).
    "stage_skip": 1,
    # The full-graph seed creates no dedup_resolution / cloud_job sidecars, but the
    # cascade always reports every table in its ordered list (0 rows deleted here).
    "dedup_resolution": 0,
    "cloud_job": 0,
    # phaze-2mwyo: the durable cloud-budget ledger is deleted explicitly here too, even though its FK
    # carries ON DELETE CASCADE -- the cascade would remove the rows silently, and this dict IS the
    # rowcount report an operator reads. Deleting a file legitimately erases its cloud budget history:
    # a re-scanned file is a NEW files.id and has genuinely never spent a cloud budget.
    "cloud_budget": 0,
    # phaze-u5dn: the full-graph seed creates no scheduling_ledger rows either -- exercised
    # separately by test_cascade_purges_scheduling_ledger_rows_for_its_files below.
    "scheduling_ledger": 0,
    "files": 2,
    "scan_batches": 1,
}


def _make_file(batch_id: uuid.UUID | None, suffix: str, file_type: str = "mp3") -> FileRecord:
    """Build a FileRecord with a unique path (uq_files_agent_id_original_path)."""
    path = f"/data/music/{uuid.uuid4().hex}-{suffix}.{file_type}"
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type=file_type,
        file_size=4096,
        batch_id=batch_id,
    )


async def _seed_full_graph(session: AsyncSession) -> uuid.UUID:
    """Seed one batch with the full descendant FK graph; return the batch id.

    Builds: 1 ScanBatch -> 2 files (media + companion). The media file carries
    metadata + analysis + tag_write_log + a proposal (which
    carries an execution_log) + a tracklist -> version -> track -> discogs_link.
    The two files are joined by one file_companions row.
    """
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-fileserver",
        scan_path=f"/data/music/{uuid.uuid4().hex}",
        status=ScanStatus.COMPLETED.value,
        total_files=2,
        processed_files=2,
    )
    session.add(batch)
    await session.flush()

    media = _make_file(batch.id, "media")
    companion = _make_file(batch.id, "cover", file_type="jpg")
    session.add_all([media, companion])
    await session.flush()

    session.add(FileMetadata(id=uuid.uuid4(), file_id=media.id, artist="A", title="T"))
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=media.id, bpm=128.0))
    session.add(
        TagWriteLog(
            id=uuid.uuid4(),
            file_id=media.id,
            before_tags={"artist": "old"},
            after_tags={"artist": "new"},
            source="metadata",
            status="completed",
        )
    )
    session.add(FileCompanion(id=uuid.uuid4(), companion_id=companion.id, media_id=media.id))
    # phaze-6l74: the force-skip sidecar. Its FK to files.id has no ON DELETE, so before the cascade
    # cleared it the files delete raised ForeignKeyViolation -> the batch was permanently undeletable.
    session.add(StageSkip(id=uuid.uuid4(), file_id=media.id, stage="metadata", reason="operator force-skip"))

    proposal = RenameProposal(id=uuid.uuid4(), file_id=media.id, proposed_filename="better.mp3")
    session.add(proposal)
    await session.flush()
    session.add(
        ExecutionLog(
            id=uuid.uuid4(),
            proposal_id=proposal.id,
            operation="move",
            source_path=media.original_path,
            destination_path="/data/music/better.mp3",
            sha256_verified=True,
            status="completed",
        )
    )

    tracklist = Tracklist(
        id=uuid.uuid4(),
        external_id=uuid.uuid4().hex,
        source_url="https://1001.tl/x",
        file_id=media.id,
    )
    session.add(tracklist)
    await session.flush()
    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tracklist.id, version_number=1)
    session.add(version)
    await session.flush()
    track = TracklistTrack(id=uuid.uuid4(), version_id=version.id, position=1, artist="A", title="T")
    session.add(track)
    await session.flush()
    session.add(
        DiscogsLink(
            id=uuid.uuid4(),
            track_id=track.id,
            discogs_release_id="r123",
            confidence=0.9,
        )
    )
    await session.flush()
    return batch.id


async def _count(session: AsyncSession, model: type) -> int:
    """Count rows of a model within the current (uncommitted) transaction."""
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.asyncio
async def test_cascade_removes_full_graph_and_returns_counts(session: AsyncSession) -> None:
    """delete_scan_cascade removes the batch + every descendant row and reports counts."""
    batch_id = await _seed_full_graph(session)

    counts = await delete_scan_cascade(session, batch_id)

    assert counts == _EXPECTED_COUNTS

    # Every table is now empty (only this one batch was seeded).
    for model in (
        DiscogsLink,
        TracklistTrack,
        TracklistVersion,
        Tracklist,
        ExecutionLog,
        RenameProposal,
        AnalysisResult,
        FileMetadata,
        TagWriteLog,
        FileCompanion,
        StageSkip,
        FileRecord,
    ):
        assert await _count(session, model) == 0, f"{model.__name__} not fully deleted"
    assert await session.get(ScanBatch, batch_id) is None


@pytest.mark.asyncio
async def test_cascade_does_not_touch_sibling_batch(session: AsyncSession) -> None:
    """A SECOND independent full-graph batch is 100% intact after deleting the first."""
    batch_a = await _seed_full_graph(session)
    batch_b = await _seed_full_graph(session)

    await delete_scan_cascade(session, batch_a)

    # Batch B survives entirely: exactly one full graph's worth of every row.
    assert await _count(session, FileRecord) == _EXPECTED_COUNTS["files"]
    assert await _count(session, FileMetadata) == _EXPECTED_COUNTS["metadata"]
    assert await _count(session, AnalysisResult) == _EXPECTED_COUNTS["analysis"]
    assert await _count(session, TagWriteLog) == _EXPECTED_COUNTS["tag_write_log"]
    assert await _count(session, RenameProposal) == _EXPECTED_COUNTS["proposals"]
    assert await _count(session, ExecutionLog) == _EXPECTED_COUNTS["execution_log"]
    assert await _count(session, Tracklist) == _EXPECTED_COUNTS["tracklists"]
    assert await _count(session, TracklistVersion) == _EXPECTED_COUNTS["tracklist_versions"]
    assert await _count(session, TracklistTrack) == _EXPECTED_COUNTS["tracklist_tracks"]
    assert await _count(session, DiscogsLink) == _EXPECTED_COUNTS["discogs_links"]
    assert await _count(session, FileCompanion) == _EXPECTED_COUNTS["file_companions"]
    assert await session.get(ScanBatch, batch_b) is not None


@pytest.mark.asyncio
async def test_cross_batch_companion_join_dies_but_other_file_survives(session: AsyncSession) -> None:
    """Deleting batch A removes a cross-batch file_companions row but NOT the batch-B file."""
    batch_a = ScanBatch(id=uuid.uuid4(), agent_id="test-fileserver", scan_path="/a", status=ScanStatus.COMPLETED.value)
    batch_b = ScanBatch(id=uuid.uuid4(), agent_id="test-fileserver", scan_path="/b", status=ScanStatus.COMPLETED.value)
    session.add_all([batch_a, batch_b])
    await session.flush()

    file_a = _make_file(batch_a.id, "a-companion", file_type="jpg")
    file_b = _make_file(batch_b.id, "b-media")
    session.add_all([file_a, file_b])
    await session.flush()

    # Cross-batch companion: batch-A file is the companion of a batch-B media file.
    session.add(FileCompanion(id=uuid.uuid4(), companion_id=file_a.id, media_id=file_b.id))
    await session.flush()

    # Capture ids before expiring -- attribute access on an expired instance would
    # trigger a synchronous lazy-load (illegal under the async greenlet).
    file_a_id, file_b_id, batch_b_id = file_a.id, file_b.id, batch_b.id

    await delete_scan_cascade(session, batch_a.id)
    # Bulk deletes run with synchronize_session=False, so expire the identity map
    # to force session.get to re-read row existence from the database.
    session.expire_all()

    # The join row is gone (its companion side lived in batch A).
    assert await _count(session, FileCompanion) == 0
    # But the batch-B file is untouched.
    assert await session.get(FileRecord, file_b_id) is not None
    assert await session.get(FileRecord, file_a_id) is None
    assert await session.get(ScanBatch, batch_b_id) is not None


@pytest.mark.asyncio
async def test_cascade_removes_dedup_resolution_and_cloud_job_sidecars(session: AsyncSession) -> None:
    """delete_scan_cascade removes the dedup_resolution + cloud_job sidecar rows (CR-01 / WR-01).

    Both tables carry bare ``ForeignKey("files.id")`` (no ``ondelete``); before the
    fix, deleting a batch containing such a file raised an IntegrityError (500).
    Also exercises the cross-batch ``canonical_file_id`` case: a dedup_resolution
    row whose ``file_id`` is in a DIFFERENT batch but whose ``canonical_file_id``
    points into the deleted batch must also be removed.
    """
    batch_a = ScanBatch(id=uuid.uuid4(), agent_id="test-fileserver", scan_path="/a", status=ScanStatus.COMPLETED.value)
    batch_b = ScanBatch(id=uuid.uuid4(), agent_id="test-fileserver", scan_path="/b", status=ScanStatus.COMPLETED.value)
    session.add_all([batch_a, batch_b])
    await session.flush()

    file_a = _make_file(batch_a.id, "a-media")  # in the batch being deleted
    file_b = _make_file(batch_b.id, "b-media")  # in a surviving batch
    session.add_all([file_a, file_b])
    await session.flush()

    # Sidecars on the batch-A file: a resolved-duplicate marker + a cloud_job row.
    session.add(DedupResolution(id=uuid.uuid4(), file_id=file_a.id))
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_a.id, status=CloudJobStatus.AWAITING.value))
    # Cross-batch: a dedup_resolution OWNED by batch B (file_id=file_b) whose
    # canonical_file_id points into batch A. Deleting A must remove this row so
    # file_a can be deleted without a dangling canonical pointer.
    cross = DedupResolution(id=uuid.uuid4(), file_id=file_b.id, canonical_file_id=file_a.id)
    session.add(cross)
    await session.flush()

    file_a_id, file_b_id, cross_id, batch_b_id = file_a.id, file_b.id, cross.id, batch_b.id

    counts = await delete_scan_cascade(session, batch_a.id)
    session.expire_all()

    # Both dedup_resolution rows removed (file_a's own + the cross-batch canonical ref).
    assert counts["dedup_resolution"] == 2
    assert counts["cloud_job"] == 1
    assert await _count(session, CloudJob) == 0
    assert await _count(session, DedupResolution) == 0
    assert await session.get(DedupResolution, cross_id) is None
    # The batch-A file is gone; the batch-B file (and its batch) survive.
    assert await session.get(FileRecord, file_a_id) is None
    assert await session.get(FileRecord, file_b_id) is not None
    assert await session.get(ScanBatch, batch_b_id) is not None


@pytest.mark.asyncio
async def test_null_file_id_tracklist_is_never_touched(session: AsyncSession) -> None:
    """A scraped tracklist with file_id=NULL survives deletion of an unrelated batch."""
    batch = await _seed_full_graph(session)

    # A scraped-but-unmatched tracklist: file_id is NULL, so it belongs to no batch.
    orphan = Tracklist(
        id=uuid.uuid4(),
        external_id=uuid.uuid4().hex,
        source_url="https://1001.tl/orphan",
        file_id=None,
    )
    session.add(orphan)
    await session.flush()

    await delete_scan_cascade(session, batch)

    # The orphan tracklist must survive; the batch's own tracklist is gone.
    assert await session.get(Tracklist, orphan.id) is not None
    assert await _count(session, Tracklist) == 1


@pytest.mark.asyncio
async def test_cascade_locks_file_rows_in_original_path_order(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-zfxy6: the file-row FOR UPDATE sweep must sort by ``original_path``.

    ``delete_scan_cascade``'s file-row lock query and ``upsert_files``' (routers/agent_files.py)
    concurrent multi-row ``ON CONFLICT DO UPDATE`` are two independent lockers over an
    overlapping row set (a rescan reassigning a completed batch's files to the agent's live
    batch, racing an operator deleting that completed batch). With no shared, deterministic
    lock-acquisition order, the two sides could lock the same rows in different orders --
    the classic ABBA deadlock. ``original_path`` is the only column both sides can sort on, so
    the cascade's lock query must carry an explicit ``ORDER BY files.original_path``.
    """
    batch_id = await _seed_full_graph(session)

    captured: list[str] = []
    real_execute = session.execute

    async def _capturing_execute(stmt: object, *args: object, **kwargs: object) -> object:
        captured.append(str(stmt))
        return await real_execute(stmt, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(session, "execute", _capturing_execute)

    await delete_scan_cascade(session, batch_id)

    file_lock_stmts = [s for s in captured if "FOR UPDATE" in s and "FROM files" in s]
    assert file_lock_stmts, "expected a FOR UPDATE statement against files"
    assert "ORDER BY files.original_path" in file_lock_stmts[0]


@pytest.mark.asyncio
async def test_cascade_purges_scheduling_ledger_rows_for_its_files(session: AsyncSession) -> None:
    """phaze-u5dn: scheduling_ledger rows keyed on a deleted batch's files are purged, not stranded.

    scheduling_ledger carries NO foreign key to files (models/scheduling_ledger.py -- deliberate,
    "the row must survive even if its target row is mid-flight"), so the cascade must locate and
    remove these rows by natural-id predicate (``payload->>'file_id'``) rather than FK order. Before
    the fix, a ledger row for a file whose scan batch was deleted became an immortal orphan: every
    recovery pass re-dispatched it (full essentia re-analysis for ``process_file``), and its
    write-back FK-failed against the missing FileRecord so ``clear_ledger_entry`` was never reached.
    """
    batch_a = ScanBatch(id=uuid.uuid4(), agent_id="test-fileserver", scan_path="/a", status=ScanStatus.COMPLETED.value)
    batch_b = ScanBatch(id=uuid.uuid4(), agent_id="test-fileserver", scan_path="/b", status=ScanStatus.COMPLETED.value)
    session.add_all([batch_a, batch_b])
    await session.flush()

    file_a = _make_file(batch_a.id, "a-media")  # in the batch being deleted
    file_b = _make_file(batch_b.id, "b-media")  # in a surviving batch
    session.add_all([file_a, file_b])
    await session.flush()

    # Two file-keyed ledger rows for batch A's file (mirrors the two IMMORTAL stages the verifier
    # confirmed: process_file and extract_file_metadata both write an FK'd row before clearing).
    session.add(
        SchedulingLedger(
            key=f"process_file:{file_a.id}",
            function="process_file",
            routing="agent",
            payload={"file_id": str(file_a.id), "agent_id": "test-fileserver"},
        )
    )
    session.add(
        SchedulingLedger(
            key=f"extract_file_metadata:{file_a.id}",
            function="extract_file_metadata",
            routing="agent",
            payload={"file_id": str(file_a.id), "agent_id": "test-fileserver"},
        )
    )
    # A ledger row for the SURVIVING batch-B file must NOT be touched.
    session.add(
        SchedulingLedger(
            key=f"process_file:{file_b.id}",
            function="process_file",
            routing="agent",
            payload={"file_id": str(file_b.id), "agent_id": "test-fileserver"},
        )
    )
    await session.flush()

    file_a_id, file_b_id, batch_b_id = file_a.id, file_b.id, batch_b.id

    counts = await delete_scan_cascade(session, batch_a.id)
    session.expire_all()

    # Both of batch A's ledger rows are purged; batch B's survives untouched.
    assert counts["scheduling_ledger"] == 2
    remaining = (await session.execute(select(SchedulingLedger.key))).scalars().all()
    assert remaining == [f"process_file:{file_b_id}"]
    assert await session.get(FileRecord, file_a_id) is None
    assert await session.get(FileRecord, file_b_id) is not None
    assert await session.get(ScanBatch, batch_b_id) is not None
