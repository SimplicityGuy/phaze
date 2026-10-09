"""Reconcile ``files`` rows whose file is no longer at ``current_path`` on its agent (phaze-5rfev).

Rows went stale when files moved in-tree before the watcher re-pointed rows on a move (phaze-oxn2m):
nothing re-pointed them, so every "Extract all" failed them with ``FileNotFoundError``, and a later
scan of the new path admitted the same bytes again as a second row. This module decides, per row
the agent reports gone, what that row is now and -- with ``apply`` -- makes it so.

Only the agent can see its disk, so the operator command runs in three steps, like the phaze-oxn2m
moved-twin cleanup (``phaze.cli`` module docstring):

1. :func:`stale_row_candidates` (controller, read-only) prints the agent's rows.
2. ``python -m phaze.agent_watcher locate-stale`` (``phaze.agent_watcher.locate``, on the agent) marks
   each ``exists`` and, for a gone row, lists every file under the agent's scan roots with the row's
   SHA-256 (``found``) -- hashed on the agent, so a re-point is verified against the bytes on disk.
3. :func:`reconcile_stale_rows` (controller) classifies, and writes only with ``apply``.

Verdicts for a row reported gone (``exists: False``):

- ``repoint`` -- its content is at exactly one other path and no row sits there: the row is
  re-pointed in place (``agent_upsert.repoint_file``, the phaze-oxn2m move route's re-point), keeping
  its id and every child row.
- ``merge`` -- its content is at exactly one other path and a scan already admitted that path as a
  second row with the same hash. The two become ONE row: the OLDER (stale) row is kept, because it is
  the one carrying the history, and every child row of the duplicate is moved onto it
  (:func:`_merge_children`); only machine-derived state the kept row already has for the same bytes
  is dropped. The duplicate is then deleted and the kept row re-pointed to its path.
- ``missing`` -- no file anywhere under the mounted scan roots has its content: ``files.missing_at``
  is stamped. The row is never deleted; it leaves the enrich pending sets and reads "Missing" on Files.
- ``ambiguous`` -- its content is at several paths (narrowed to those keeping the row's filename),
  or several gone rows claim the same path. Left untouched.
- ``kept_reviewed`` -- either row carries operator-reviewed state (``scan_deletion.retention_blockers``:
  a reviewed proposal, a tag write, a dedup resolution, a relocation by execution, or a busy cloud
  job on a row a merge would delete). Never rewritten on a guess; reported with the reasons.
- ``changed`` -- the row (or the duplicate) changed after the agent's check. Re-run the pipeline.

A row reported present that carries ``missing_at`` came back and is ``restored`` (the marker is
cleared). A row under no mounted scan root is ``unverifiable``.

Every re-pointed or merged row's metadata FAILURE marker is deleted and its scheduling-ledger rows
cleared (guarded), so the next "Extract all" picks it up afresh (the bead's acceptance); a successful
metadata row is never touched. A missing row's ledger rows are cleared too, so recovery stops
replaying work that can only fail.

Idempotent: a re-pointed row reads present on the next run, a merged duplicate is gone, and an
already-missing row is counted, not re-stamped. Nothing commits here -- the caller owns the
transaction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
import enum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
import uuid

from pydantic import ValidationError
from sqlalchemy import delete, exists, or_, select, update
import structlog

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.cloud_budget import CloudBudget
from phaze.models.cloud_job import CloudJob
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.set_profile import SetProfile
from phaze.models.stage_skip import StageSkip
from phaze.models.tag_write_log import TagWriteLog
from phaze.models.tracklist import Tracklist
from phaze.schemas.agent_files import FileUpsertRecord
from phaze.services.agent_upsert import file_row, repoint_file
from phaze.services.companion_content import COMPANION_FILE_TYPES
from phaze.services.like_escape import LIKE_ESCAPE_CHAR, escape_like
from phaze.services.scan_deletion import delete_file_cascade, retention_blockers
from phaze.services.scheduling_ledger import clear_ledger_entry


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

_ID_PAGE = 5000
"""Ids per ``IN (...)`` read when finding the marked rows among the present ones."""


class Verdict(enum.StrEnum):
    """What a reconcile decided for one row (module docstring)."""

    REPOINT = "repoint"
    MERGE = "merge"
    MISSING = "missing"
    RESTORED = "restored"
    AMBIGUOUS = "ambiguous"
    KEPT_REVIEWED = "kept_reviewed"
    CHANGED = "changed"


@dataclass(slots=True)
class RowAction:
    """One row's verdict. ``target_path``/``target_id`` name where it moved to and the row it merges with."""

    verdict: Verdict
    file_id: uuid.UUID
    path: str
    target_path: str | None = None
    target_id: uuid.UUID | None = None
    detail: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReconcileReport:
    """Every non-trivial verdict, plus the counts of rows that needed nothing."""

    actions: list[RowAction] = field(default_factory=list)
    present: int = 0
    unverifiable: int = 0
    already_missing: int = 0
    walk_errors: int = 0

    def counts(self) -> dict[str, int]:
        """``{verdict: rows}`` for every verdict, zeros included, in declaration order."""
        tally = Counter(action.verdict for action in self.actions)
        return {verdict.value: tally[verdict] for verdict in Verdict}


