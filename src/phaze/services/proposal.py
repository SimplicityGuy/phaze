"""Stable proposal facade over parsing, context, persistence, and provider boundaries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from litellm import acompletion
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.services.proposal_context import (  # noqa: F401 -- compatibility facade
    _COMPANION_CHUNK,
    _COMPANION_READ_TIMEOUT_S,
    _DATE_CONVENTION_GUIDANCE,
    _DATE_CONVENTION_PLACEHOLDER,
    _PROMPTS_DIR,
    MAX_COMPANION_CHARS,
    _date_convention_guidance,
    _read_companion_chunk,
    build_file_context,
    clean_companion_content,
    fetch_companion_contents,
    load_companion_contents,
    load_companion_targets,
    load_prompt_template,
)
from phaze.services.proposal_parsing import (  # noqa: F401 -- compatibility facade
    BatchProposalResponse,
    FileProposalResponse,
    MalformedCompletionError,
    SalvagedBatchProposalResponse,
    _extract_json_span,
    _failure_mode,
    _preview,
    _reject_contentless_completion,
    _salvage_proposals,
    _wrapping_mode,
    clamp_confidence,
    parse_completion,
)
from phaze.services.proposal_persistence import _sanitize_json, store_proposals as _store_proposals  # noqa: F401 -- compatibility facade
from phaze.services.proposal_provider import check_rate_limit as _check_rate_limit, generate_batch as _generate_batch


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


class ProposalService:
    """Coordinate provider requests while preserving the supported service contract."""

    def __init__(self, model: str, prompt_template: str, max_rpm: int) -> None:
        self.model = model
        self.prompt_template = prompt_template
        self.max_rpm = max_rpm

    async def generate_batch(self, files_context: list[dict[str, Any]]) -> BatchProposalResponse:
        """Call the configured LLM and return its parsed proposal batch."""
        # The provider boundary deliberately retains the unchecked ``response.choices[0]`` access:
        # LiteLLM owns the empty-choices guard, and its vetted minor pin protects that contract.
        return await _generate_batch(
            model=self.model,
            prompt_template=self.prompt_template,
            files_context=files_context,
            completion_provider=acompletion,
            completion_parser=self._parse_completion,
            date_guidance=_date_convention_guidance,
        )

    def _parse_completion(self, content: str | None, *, batch_size: int, finish_reason: str | None) -> BatchProposalResponse:
        """Apply the stable parse and discard-only salvage policy."""
        return parse_completion(
            content,
            model=self.model,
            batch_size=batch_size,
            finish_reason=finish_reason,
            logger=logger,
            extract_json_span=_extract_json_span,
            salvage_proposals=_salvage_proposals,
        )

    @staticmethod
    def _clamp_confidence(value: float) -> float:
        """Clamp confidence to the supported range."""
        return clamp_confidence(value)


async def check_rate_limit(redis_pool: Any, max_rpm: int) -> None:
    """Block until a provider request slot is available."""
    await _check_rate_limit(redis_pool, max_rpm, sleep=asyncio.sleep)


async def store_proposals(
    session: AsyncSession,
    file_ids: list[str],
    batch_response: BatchProposalResponse,
    files_context: list[dict[str, Any]],
) -> int:
    """Persist proposals without taking ownership of the caller's transaction."""
    return await _store_proposals(
        session,
        file_ids,
        batch_response,
        files_context,
        insert_factory=pg_insert,
        clamp_confidence=ProposalService._clamp_confidence,
    )
