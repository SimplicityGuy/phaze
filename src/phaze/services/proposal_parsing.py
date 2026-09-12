"""Response contracts and malformed-completion policy for filename proposals."""

from __future__ import annotations

import json
import math
import re
from typing import Any, NoReturn

from pydantic import BaseModel, ValidationError


class FileProposalResponse(BaseModel):
    """LLM response for a single file in a batch.

    Confidence intentionally has no Pydantic range constraint because constrained floats are not
    compatible with every supported LiteLLM provider. The persistence boundary clamps it instead.
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
    """LLM response for a batch of files and the provider response schema."""

    proposals: list[FileProposalResponse]


class SalvagedBatchProposalResponse(BatchProposalResponse):
    """A batch parsed by discarding invalid items without changing the provider schema."""

    discarded_positions: list[int]


class MalformedCompletionError(ValueError):
    """The provider's completion could not be turned into any usable proposal."""

    def __init__(self, message: str, *, mode: str) -> None:
        super().__init__(message)
        self.mode = mode


_JSON_FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_+-]*)\r?\n(?P<body>.*?)\r?\n?```", re.DOTALL)
_PREVIEW_HEAD = 240
_PREVIEW_TAIL = 120


def _preview(content: str) -> str:
    """Return a bounded head-and-tail excerpt for diagnostic logging."""
    if len(content) <= _PREVIEW_HEAD + _PREVIEW_TAIL:
        return content
    return f"{content[:_PREVIEW_HEAD]}…[{len(content) - _PREVIEW_HEAD - _PREVIEW_TAIL} chars elided]…{content[-_PREVIEW_TAIL:]}"


def _extract_json_span(content: str) -> str | None:
    """Select a fenced or prose-wrapped JSON substring without repairing its bytes."""
    fence = _JSON_FENCE_RE.search(content)
    if fence is not None:
        body = fence.group("body").strip()
        if body:
            return body

    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end > start:
        return content[start : end + 1]
    return None


def _wrapping_mode(content: str) -> str:
    """Name the wrapper removed by JSON-span extraction."""
    return "fenced" if _JSON_FENCE_RE.search(content) is not None else "preamble"


def _salvage_proposals(text: str) -> tuple[list[FileProposalResponse], list[int]] | None:
    """Keep valid proposal items and discard invalid items without inferring values."""
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    raw_items = document.get("proposals")
    if not isinstance(raw_items, list):
        return None

    kept: list[FileProposalResponse] = []
    discarded_positions: list[int] = []
    for position, item in enumerate(raw_items):
        try:
            kept.append(FileProposalResponse.model_validate(item))
        except ValidationError:
            discarded_positions.append(position)

    if not kept or not discarded_positions:
        return None
    return kept, discarded_positions


def _failure_mode(content: str, error: ValidationError, *, finish_reason: str | None) -> str:
    """Classify an unrecoverable completion for stable telemetry."""
    error_types = {item["type"] for item in error.errors()}
    if finish_reason == "length":
        return "truncated"
    if error_types == {"json_invalid"}:
        if any("EOF while parsing" in str(item.get("ctx", {}).get("error", "")) or "EOF while parsing" in item["msg"] for item in error.errors()):
            return "truncated"
        return "fenced_unrecovered" if _JSON_FENCE_RE.search(content) is not None else "json_invalid"
    return "schema_invalid"


def _reject_contentless_completion(content: object, *, finish_reason: str | None, log: Any) -> NoReturn:
    """Log and reject a provider response containing no usable text."""
    mode = "content_none" if content is None else "content_empty"
    log.error(
        "llm proposal batch: provider returned no content — whole batch lost",
        parse_mode=mode,
        content_type=type(content).__name__,
    )
    msg = f"LLM returned no usable content (mode={mode}, finish_reason={finish_reason})"
    raise MalformedCompletionError(msg, mode=mode)


def parse_completion(
    content: str | None,
    *,
    model: str,
    batch_size: int,
    finish_reason: str | None,
    logger: Any,
    extract_json_span: Any = _extract_json_span,
    salvage_proposals: Any = _salvage_proposals,
) -> BatchProposalResponse:
    """Apply the established straight-parse, extraction, and discard-only salvage ladder."""
    log = logger.bind(llm_model=model, batch_size=batch_size, finish_reason=finish_reason)

    if not isinstance(content, str) or not content.strip():
        _reject_contentless_completion(content, finish_reason=finish_reason, log=log)

    try:
        return BatchProposalResponse.model_validate_json(content)
    except ValidationError as first_error:
        direct_error = first_error

    span = extract_json_span(content)
    if span is not None and span != content:
        try:
            parsed = BatchProposalResponse.model_validate_json(span)
        except ValidationError:
            pass
        else:
            log.warning(
                "llm proposal batch: recovered by extracting the JSON span — provider wrapped it",
                parse_mode=_wrapping_mode(content),
                proposals=len(parsed.proposals),
                content_preview=_preview(content),
            )
            return parsed

    salvage_source = span if span is not None else content
    salvaged = salvage_proposals(salvage_source)
    if salvaged is not None:
        kept, discarded_positions = salvaged
        log.warning(
            "llm proposal batch: salvaged — some proposals were DISCARDED, not repaired",
            parse_mode="item_invalid",
            kept=len(kept),
            discarded=len(discarded_positions),
            discarded_positions=discarded_positions,
            error_types=sorted({error["type"] for error in direct_error.errors()}),
            content_preview=_preview(content),
        )
        return SalvagedBatchProposalResponse(proposals=kept, discarded_positions=discarded_positions)

    mode = _failure_mode(content, direct_error, finish_reason=finish_reason)
    log.error(
        "llm proposal batch: unparseable completion — whole batch lost",
        parse_mode=mode,
        error_types=sorted({error["type"] for error in direct_error.errors()}),
        content_len=len(content),
        content_preview=_preview(content),
    )
    msg = f"LLM completion could not be parsed (mode={mode}, finish_reason={finish_reason})"
    raise MalformedCompletionError(msg, mode=mode) from direct_error


def clamp_confidence(value: float) -> float:
    """Clamp confidence to 0..1, treating non-finite values as untrusted."""
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))
