"""SAQ task: execute_approved_batch -- per-proposal local file ops + HTTP state reporting (Phase 26 B2 Option A + Phase 28 D-03/D-15).

Reads file paths from payload (no DB lookup -- D-23 invariant). For each proposal:
1. Resolve `original_path` under its owning scan_root, then build the destination as
   ``owning_root/proposed_path/proposed_filename`` (``proposed_path`` is a RELATIVE dir;
   empty == rename in place) and containment-check it (T-26-11-S1 path-traversal guard).
2. POST /execution-log with status='in_progress' (per-proposal audit row).
3. Optionally verify sha256 of `original_path` against `payload.sha256_hash`.
4. Move `original_path` -> destination (os.replace when same-fs, else a bounded
   streamed copy -- never load the whole file into RAM; concert videos are multi-GB).
5. Delete the original (only needed on the cross-filesystem copy path).
6. PATCH /execution-log/{id} with status='completed' (or 'failed').
7. PATCH /proposals/{id}/state with proposal_state=executed, file_state=moved, current_path=proposed_path.
8. POST /exec-batches/{batch_id}/progress with terminal_step + failed_at_step (Phase 28 D-03).

On any per-proposal IO error: PATCH execution-log status='failed' + PATCH proposal_state='failed' + POST
exec-batch progress with terminal_step="failed" + error_message + continue with the rest.
The batch returns aggregate processed/error counts; cross-proposal failures are isolated.

Phase 28 changes (Plan 28-05):
- BOTH ``execution_log_id`` AND ``progress_request_id`` are persisted in ``ctx['job'].meta`` so
  SAQ retries reuse the same UUIDs per proposal (closes L6/L22; delivers D-15). The meta-key
  convention is ``log_id:{proposal_id}`` / ``req_id:{proposal_id}``. UUIDs are written as
  strings (SAQ serializes ``meta`` via JSON-compatible types).
- ``_execute_one`` tracks a local ``current_step`` variable through the copy/verify/delete
  transitions; the except clause uses ``_classify_failure_step`` to map exc + current_step to
  the literal ``failed_at_step`` posted to the new progress endpoint.
- ``error_message`` on failed ExecutionLog PATCHes adopts the ``"<step>: <reason>"`` prefix
  convention (D-01 contract).
- Each terminal proposal POSTs to ``/api/internal/agent/exec-batches/{batch_id}/progress``;
  the LAST item of a sub-batch sets ``sub_batch_terminal=True`` so the controller can detect
  ``subjobs_completed == subjobs_expected`` and promote the batch status.
- Progress POST failures (after the agent_client's tenacity retries) log WARNING and do NOT
  raise -- file ops are already committed via ``patch_proposal_state`` (D-16).
- The success-path ``patch_proposal_state`` (the 'report' step) is likewise guarded: the move
  is committed on disk before it runs, so a 5xx there is swallowed + logged and the proposal
  still counts as executed. Letting it raise would misattribute the failure to
  ``failed_at_step='delete'`` and mark an already-moved file's proposal FAILED.

phaze-ebpt fix (already-moved replay detection): the guard above closes the ``patch_proposal_state``
5xx window, but a WORKER CRASH between the committed move (``original.replace(proposed)`` / the
cross-filesystem copy+unlink) and those same completed/executed/progress PATCHes left a second,
unguarded divergence -- a SAQ retry of the crashed job re-enters ``_execute_one`` from scratch,
``_resolve_and_check_containment``'s non-strict resolve lets the now-missing ``original`` resolve
without error, and the verify/copy code then raises FileNotFoundError, which the generic failure
handler mistook for a fresh failure: it flipped the already-executed proposal APPROVED -> FAILED
and left ``FileRecord.current_path`` pointing at the deleted ``original``. ``_execute_one`` now
detects this explicitly right after resolving ``original``/``proposed``: ``not original.exists()
and proposed.exists()`` is conclusive replay evidence (only this function ever creates
``proposed``), distinct from both a live in-flight move and a genuinely missing/never-started
file. When detected (and, if a hash was supplied, confirmed by hashing ``proposed`` instead of the
gone ``original``) the file op is skipped entirely and the replay falls through to the SAME
success-reporting path used by a first-time success -- already idempotent via the retry-stable
``execution_log_id``/``progress_request_id`` -- which also self-heals ``current_path`` (the
success PATCH sets it from ``str(proposed)``).

NOTE on schema mapping: Phase 25's ExecutionLog schema is per-proposal (one row per file op),
not per-batch. Plan 11 invariants (one POST at start, per-proposal state PATCH, one PATCH at
end) are adapted to the existing schema as: one POST+PATCH per proposal (matching the
ExecutionLog table's natural key `proposal_id`). The "completed_with_errors" plan label
becomes "completed_with_errors" in the returned batch dict (no schema field for it).

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Any, Literal
import uuid

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.enums.execution import ExecutionStatus
from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload
from phaze.schemas.agent_execution import ExecutionLogCreate, ExecutionLogPatch
from phaze.schemas.agent_proposals import ProposalStatePatch
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)


# Literal type alias for the three terminal sub-steps tracked by _execute_one.
# Matches ExecBatchProgressPayload.failed_at_step (Phase 28 D-06).
FailedAtStep = Literal["copy", "verify", "delete"]


def _resolve_and_check_containment(candidate: str, scan_roots: list[str]) -> tuple[Path, Path]:
    """Resolve `candidate` and assert it lives under at least one of `scan_roots`.

    Returns ``(resolved, owning_root)`` -- the resolved candidate path and the
    resolved scan_root it lives under. Callers resolve a proposed RELATIVE
    destination directory against this same ``owning_root`` (mirroring
    ``services.collision`` ``concat(proposed_path, '/', proposed_filename)``)
    so the destination lands under the file's own scan_root.

    Raises ValueError on path traversal (T-26-11-S1). The resolved path is what
    we use for the actual file op so symlinks-out are also caught.
    """
    resolved = Path(candidate).resolve()
    for root in scan_roots:
        root_resolved = Path(root).resolve()
        try:
            resolved.relative_to(root_resolved)
            return resolved, root_resolved
        except ValueError:
            continue
    msg = f"path {candidate!r} (resolved to {resolved}) escapes all scan_roots {scan_roots}"
    raise ValueError(msg)


def _resolve_destination(
    item: ExecuteBatchProposalItem,
    original: Path,
    owning_root: Path,
    scan_roots: list[str],
) -> Path:
    """Build the absolute destination path for `item` and containment-check it.

    ``proposed_path`` is a RELATIVE destination directory under the file's own
    scan_root; the destination is ``owning_root / proposed_path /
    proposed_filename`` (mirrors ``services.collision`` joining semantics). An
    empty/null ``proposed_path`` means "rename in place" -- keep the original's
    directory and apply the new filename. The constructed absolute path is
    re-run through :func:`_resolve_and_check_containment` so a ``../`` embedded
    in ``proposed_path`` cannot escape the scan_roots (T-26-11-S1).
    """
    dest_dir = (owning_root / item.proposed_path) if item.proposed_path else original.parent
    resolved, _ = _resolve_and_check_containment(str(dest_dir / item.proposed_filename), scan_roots)
    return resolved


def _sha256_of_file(path: Path) -> str:
    """Streaming sha256 (avoid loading large files into memory)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Chunk size for the cross-filesystem streamed copy. Bounds peak memory
