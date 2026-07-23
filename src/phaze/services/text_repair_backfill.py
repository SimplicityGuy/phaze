"""Idempotent backfill of ``files.original_filename_repaired`` for pre-phaze-x4ux rows.

``routers/agent_files.py::upsert_files`` populates ``original_filename_repaired`` at ingest for
every NEW/rescanned row (migration ``045``). Rows that already existed before that migration
shipped carry ``NULL`` there -- not because no repair was needed, but because the column did not
exist yet when they were written. This module closes that gap over the existing archive.

Why a Python maintenance routine and not a SQL data migration (unlike, e.g., migration 042's
``redrive_attempt`` backfill): the repair itself is the ``repair_mojibake`` codec round trip,
which has no straightforward Postgres-native equivalent that also handles per-row decode failure
gracefully (embedding it would mean re-implementing and re-testing the same logic in PL/pgSQL,
risking divergence from the one tested Python implementation). Postgres DDL/DML stays limited to
adding the column (migration ``045``); this is the one-time (or periodic, if run again) data fill.

Idempotent by construction: only rows where ``original_filename_repaired IS NULL`` are selected,
and every row visited is unconditionally set to ``repair_mojibake(original_filename)`` (even when
that equals the original text) -- so a visited row is NEVER re-selected on a later run. Safe to
run against a live, actively-ingesting archive (each batch is its own committed transaction), and
safe to re-run after a crash (whatever was already committed simply is not re-selected).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.services.text_repair import repair_mojibake


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_DEFAULT_BATCH_SIZE = 500


async def backfill_repaired_filenames(session: AsyncSession, *, batch_size: int = _DEFAULT_BATCH_SIZE) -> int:
    """Populate ``original_filename_repaired`` for every ``FileRecord`` row that still has it NULL.

    Processes rows in committed batches of *batch_size* so a large archive does not hold one huge
    transaction open. Returns the total number of rows visited (== rows updated, since every
    visited row is set — see module docstring on idempotency).
    """
    total_visited = 0
    while True:
        stmt = select(FileRecord).where(FileRecord.original_filename_repaired.is_(None)).order_by(FileRecord.id).limit(batch_size)
        batch = (await session.execute(stmt)).scalars().all()
        if not batch:
            break
        for record in batch:
            record.original_filename_repaired = repair_mojibake(record.original_filename)
        await session.commit()
        total_visited += len(batch)
    return total_visited
