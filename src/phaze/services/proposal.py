"""Proposal service — Pydantic response models, prompt loading, companion cleaning, and context building."""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel


if TYPE_CHECKING:
    from phaze.models.analysis import AnalysisResult
    from phaze.models.file import FileRecord


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
) -> dict[str, object]:
    """Assemble a context dict for a single file to be sent to the LLM.

    Args:
        file_record: The ``FileRecord`` ORM instance.
        analysis: The associated ``AnalysisResult``, or ``None`` if not analyzed.
        companion_contents: List of dicts with ``"filename"`` and ``"content"`` keys
            for each companion file.

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

    return {
        "index": 0,
        "original_filename": file_record.original_filename,
        "original_path": file_record.original_path,
        "file_type": file_record.file_type,
        "analysis": analysis_dict,
        "companions": companion_contents,
    }