async def _agent(session: AsyncSession, agent_id: str) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        msg = f"no agent {agent_id!r}"
        raise ValueError(msg)
    return agent


async def stale_row_candidates(session: AsyncSession, agent_id: str, under: str | None = None) -> dict[str, Any]:
    """Every row of ``agent_id`` (only those at or below ``under``, when given), as the document ``locate-stale`` annotates. Read-only.

    ``path`` is ``current_path``, where the row says the file is now; ``sha256``/``size`` are what the
    agent searches for when that path is gone; ``scan_roots`` bound where it may look.
    """
    agent = await _agent(session, agent_id)
    stmt = select(FileRecord.id, FileRecord.current_path, FileRecord.sha256_hash, FileRecord.file_size).where(FileRecord.agent_id == agent_id)
    if under is not None:
        prefix = under.rstrip("/")
        stmt = stmt.where((FileRecord.current_path == prefix) | FileRecord.current_path.like(f"{escape_like(prefix)}/%", escape=LIKE_ESCAPE_CHAR))
    rows = await session.stream(stmt.order_by(FileRecord.current_path).execution_options(yield_per=500))
    return {
        "agent_id": agent_id,
        "scan_roots": list(agent.scan_roots or []),
        "under": under,
        "rows": [{"id": str(row.id), "path": row.current_path, "sha256": row.sha256_hash, "size": row.file_size} async for row in rows],
    }


def _target(row: dict[str, Any], found: list[FileUpsertRecord], *, strict: bool = False) -> tuple[FileUpsertRecord | None, list[str]]:
    """The one found file a gone row moved to, or ``None`` and the candidate paths when there is not exactly one.

    Several copies of the same bytes are narrowed to those that kept the row's filename (a moved
    release keeps its track names); anything still plural is the operator's call. Companions use
    ``strict=True``: a matching filename never establishes a unique destination among byte copies.
    """
    if len(found) > 1 and not strict:
        name = PurePosixPath(row["path"]).name
        found = [record for record in found if record.original_filename == name] or found
    if len(found) == 1:
        return found[0], []
    return None, [record.original_path for record in found]


