"""Sequential, transaction-neutral persistence for filename proposals."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import func
import structlog

from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.date_convention import CONTEXT_KEY as DATE_CONVENTION_CONTEXT_KEY
from phaze.services.pg_text import sanitize_pg_text


if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.services.proposal_parsing import BatchProposalResponse


# Preserve the facade's historical logger name so warning attribution is unchanged.
logger = structlog.get_logger("phaze.services.proposal")


def _sanitize_json(value: Any) -> Any:
    """Recursively strip PostgreSQL-unstorable characters from JSON-compatible values."""
    if isinstance(value, str):
        return sanitize_pg_text(value)
    if isinstance(value, dict):
        return {k: _sanitize_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(v) for v in value]
    return value


async def store_proposals(
    session: AsyncSession,
    file_ids: list[str],
    batch_response: BatchProposalResponse,
    files_context: list[dict[str, Any]],
    *,
    insert_factory: Callable[[type[RenameProposal]], Any],
    clamp_confidence: Callable[[float], float],
) -> int:
    """Upsert the one active pending proposal per file without owning the transaction.

    The statements intentionally execute sequentially on the caller's single ``AsyncSession``.
    Apart from respecting SQLAlchemy's non-concurrent session contract, this preserves the
    established last-write-wins behavior when a malformed model response repeats a file index.
    This function never commits, flushes, or rolls back; the task adapter retains transaction
    ownership.
    """
    count = 0
    for proposal in batch_response.proposals:
        idx = proposal.file_index
        if not (0 <= idx < len(file_ids)):
            logger.warning("proposal file_index out of range — skipping", file_index=idx, batch_size=len(file_ids))
            continue
        fid = file_ids[idx]
        confidence = clamp_confidence(proposal.confidence)
        context_used = {
            "artist": proposal.artist,
            "event_name": proposal.event_name,
            "venue": proposal.venue,
            "date": proposal.date,
            "source_type": proposal.source_type,
            "stage": proposal.stage,
            "day_number": proposal.day_number,
            "b2b_partners": proposal.b2b_partners,
            "input_context": files_context[idx],
        }
        date_provenance = files_context[idx].get(DATE_CONVENTION_CONTEXT_KEY)
        if date_provenance is not None:
            context_used[DATE_CONVENTION_CONTEXT_KEY] = date_provenance
        context_used = _sanitize_json(context_used)

        path_raw = proposal.proposed_path
        if path_raw:
            path_raw = path_raw.strip("/")
            while "//" in path_raw:
                path_raw = path_raw.replace("//", "/")

        row = {
            "id": uuid.uuid4(),
            "file_id": uuid.UUID(fid),
            "proposed_filename": sanitize_pg_text(proposal.proposed_filename),
            "proposed_path": sanitize_pg_text(path_raw) if path_raw else path_raw,
            "confidence": confidence,
            "status": ProposalStatus.PENDING,
            "context_used": context_used,
            "reason": sanitize_pg_text(proposal.reasoning),
        }
        stmt = insert_factory(RenameProposal).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["file_id"],
            index_where=(RenameProposal.status == "pending"),
            set_={
                "proposed_filename": stmt.excluded.proposed_filename,
                "proposed_path": stmt.excluded.proposed_path,
                "confidence": stmt.excluded.confidence,
                "context_used": stmt.excluded.context_used,
                "reason": stmt.excluded.reason,
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)
        count += 1
    return count
