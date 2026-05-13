"""SAQ task: execute_approved_batch -- per-proposal local file ops + HTTP state reporting (Phase 26 B2 Option A).

Reads file paths from payload (no DB lookup -- D-23 invariant). For each proposal:
1. Validate `proposed_path` is contained within an agent scan_root (T-26-11-S1 path-traversal guard).
2. POST /execution-log with status='in_progress' (per-proposal audit row).
3. Optionally verify sha256 of `original_path` against `payload.sha256_hash`.
4. Copy `original_path` -> `proposed_path` (mkdir parent as needed).
5. Delete the original.
6. PATCH /execution-log/{id} with status='completed' (or 'failed').
7. PATCH /proposals/{id}/state with proposal_state=executed, file_state=moved, current_path=proposed_path.

On any per-proposal IO error: PATCH execution-log status='failed' + PATCH proposal_state='failed' + error_message + continue with the rest.
The batch returns aggregate processed/error counts; cross-proposal failures are isolated.

NOTE on schema mapping: Phase 25's ExecutionLog schema is per-proposal (one row per file op),
not per-batch. Plan 11 invariants (one POST at start, per-proposal state PATCH, one PATCH at
end) are adapted to the existing schema as: one POST+PATCH per proposal (matching the
ExecutionLog table's natural key `proposal_id`). The "completed_with_errors" plan label
becomes "completed_with_errors" in the returned batch dict (no schema field for it).

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/test_task_split.py (Plan 10).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
import uuid

from phaze.config import AgentSettings, get_settings
from phaze.enums.execution import ExecutionStatus
from phaze.schemas.agent_execution import ExecutionLogCreate, ExecutionLogPatch
from phaze.schemas.agent_proposals import ProposalStatePatch
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


logger = logging.getLogger(__name__)


def _resolve_and_check_containment(candidate: str, scan_roots: list[str]) -> Path:
    """Resolve `candidate` and assert it lives under at least one of `scan_roots`.

    Raises ValueError on path traversal (T-26-11-S1). The resolved path is what
    we use for the actual file op so symlinks-out are also caught.
    """
    resolved = Path(candidate).resolve()
    for root in scan_roots:
        root_resolved = Path(root).resolve()
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            continue
    msg = f"path {candidate!r} (resolved to {resolved}) escapes all scan_roots {scan_roots}"
    raise ValueError(msg)


def _sha256_of_file(path: Path) -> str:
    """Streaming sha256 (avoid loading large files into memory)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _execute_one(
    api: PhazeAgentClient,
    item: ExecuteBatchProposalItem,
    scan_roots: list[str],
) -> bool:
    """Execute one proposal. Returns True on success, False on any failure.

    Per-proposal lifecycle:
    1. POST execution-log (status=in_progress) -- one row per file op.
    2. Path-traversal guard for original_path and proposed_path.
    3. Optional sha256 verify.
    4. Copy + delete.
    5. PATCH execution-log (status=completed | failed).
    6. PATCH proposal-state (executed | failed).
    """
    execution_log_id = uuid.uuid4()
    sha_verified = item.sha256_hash is not None
    # Always POST the in-progress audit row first -- this is the durable trail
    # that survives a crash mid-copy.
    try:
        await api.post_execution_log(
            ExecutionLogCreate(
                id=execution_log_id,
                proposal_id=item.proposal_id,
                operation="move",
                source_path=item.original_path,
                destination_path=item.proposed_path,
                sha256_verified=False,  # not yet verified at this point
                status=ExecutionStatus.IN_PROGRESS,
            ),
        )
    except Exception as exc:
        # If the audit log POST itself fails (network blip), still attempt the
        # file op so we don't leave the user with stalled state. Best-effort.
        logger.warning("execute_approved_batch: could not record start log for %s: %s", item.proposal_id, exc)

    try:
        # 2. Path-traversal guard for both original_path and proposed_path
        original = _resolve_and_check_containment(item.original_path, scan_roots)
        proposed = _resolve_and_check_containment(item.proposed_path, scan_roots)

        # 3. Optional sha256 verify (caller may supply None to skip)
        if item.sha256_hash is not None:
            actual = _sha256_of_file(original)
            if actual != item.sha256_hash:
                msg = f"sha256 mismatch for {item.original_path}: expected {item.sha256_hash}, got {actual}"
                raise ValueError(msg)

        # 4. Copy original -> proposed (mkdir parent as needed). os.replace would
        # also work but copy+delete leaves the original intact until the copy is
        # committed.
        proposed.parent.mkdir(parents=True, exist_ok=True)
        proposed.write_bytes(original.read_bytes())

        # 5. Delete the original
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

        # 6b. Report SUCCESS via patch_proposal_state (joint Proposal + FileRecord transition)
        await api.patch_proposal_state(
            item.proposal_id,
            ProposalStatePatch(
                proposal_state="executed",
                file_state="moved",
                current_path=str(proposed),
            ),
        )
        return True
    except Exception as exc:
        logger.warning(
            "execute_approved_batch: proposal %s failed: %s",
            item.proposal_id,
            exc,
            exc_info=True,
        )
        # 6a-failed. PATCH execution log to failed
        try:
            await api.patch_execution_log(
                execution_log_id,
                ExecutionLogPatch(
                    status=ExecutionStatus.FAILED,
                    error_message=str(exc)[:500],
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
                    error_message=str(exc)[:500],
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
        return False


async def execute_approved_batch(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Per-agent sub-batch executor (B2 Option A -- full implementation).

    Validates payload (extra='forbid'), executes each proposal with failure
    isolation, and returns aggregate counts. Cross-proposal failures are
    isolated: one bad file does NOT fail the batch.
    """
    payload = ExecuteApprovedBatchPayload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]

    cfg = get_settings()
    scan_roots: list[str] = list(cfg.scan_roots) if isinstance(cfg, AgentSettings) else []
    if not scan_roots:
        # Mis-deployment: agent has no scan_roots configured. Refuse to perform any
        # file ops (path-traversal guard would reject every path anyway).
        msg = "agent has no scan_roots configured; cannot execute batch"
        raise RuntimeError(msg)

    processed = 0
    errors = 0
    for item in payload.proposals:
        ok = await _execute_one(api, item, scan_roots)
        processed += 1
        if not ok:
            errors += 1

    final_status = "completed" if errors == 0 else "completed_with_errors"

    return {
        "batch_id": str(payload.batch_id),
        "status": final_status,
        "processed_count": processed,
        "error_count": errors,
    }
