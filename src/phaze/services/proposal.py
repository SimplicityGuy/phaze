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
from sqlalchemy import select

from phaze.models.file import FileRecord, FileState
from phaze.models.file_companion import FileCompanion
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.analysis import AnalysisResult
    from phaze.models.metadata import FileMetadata


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


async def check_rate_limit(redis_pool: Any, max_rpm: int) -> None:
    """Block until an LLM request slot is available.

    Uses a Redis INCR/EXPIRE pattern with a 60-second rolling window.
    When the counter exceeds *max_rpm*, backs off with a 2-second sleep
    and retries.

    Args:
        redis_pool: An async Redis connection (e.g. arq's ``ArqRedis``).
        max_rpm: Maximum requests allowed per minute.
    """
    key = "phaze:llm:rpm"
    while True:
        count: int = await redis_pool.incr(key)
        if count == 1:
            await redis_pool.expire(key, 60)
        if count <= max_rpm:
            return
        # Over limit — undo the increment and wait
        await redis_pool.decr(key)
        await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Proposal storage
# ---------------------------------------------------------------------------


async def store_proposals(
    session: AsyncSession,
    file_ids: list[str],
    batch_response: BatchProposalResponse,
    files_context: list[dict[str, Any]],
) -> int:
    """Store LLM proposals as immutable RenameProposal records.

    Also transitions each file's state to ``PROPOSAL_GENERATED``.

    Args:
        session: Active async database session.
        file_ids: Ordered list of file ID strings matching the batch.
        batch_response: Parsed LLM response containing proposals.
        files_context: The input contexts sent to the LLM (for context_used JSONB).

    Returns:
        Number of proposals stored.
    """
    count = 0
    for proposal in batch_response.proposals:
        fid = file_ids[proposal.file_index]
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
            "input_context": files_context[proposal.file_index],
        }
        record = RenameProposal(
            file_id=uuid.UUID(fid),
            proposed_filename=proposal.proposed_filename,
            confidence=confidence,
            status=ProposalStatus.PENDING,
            context_used=context_used,
            reason=proposal.reasoning,
        )
        session.add(record)

        # Update file state
        result = await session.execute(select(FileRecord).where(FileRecord.id == uuid.UUID(fid)))
        file_record = result.scalar_one_or_none()
        if file_record is not None:
            file_record.state = FileState.PROPOSAL_GENERATED

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
