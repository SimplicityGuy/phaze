"""SAQ orchestration and compatibility facade for approved file execution.

The local filesystem capability lives in :mod:`phaze.tasks.execution_filesystem`.
This adapter deliberately retains the established import and patch surface while
owning SAQ job metadata, API audit/report ordering, per-proposal isolation, and
the terminal progress token whose loss must make SAQ replay the job.

The task reads paths from its payload and never imports application ORM models,
the database layer, or SQLAlchemy (D-23 and the agent import-boundary guard).
Filesystem failures remain isolated per proposal.  Reporting failures after a
committed move remain best-effort except for a lost ``sub_batch_terminal`` event,
which is the sole completion token and therefore raises for replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
import uuid

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.enums.execution import ExecutionStatus
from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload
from phaze.schemas.agent_execution import ExecutionLogCreate, ExecutionLogPatch
from phaze.schemas.agent_proposals import ProposalStatePatch
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.containment import resolve_and_check_containment as _resolve_and_check_containment
from phaze.tasks import execution_filesystem as _filesystem


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)

# Historical tests patch these module objects directly.  Both aliases reference
# the same stdlib module singletons used by the concrete filesystem adapter.
asyncio = _filesystem.asyncio
os = _filesystem.os

FailedAtStep = _filesystem.FailedAtStep
_MoveStep = _filesystem.MoveStep
_COPY_CHUNK_BYTES = _filesystem.COPY_CHUNK_BYTES
_COPY_TMP_SUFFIX = _filesystem.COPY_TMP_SUFFIX
_COMMIT_MARKER_SUFFIX = _filesystem.COMMIT_MARKER_SUFFIX

_MOVED_FLAG_PREFIX = "moved:"


class ExecBatchTerminalReportError(RuntimeError):
    """The sub-batch completion event could not be delivered, so SAQ must replay."""


class _FacadeFilesystemPrimitives(_filesystem.LocalFilesystemPrimitives):
    """Resolve historical facade patch points at call time.

    Tests and downstream diagnostics have long patched private names on
    ``phaze.tasks.execution``.  The engine receives this adapter instead of
    capturing implementation functions at import time, so those supported patch
    points continue to intercept the exact operation they intercepted before the
    extraction.
    """

    def resolve_containment(self, candidate: str, roots: list[str]) -> tuple[Path, Path]:
        return _resolve_and_check_containment(candidate, roots)

    def resolve_destination(
        self,
        item: ExecuteBatchProposalItem,
        original: Path,
        owning_root: Path,
        scan_roots: list[str],
    ) -> Path:
        return _resolve_destination(item, original, owning_root, scan_roots)

    def sha256_of_file(self, path: Path) -> str:
        return _sha256_of_file(path)

    def committed_copy_marker_path(self, proposed: Path, proposal_id: uuid.UUID) -> Path:
        return _committed_copy_marker_path(proposed, proposal_id)

    def unique_tmp_path(self, dst: Path) -> Path:
        return _unique_tmp_path(dst)

    def same_filesystem(self, src: Path, dst_dir: Path) -> bool:
        return _same_filesystem(src, dst_dir)

    def is_same_file(self, a: Path, b: Path) -> bool:
        return _is_same_file(a, b)

    def is_case_only_same_entry(self, original: Path, proposed: Path) -> bool:
        return _is_case_only_same_entry(original, proposed)

    def destination_is_committed_copy(self, original: Path, proposed: Path, expected_hash: str | None) -> bool:
        return _destination_is_committed_copy(original, proposed, expected_hash)

    def streamed_copy(self, src: Path, dst: Path) -> None:
        _streamed_copy(src, dst)

    def claim_destination_by_link(self, src: Path, dst: Path) -> bool:
        return _claim_destination_by_link(src, dst)

    def atomic_cross_fs_copy(self, src: Path, dst: Path) -> None:
        _atomic_cross_fs_copy(src, dst)

    def atomic_same_fs_move(self, src: Path, dst: Path) -> None:
        _atomic_same_fs_move(src, dst)

    async def verify_hash_or_raise(self, path: Path, expected_hash: str, *, label: str, suffix: str = "") -> None:
        await _verify_hash_or_raise(path, expected_hash, label=label, suffix=suffix)

    @property
    def hash_file_for_offload(self) -> Callable[[Path], str]:
        return _sha256_of_file

    @property
    def cross_fs_copy_for_offload(self) -> Callable[[Path, Path], None]:
        return _atomic_cross_fs_copy


_FACADE_FILESYSTEM_PRIMITIVES = _FacadeFilesystemPrimitives()


def _filesystem_engine() -> _filesystem.ExecutionFilesystemEngine:
    return _filesystem.LocalExecutionFilesystemEngine(_FACADE_FILESYSTEM_PRIMITIVES)


# Compatibility facade.  Each wrapper delegates to the concrete engine adapter;
# the unbound calls deliberately preserve late-bound sibling patch points.
def _resolve_destination(
    item: ExecuteBatchProposalItem,
    original: Path,
    owning_root: Path,
    scan_roots: list[str],
) -> Path:
    return _filesystem.LocalFilesystemPrimitives.resolve_destination(
        _FACADE_FILESYSTEM_PRIMITIVES,
        item,
        original,
        owning_root,
        scan_roots,
    )


def _sha256_of_file(path: Path) -> str:
    return _filesystem.LocalFilesystemPrimitives.sha256_of_file(_FACADE_FILESYSTEM_PRIMITIVES, path)


def _committed_copy_marker_path(proposed: Path, proposal_id: uuid.UUID) -> Path:
    return _filesystem.LocalFilesystemPrimitives.committed_copy_marker_path(_FACADE_FILESYSTEM_PRIMITIVES, proposed, proposal_id)


def _unique_tmp_path(dst: Path) -> Path:
    return _filesystem.LocalFilesystemPrimitives.unique_tmp_path(_FACADE_FILESYSTEM_PRIMITIVES, dst)


def _moved_flag_key(proposal_id: uuid.UUID) -> str:
    return f"{_MOVED_FLAG_PREFIX}{proposal_id}"


def _same_filesystem(src: Path, dst_dir: Path) -> bool:
    return _filesystem.LocalFilesystemPrimitives.same_filesystem(_FACADE_FILESYSTEM_PRIMITIVES, src, dst_dir)


def _is_same_file(a: Path, b: Path) -> bool:
    return _filesystem.LocalFilesystemPrimitives.is_same_file(_FACADE_FILESYSTEM_PRIMITIVES, a, b)


def _is_case_only_same_entry(original: Path, proposed: Path) -> bool:
    return _filesystem.LocalFilesystemPrimitives.is_case_only_same_entry(_FACADE_FILESYSTEM_PRIMITIVES, original, proposed)


def _destination_is_committed_copy(original: Path, proposed: Path, expected_hash: str | None) -> bool:
    return _filesystem.LocalFilesystemPrimitives.destination_is_committed_copy(
        _FACADE_FILESYSTEM_PRIMITIVES,
        original,
        proposed,
        expected_hash,
    )


def _streamed_copy(src: Path, dst: Path) -> None:
    _filesystem.LocalFilesystemPrimitives.streamed_copy(_FACADE_FILESYSTEM_PRIMITIVES, src, dst)


def _claim_destination_by_link(src: Path, dst: Path) -> bool:
    return _filesystem.LocalFilesystemPrimitives.claim_destination_by_link(_FACADE_FILESYSTEM_PRIMITIVES, src, dst)


def _atomic_cross_fs_copy(src: Path, dst: Path) -> None:
    _filesystem.LocalFilesystemPrimitives.atomic_cross_fs_copy(_FACADE_FILESYSTEM_PRIMITIVES, src, dst)


def _atomic_same_fs_move(src: Path, dst: Path) -> None:
    _filesystem.LocalFilesystemPrimitives.atomic_same_fs_move(_FACADE_FILESYSTEM_PRIMITIVES, src, dst)


async def _verify_hash_or_raise(path: Path, expected_hash: str, *, label: str, suffix: str = "") -> None:
    await _filesystem.LocalFilesystemPrimitives.verify_hash_or_raise(
        _FACADE_FILESYSTEM_PRIMITIVES,
        path,
        expected_hash,
        label=label,
        suffix=suffix,
    )


def _check_replay_corroborated(
    original: Path,
    proposed: Path,
    item: ExecuteBatchProposalItem,
    job: Any | None,
) -> bool:
    job_meta = dict(getattr(job, "meta", None) or {}) if job is not None else {}
    if original.exists() or not proposed.exists():
        return False
    return job_meta.get(_moved_flag_key(item.proposal_id)) is not None or _committed_copy_marker_path(proposed, item.proposal_id).exists()


async def _reclaim_or_refuse_existing_destination(
    original: Path,
    proposed: Path,
    item: ExecuteBatchProposalItem,
    step: _MoveStep,
    *,
    same_fs: bool,
) -> None:
    engine = _filesystem.LocalExecutionFilesystemEngine(_FACADE_FILESYSTEM_PRIMITIVES)
    await engine._reclaim_or_refuse_existing_destination(original, proposed, item, step, same_fs=same_fs)


def _move_same_fs_entry(original: Path, proposed: Path, step: _MoveStep) -> None:
    engine = _filesystem.LocalExecutionFilesystemEngine(_FACADE_FILESYSTEM_PRIMITIVES)
    engine._move_same_fs_entry(original, proposed, step)


async def _move_across_filesystem(
    original: Path,
    proposed: Path,
    item: ExecuteBatchProposalItem,
    step: _MoveStep,
) -> None:
    engine = _filesystem.LocalExecutionFilesystemEngine(_FACADE_FILESYSTEM_PRIMITIVES)
    await engine._move_across_filesystem(original, proposed, item, step)


async def _apply_file_move(
    original: Path,
    proposed: Path,
    item: ExecuteBatchProposalItem,
    step: _MoveStep,
) -> None:
    engine = _filesystem.LocalExecutionFilesystemEngine(_FACADE_FILESYSTEM_PRIMITIVES)
    await engine._apply_file_move(original, proposed, item, step)


def _classify_failure_step(current_step: FailedAtStep, exc: BaseException) -> FailedAtStep:
    if "sha256 mismatch" in str(exc):
        return "verify"
    return current_step


async def _ensure_start_log(api: PhazeAgentClient, start_log: ExecutionLogCreate, *, start_logged: bool) -> bool:
    """Re-create a missing write-ahead audit row before the terminal patch."""
    if start_logged:
        return True
    try:
        await api.post_execution_log(start_log)
    except Exception as exc:
        logger.error(
            "execute_approved_batch: audit row for %s could not be created -- this committed move will have NO audit trail: %s",
            start_log.proposal_id,
            exc,
        )
        return False
    logger.info(
        "execute_approved_batch: recovered the missing audit row for %s before its terminal report",
        start_log.proposal_id,
    )
    return True


def _report_progress_failure(item: ExecuteBatchProposalItem, is_last: bool, exc: BaseException) -> None:
    """Swallow telemetry loss but raise when the sub-batch completion event is lost."""
    if not is_last:
        logger.warning("execute_approved_batch: progress POST failed for %s: %s", item.proposal_id, exc)
        return
    logger.error(
        "execute_approved_batch: sub-batch terminal completion event lost for %s: %s -- failing the job so SAQ replays it",
        item.proposal_id,
        exc,
    )
    msg = f"sub_batch_terminal progress POST failed for proposal {item.proposal_id}: {exc}"
    raise ExecBatchTerminalReportError(msg) from exc


@dataclass
class _ReportContext:
    """Per-proposal API/reporting values shared by both terminal paths."""

    api: PhazeAgentClient
    item: ExecuteBatchProposalItem
    execution_log_id: uuid.UUID
    progress_request_id: uuid.UUID
    payload: ExecuteApprovedBatchPayload
    is_last: bool
    start_log: ExecutionLogCreate


async def _finalize_execution_log(
    ctx: _ReportContext,
    patch: ExecutionLogPatch,
    *,
    start_logged: bool,
    terminal_label: str,
) -> bool:
    start_logged = await _ensure_start_log(ctx.api, ctx.start_log, start_logged=start_logged)
    try:
        await ctx.api.patch_execution_log(ctx.execution_log_id, patch)
    except Exception as patch_exc:
        logger.warning(
            "execute_approved_batch: could not patch %s log for %s: %s",
            terminal_label,
            ctx.item.proposal_id,
            patch_exc,
        )
    return start_logged


async def _post_exec_batch_progress_terminal(
    ctx: _ReportContext,
    *,
    terminal_step: Literal["deleted", "failed"],
    failed_at_step: FailedAtStep | None = None,
) -> None:
    try:
        await ctx.api.post_exec_batch_progress(
            ctx.payload.batch_id,
            ExecBatchProgressPayload(
                request_id=ctx.progress_request_id,
                batch_id=ctx.payload.batch_id,
                agent_id=ctx.payload.agent_id,
                sub_batch_index=ctx.payload.sub_batch_index,
                proposal_id=ctx.item.proposal_id,
                terminal_step=terminal_step,
                failed_at_step=failed_at_step,
                sub_batch_terminal=ctx.is_last,
            ),
        )
    except Exception as progress_exc:
        _report_progress_failure(ctx.item, ctx.is_last, progress_exc)


async def _report_success(
    ctx: _ReportContext,
    proposed: Path,
    *,
    start_logged: bool,
    sha_verified: bool,
) -> None:
    await _finalize_execution_log(
        ctx,
        ExecutionLogPatch(status=ExecutionStatus.COMPLETED, sha256_verified=sha_verified),
        start_logged=start_logged,
        terminal_label="completed",
    )
    try:
        await ctx.api.patch_proposal_state(
            ctx.item.proposal_id,
            ProposalStatePatch(proposal_state="executed", file_state="moved", current_path=str(proposed)),
        )
    except Exception as report_exc:
        logger.error(
            "execute_approved_batch: move committed but reporting executed state failed for %s: %s",
            ctx.item.proposal_id,
            report_exc,
        )
    await _post_exec_batch_progress_terminal(ctx, terminal_step="deleted")


async def _report_failure(
    ctx: _ReportContext,
    failed_step: FailedAtStep,
    formatted_error: str,
    *,
    start_logged: bool,
) -> None:
    await _finalize_execution_log(
        ctx,
        ExecutionLogPatch(status=ExecutionStatus.FAILED, error_message=formatted_error),
        start_logged=start_logged,
        terminal_label="failed",
    )
    try:
        await ctx.api.patch_proposal_state(
            ctx.item.proposal_id,
            ProposalStatePatch(proposal_state="failed", file_state=None, error_message=formatted_error),
        )
    except Exception as report_exc:
        logger.error("execute_approved_batch: failed to report failure for %s: %s", ctx.item.proposal_id, report_exc)
    await _post_exec_batch_progress_terminal(ctx, terminal_step="failed", failed_at_step=failed_step)


async def _compute_proposed(
    item: ExecuteBatchProposalItem,
    scan_roots: list[str],
    job: Any | None,
    step: _MoveStep,
) -> Path:
    """Delegate the destructive operation to the filesystem port, then persist SAQ corroboration."""
    job_meta = dict(getattr(job, "meta", None) or {}) if job is not None else {}
    request = _filesystem.FilesystemMoveRequest(
        item=item,
        scan_roots=scan_roots,
        replay_corroborated=job_meta.get(_moved_flag_key(item.proposal_id)) is not None,
    )
    result = await _filesystem_engine().move(request, step)
    if result.committed_now and job is not None:
        updated_job_meta = dict(getattr(job, "meta", None) or {})
        updated_job_meta[_moved_flag_key(item.proposal_id)] = "1"
        await job.update(meta=updated_job_meta)
    return result.proposed


async def _execute_one(
    api: PhazeAgentClient,
    item: ExecuteBatchProposalItem,
    scan_roots: list[str],
    payload: ExecuteApprovedBatchPayload,
    is_last: bool,
    execution_log_id: uuid.UUID,
    progress_request_id: uuid.UUID,
    job: Any | None,
) -> bool:
    """Execute and report one proposal, isolating ordinary failures to that proposal."""
    sha_verified = item.sha256_hash is not None
    dest_display = f"{item.proposed_path.rstrip('/')}/{item.proposed_filename}" if item.proposed_path else item.proposed_filename
    start_log = ExecutionLogCreate(
        id=execution_log_id,
        proposal_id=item.proposal_id,
        operation="move",
        source_path=item.source_path,
        destination_path=dest_display,
        sha256_verified=False,
        status=ExecutionStatus.IN_PROGRESS,
    )
    report_ctx = _ReportContext(api, item, execution_log_id, progress_request_id, payload, is_last, start_log)
    start_logged = True
    try:
        await api.post_execution_log(start_log)
    except Exception as exc:
        start_logged = False
        logger.warning("execute_approved_batch: could not record start log for %s: %s", item.proposal_id, exc)

    step = _MoveStep()
    try:
        proposed = await _compute_proposed(item, scan_roots, job, step)
        await _report_success(report_ctx, proposed, start_logged=start_logged, sha_verified=sha_verified)
        return True
    except ExecBatchTerminalReportError:
        raise
    except Exception as exc:
        failed_step: FailedAtStep = _classify_failure_step(step.current, exc)
        formatted_error = f"{failed_step}: {exc!s}"[:500]
        logger.warning(
            "execute_approved_batch: proposal %s failed at step=%s: %s",
            item.proposal_id,
            failed_step,
            exc,
            exc_info=True,
        )
        await _report_failure(report_ctx, failed_step, formatted_error, start_logged=start_logged)
        return False


def _load_or_seed_uuids(
    job: Any,
    proposals: list[ExecuteBatchProposalItem],
) -> tuple[dict[uuid.UUID, uuid.UUID], dict[uuid.UUID, uuid.UUID], dict[str, str], bool]:
    """Load or seed retry-stable audit and progress identifiers in SAQ metadata."""
    existing_meta: dict[str, str] = dict(getattr(job, "meta", None) or {})
    log_ids: dict[uuid.UUID, uuid.UUID] = {}
    request_ids: dict[uuid.UUID, uuid.UUID] = {}
    changed = False
    for item in proposals:
        log_key = f"log_id:{item.proposal_id}"
        request_key = f"req_id:{item.proposal_id}"
        if log_key in existing_meta:
            log_ids[item.proposal_id] = uuid.UUID(existing_meta[log_key])
        else:
            log_id = uuid.uuid4()
            existing_meta[log_key] = str(log_id)
            log_ids[item.proposal_id] = log_id
            changed = True
        if request_key in existing_meta:
            request_ids[item.proposal_id] = uuid.UUID(existing_meta[request_key])
        else:
            request_id = uuid.uuid4()
            existing_meta[request_key] = str(request_id)
            request_ids[item.proposal_id] = request_id
            changed = True
    return log_ids, request_ids, existing_meta, changed


async def execute_approved_batch(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Execute one agent sub-batch while preserving retry and failure isolation."""
    payload = ExecuteApprovedBatchPayload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]
    logger.info(
        "execute batch started",
        batch_id=str(payload.batch_id),
        agent=payload.agent_id,
        proposals=len(payload.proposals),
    )

    cfg = get_settings()
    scan_roots: list[str] = list(cfg.scan_roots) if isinstance(cfg, AgentSettings) else []
    if not scan_roots:
        msg = "agent has no scan_roots configured; cannot execute batch"
        raise RuntimeError(msg)

    job = ctx.get("job")
    if job is not None:
        log_ids, request_ids, updated_meta, changed = _load_or_seed_uuids(job, list(payload.proposals))
        if changed:
            await job.update(meta=updated_meta)
    else:
        logger.debug("execute_approved_batch: ctx has no 'job' key -- using fresh UUIDs (legacy ctx).")
        log_ids = {item.proposal_id: uuid.uuid4() for item in payload.proposals}
        request_ids = {item.proposal_id: uuid.uuid4() for item in payload.proposals}

    processed = 0
    errors = 0
    total = len(payload.proposals)
    for index, item in enumerate(payload.proposals):
        ok = await _execute_one(
            api,
            item,
            scan_roots,
            payload,
            index == total - 1,
            log_ids[item.proposal_id],
            request_ids[item.proposal_id],
            job,
        )
        processed += 1
        if not ok:
            errors += 1

    final_status = "completed" if errors == 0 else "completed_with_errors"
    logger.info(
        "execute batch completed",
        batch_id=str(payload.batch_id),
        status=final_status,
        processed_count=processed,
        error_count=errors,
    )
    return {
        "batch_id": str(payload.batch_id),
        "status": final_status,
        "processed_count": processed,
        "error_count": errors,
    }
