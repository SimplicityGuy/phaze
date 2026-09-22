"""Exact, read-only selection for the phaze-hia9z one-time recovery.

The normal Recover button reconciles the entire scheduling ledger. This selector names only
unfinished analyses with a completed fine tier, no cloud owner, no live broker key, and no
executed move. The caller passes these keys to ``recover_orphaned_work(only_keys=...)`` so the
existing owner-affine replay and per-row failure isolation remain the only enqueue mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_STRANDED_KEYS_SQL = text(
    """
    SELECT l.key
    FROM analysis AS a
    JOIN scheduling_ledger AS l ON l.key = 'process_file:' || a.file_id::text
    WHERE l.function = 'process_file'
      AND a.analysis_completed_at IS NULL
      AND a.failed_at IS NULL
      AND a.fine_windows_analyzed > 0
      AND a.fine_windows_analyzed = a.fine_windows_total
      AND NOT EXISTS (SELECT 1 FROM cloud_job AS c WHERE c.file_id = a.file_id)
      AND NOT EXISTS (SELECT 1 FROM saq_jobs AS j WHERE j.key = l.key AND j.status IN ('queued', 'active'))
      AND NOT EXISTS (SELECT 1 FROM proposals AS p WHERE p.file_id = a.file_id AND p.status = 'executed')
    """
)


async def select_stranded_analysis_keys(session: AsyncSession) -> set[str]:
    """Return exact recovery keys without printing file IDs or archive paths."""
    return set((await session.scalars(_STRANDED_KEYS_SQL)).all())
