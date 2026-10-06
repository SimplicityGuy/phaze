"""phaze-oxn2m: the single-file halves of ``services/scan_deletion.py`` a watcher move relies on.

- ``delete_file_cascade`` retires ONE stale row and every descendant, and nothing of its siblings.
- ``retention_blockers`` names the operator-reviewed state that stops a re-point or a retirement.
- ``invalidate_content_state`` drops exactly the state computed from a file's old bytes.
- ``moved_twin_candidates`` / ``retire_moved_twins`` are the one-off cleanup of rows a move left behind.

Real Postgres throughout (the ``session`` fixture): every function is set-based SQL, and the
blockers' correlated ``cloud_busy_clause`` only means anything against a real planner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import func, select

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.cloud_budget import CloudBudget
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scan_batch import ScanBatch
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.set_profile import SetProfile
from phaze.models.stage_skip import StageSkip
from phaze.models.tag_write_log import TagWriteLog
from phaze.models.tracklist import Tracklist
from phaze.services.scan_deletion import (
    delete_file_cascade,
    invalidate_content_state,
    moved_twin_candidates,
    retention_blockers,
    retire_moved_twins,
)
from tests.discovery.services.test_scan_deletion import _make_file, _seed_full_graph


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.asyncio


async def _count(session: AsyncSession, model: type, file_id: uuid.UUID | None = None) -> int:
    stmt = select(func.count()).select_from(model)
    if file_id is not None:
        stmt = stmt.where(model.file_id == file_id)  # type: ignore[attr-defined]
    return int((await session.execute(stmt)).scalar_one())


async def test_delete_file_cascade_removes_one_file_and_leaves_its_batch_and_sibling(session: AsyncSession) -> None:
    batch_id = await _seed_full_graph(session)
    files = (await session.execute(select(FileRecord).where(FileRecord.batch_id == batch_id))).scalars().all()
    media = next(f for f in files if f.file_type == "mp3")
    companion = next(f for f in files if f.file_type == "jpg")
    media_id, companion_id = media.id, companion.id

    counts = await delete_file_cascade(session, media_id)
    session.expire_all()

    assert counts["files"] == 1
    assert counts["metadata"] == counts["analysis"] == counts["proposals"] == counts["execution_log"] == 1
    assert counts["tracklists"] == counts["discogs_links"] == counts["file_companions"] == counts["stage_skip"] == 1
    assert "scan_batches" not in counts
    assert await session.get(FileRecord, media_id) is None
    assert await session.get(FileRecord, companion_id) is not None
    assert await session.get(ScanBatch, batch_id) is not None


async def _file(session: AsyncSession, **overrides: Any) -> FileRecord:
    record = _make_file(None, uuid.uuid4().hex[:8])
    for key, value in overrides.items():
        setattr(record, key, value)
    session.add(record)
    await session.flush()
    return record


async def test_retention_blockers_is_empty_for_machine_state_only(session: AsyncSession) -> None:
    record = await _file(session)
    session.add_all(
        [
            FileMetadata(file_id=record.id, artist="A"),
            RenameProposal(file_id=record.id, proposed_filename="x.mp3", status=ProposalStatus.PENDING.value),
            StageSkip(file_id=record.id, stage="metadata", reason="operator force-skip"),
        ]
    )
    await session.flush()

    assert await retention_blockers(session, record, content_changed=True) == []


async def test_retention_blockers_names_every_operator_reviewed_kind(session: AsyncSession) -> None:
    record = await _file(session)
    record.current_path = record.original_path + ".moved-by-execution"
    other = await _file(session)
    session.add_all(
        [
            RenameProposal(file_id=record.id, proposed_filename="x.mp3", status=ProposalStatus.APPROVED.value),
            RenameProposal(file_id=record.id, proposed_filename="y.mp3", status=ProposalStatus.REJECTED.value),
            TagWriteLog(file_id=record.id, before_tags={}, after_tags={}, source="metadata", status="completed"),
            DedupResolution(file_id=other.id, canonical_file_id=record.id),
        ]
    )
    await session.flush()

    assert await retention_blockers(session, record, content_changed=False) == [
        "relocated",
        "proposal:approved",
        "proposal:rejected",
        "tag_write",
        "dedup_resolution",
    ]


async def test_tracklists_and_companion_links_block_only_a_retirement(session: AsyncSession) -> None:
    """delete_file_cascade takes both with the row; a re-point keeps them, so they block retiring only."""
    record = await _file(session)
    companion = await _file(session, file_type="cue")
    session.add_all(
        [
            Tracklist(external_id=uuid.uuid4().hex, source_url="https://1001.tl/x", file_id=record.id),
            FileCompanion(companion_id=companion.id, media_id=record.id),
        ]
    )
    await session.flush()

    assert await retention_blockers(session, record, content_changed=True) == []
    assert await retention_blockers(session, record, content_changed=True, retiring=True) == ["tracklist", "companion_link"]
    assert await retention_blockers(session, companion, content_changed=True, retiring=True) == ["companion_link"]


async def test_a_busy_cloud_job_blocks_only_a_content_change(session: AsyncSession) -> None:
    record = await _file(session)
    session.add(CloudJob(file_id=record.id, status=CloudJobStatus.RUNNING.value))
    await session.flush()

    assert await retention_blockers(session, record, content_changed=False) == []
    assert await retention_blockers(session, record, content_changed=True) == ["cloud_job:busy"]


async def test_invalidate_content_state_drops_exactly_the_state_derived_from_the_old_bytes(session: AsyncSession) -> None:
    record = await _file(session)
    companion = await _file(session, file_type="cue")
    neighbour = await _file(session)
    file_id = record.id
    pending = RenameProposal(file_id=file_id, proposed_filename="x.mp3", status=ProposalStatus.PENDING.value)
    session.add_all(
        [
            FileMetadata(file_id=file_id, artist="truncated"),
            AnalysisResult(file_id=file_id, failed_at=datetime.now(UTC)),
            AnalysisWindow(file_id=file_id, tier="fine", window_index=0, start_sec=0.0, end_sec=30.0),
            SetProfile(file_id=file_id),
            pending,
            CloudJob(file_id=file_id, status=CloudJobStatus.SUCCEEDED.value),
            CloudBudget(file_id=file_id, budget_spent_at=datetime.now(UTC)),
            SchedulingLedger(key=f"process_file:{file_id}", function="process_file", routing="agent", payload={"file_id": str(file_id)}),
            # Kept: an operator force-skip and state that is not derived from the bytes.
            StageSkip(file_id=file_id, stage="analyze", reason="operator force-skip"),
            FileCompanion(companion_id=companion.id, media_id=file_id),
            Tracklist(external_id=uuid.uuid4().hex, source_url="https://1001.tl/x", file_id=file_id),
            # A neighbour's rows are never touched.
            FileMetadata(file_id=neighbour.id, artist="other"),
            SchedulingLedger(key=f"process_file:{neighbour.id}", function="process_file", routing="agent", payload={"file_id": str(neighbour.id)}),
        ]
    )
    await session.flush()

    neighbour_id = neighbour.id
    counts = await invalidate_content_state(session, file_id)
    session.expire_all()

    for model in (FileMetadata, AnalysisResult, AnalysisWindow, SetProfile, RenameProposal, CloudJob, CloudBudget):
        assert await _count(session, model, file_id) == 0, f"{model.__name__} survived a content change"
    assert counts["proposals"] == 1
    assert counts["scheduling_ledger"] == 1
    assert await session.get(SchedulingLedger, f"process_file:{file_id}") is None
    assert await _count(session, StageSkip, file_id) == 1
    assert await _count(session, Tracklist, file_id) == 1
    assert await _count(session, FileCompanion) == 1
    assert await _count(session, FileMetadata, neighbour_id) == 1
    assert await session.get(SchedulingLedger, f"process_file:{neighbour_id}") is not None
    assert await _count(session, ExecutionLog) == 0


# The one-off cleanup. Paths are invented placeholders under a fake scan root.
_ROOT = "/data/incoming"


async def _twin_agent(session: AsyncSession) -> str:
    agent_id = f"twin-{uuid.uuid4().hex[:8]}"
    session.add(Agent(id=agent_id, name=agent_id, token_hash=uuid.uuid4().hex * 2, scan_roots=[_ROOT]))
    await session.flush()
    return agent_id


async def _row(session: AsyncSession, agent_id: str, path: str, *, size: int = 100, sha: str | None = None) -> FileRecord:
    record = FileRecord(
        agent_id=agent_id,
        sha256_hash=sha or uuid.uuid4().hex * 2,
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="mp3",
        file_size=size,
    )
    session.add(record)
    await session.flush()
    return record


def _check(document: dict[str, Any], present: set[str], unknown: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Stand-in for the agent-side ``check-paths`` (tested on its own in test___main__.py)."""
    for group in document["groups"]:
        for member in group:
            member["exists"] = None if member["path"] in unknown else member["path"] in present
    return document