async def reconcile_stale_rows(session: AsyncSession, agent_id: str, located: dict[str, Any], *, apply: bool) -> ReconcileReport:
    """Classify every row of the agent-annotated document and, with ``apply``, reconcile it (module docstring). Does NOT commit."""
    if located.get("agent_id") != agent_id:
        msg = f"the located document is for agent {located.get('agent_id')!r}, not {agent_id!r}"
        raise ValueError(msg)
    agent = await _agent(session, agent_id)
    report = ReconcileReport(walk_errors=int(located.get("walk_errors") or 0))

    gone: list[tuple[dict[str, Any], FileUpsertRecord | None, list[str]]] = []
    present: dict[uuid.UUID, dict[str, Any]] = {}
    for row in located["rows"]:
        if row.get("exists") is None:
            report.unverifiable += 1
        elif row["exists"] is True:
            present[uuid.UUID(row["id"])] = row
        else:
            try:
                found = [FileUpsertRecord.model_validate(record) for record in row.get("found") or []]
            except ValidationError as exc:
                report.actions.append(
                    RowAction(Verdict.CHANGED, uuid.UUID(row["id"]), row["path"], detail=[f"invalid found record: {exc.error_count()} error(s)"])
                )
                continue
            record = await session.get(FileRecord, uuid.UUID(row["id"]))
            strict = record is not None and record.file_type in COMPANION_FILE_TYPES
            gone.append((row, *_target(row, found, strict=strict)))
    await _restore_present(session, agent_id, present, report, apply=apply)
    claims = Counter(target.original_path for _row, target, _paths in gone if target is not None)
    for row, target, paths in gone:
        if not await _companion_walk_complete(session, agent_id, agent, row, located, report):
            continue
        if target is None and paths:
            await _mark_ambiguous(session, agent_id, row, report, paths, apply=apply)
        elif target is None:
            await _mark_missing(session, agent_id, row, report, apply=apply)
        elif claims[target.original_path] > 1:
            await _mark_ambiguous(session, agent_id, row, report, ["claimed by several gone rows"], apply=apply, target_path=target.original_path)
        else:
            await _move(session, agent_id, row, target, report, apply=apply)
    return report


async def _companion_walk_complete(
    session: AsyncSession, agent_id: str, agent: Agent, row: dict[str, Any], located: dict[str, Any], report: ReconcileReport
) -> bool:
    """Companion absence needs the complete configured-root search, not just an empty found list.

    Non-companion reconciliation retains its existing behavior. The document is the owning agent's
    locate output; a partial walk cannot certify absence or uniqueness for new companion state.
    """
    record = await session.get(FileRecord, uuid.UUID(row["id"]))
    if record is None or record.agent_id != agent_id or record.file_type not in COMPANION_FILE_TYPES:
        return True
    roots = {root.rstrip("/") for root in agent.scan_roots or []}
    walked = {root.rstrip("/") for root in located.get("walked_roots") or []}
    covered = any(row["path"] == root or row["path"].startswith(root + "/") for root in roots)
    if roots and roots == walked and covered and located.get("walk_errors") == 0:
        return True
    report.unverifiable += 1
    return False


async def _mark_ambiguous(
    session: AsyncSession,
    agent_id: str,
    row: dict[str, Any],
    report: ReconcileReport,
    details: list[str],
    *,
    apply: bool,
    target_path: str | None = None,
) -> None:
    """Retain the absent row and history; persist only companion ambiguity after guarded locate."""
    file_id = uuid.UUID(row["id"])
    records = await _locked(session, agent_id, FileRecord.id == file_id, apply=apply)
    if not records or records[0].current_path != row["path"] or records[0].sha256_hash != row["sha256"] or records[0].file_size != row["size"]:
        report.actions.append(RowAction(Verdict.CHANGED, file_id, row["path"]))
        return
    record = records[0]
    report.actions.append(RowAction(Verdict.AMBIGUOUS, file_id, row["path"], target_path, detail=details))
    if apply and record.file_type in COMPANION_FILE_TYPES:
        record.missing_at = None
        if record.companion_ambiguous_at is None:
            record.companion_ambiguous_at = datetime.now(UTC)
        await _clear_ledger(session, file_id)