# regardless of file size: the core use case is multi-GB concert videos, and
# execute_approved_batch runs on the 'meta' lane (concurrency 2, no memory pin),
# so a whole-file read would MemoryError -> proposal failed, or the OOM-killer
# SIGKILLs the worker (uncatchable) and the batch crash-loops on SAQ replay.
_COPY_CHUNK_BYTES = 16 * 1024 * 1024


def _same_filesystem(src: Path, dst_dir: Path) -> bool:
    """True when `src` and `dst_dir` live on the same filesystem (matching st_dev).

    os.replace is atomic + O(1) only within one filesystem; across a mount
    boundary it raises ``OSError(EXDEV)``. We pick the branch up front from the
    device ids instead of catching EXDEV, so the fallback path is deterministic
    and testable. ``dst_dir`` (not the not-yet-existent destination file) is what
    we stat -- the caller has already created it via ``mkdir(parents=True)``.
    """
    return src.stat().st_dev == dst_dir.stat().st_dev


def _is_same_file(a: Path, b: Path) -> bool:
    """True when `a` and `b` refer to the same on-disk file (same device + inode).

    Guards the no-op / case-only rename: a move whose destination resolves to the
    very file being moved must NOT be treated as a clobber. ``os.path.samestat``
    compares ``st_dev``/``st_ino`` so hard links and case-insensitive filesystems
    are handled correctly. Any stat error (races, permission) is treated as
    "not the same file" so the caller falls through to the exists guard.
    """
    try:
        return os.path.samestat(a.stat(), b.stat())
    except OSError:
        return False


