"""Explicit unavailable inventory is not successful extraction (phaze-st1ty).

Only operator reconciliation sets these markers after owning-agent absence checks. NULL means
not known unavailable, never verified readable. Consumers leave inventory, features and history
intact and count unavailable rows separately from rows still awaiting extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS
from phaze.models.file import FileRecord


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_TYPES = frozenset(extension.lstrip(".") for extension in INGESTIBLE_COMPANION_EXTENSIONS)


def available_companion_clause() -> Any:
    """Not explicitly unavailable; no assertion about filesystem presence."""
    return FileRecord.missing_at.is_(None) & FileRecord.companion_ambiguous_at.is_(None)


@dataclass(frozen=True)
class UnavailableCounts:
    """Disjoint counts: missing wins if an inconsistent row carries both markers."""

    missing: int = 0
    ambiguous: int = 0


async def count_unavailable_companions(session: AsyncSession, agent_id: str | None = None) -> UnavailableCounts:
    """Aggregate in Postgres without materializing the inventory."""
    statement = select(
        func.count().filter(FileRecord.missing_at.isnot(None)),
        func.count().filter(FileRecord.missing_at.is_(None), FileRecord.companion_ambiguous_at.isnot(None)),
    ).where(FileRecord.file_type.in_(_TYPES))
    if agent_id is not None:
        statement = statement.where(FileRecord.agent_id == agent_id)
    missing, ambiguous = (await session.execute(statement)).one()
    return UnavailableCounts(missing, ambiguous)
