"""Shared LIVE-sentinel ``ScanBatch`` helper (phaze-tvdu1).

The partial unique index ``uq_scan_batches_agent_id_live``
(``models/scan_batch.py``) only guarantees AT MOST one LIVE sentinel batch per
agent -- it says nothing about whether one exists at all. A sentinel is only
created by whichever code path happens to call this helper; any agent
registered another way (e.g. an operator's raw ``INSERT INTO agents`` bypassing
both ``phaze agents add`` and the dev seed) previously had none, and every
watcher chunk-of-1 upsert (``routers/agent_files.py::upsert_files``, the
``body.batch_id is None`` path) crashed with ``sqlalchemy.exc.NoResultFound`` --
an uncaught 500 that dropped the file.

:func:`ensure_live_sentinel` is the ONE place that creates a LIVE sentinel:
``services/agent_bootstrap.py`` (dev seed), ``phaze.cli.add_agent`` (``phaze
agents add``), and ``routers/agent_files.py::upsert_files`` (self-heal on a
missing sentinel) all call it instead of each hand-rolling the same
conflict-safe insert.

Conflict-safe by construction: ``ON CONFLICT (agent_id) WHERE status = 'live'
DO NOTHING`` mirrors the partial unique index exactly, so two callers racing to
create the first sentinel for the same ``agent_id`` never collide -- Postgres
serializes the two INSERTs via the index's speculative-insertion lock; exactly
one wins, the other becomes a no-op, and both re-``SELECT`` the same
now-committed-or-visible row afterwards.

Does NOT commit -- the caller owns the transaction boundary (mirrors
``services/scan_deletion.py``'s convention), so this composes into an existing
unit of work instead of forcing an early commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.models.scan_batch import ScanBatch, ScanStatus


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def ensure_live_sentinel(session: AsyncSession, agent_id: str) -> uuid.UUID:
    """Idempotently ensure a LIVE sentinel ``ScanBatch`` exists for ``agent_id``; return its id.

    Always safe to call, whether or not a sentinel already exists:

    - If one already exists, the ``INSERT ... ON CONFLICT DO NOTHING`` is a no-op
      and the trailing ``SELECT`` returns the existing row's id.
    - If none exists, the insert creates it (``scan_path``/``configured_root`` set
      to the canonical ``"<watcher>"`` marker, mirroring the dev-seed's sentinel)
      and the trailing ``SELECT`` returns the id it just wrote.

    The caller commits (or not) on its own schedule -- this function issues no
    ``COMMIT`` itself.
    """
    insert_stmt = pg_insert(ScanBatch).values(
        id=uuid.uuid4(),
        agent_id=agent_id,
        scan_path="<watcher>",
        configured_root="<watcher>",
        status=ScanStatus.LIVE.value,
        total_files=0,
        processed_files=0,
    )
    insert_stmt = insert_stmt.on_conflict_do_nothing(
        index_elements=["agent_id"],
        index_where=(ScanBatch.status == ScanStatus.LIVE.value),
    )
    await session.execute(insert_stmt)

    select_stmt = select(ScanBatch.id).where(
        ScanBatch.agent_id == agent_id,
        ScanBatch.status == ScanStatus.LIVE.value,
    )
    return (await session.execute(select_stmt)).scalar_one()


__all__ = ["ensure_live_sentinel"]