def _streamed_copy(src: Path, dst: Path) -> None:
    """Copy `src` -> `dst` in bounded chunks, flushing + fsyncing before return.

    Uses ``shutil.copyfileobj`` with an explicit chunk length so peak memory
    stays bounded no matter how large the file is (never ``read_bytes()`` the
    whole file). ``copystat`` preserves mtime to match the atomic-rename branch
    (which keeps it for free). The fsync durably lands the bytes on disk before
    the caller unlinks the original, so a crash between copy and unlink cannot
    lose data.
    """
    with src.open("rb") as fsrc, dst.open("wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, length=_COPY_CHUNK_BYTES)
        fdst.flush()
        os.fsync(fdst.fileno())
    shutil.copystat(src, dst)


def _classify_failure_step(current_step: FailedAtStep, exc: BaseException) -> FailedAtStep:
    """Map (current_step, exc) -> the ``failed_at_step`` literal for the progress POST.

    Phase 28 RESEARCH L9 + PATTERNS L594: most failures map directly to
    ``current_step`` (set by the body as it progresses through copy -> verify ->
    delete). The one nuance is the sha256-mismatch ValueError raised by the
    verify branch: even though the agent enters that branch with
    ``current_step="verify"`` already, we encode the rule explicitly so a
    refactor that re-orders the body cannot regress the contract.
    """
    text = str(exc)
    if "sha256 mismatch" in text:
        return "verify"
    return current_step


async def _execute_one(
    api: PhazeAgentClient,
    item: ExecuteBatchProposalItem,
    scan_roots: list[str],
    payload: ExecuteApprovedBatchPayload,
    is_last: bool,
    execution_log_id: uuid.UUID,
    progress_request_id: uuid.UUID,
) -> bool:
    """Execute one proposal. Returns True on success, False on any failure.

    Per-proposal lifecycle:
    1. POST execution-log (status=in_progress) -- one row per file op.
    2. Path-traversal guard for original_path and proposed_path.
    3. Optional sha256 verify.
    4. Copy + delete.
    5. PATCH execution-log (status=completed | failed).
    6. PATCH proposal-state (executed | failed).
    7. POST exec-batch progress (terminal_step=deleted | failed) -- Phase 28 D-03.

    The two UUID arguments (``execution_log_id`` and ``progress_request_id``)
    come from ``execute_approved_batch`` which loaded them from ``ctx['job'].meta``
    so SAQ retries reuse the same per-proposal values (closes L6/L22; delivers D-15).
    """
    sha_verified = item.sha256_hash is not None
    # Relative destination for the audit trail (source_path is absolute, but the
    # true absolute destination is only known after resolving the owning
    # scan_root inside the guarded block below). proposed_filename is always
    # present, so this is never empty (satisfies ExecutionLogCreate min_length=1).
    dest_display = f"{item.proposed_path.rstrip('/')}/{item.proposed_filename}" if item.proposed_path else item.proposed_filename
    # Always POST the in-progress audit row first -- this is the durable trail
    # that survives a crash mid-copy.
    try:
        await api.post_execution_log(
            ExecutionLogCreate(
                id=execution_log_id,
                proposal_id=item.proposal_id,
                operation="move",
                source_path=item.original_path,
                destination_path=dest_display,
                sha256_verified=False,  # not yet verified at this point
                status=ExecutionStatus.IN_PROGRESS,
            ),
        )
    except Exception as exc:
        # If the audit log POST itself fails (network blip), still attempt the
        # file op so we don't leave the user with stalled state. Best-effort.
        logger.warning("execute_approved_batch: could not record start log for %s: %s", item.proposal_id, exc)

    # Phase 28: track which sub-step is currently executing so the failure
    # handler can map exception -> failed_at_step without inspecting types.
    current_step: FailedAtStep = "copy"
    try:
        # 2. Path-traversal guard for original_path + construct/guard the
        # destination. current_step="copy" covers path-resolve (a failure here
        # means "the copy couldn't begin" -- matches operator intuition).
        # proposed_path is a RELATIVE dir under the owning scan_root; the
        # destination is owning_root/proposed_path/proposed_filename (empty
        # proposed_path == in-place rename).
        original, owning_root = _resolve_and_check_containment(item.original_path, scan_roots)
        proposed = _resolve_destination(item, original, owning_root, scan_roots)

        # phaze-ebpt: already-moved replay detection. `_resolve_and_check_containment`
        # uses a non-strict `Path.resolve()`, so a SAQ retry that resumes after a prior
        # attempt already committed `original.replace(proposed)` (or the cross-filesystem
        # copy+unlink) -- but crashed before the completed/executed/progress PATCHes
        # below -- resolves `original` cleanly even though it no longer exists. Without
        # this check that retry falls straight into `_sha256_of_file(original)` or
        # `_same_filesystem`'s `original.stat()` and raises FileNotFoundError, which the
        # generic failure handler misreports as a fresh failure: it flips the
        # already-executed proposal APPROVED -> FAILED and leaves
        # FileRecord.current_path pointing at the now-deleted `original` (the stale
        # current_path this bead closes). Distinguish that terminal "already moved"
        # state from a genuinely missing/never-started file explicitly here, rather
        # than letting the move/verify code below discover it implicitly via an
        # exception: `original` gone + `proposed` present is conclusive replay
        # evidence (only this function ever creates `proposed`), so skip the file op
        # entirely and fall through to the success-reporting path, which is already
        # idempotent (retry-stable execution_log_id/progress_request_id) and also
        # self-heals current_path (the success PATCH sets it from `str(proposed)`).
        already_moved = not original.exists() and proposed.exists()
        if already_moved and item.sha256_hash is not None:
            # Confirm `proposed` is actually the expected file before trusting the
            # replay -- a hash mismatch means this isn't the already-moved file (e.g.
            # an unrelated file landed at the destination), so treat it as a genuine
            # verify failure instead of silently reporting success.
            current_step = "verify"
            actual = _sha256_of_file(proposed)
            if actual != item.sha256_hash:
                msg = f"sha256 mismatch for {item.original_path}: expected {item.sha256_hash}, got {actual} (already-moved replay check against {proposed})"
                raise ValueError(msg)

        if not already_moved:
            # 3. Optional sha256 verify (caller may supply None to skip)
            if item.sha256_hash is not None:
                current_step = "verify"
                actual = _sha256_of_file(original)
                if actual != item.sha256_hash:
                    msg = f"sha256 mismatch for {item.original_path}: expected {item.sha256_hash}, got {actual}"
                    raise ValueError(msg)

            # 4. Move original -> proposed (mkdir parent as needed). Prefer
            # os.replace (atomic, O(1), constant memory) when src + dst share a
            # filesystem; otherwise stream the bytes in bounded chunks -- concert
            # videos are multi-GB and the meta lane has no memory pin, so a
            # whole-file read would MemoryError / OOM-kill the worker.
            current_step = "copy"
            proposed.parent.mkdir(parents=True, exist_ok=True)
            # phaze-yu2e: refuse to clobber a pre-existing destination. Both branches
            # below silently destroy whatever sits at `proposed` -- os.replace atomically
            # replaces it and the streamed copy's open("wb") truncates it. The
            # dispatch-time collision gate cannot catch every case (NULL-path in-place
            # renames, a destination already occupied by an earlier executed proposal, or
            # an untracked on-disk file), so fail the copy step loudly here rather than
            # overwrite. ``_is_same_file`` exempts the no-op / case-only rename.
            if proposed.exists() and not _is_same_file(original, proposed):
                msg = f"destination already exists, refusing to overwrite: {proposed}"
                raise FileExistsError(msg)
            if _same_filesystem(original, proposed.parent):
                # Atomic rename also removes the original in one syscall -- the move
                # IS the delete, so there is no separate delete step to fail.
                original.replace(proposed)
            else:
                _streamed_copy(original, proposed)
                # 5. Delete the original (a cross-filesystem copy leaves it in place).
                current_step = "delete"
                original.unlink()

        # 6a. PATCH execution log to completed
        try:
            await api.patch_execution_log(
                execution_log_id,
                ExecutionLogPatch(
                    status=ExecutionStatus.COMPLETED,
                    sha256_verified=sha_verified,
                ),
            )
        except Exception as patch_exc:
            logger.warning(
                "execute_approved_batch: could not patch completed log for %s: %s",
                item.proposal_id,
                patch_exc,
            )

        # 6b. Report SUCCESS via patch_proposal_state (joint Proposal + FileRecord transition).
        # This is the 'report' step: the move is ALREADY committed on disk (the
        # file sits at `proposed` and the original is gone), so a failure here
        # must NOT bubble into the generic failure handler. If it did, a 5xx
        # after tenacity retries would flip an APPROVED->executed proposal to
        # FAILED, misattribute failed_at_step='delete' (current_step's last
        # value), and leave FileRecord.current_path pointing at the deleted
        # original -- a divergence SAQ replay cannot heal (the original is gone).
        # Swallow + log and still return success; the state report is recoverable
        # via reconciliation, the committed move is not.
        try:
            await api.patch_proposal_state(
                item.proposal_id,
                ProposalStatePatch(
                    proposal_state="executed",
                    file_state="moved",
                    current_path=str(proposed),
                ),
            )
        except Exception as report_exc:
            logger.error(
                "execute_approved_batch: move committed but reporting executed state failed for %s: %s",
                item.proposal_id,
                report_exc,
            )

        # 7. Phase 28 D-03: per-proposal terminal progress POST (success path).
        # Fire-and-forget: D-16 says swallow + log WARNING on failure because the
        # file ops + per-proposal PATCH have already committed.
        try:
            await api.post_exec_batch_progress(
                payload.batch_id,
                ExecBatchProgressPayload(
                    request_id=progress_request_id,
                    batch_id=payload.batch_id,
                    agent_id=payload.agent_id,
                    sub_batch_index=payload.sub_batch_index,
                    proposal_id=item.proposal_id,
                    terminal_step="deleted",
                    sub_batch_terminal=is_last,
                ),
            )
        except Exception as progress_exc:
            logger.warning(
                "execute_approved_batch: progress POST failed for %s: %s",
                item.proposal_id,
                progress_exc,
            )

        return True
    except Exception as exc:
        # Phase 28: classify the failure step BEFORE any PATCH so both the
        # error_message prefix (D-01) and the progress POST failed_at_step
        # (D-06) come from one source of truth.
        failed_step: FailedAtStep = _classify_failure_step(current_step, exc)
        formatted_error = f"{failed_step}: {exc!s}"[:500]

        logger.warning(
            "execute_approved_batch: proposal %s failed at step=%s: %s",
            item.proposal_id,
            failed_step,
            exc,
            exc_info=True,
        )
        # 6a-failed. PATCH execution log to failed (D-01 "<step>: <reason>" prefix).
        try:
            await api.patch_execution_log(
                execution_log_id,
                ExecutionLogPatch(
                    status=ExecutionStatus.FAILED,
                    error_message=formatted_error,
                ),
            )
        except Exception as patch_exc:
            logger.warning(
                "execute_approved_batch: could not patch failed log for %s: %s",
                item.proposal_id,
                patch_exc,
            )
        # 6b-failed. Report failure via patch_proposal_state
        try:
            await api.patch_proposal_state(
                item.proposal_id,
                ProposalStatePatch(
                    proposal_state="failed",
                    file_state=None,
                    error_message=formatted_error,
                ),
            )
        except Exception as report_exc:
            # If we can't even REPORT the failure, log and continue -- one bad
            # network blip should not bring the whole batch down.
            logger.error(
                "execute_approved_batch: failed to report failure for %s: %s",
                item.proposal_id,
                report_exc,
            )

        # 7-failed. Phase 28 D-03: per-proposal terminal progress POST (failure path).
        try:
            await api.post_exec_batch_progress(
                payload.batch_id,
                ExecBatchProgressPayload(
                    request_id=progress_request_id,
                    batch_id=payload.batch_id,
                    agent_id=payload.agent_id,
                    sub_batch_index=payload.sub_batch_index,
                    proposal_id=item.proposal_id,
                    terminal_step="failed",
                    failed_at_step=failed_step,
                    sub_batch_terminal=is_last,
                ),
            )
        except Exception as progress_exc:
            logger.warning(
                "execute_approved_batch: progress POST failed for %s: %s",
                item.proposal_id,
                progress_exc,
            )

        return False


def _load_or_seed_uuids(
    job: Any,
    proposals: list[ExecuteBatchProposalItem],
) -> tuple[dict[uuid.UUID, uuid.UUID], dict[uuid.UUID, uuid.UUID], dict[str, str], bool]:
    """Read per-proposal UUIDs from ``job.meta`` or generate fresh ones.

    Returns ``(log_ids_by_proposal, req_ids_by_proposal, updated_meta, changed)``
    where ``changed`` is True iff any keys were newly seeded (caller is responsible
    for persisting via ``await job.update(meta=updated_meta)``). UUIDs in meta are
    stored as strings; in-memory they're returned as ``uuid.UUID`` objects.

    Phase 28 L6/L22/D-15 contract: on a SAQ retry the same ``job`` is reloaded
    from Redis with ``meta`` already populated, so this function returns the
    existing UUIDs and ``changed=False`` -- caller skips the ``job.update`` call
    AND the underlying ExecutionLog INSERT / progress HINCRBY both dedup via
    server-side idempotency (INSERT ON CONFLICT + SET NX EX).
    """
    existing_meta: dict[str, str] = dict(getattr(job, "meta", None) or {})
    log_ids: dict[uuid.UUID, uuid.UUID] = {}
    req_ids: dict[uuid.UUID, uuid.UUID] = {}
    changed = False
    for item in proposals:
        log_key = f"log_id:{item.proposal_id}"
        req_key = f"req_id:{item.proposal_id}"
        if log_key in existing_meta:
            log_ids[item.proposal_id] = uuid.UUID(existing_meta[log_key])
        else:
            new_log_id = uuid.uuid4()
            existing_meta[log_key] = str(new_log_id)
            log_ids[item.proposal_id] = new_log_id
            changed = True
        if req_key in existing_meta:
            req_ids[item.proposal_id] = uuid.UUID(existing_meta[req_key])
        else:
            new_req_id = uuid.uuid4()
            existing_meta[req_key] = str(new_req_id)
            req_ids[item.proposal_id] = new_req_id
            changed = True
    return log_ids, req_ids, existing_meta, changed


async def execute_approved_batch(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Per-agent sub-batch executor (B2 Option A -- full implementation + Phase 28 D-03/D-15).

    Validates payload (extra='forbid'), seeds retry-stable per-proposal UUIDs
    in ``ctx['job'].meta`` (so SAQ retries reuse the same ``execution_log_id``
    and ``progress_request_id`` per proposal), then executes each proposal with
    failure isolation. Cross-proposal failures are isolated: one bad file does
    NOT fail the batch.
    """
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
        # Mis-deployment: agent has no scan_roots configured. Refuse to perform any
        # file ops (path-traversal guard would reject every path anyway).
        msg = "agent has no scan_roots configured; cannot execute batch"
        raise RuntimeError(msg)

    # Phase 28 L6/L22 + D-15: load retry-stable UUIDs from SAQ job meta (or seed if absent).
    # Legacy callers (Phase 26 in-memory test fixtures) may pass a ctx without 'job' -- in that
    # case we fall back to generating fresh UUIDs per call. The fall-back has no SAQ retry
    # semantics (which legacy callers don't have anyway) and matches Phase 26 B2 behavior.
    job = ctx.get("job")
    if job is not None:
        log_ids, req_ids, updated_meta, changed = _load_or_seed_uuids(job, list(payload.proposals))
        if changed:
            await job.update(meta=updated_meta)
    else:
        logger.debug("execute_approved_batch: ctx has no 'job' key -- using fresh UUIDs (legacy ctx).")
        log_ids = {item.proposal_id: uuid.uuid4() for item in payload.proposals}
        req_ids = {item.proposal_id: uuid.uuid4() for item in payload.proposals}

    processed = 0
    errors = 0
    total = len(payload.proposals)
    for idx, item in enumerate(payload.proposals):
        is_last = idx == total - 1
        ok = await _execute_one(
            api,
            item,
            scan_roots,
            payload,
            is_last,
            log_ids[item.proposal_id],
            req_ids[item.proposal_id],
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
