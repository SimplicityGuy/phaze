"""phaze-5rfev: reconcile rows whose file is no longer at ``current_path`` -- against real files and real Postgres.

Every test drives the whole operator pipeline with its real consumers: ``stale_row_candidates`` reads
the rows from the ``session`` fixture's Postgres seat, the AGENT-side ``locate_stale`` stats and hashes
real files under a ``tmp_path`` scan root, the document round-trips through JSON (the stdin/stdout hop
between the two containers), and ``reconcile_stale_rows`` classifies and writes. Nothing is mocked:
the bead's acceptance is about what a re-point, a merge and a missing mark do to rows with history,
which only real tables (unique constraints, partial indexes, FKs) can answer.

Paths are invented placeholders under a temporary directory.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import func, select

from phaze.agent_watcher.locate import locate_stale
from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.models.tag_write_log import TagWriteLog
from phaze.services.hashing import compute_sha256
from phaze.services.pipeline import (
    get_discovered_files_with_duration,
    get_metadata_failed_files,
    get_metadata_pending_files,
    get_pending_files_page,
)
from phaze.services.stale_rows import ReconcileReport, Verdict, reconcile_stale_rows, stale_row_candidates


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.asyncio


async def _agent(session: AsyncSession, *roots: Path) -> str:
    agent_id = f"stale-{uuid.uuid4().hex[:8]}"
    session.add(Agent(id=agent_id, name=agent_id, token_hash=uuid.uuid4().hex * 2, scan_roots=[str(root) for root in roots]))
    await session.flush()
    return agent_id


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


async def _row(session: AsyncSession, agent_id: str, path: Path, *, sha: str | None = None, size: int | None = None) -> FileRecord:
    """A row exactly as a scan of ``path`` would have made it (hashed from the real bytes when the file exists)."""
    record = FileRecord(
        agent_id=agent_id,
        sha256_hash=sha or compute_sha256(path),
        original_path=str(path),
        original_filename=path.name,
        current_path=str(path),
        file_type=path.suffix.lstrip("."),
        file_size=size if size is not None else path.stat().st_size,
    )
    session.add(record)
    await session.flush()
    return record


async def _failed_metadata(session: AsyncSession, file_id: uuid.UUID) -> None:
    """What 4 retries of a moved file left behind: a failure-only metadata row and its ledger row."""
    session.add_all(
        [
            FileMetadata(file_id=file_id, failed_at=datetime.now(UTC), error_message="FileNotFoundError: [Errno 2] No such file or directory"),
            SchedulingLedger(
                key=f"extract_file_metadata:{file_id}", function="extract_file_metadata", routing="agent", payload={"file_id": str(file_id)}
            ),
        ]
    )
    await session.flush()


async def _reconcile(session: AsyncSession, agent_id: str, *, apply: bool, under: str | None = None) -> ReconcileReport:
    """Steps 1-3 of the operator pipeline, with the JSON hop between the controller and the agent."""
    candidates = json.loads(json.dumps(await stale_row_candidates(session, agent_id, under)))
    located = json.loads(json.dumps(locate_stale(candidates)))
    report = await reconcile_stale_rows(session, agent_id, located, apply=apply)
    await session.flush()
    session.expire_all()
    return report


def _verdicts(report: ReconcileReport) -> dict[uuid.UUID, Verdict]:
    return {action.file_id: action.verdict for action in report.actions}


async def _ids_at(session: AsyncSession, agent_id: str) -> list[tuple[uuid.UUID, str]]:
    rows = await session.execute(
        select(FileRecord.id, FileRecord.current_path).where(FileRecord.agent_id == agent_id).order_by(FileRecord.current_path)
    )
    return [(row.id, row.current_path) for row in rows]


async def test_a_moved_file_with_no_duplicate_is_repointed_and_extract_all_picks_it_up_again(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    old = _write(root / "incoming" / "rel" / "01 track.mp3", b"moved audio bytes")
    agent_id = await _agent(session, root)
    stale = await _row(session, agent_id, old)
    stale_id, sha = stale.id, stale.sha256_hash
    await _failed_metadata(session, stale_id)
    session.add(AnalysisResult(file_id=stale_id, bpm=124.0, analysis_completed_at=datetime.now(UTC)))
    await session.flush()
    new = root / "sets" / "rel renamed" / "01 track (live).mp3"
    new.parent.mkdir(parents=True)
    old.rename(new)

    dry = await _reconcile(session, agent_id, apply=False)

    assert [(a.verdict, a.file_id, a.target_path, a.target_id) for a in dry.actions] == [(Verdict.REPOINT, stale_id, str(new), None)]
    assert (await session.get(FileRecord, stale_id)).current_path == str(old), "a dry run writes nothing"

    applied = await _reconcile(session, agent_id, apply=True)

    assert _verdicts(applied) == {stale_id: Verdict.REPOINT}
    record = await session.get(FileRecord, stale_id)
    assert (record.original_path, record.current_path, record.original_filename, record.sha256_hash) == (str(new), str(new), new.name, sha)
    assert record.missing_at is None
    assert await session.scalar(select(func.count()).select_from(FileMetadata).where(FileMetadata.file_id == stale_id)) == 0, (
        "the failure row is gone, so the stage reads not-started"
    )
    assert (
        await session.scalar(select(func.count()).select_from(SchedulingLedger).where(SchedulingLedger.key == f"extract_file_metadata:{stale_id}"))
        == 0
    )
    assert (await session.scalar(select(AnalysisResult.bpm).where(AnalysisResult.file_id == stale_id))) == 124.0, "same bytes: analysis kept"
    assert stale_id in {f.id for f in await get_metadata_pending_files(session)}, "'Extract all' selects it again"

    again = await _reconcile(session, agent_id, apply=True)
    assert again.actions == [] and again.present == 1, "idempotent: the re-pointed row now reads present"


async def test_a_moved_file_the_scan_readmitted_is_merged_into_one_row_keeping_all_history(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    old = _write(root / "a" / "set.opus", b"the same set bytes")
    agent_id = await _agent(session, root)
    stale = await _row(session, agent_id, old)
    stale_id, sha = stale.id, stale.sha256_hash
    # The stale row's history: hours of analysis (with windows), a pending proposal, a force-skip,
    # and the failed metadata from the retries.
    await _failed_metadata(session, stale_id)
    session.add_all(
        [
            AnalysisResult(file_id=stale_id, bpm=128.0, analysis_completed_at=datetime.now(UTC)),
            AnalysisWindow(file_id=stale_id, tier="coarse", window_index=0, start_sec=0.0, end_sec=30.0),
            RenameProposal(file_id=stale_id, proposed_filename="kept.opus", status=ProposalStatus.PENDING.value),
            StageSkip(file_id=stale_id, stage="analyze", reason="operator force-skip"),
        ]
    )
    new = root / "b" / "set.opus"
    new.parent.mkdir(parents=True)
    old.rename(new)
    # The scan of 2026-10-08 admitted the new path as a fresh row: real metadata, its own pending
    # proposal, a cover-art companion link and a partial analysis started before the merge.
    duplicate = await _row(session, agent_id, new)
    duplicate_id = duplicate.id
    cover = await _row(session, agent_id, _write(root / "b" / "cover.jpg", b"jpeg"))
    cover_id = cover.id
    session.add_all(
        [
            FileMetadata(file_id=duplicate_id, artist="Artist", title="Set", duration=3600.0),
            RenameProposal(file_id=duplicate_id, proposed_filename="dropped.opus", status=ProposalStatus.PENDING.value),
            FileCompanion(companion_id=cover.id, media_id=duplicate_id),
            AnalysisResult(file_id=duplicate_id),
            StageSkip(file_id=duplicate_id, stage="metadata", reason="operator force-skip"),
        ]
    )
    await session.flush()

    dry = await _reconcile(session, agent_id, apply=False)

    assert [(a.verdict, a.file_id, a.target_path, a.target_id) for a in dry.actions] == [(Verdict.MERGE, stale_id, str(new), duplicate_id)]
    assert await session.get(FileRecord, duplicate_id) is not None, "a dry run writes nothing"

    await _reconcile(session, agent_id, apply=True)

    assert await session.get(FileRecord, duplicate_id) is None
    assert await _ids_at(session, agent_id) == sorted([(stale_id, str(new)), (cover_id, str(root / "b" / "cover.jpg"))], key=lambda pair: pair[1])
    kept = await session.get(FileRecord, stale_id)
    assert (kept.original_path, kept.sha256_hash) == (str(new), sha)
    # History: the stale row's analysis, windows, proposal and skip survive ...
    assert (await session.scalar(select(AnalysisResult.bpm).where(AnalysisResult.file_id == stale_id))) == 128.0
    assert await session.scalar(select(func.count()).select_from(AnalysisWindow).where(AnalysisWindow.file_id == stale_id)) == 1
    proposals = (await session.execute(select(RenameProposal.proposed_filename).where(RenameProposal.file_id == stale_id))).scalars().all()
    assert proposals == ["kept.opus"], "one pending proposal per file: the kept row's wins"
    # ... and the duplicate's successful metadata, companion link and its other skip move over.
    metadata = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == stale_id))).scalar_one()
    assert (metadata.artist, metadata.failed_at) == ("Artist", None)
    assert (await session.execute(select(FileCompanion.media_id).where(FileCompanion.companion_id == cover_id))).scalar_one() == stale_id
    stages = (await session.execute(select(StageSkip.stage).where(StageSkip.file_id == stale_id).order_by(StageSkip.stage))).scalars().all()
    assert stages == ["analyze", "metadata"]
    # One row for the bytes: no duplicate group left for them.
    assert await session.scalar(select(func.count()).select_from(FileRecord).where(FileRecord.sha256_hash == sha)) == 1

    again = await _reconcile(session, agent_id, apply=True)
    assert again.actions == [] and again.present == 2, "idempotent"


async def test_a_completed_analysis_on_the_duplicate_replaces_a_failed_one_on_the_kept_row(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    old = _write(root / "a" / "t.m4a", b"m4a bytes")
    agent_id = await _agent(session, root)
    stale_id = (await _row(session, agent_id, old)).id
    session.add(AnalysisResult(file_id=stale_id, failed_at=datetime.now(UTC), error_message="FileNotFoundError"))
    new = root / "b" / "t.m4a"
    new.parent.mkdir(parents=True)
    old.rename(new)
    duplicate_id = (await _row(session, agent_id, new)).id
    session.add_all(
        [
            AnalysisResult(file_id=duplicate_id, bpm=90.0, analysis_completed_at=datetime.now(UTC)),
            AnalysisWindow(file_id=duplicate_id, tier="fine", window_index=0, start_sec=0.0, end_sec=5.0),
        ]
    )
    await session.flush()

    await _reconcile(session, agent_id, apply=True)

    analysis = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == stale_id))).scalar_one()
    assert (analysis.bpm, analysis.failed_at) == (90.0, None)
    assert await session.scalar(select(func.count()).select_from(AnalysisWindow).where(AnalysisWindow.file_id == stale_id)) == 1
    assert await session.get(FileRecord, duplicate_id) is None


async def test_a_file_gone_everywhere_is_marked_missing_kept_and_no_longer_retried(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    _write(root / "other" / "same size.mp3", b"0123456789")  # same size, other bytes: never a match
    gone = await _row(session, agent_id, root / "rel" / "gone.mp3", sha="d" * 64, size=10)
    gone_id = gone.id
    await _failed_metadata(session, gone_id)
    session.add(RenameProposal(file_id=gone_id, proposed_filename="x.mp3", status=ProposalStatus.PENDING.value))
    await session.flush()
    assert gone_id in {f.id for f in await get_metadata_failed_files(session)}

    dry = await _reconcile(session, agent_id, apply=False)
    assert _verdicts(dry) == {gone_id: Verdict.MISSING}
    assert (await session.get(FileRecord, gone_id)).missing_at is None

    await _reconcile(session, agent_id, apply=True)

    record = await session.get(FileRecord, gone_id)
    assert record is not None and record.missing_at is not None, "kept, not deleted"
    assert await session.scalar(select(func.count()).select_from(RenameProposal).where(RenameProposal.file_id == gone_id)) == 1
    assert gone_id not in {f.id for f in await get_metadata_pending_files(session)}
    assert gone_id not in {f.id for f in await get_metadata_failed_files(session)}
    assert gone_id not in {f.id for f, _duration in await get_discovered_files_with_duration(session)}
    for stage in (Stage.METADATA, Stage.ANALYZE):
        assert gone_id not in {f.id for f in (await get_pending_files_page(session, stage, page_size=500)).rows}
    assert (
        await session.scalar(select(func.count()).select_from(SchedulingLedger).where(SchedulingLedger.key == f"extract_file_metadata:{gone_id}"))
        == 0
    )

    again = await _reconcile(session, agent_id, apply=True)
    assert again.actions == [] and again.already_missing == 1, "idempotent: counted, not re-stamped"

    # The file comes back at its path: the next reconcile clears the marker.
    _write(root / "rel" / "gone.mp3", b"back")
    restored = await _reconcile(session, agent_id, apply=True)
    assert _verdicts(restored) == {gone_id: Verdict.RESTORED}
    assert (await session.get(FileRecord, gone_id)).missing_at is None


async def test_operator_reviewed_state_is_reported_and_never_rewritten(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    old = _write(root / "a" / "r.mp3", b"reviewed")
    stale_id = (await _row(session, agent_id, old)).id
    session.add(RenameProposal(file_id=stale_id, proposed_filename="r.mp3", status=ProposalStatus.APPROVED.value))
    old_dup = _write(root / "a" / "d.mp3", b"duplicate tagged")
    stale_dup_id = (await _row(session, agent_id, old_dup)).id
    new_dup = root / "b" / "d.mp3"
    new_dup.parent.mkdir(parents=True)
    old_dup.rename(new_dup)
    duplicate_id = (await _row(session, agent_id, new_dup)).id
    session.add(TagWriteLog(file_id=duplicate_id, before_tags={}, after_tags={}, source="metadata", status="completed"))
    old.rename(root / "b" / "r.mp3")
    await session.flush()

    report = await _reconcile(session, agent_id, apply=True)

    kept = {action.file_id: (action.verdict, action.detail) for action in report.actions}
    assert kept == {stale_id: (Verdict.KEPT_REVIEWED, ["proposal:approved"]), stale_dup_id: (Verdict.KEPT_REVIEWED, ["duplicate:tag_write"])}
    assert (await session.get(FileRecord, stale_id)).current_path == str(old)
    assert await session.get(FileRecord, duplicate_id) is not None


async def test_several_copies_are_narrowed_by_name_or_left_ambiguous(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    named = await _row(session, agent_id, _write(root / "a" / "keep name.mp3", b"copied"))
    unnamed = await _row(session, agent_id, _write(root / "a" / "x.mp3", b"twice"))
    claimed_a = await _row(session, agent_id, _write(root / "c" / "one.mp3", b"claimed"))
    claimed_b = await _row(session, agent_id, root / "d" / "one.mp3", sha=claimed_a.sha256_hash, size=claimed_a.file_size)
    for path in (root / "a" / "keep name.mp3", root / "a" / "x.mp3", root / "c" / "one.mp3"):
        path.unlink()
    _write(root / "b" / "keep name.mp3", b"copied")
    _write(root / "b" / "other name.mp3", b"copied")
    _write(root / "b" / "y.mp3", b"twice")
    _write(root / "b" / "z.mp3", b"twice")
    _write(root / "e" / "one.mp3", b"claimed")

    named_id, unnamed_id, claimed_ids = named.id, unnamed.id, (claimed_a.id, claimed_b.id)

    report = await _reconcile(session, agent_id, apply=False)

    assert _verdicts(report) == {
        named_id: Verdict.REPOINT,
        unnamed_id: Verdict.AMBIGUOUS,
        claimed_ids[0]: Verdict.AMBIGUOUS,
        claimed_ids[1]: Verdict.AMBIGUOUS,
    }
    by_id = {action.file_id: action for action in report.actions}
    assert by_id[named_id].target_path == str(root / "b" / "keep name.mp3")
    assert by_id[unnamed_id].detail == [str(root / "b" / "y.mp3"), str(root / "b" / "z.mp3")]
    assert by_id[claimed_ids[0]].detail == ["claimed by several gone rows"]


async def test_rows_under_an_unmounted_root_are_unverifiable_and_changed_rows_are_skipped(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root, tmp_path / "not-mounted")
    await _row(session, agent_id, tmp_path / "not-mounted" / "a.mp3", sha="e" * 64, size=1)
    old = _write(root / "a" / "m.mp3", b"moved")
    moved = await _row(session, agent_id, old)
    (root / "b").mkdir()
    old.rename(root / "b" / "m.mp3")
    gone = await _row(session, agent_id, root / "g.mp3", sha="f" * 64, size=3)

    candidates = json.loads(json.dumps(await stale_row_candidates(session, agent_id)))
    located = locate_stale(candidates)
    # The rows change between the agent's check and the reconcile.
    moved.current_path = str(root / "elsewhere.mp3")
    gone.sha256_hash = "0" * 64
    await session.flush()

    report = await reconcile_stale_rows(session, agent_id, located, apply=True)

    assert report.unverifiable == 1
    assert _verdicts(report) == {moved.id: Verdict.CHANGED, gone.id: Verdict.CHANGED}


async def test_candidates_can_be_limited_to_one_root_and_refuse_another_agents_document(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root_1"
    agent_id = await _agent(session, root, tmp_path / "root_2")
    await _row(session, agent_id, root / "a.mp3", sha="1" * 64, size=1)
    await _row(session, agent_id, tmp_path / "root_2" / "b.mp3", sha="2" * 64, size=1)
    await _row(session, agent_id, tmp_path / "rootX1" / "c.mp3", sha="3" * 64, size=1)  # `_` is not a LIKE wildcard here

    document = await stale_row_candidates(session, agent_id, under=f"{root}/")

    assert [row["path"] for row in document["rows"]] == [str(root / "a.mp3")]
    assert document["scan_roots"] == [str(root), str(tmp_path / "root_2")]
    with pytest.raises(ValueError, match="not"):
        await reconcile_stale_rows(session, agent_id, {"agent_id": "someone-else", "rows": []}, apply=False)
    with pytest.raises(ValueError, match="no agent"):
        await stale_row_candidates(session, "no-such-agent")


async def test_a_found_record_that_fails_validation_is_reported_changed(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    row = await _row(session, agent_id, root / "a.mp3", sha="a" * 64, size=1)
    located: dict[str, Any] = {
        "agent_id": agent_id,
        "rows": [{"id": str(row.id), "path": row.current_path, "sha256": row.sha256_hash, "size": 1, "exists": False, "found": [{"bogus": 1}]}],
    }

    report = await reconcile_stale_rows(session, agent_id, located, apply=True)

    assert _verdicts(report) == {row.id: Verdict.CHANGED}
    assert report.counts()["changed"] == 1


async def test_a_merge_keeps_the_kept_rows_own_one_to_one_rows_and_links_it_already_has(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    old = _write(root / "a" / "k.mp3", b"kept bytes")
    agent_id = await _agent(session, root)
    stale_id = (await _row(session, agent_id, old)).id
    cover = await _row(session, agent_id, _write(root / "a" / "cover.jpg", b"jpeg"))
    cover_id = cover.id
    session.add_all([FileMetadata(file_id=stale_id, artist="Kept"), FileCompanion(companion_id=cover_id, media_id=stale_id)])
    new = root / "b" / "k.mp3"
    new.parent.mkdir(parents=True)
    old.rename(new)
    duplicate_id = (await _row(session, agent_id, new)).id
    session.add_all(
        [
            FileMetadata(file_id=duplicate_id, artist="Dropped"),
            FileCompanion(companion_id=cover_id, media_id=duplicate_id),  # the same pair the kept row already has
            FileCompanion(companion_id=duplicate_id, media_id=stale_id),  # would become a link onto itself
        ]
    )
    await session.flush()

    report = await _reconcile(session, agent_id, apply=True)

    assert _verdicts(report) == {stale_id: Verdict.MERGE}
    assert (await session.execute(select(FileMetadata.artist).where(FileMetadata.file_id == stale_id))).scalar_one() == "Kept"
    links = (await session.execute(select(FileCompanion.companion_id, FileCompanion.media_id))).all()
    assert [tuple(link) for link in links] == [(cover_id, stale_id)]


async def test_a_row_already_at_the_new_path_with_other_content_is_left_alone(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    old = _write(root / "a" / "c.mp3", b"current bytes")
    agent_id = await _agent(session, root)
    stale_id = (await _row(session, agent_id, old)).id
    new = root / "b" / "c.mp3"
    new.parent.mkdir(parents=True)
    old.rename(new)
    other_id = (await _row(session, agent_id, new, sha="e" * 64)).id  # admitted before the bytes settled
    await session.flush()

    report = await _reconcile(session, agent_id, apply=True)

    assert [(a.verdict, a.file_id, a.target_id, a.detail) for a in report.actions] == [
        (Verdict.CHANGED, stale_id, other_id, ["the row at the new path has other content"])
    ]
    assert await session.get(FileRecord, other_id) is not None


async def test_a_marked_row_that_changed_since_the_check_is_not_restored(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    back = await _row(session, agent_id, _write(root / "back.mp3", b"back"))
    back.missing_at = datetime.now(UTC)
    await session.flush()
    located = locate_stale(json.loads(json.dumps(await stale_row_candidates(session, agent_id))))
    back.current_path = str(root / "elsewhere.mp3")
    await session.flush()

    report = await reconcile_stale_rows(session, agent_id, located, apply=True)

    assert _verdicts(report) == {back.id: Verdict.CHANGED}
    assert back.missing_at is not None
