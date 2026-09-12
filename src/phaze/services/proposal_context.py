"""Prompt, file, and companion context loading for filename proposals."""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
import structlog

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.schemas.agent_tasks import CompanionReadItem, ReadCompanionFilesPayload
from phaze.services.date_convention import CONTEXT_KEY as DATE_CONVENTION_CONTEXT_KEY
from phaze.services.enqueue_router import lane_for_task
from phaze.services.pg_text import sanitize_pg_text
from phaze.services.text_repair import repair_mojibake


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.analysis import AnalysisResult
    from phaze.models.metadata import FileMetadata
    from phaze.services.agent_task_router import AgentTaskRouter


# Preserve the facade's historical logger name so operational event routing is unchanged.
logger = structlog.get_logger("phaze.services.proposal")

MAX_COMPANION_CHARS = 3000
_COMPANION_READ_TIMEOUT_S = 30.0
_COMPANION_CHUNK = 200
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_ASCII_ART_RE = re.compile(r"^[\s\-=_*#~|/\\]{10,}$")

_DATE_CONVENTION_PLACEHOLDER = "{date_convention_guidance}\n"
_DATE_CONVENTION_GUIDANCE = (
    "- `date_convention`: Present only when the filename carries an `NN-NN-YYYY` scene date that was resolved. "
    "`date` is that date as ISO `YYYY-MM-DD` and `raw` is the token it came from. `source` is `filename` when the "
    "token could only be read one way, or `release_group_convention` when the token was genuinely ambiguous and was "
    "resolved from the release group's learned date-order convention -- in which case `scope_value`, "
    "`convention_value`, `supporting_count` and `contradicting_count` are the evidence behind it. Prefer this `date` "
    "over your own reading of that token: it is either a fact about the string or an inference backed by counted "
    "evidence. It is absent for every other date shape; read those yourself as usual.\n"
)


def load_prompt_template(name: str = "naming") -> str:
    """Load a prompt template from the package prompts directory."""
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        msg = f"Prompt template not found: {path}"
        raise FileNotFoundError(msg)
    return path.read_text(encoding="utf-8")


def clean_companion_content(text: str, max_chars: int = MAX_COMPANION_CHARS) -> str:
    """Remove ASCII-art separators and truncate companion text for an LLM prompt."""
    lines = text.splitlines()
    cleaned = [line for line in lines if not _ASCII_ART_RE.match(line)]
    result = "\n".join(cleaned).strip()
    if len(result) > max_chars:
        result = result[:max_chars] + "\n[...truncated]"
    return result


def build_file_context(
    file_record: FileRecord,
    analysis: AnalysisResult | None,
    companion_contents: list[dict[str, str]],
    metadata: FileMetadata | None = None,
) -> dict[str, object]:
    """Assemble the stable per-file dictionary sent to the provider."""
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
        "original_filename": repair_mojibake(file_record.original_filename),
        "original_path": file_record.original_path,
        "file_type": file_record.file_type,
        "analysis": analysis_dict,
        "tags": tags_dict,
        "companions": companion_contents,
    }


def _date_convention_guidance(files_context: list[dict[str, Any]]) -> str:
    """Return date guidance only when at least one context carries provenance."""
    if any(DATE_CONVENTION_CONTEXT_KEY in context for context in files_context):
        return _DATE_CONVENTION_GUIDANCE
    return ""


async def load_companion_targets(
    session: AsyncSession,
    media_file_id: uuid.UUID,
    task_router: AgentTaskRouter | None = None,
) -> dict[str, list[CompanionReadItem]]:
    """Resolve companion targets by owning agent using database reads only."""
    if task_router is None:
        logger.debug("companion_read_skipped_no_task_router", media_file_id=str(media_file_id))
        return {}

    result = await session.execute(select(FileCompanion).where(FileCompanion.media_id == media_file_id))
    companions = result.scalars().all()
    if not companions:
        return {}

    companion_ids = [comp.companion_id for comp in companions]
    records_result = await session.execute(select(FileRecord).where(FileRecord.id.in_(companion_ids)))
    records_by_id = {rec.id: rec for rec in records_result.scalars().all()}

    by_agent: dict[str, list[CompanionReadItem]] = {}
    for comp in companions:
        rec = records_by_id.get(comp.companion_id)
        if rec is None:
            continue
        by_agent.setdefault(rec.agent_id, []).append(CompanionReadItem(filename=rec.original_filename, path=rec.current_path))

    return by_agent


async def _read_companion_chunk(
    task_router: AgentTaskRouter,
    agent_id: str,
    chunk: list[CompanionReadItem],
    max_chars: int,
    media_file_id: uuid.UUID,
) -> list[dict[str, str]]:
    """Read one wire-bounded companion chunk, degrading a failed read to no context."""
    try:
        queue = task_router.queue_for(agent_id, lane_for_task("read_companion_files"))
        await queue.connect()
        payload = ReadCompanionFilesPayload(agent_id=agent_id, companions=chunk, max_chars=max_chars)
        job_result = await queue.apply("read_companion_files", timeout=_COMPANION_READ_TIMEOUT_S, **payload.model_dump(mode="json"))
    except Exception:
        logger.warning("companion_read_unavailable", agent_id=agent_id, media_file_id=str(media_file_id), exc_info=True)
        return []

    contents: list[dict[str, str]] = []
    for entry in (job_result or {}).get("contents", []):
        cleaned = sanitize_pg_text(clean_companion_content(entry["content"], max_chars))
        contents.append({"filename": sanitize_pg_text(entry["filename"]), "content": cleaned})
    return contents


async def fetch_companion_contents(
    by_agent: dict[str, list[CompanionReadItem]],
    max_chars: int,
    task_router: AgentTaskRouter | None,
    media_file_id: uuid.UUID,
) -> list[dict[str, str]]:
    """Fetch companion text after the caller has released its database session."""
    if task_router is None or not by_agent:
        return []

    contents: list[dict[str, str]] = []
    for agent_id, items in by_agent.items():
        for start in range(0, len(items), _COMPANION_CHUNK):
            chunk = items[start : start + _COMPANION_CHUNK]
            contents.extend(await _read_companion_chunk(task_router, agent_id, chunk, max_chars, media_file_id))

    return contents


async def load_companion_contents(
    session: AsyncSession,
    media_file_id: uuid.UUID,
    max_chars: int,
    task_router: AgentTaskRouter | None = None,
) -> list[dict[str, str]]:
    """Convenience composition of target loading and agent-side companion reads."""
    by_agent = await load_companion_targets(session, media_file_id, task_router=task_router)
    return await fetch_companion_contents(by_agent, max_chars, task_router, media_file_id)