async def test_moved_twins_dry_run_classifies_and_apply_retires_only_the_stale_row(session: AsyncSession) -> None:
    agent_id = await _twin_agent(session)
    stale = await _row(session, agent_id, f"{_ROOT}/incomplete/rel/01.mp3", sha="a" * 64)
    twin = await _row(session, agent_id, f"{_ROOT}/rel/01.mp3", sha="b" * 64)
    same_stale = await _row(session, agent_id, f"{_ROOT}/incomplete/rel/02.mp3", sha="c" * 64)
    same_twin = await _row(session, agent_id, f"{_ROOT}/rel/02.mp3", sha="c" * 64)
    reviewed = await _row(session, agent_id, f"{_ROOT}/incomplete/rel/03.mp3")
    await _row(session, agent_id, f"{_ROOT}/rel/03.mp3")
    session.add(RenameProposal(file_id=reviewed.id, proposed_filename="x.mp3", status=ProposalStatus.APPROVED.value))
    await _row(session, agent_id, f"{_ROOT}/a/04.mp3")
    await _row(session, agent_id, f"{_ROOT}/b/04.mp3")
    for directory in ("a", "b", "c"):
        await _row(session, agent_id, f"{_ROOT}/{directory}/05.mp3")
    # A fixed-size stamp file present in many releases: the twin is the one in the same-named release.
    stamp_stale = await _row(session, agent_id, f"{_ROOT}/incomplete/rel/stamp.mp3", size=7)
    stamp_twin = await _row(session, agent_id, f"{_ROOT}/rel/stamp.mp3", size=7)
    await _row(session, agent_id, f"{_ROOT}/other/stamp.mp3", size=7)
    await _row(session, agent_id, f"{_ROOT}/x/06.mp3")
    await _row(session, agent_id, f"{_ROOT}/y/06.mp3")
    await _row(session, agent_id, f"{_ROOT}/rel/unique.mp3")
    await _row(session, agent_id, f"{_ROOT}/rel/07.mp3", size=1)  # same name, different size: not a twin
    await _row(session, agent_id, f"{_ROOT}/incomplete/rel/07.mp3", size=2)
    await session.flush()

    document = await moved_twin_candidates(session, agent_id)
    assert document["scan_roots"] == [_ROOT]
    assert sorted(len(group) for group in document["groups"]) == [2, 2, 2, 2, 2, 3, 3]
    present = {
        twin.current_path,
        same_twin.current_path,
        f"{_ROOT}/rel/03.mp3",
        f"{_ROOT}/b/05.mp3",
        f"{_ROOT}/c/05.mp3",
        stamp_twin.current_path,
        f"{_ROOT}/other/stamp.mp3",
    }
    checked = _check(document, present, unknown=frozenset({f"{_ROOT}/x/06.mp3"}))

    dry = await retire_moved_twins(session, agent_id, checked, apply=False)

    assert sorted(dry.retire) == sorted([(stale.id, twin.id), (same_stale.id, same_twin.id), (stamp_stale.id, stamp_twin.id)])
    assert dry.same_content == 1
    assert [(s, t) for s, t, _ in dry.kept_reviewed] == [(reviewed.id, (await _id(session, agent_id, f"{_ROOT}/rel/03.mp3")))]
    assert dry.kept_reviewed[0][2] == ["proposal:approved"]
    assert (dry.absent_without_twin, dry.absent_with_several_twins, dry.unverifiable_groups) == (2, 1, 1)
    assert await session.get(FileRecord, stale.id) is not None, "a dry run writes nothing"

    stale_id, same_stale_id, twin_id, reviewed_id = stale.id, same_stale.id, twin.id, reviewed.id
    applied = await retire_moved_twins(session, agent_id, checked, apply=True)
    session.expire_all()

    assert sorted(applied.retire) == sorted(dry.retire)
    assert await session.get(FileRecord, stale_id) is None
    assert await session.get(FileRecord, same_stale_id) is None
    assert await session.get(FileRecord, twin_id) is not None
    assert await session.get(FileRecord, reviewed_id) is not None

    again = await retire_moved_twins(session, agent_id, checked, apply=True)
    assert again.retire == []
    assert again.changed_since_check == 3, "the already-retired rows no longer match the checked document"


async def _id(session: AsyncSession, agent_id: str, path: str) -> uuid.UUID:
    return (await session.execute(select(FileRecord.id).where(FileRecord.agent_id == agent_id, FileRecord.original_path == path))).scalar_one()


async def test_moved_twins_refuse_a_document_for_another_agent_or_an_unknown_agent(session: AsyncSession) -> None:
    agent_id = await _twin_agent(session)

    with pytest.raises(ValueError, match="not"):
        await retire_moved_twins(session, agent_id, {"agent_id": "someone-else", "groups": []}, apply=False)
    with pytest.raises(ValueError, match="no agent"):
        await moved_twin_candidates(session, "no-such-agent")
