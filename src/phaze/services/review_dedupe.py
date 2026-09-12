"""Read model and card formatting for the Dedupe operator workspace."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


class DedupeReviewReader(Protocol):
    """Consumer-facing port for paginated duplicate-group review reads."""

    async def get_dedupe_groups(self, session: AsyncSession, *, limit: int, offset: int = 0) -> list[dict[str, Any]]: ...


def _format_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "unknown size"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _format_quality(file_dict: dict[str, Any]) -> str:
    size = _format_size(file_dict.get("file_size"))
    bitrate = file_dict.get("bitrate")
    if bitrate:
        return f"{bitrate // 1000} kbps · {size}"
    return size


def build_dupe_group_card(group: dict[str, Any]) -> dict[str, Any]:
    """Map a scored duplicate group to the keeper-select card's stable plain-dict shape."""
    canonical_id = group["canonical_id"]
    files = group["files"]
    return {
        "sha256_hash": group["sha256_hash"],
        "group_name": Path(files[0]["original_path"]).name if files else group["sha256_hash"][:12],
        "count": len(files),
        "truncated": group.get("truncated", False),
        "rationale": group.get("rationale", "highest-quality ranking"),
        "files": [
            {
                "id": file["id"],
                "name": Path(file["original_path"]).name,
                "path": file["original_path"],
                "quality": _format_quality(file),
                "duration": file.get("duration"),
                "tag_label": file.get("tag_label", "None"),
                "keeper": file["id"] == canonical_id,
            }
            for file in files
        ],
    }


def dedupe_subcount_text(rendered: int, total: int) -> str:
    """Render the exact workspace subcount, including partial-page disclosure."""
    if rendered >= total:
        noun = "group" if total == 1 else "groups"
        return f"{total} duplicate {noun} · pick the keeper, others archived"
    return f"Showing {rendered} of {total} duplicate groups · pick the keeper, others archived"


class SqlDedupeReviewReader:
    """SQLAlchemy-backed implementation of :class:`DedupeReviewReader`."""

    def __init__(
        self,
        *,
        find_duplicate_groups_with_metadata: Callable[..., Awaitable[list[dict[str, Any]]]],
        score_group: Callable[[dict[str, Any]], Any],
    ) -> None:
        self._find_duplicate_groups_with_metadata = find_duplicate_groups_with_metadata
        self._score_group = score_group

    async def get_dedupe_groups(self, session: AsyncSession, *, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        try:
            async with session.begin_nested():
                groups = await self._find_duplicate_groups_with_metadata(session, limit=limit, offset=offset)
                cards: list[dict[str, Any]] = []
                for group in groups:
                    self._score_group(group)
                    cards.append(build_dupe_group_card(group))
                return cards
        except Exception:
            logger.warning("dedupe_groups_degraded", exc_info=True)
            return []