async def _locked(session: AsyncSession, agent_id: str, *predicates: Any, apply: bool) -> list[FileRecord]:
    """The agent's rows matching any predicate, locked ``FOR UPDATE`` in ``original_path`` order when applying (phaze-zfxy6)."""
    # Callers select one id, or that id plus one unique (agent_id, original_path) destination.
    query = select(FileRecord).where(FileRecord.agent_id == agent_id, or_(*predicates)).order_by(FileRecord.original_path).limit(2)
    return list((await session.execute(query.with_for_update() if apply else query)).scalars().all())


async def _restore_present(
    session: AsyncSession, agent_id: str, present: dict[uuid.UUID, dict[str, Any]], report: ReconcileReport, *, apply: bool
) -> None:
    """Clear ``missing_at`` on every row the agent reports present again; count the rest as present.

    One read finds the few marked rows among the (many) present ones, in pages, so a whole agent's
    rows cost a handful of queries rather than one each.
    """
    marked: list[uuid.UUID] = []
    ids = list(present)
    for start in range(0, len(ids), _ID_PAGE):
        page = ids[start : start + _ID_PAGE]
        marked += (
            (
                await session.execute(
                    select(FileRecord.id).where(
                        FileRecord.id.in_(page), (FileRecord.missing_at.isnot(None) | FileRecord.companion_ambiguous_at.isnot(None))
                    )
                )
            )
            .scalars()
            .all()
        )
    report.present += len(present) - len(marked)
    for file_id in marked:
        path = present[file_id]["path"]
        records = await _locked(session, agent_id, FileRecord.id == file_id, apply=apply)
        if not records or (records[0].missing_at is None and records[0].companion_ambiguous_at is None) or records[0].current_path != path:
            report.actions.append(RowAction(Verdict.CHANGED, file_id, path))
            continue
        report.actions.append(RowAction(Verdict.RESTORED, file_id, path))
        if apply:
            records[0].missing_at = None
            records[0].companion_ambiguous_at = None
            logger.info("reconcile_stale_rows: file is back; clearing missing", agent_id=agent_id, file_id=str(file_id), path=path)


async def _mark_missing(session: AsyncSession, agent_id: str, row: dict[str, Any], report: ReconcileReport, *, apply: bool) -> None:
    file_id = uuid.UUID(row["id"])
    records = await _locked(session, agent_id, FileRecord.id == file_id, apply=apply)
    if not records or records[0].current_path != row["path"] or records[0].sha256_hash != row["sha256"] or records[0].file_size != row["size"]:
        report.actions.append(RowAction(Verdict.CHANGED, file_id, row["path"]))
        return
    record = records[0]
    if record.missing_at is not None:
        report.already_missing += 1
        return
    report.actions.append(RowAction(Verdict.MISSING, file_id, row["path"]))
    if apply:
        logger.warning("reconcile_stale_rows: marking missing", agent_id=agent_id, file_id=str(file_id), path=row["path"])
        record.missing_at = datetime.now(UTC)
        record.companion_ambiguous_at = None
        await _clear_ledger(session, file_id)


