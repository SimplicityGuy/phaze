"""Proposal service — LLM calling, rate limiting, proposal storage, and context building."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any
import uuid

from litellm import acompletion
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.analysis import AnalysisResult
    from phaze.models.metadata import FileMetadata


logger = structlog.get_logger(__name__)

# Module constant: max chars for companion file content sent to LLM
MAX_COMPANION_CHARS = 3000

# Path to the prompts directory (sibling of services/)
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Regex to match ASCII art lines (10+ repeated non-alphanumeric chars)
_ASCII_ART_RE = re.compile(r"^[\s\-=_*#~|/\\]{10,}$")


# ---------------------------------------------------------------------------
# Pydantic response models for structured LLM output
# ---------------------------------------------------------------------------


class FileProposalResponse(BaseModel):
    """LLM response for a single file in a batch.

    Note: confidence is typed as plain ``float`` with NO Field(ge=, le=)
    constraints due to a known litellm bug with Anthropic models (GitHub
    issue #21016).  Post-parse clamping should be applied by the caller.
    """

    file_index: int
    proposed_filename: str
    proposed_path: str | None = None
    confidence: float
    artist: str | None = None
    event_name: str | None = None
    venue: str | None = None
    date: str | None = None
    source_type: str | None = None
    stage: str | None = None
    day_number: int | None = None
    b2b_partners: list[str] = []
    reasoning: str


class BatchProposalResponse(BaseModel):
    """LLM response for a batch of files."""

    proposals: list[FileProposalResponse]


# ---------------------------------------------------------------------------
# Prompt template loading
# ---------------------------------------------------------------------------


def load_prompt_template(name: str = "naming") -> str:
    """Load a prompt template from the ``prompts/`` directory.

    Args:
        name: Template name (without ``.md`` extension).  Defaults to ``"naming"``.

    Returns:
        The full text of the template.

    Raises:
        FileNotFoundError: If the template file does not exist.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        msg = f"Prompt template not found: {path}"
        raise FileNotFoundError(msg)
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Companion content cleaning
# ---------------------------------------------------------------------------


def clean_companion_content(text: str, max_chars: int = MAX_COMPANION_CHARS) -> str:
    """Clean and truncate companion file content for LLM context.

    Strips ASCII art lines (runs of 10+ non-alphanumeric characters) and
    truncates the result to *max_chars*, appending ``"[...truncated]"`` when
    the content is cut.

    Args:
        text: Raw companion file text.
        max_chars: Maximum character count before truncation.

    Returns:
        Cleaned and (possibly) truncated text.
    """
    lines = text.splitlines()
    cleaned = [line for line in lines if not _ASCII_ART_RE.match(line)]
    result = "\n".join(cleaned).strip()
    if len(result) > max_chars:
        result = result[:max_chars] + "\n[...truncated]"
    return result


# ---------------------------------------------------------------------------
# File context assembly
# ---------------------------------------------------------------------------


def build_file_context(
    file_record: FileRecord,
    analysis: AnalysisResult | None,
    companion_contents: list[dict[str, str]],
    metadata: FileMetadata | None = None,
) -> dict[str, object]:
    """Assemble a context dict for a single file to be sent to the LLM.

    Args:
        file_record: The ``FileRecord`` ORM instance.
        analysis: The associated ``AnalysisResult``, or ``None`` if not analyzed.
        companion_contents: List of dicts with ``"filename"`` and ``"content"`` keys
            for each companion file.
        metadata: The associated ``FileMetadata``, or ``None`` if not extracted.

    Returns:
        A dict ready to be serialized to JSON for the LLM prompt.  The
        ``"index"`` key defaults to ``0`` — callers should overwrite it with
        the actual batch index.
    """
    analysis_dict: dict[str, object] | None = None
    if analysis is not None:
        analysis_dict = {
            "bpm": analysis.bpm,
            "musical_key": analysis.musical_key,
            "mood": analysis.mood,
            "style": analysis.style,
            "features": analysis.features,
        }

    tags_dict: dict[str, object] | None = None
    if metadata is not None:
        tags_dict = {
            "artist": metadata.artist,
            "title": metadata.title,
            "album": metadata.album,
            "year": metadata.year,
            "genre": metadata.genre,
            "raw_tags": metadata.raw_tags,
        }

    return {
        "index": 0,
        "original_filename": file_record.original_filename,
        "original_path": file_record.original_path,
        "file_type": file_record.file_type,
        "analysis": analysis_dict,
        "tags": tags_dict,
        "companions": companion_contents,
    }


# ---------------------------------------------------------------------------
# ProposalService — LLM calling and confidence clamping
# ---------------------------------------------------------------------------


class ProposalService:
    """Handles LLM-based filename proposal generation."""

    def __init__(self, model: str, prompt_template: str, max_rpm: int) -> None:
        self.model = model
        self.prompt_template = prompt_template
        self.max_rpm = max_rpm

    async def generate_batch(self, files_context: list[dict[str, Any]]) -> BatchProposalResponse:
        """Call the LLM to generate filename proposals for a batch of files.

        Args:
            files_context: List of per-file context dicts (output of build_file_context).

        Returns:
            Parsed ``BatchProposalResponse`` from the LLM.
        """
        prompt = self.prompt_template.replace("{files_json}", json.dumps(files_context, indent=2))
        response = await acompletion(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format=BatchProposalResponse,
        )
        return BatchProposalResponse.model_validate_json(response.choices[0].message.content)

    @staticmethod
    def _clamp_confidence(value: float) -> float:
        """Clamp a confidence value to the 0.0-1.0 range."""
        return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Rate limiting via Redis counter
# ---------------------------------------------------------------------------


# Rolling-window length (seconds) for the LLM requests-per-minute counter.
_RATE_LIMIT_WINDOW_SEC = 60

# phaze-pkgb: atomic INCR + conditional-arm, evaluated server-side so no
# CancelledError (SAQ job timeout / worker shutdown) or dropped connection can land
# BETWEEN the increment and the EXPIRE. The previous two-command form armed the TTL
# only inside an `if count == 1` branch after a separate INCR; a single lost EXPIRE
# left the counter with NO expiry, and — because successful acquisitions never
# release their increment and the over-limit path only DECRs the current iteration —
# the count could never fall back to 1, so the TTL was never re-armed and proposal
# generation wedged permanently until a manual `DEL phaze:llm:rpm`.
#
# This script re-arms whenever the key has no expiry (TTL == -1), which covers BOTH
# the first increment (INCR creates the key without a TTL) AND any pre-existing
# TTL-less key left by the old code path or a prior lost EXPIRE — so the limiter
# self-heals and a lost arm can never permanently wedge it. TTL returns -2 for a
# missing key (never true right after INCR) and -1 for "exists, no expiry".
_RATE_LIMIT_LUA = """
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) == -1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


async def check_rate_limit(redis_pool: Any, max_rpm: int) -> None:
    """Block until an LLM request slot is available.

    Uses an ATOMIC Redis INCR + conditional-EXPIRE (a single ``EVAL`` script) over a
    60-second rolling window. When the counter exceeds *max_rpm*, backs off with a
    2-second sleep and retries.

    The counter's only reset mechanism is its TTL, so the INCR and the EXPIRE MUST be
    inseparable: the script arms the TTL server-side in the same atomic execution as
    the increment (and re-arms any key that somehow lacks an expiry), which is why a
    lost EXPIRE can no longer permanently wedge the limiter (phaze-pkgb).

    Args:
        redis_pool: An async Redis connection (e.g. ``ctx["redis"]``, the dedicated
            cache handle wired in the controller worker startup — never ``queue.redis``,
            which the Postgres broker does not expose).
        max_rpm: Maximum requests allowed per minute.
    """
    key = "phaze:llm:rpm"
    while True:
        count = int(await redis_pool.eval(_RATE_LIMIT_LUA, 1, key, _RATE_LIMIT_WINDOW_SEC))
        if count <= max_rpm:
            return
        # Over limit — undo the increment and wait. The DECR keeps the existing TTL
        # (DECR never clears an expiry), so the window still resets on schedule.
        await redis_pool.decr(key)
        await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Proposal storage
# ---------------------------------------------------------------------------
#
# Pipeline DB-write idempotency audit (Phase 35, D-04 / RESEARCH Q4):
#   * proposals      -> store_proposals (below) is now the partial-index upsert.
#   * execution_log  -> already idempotent: routers/agent_execution.py:77 issues
#                       `pg_insert(ExecutionLog).on_conflict_do_nothing(["id"])`
#                       (agent-supplied PK, D-13). No change needed.
#   * tag_write_log  -> INTENTIONALLY append-only (models/tag_write_log.py): it is
#                       an audit trail recording every write attempt with
#                       before/after snapshots. Adding an upsert would erase that
#                       history, so it MUST stay insert-only. No change needed.
# After this change the proposals table is the last non-idempotent task write to
# be fixed; pipeline DB-write idempotency is complete.


async def store_proposals(
    session: AsyncSession,
    file_ids: list[str],
    batch_response: BatchProposalResponse,
    files_context: list[dict[str, Any]],
) -> int:
    """Upsert LLM proposals as the one active (PENDING) RenameProposal per file (D-04).

    Idempotent: re-running ``generate_proposals`` for a file OVERWRITES its
    existing PENDING proposal in place rather than appending a second pending
    row. The conflict target is the partial unique index
    ``uq_proposals_file_id_pending`` (``ON (file_id) WHERE status = 'pending'``,
    alembic 019). Because that index only covers PENDING rows, an
    APPROVED / EXECUTED / REJECTED / FAILED proposal for the same file is never a
    conflict target and is structurally protected from being overwritten -- human
    approvals survive any number of re-runs.

    PK-STAMP GOTCHA: ``RenameProposal.id`` declares only a Python-side
    ``default=uuid.uuid4``, which fires through ORM ``session.add()`` but NOT
    through ``pg_insert(...).values()``. We therefore stamp ``id`` explicitly on
    every row so a fresh INSERT does not raise ``NotNullViolationError``. The
    ``ON CONFLICT DO UPDATE`` set_ deliberately omits ``id`` and ``file_id`` so an
    overwrite keeps the existing row's identity.

    Args:
        session: Active async database session.
        file_ids: Ordered list of file ID strings matching the batch.
        batch_response: Parsed LLM response containing proposals.
        files_context: The input contexts sent to the LLM (for context_used JSONB).

    Returns:
        Number of proposals upserted.
    """
    count = 0
    for proposal in batch_response.proposals:
        # WR-01: file_index is an unbounded int the LLM emits. An index >= len(file_ids) would
        # crash the whole batch with IndexError; a NEGATIVE index would silently wrap (Python
        # negative indexing) and write the proposal against the WRONG file -- silent data
        # corruption. Reject out-of-range indices and skip the proposal.
        idx = proposal.file_index
        if not (0 <= idx < len(file_ids)):
            logger.warning("proposal file_index out of range — skipping", file_index=idx, batch_size=len(file_ids))
            continue
        fid = file_ids[idx]
        confidence = ProposalService._clamp_confidence(proposal.confidence)
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
        path_raw = proposal.proposed_path
        if path_raw:
            path_raw = path_raw.strip("/")
            while "//" in path_raw:
                path_raw = path_raw.replace("//", "/")

        row = {
            # Explicit PK stamp -- pg_insert bypasses the Python-side default.
            "id": uuid.uuid4(),
            "file_id": uuid.UUID(fid),
            "proposed_filename": proposal.proposed_filename,
            "proposed_path": path_raw,
            "confidence": confidence,
            "status": ProposalStatus.PENDING,
            "context_used": context_used,
            "reason": proposal.reasoning,
        }
        stmt = pg_insert(RenameProposal).values(**row)
        stmt = stmt.on_conflict_do_update(
            # Match the partial unique index uq_proposals_file_id_pending: the
            # index_where makes the conflict fire ONLY against an existing PENDING
            # row, so approvals (outside the index) are never overwritten.
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


# ---------------------------------------------------------------------------
# Companion content loading
# ---------------------------------------------------------------------------


async def load_companion_contents(
    session: AsyncSession,
    media_file_id: uuid.UUID,
    max_chars: int,
) -> list[dict[str, str]]:
    """Load and clean companion file contents for a media file.

    Queries the ``FileCompanion`` join table, reads each companion file from
    disk, cleans the content, and returns a list of filename/content dicts.

    Args:
        session: Active async database session.
        media_file_id: UUID of the media file.
        max_chars: Maximum chars per companion file (passed to ``clean_companion_content``).

    Returns:
        List of dicts with ``"filename"`` and ``"content"`` keys.
    """
    result = await session.execute(select(FileCompanion).where(FileCompanion.media_id == media_file_id))
    companions = result.scalars().all()

    contents: list[dict[str, str]] = []
    for comp in companions:
        rec_result = await session.execute(select(FileRecord).where(FileRecord.id == comp.companion_id))
        rec = rec_result.scalar_one_or_none()
        if rec is None:
            continue
        try:
            raw = Path(rec.current_path).read_text(encoding="utf-8", errors="replace")
            cleaned = clean_companion_content(raw, max_chars)
            contents.append({"filename": rec.original_filename, "content": cleaned})
        except OSError:
            continue

    return contents