async def _move(session: AsyncSession, agent_id: str, row: dict[str, Any], target: FileUpsertRecord, report: ReconcileReport, *, apply: bool) -> None:
    """Re-point a gone row to the one path its bytes are at, merging with the row a scan already made there."""
    file_id = uuid.UUID(row["id"])
    wire = file_row(target, agent_id, None)
    new_path: str = wire["original_path"]
    records = await _locked(session, agent_id, FileRecord.id == file_id, FileRecord.original_path == new_path, apply=apply)
    stale = next((record for record in records if record.id == file_id), None)
    duplicate = next((record for record in records if record.id != file_id), None)
    if stale is None or stale.current_path != row["path"] or stale.sha256_hash != row["sha256"] or wire["sha256_hash"] != stale.sha256_hash:
        report.actions.append(RowAction(Verdict.CHANGED, file_id, row["path"], new_path))
        return
    if stale.file_type in COMPANION_FILE_TYPES and (stale.file_size != row["size"] or wire["file_size"] != stale.file_size):
        report.actions.append(RowAction(Verdict.CHANGED, file_id, row["path"], new_path, detail=["size changed since locate"]))
        return
    if duplicate is not None and duplicate.sha256_hash != stale.sha256_hash:
        report.actions.append(RowAction(Verdict.CHANGED, file_id, row["path"], new_path, duplicate.id, ["the row at the new path has other content"]))
        return

    blockers = await retention_blockers(session, stale, content_changed=False)
    if duplicate is not None:
        # The duplicate is deleted once its children move over: a cloud job busy on it would be orphaned.
        blockers += [f"duplicate:{reason}" for reason in await retention_blockers(session, duplicate, content_changed=True)]
    verdict = Verdict.REPOINT if duplicate is None else Verdict.MERGE
    target_id = None if duplicate is None else duplicate.id
    if blockers:
        report.actions.append(RowAction(Verdict.KEPT_REVIEWED, file_id, row["path"], new_path, target_id, blockers))
        return
    report.actions.append(RowAction(verdict, file_id, row["path"], new_path, target_id))
    if not apply:
        return

    logger.info(
        "reconcile_stale_rows: re-pointing row",
        agent_id=agent_id,
        file_id=str(file_id),
        old_path=stale.current_path,
        new_path=new_path,
        merged_file_id=str(target_id) if target_id else None,
    )
    if duplicate is not None:
        await _merge_children(session, keep=stale.id, drop=duplicate.id)
        await delete_file_cascade(session, duplicate.id)
        await session.flush()
    repoint_file(stale, {**wire, "batch_id": stale.batch_id})
    await session.flush()
    await _reset_metadata_failure(session, stale.id)


async def _reset_metadata_failure(session: AsyncSession, file_id: uuid.UUID) -> None:
    """Delete a FAILURE-only metadata row (never a successful one) and clear the file's ledger rows, so "Extract all" picks it up."""
    await session.execute(
        delete(FileMetadata).where(FileMetadata.file_id == file_id, FileMetadata.failed_at.isnot(None)).execution_options(synchronize_session=False)
    )
    await _clear_ledger(session, file_id)


async def _clear_ledger(session: AsyncSession, file_id: uuid.UUID) -> None:
    """Clear the file's scheduling-ledger rows through the guarded clear (a row whose job is still live is left to it)."""
    keys = await session.stream_scalars(
        select(SchedulingLedger.key).where(SchedulingLedger.payload["file_id"].astext == str(file_id)).execution_options(yield_per=500)
    )
    async for key in keys:
        await clear_ledger_entry(session, key)


async def _reparent(session: AsyncSession, model: Any, keep: uuid.UUID, drop: uuid.UUID) -> None:
    """Point every ``model`` row of ``drop`` at ``keep``."""
    await session.execute(update(model).where(model.file_id == drop).values(file_id=keep).execution_options(synchronize_session=False))


async def _move_one_to_one(session: AsyncSession, model: Any, keep: uuid.UUID, drop: uuid.UUID) -> None:
    """Move the duplicate's row of a 1:1 ``file_id`` table onto the kept row when the kept row has none."""
    if not await session.scalar(select(exists().where(model.file_id == keep))):
        await _reparent(session, model, keep, drop)


async def _merge_children(session: AsyncSession, *, keep: uuid.UUID, drop: uuid.UUID) -> None:
    """Move every child row of ``drop`` onto ``keep`` -- two rows for the SAME bytes becoming one.

    Many-per-file history moves over whole: proposals (with their execution rows), tag writes,
    tracklists, companion links, force-skips. For a 1:1 table the kept row's own row wins, except
    that a SUCCESSFUL metadata or COMPLETED analysis on the duplicate replaces a failed/partial one
    on the kept row (analysis moves with its windows, set profile, cloud job and cloud budget, which
    describe that analysis run). What is left on ``drop`` afterwards is derived state the kept row
    already holds for identical content, and ``delete_file_cascade`` removes it with the row.
    ``dedup_resolution`` never reaches here: either side carrying one is a retention blocker.
    """
    keep_meta = await session.scalar(select(FileMetadata.failed_at.is_(None)).where(FileMetadata.file_id == keep))
    drop_meta_ok = await session.scalar(select(exists().where(FileMetadata.file_id == drop, FileMetadata.failed_at.is_(None))))
    if drop_meta_ok and not keep_meta:
        await session.execute(delete(FileMetadata).where(FileMetadata.file_id == keep).execution_options(synchronize_session=False))
        await session.execute(
            update(FileMetadata).where(FileMetadata.file_id == drop).values(file_id=keep).execution_options(synchronize_session=False)
        )

    completed = AnalysisResult.analysis_completed_at.isnot(None)
    keep_done = await session.scalar(select(exists().where(AnalysisResult.file_id == keep, completed)))
    drop_done = await session.scalar(select(exists().where(AnalysisResult.file_id == drop, completed)))
    if drop_done and not keep_done:
        for analysis_model in (AnalysisWindow, SetProfile, AnalysisResult, CloudJob, CloudBudget):
            await session.execute(delete(analysis_model).where(analysis_model.file_id == keep).execution_options(synchronize_session=False))
            await _reparent(session, analysis_model, keep, drop)

    # uq_proposals_file_id_pending: at most one pending proposal per file; the kept row's wins.
    keep_pending = await session.scalar(select(exists().where(RenameProposal.file_id == keep, RenameProposal.status == ProposalStatus.PENDING.value)))
    moved_proposals = update(RenameProposal).where(RenameProposal.file_id == drop)
    if keep_pending:
        moved_proposals = moved_proposals.where(RenameProposal.status != ProposalStatus.PENDING.value)
    await session.execute(moved_proposals.values(file_id=keep).execution_options(synchronize_session=False))
    for history_model in (TagWriteLog, Tracklist):
        await _reparent(session, history_model, keep, drop)
    await _move_one_to_one(session, CompanionContentFeatures, keep, drop)
    kept_stages = select(StageSkip.stage).where(StageSkip.file_id == keep)
    await session.execute(
        update(StageSkip)
        .where(StageSkip.file_id == drop, StageSkip.stage.not_in(kept_stages))
        .values(file_id=keep)
        .execution_options(synchronize_session=False)
    )
    await _merge_companion_links(session, keep=keep, drop=drop)
    # FK-free traceability column (models/companion_junk_review.py): follow the surviving row.
    await session.execute(
        update(CompanionJunkReview).where(CompanionJunkReview.file_id == drop).values(file_id=keep).execution_options(synchronize_session=False)
    )


async def _merge_companion_links(session: AsyncSession, *, keep: uuid.UUID, drop: uuid.UUID) -> None:
    """Re-target ``drop``'s companion links to ``keep``, skipping a pair ``keep`` already has (uq_file_companions_pair) or one onto itself."""
    links = await session.stream_scalars(
        select(FileCompanion).where((FileCompanion.media_id == drop) | (FileCompanion.companion_id == drop)).execution_options(yield_per=500)
    )
    kept = await session.stream_scalars(
        select(FileCompanion).where((FileCompanion.media_id == keep) | (FileCompanion.companion_id == keep)).execution_options(yield_per=500)
    )
    existing = {(link.companion_id, link.media_id) async for link in kept}
    async for link in links:
        pair = (keep if link.companion_id == drop else link.companion_id, keep if link.media_id == drop else link.media_id)
        if pair[0] == pair[1] or pair in existing:
            continue
        link.companion_id, link.media_id = pair
        existing.add(pair)
    await session.flush()
