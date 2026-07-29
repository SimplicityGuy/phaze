"""phaze-r7j9: the asyncpg 32767-bind-parameter cap sweep for pipeline.py's failed-set clears.

``retry_analysis_failed`` (the operator's bulk 'Retry failed' button) and ``trigger_backfill_cloud``
both used to clear ``analysis.failed_at`` for a client-derived id list via a bare
``AnalysisResult.file_id.in_(ids)``. SQLAlchemy renders that as an expanding IN -- one bind
parameter PER id -- and PostgreSQL's extended-protocol Bind message carries the argument count as a
signed int16, capping any single statement at 32767 parameters. This repo's own incident history
(``.planning/STATE.md``, ``tasks/reenqueue.py``) documents ~44.5K-file failure sets, well past that
ceiling: at that scale the UPDATE raised at the driver before anything committed, so the bulk retry
-- the operator's ONLY bulk re-drive path for exactly that scale -- 500'd deterministically and
permanently.

The fix reuses the repo's established idiom (``tasks/reenqueue.py::_fids_scope``): bind the WHOLE id
list as ONE ``uuid[]`` (or ``text[]``, for the scheduling-ledger key list) Postgres array parameter
via ``_analysis_file_ids_scope`` / ``_ledger_keys_scope``, which sidesteps the per-element cap
regardless of list size. These tests exercise the real asyncpg driver with a list comfortably past
the 32767-parameter break, proving the statement can be bound and executed -- the exact wire-level
step the original bug crashed at, before any row match happens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select, update

from phaze.models.analysis import AnalysisResult
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.pipeline import _analysis_file_ids_scope, _ledger_keys_scope


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration

# Comfortably past asyncpg's 32767-parameter int16 wire cap -- an unchunked `.in_(ids)` UPDATE over
# this many uuid binds raises `InterfaceError: the number of query arguments cannot exceed 32767`
# before the statement is even sent. This is well below the ~44.5K figure the repo's own incident
# history documents, so it is a conservative (not merely a bare-minimum) regression size.
_BIND_LIMIT_BREAK_IDS = 40_000


@pytest.mark.asyncio
async def test_analysis_file_ids_scope_survives_past_bind_param_cap(session: AsyncSession) -> None:
    """An id list past the bind-parameter cap must not raise (phaze-r7j9 regression).

    No matching rows exist in the empty test schema for these random ids -- the point of the test
    is that the statement is BOUND and EXECUTED at all (the driver-level step the pre-fix `.in_()`
    crashed at), not that it matches anything.
    """
    file_ids = [uuid.uuid4() for _ in range(_BIND_LIMIT_BREAK_IDS)]

    result = await session.execute(
        update(AnalysisResult).where(_analysis_file_ids_scope(file_ids, "ids")).values(failed_at=None, error_message=None),
    )

    assert result.rowcount == 0
    await session.rollback()


@pytest.mark.asyncio
async def test_analysis_file_ids_scope_matches_only_the_bound_ids(session: AsyncSession) -> None:
    """Functional sanity alongside the scale test: the array-bind ``= ANY`` selects exactly the
    rows whose ids were passed, same as a (small-scale) ``.in_(...)`` would -- the fix changes the
    wire encoding, not the query semantics."""
    from phaze.models.file import FileRecord

    def _make_file() -> FileRecord:
        uid = uuid.uuid4()
        return FileRecord(
            agent_id="test-fileserver",
            id=uid,
            sha256_hash=uid.hex,
            original_path=f"/music/{uid.hex}.mp3",
            original_filename=f"{uid.hex}.mp3",
            current_path=f"/music/{uid.hex}.mp3",
            file_type="mp3",
            file_size=1000,
        )

    matched_file = _make_file()
    other_file = _make_file()
    session.add_all([matched_file, other_file])
    await session.flush()
    session.add_all(
        [
            AnalysisResult(id=uuid.uuid4(), file_id=matched_file.id, failed_at=None, error_message=None),
            AnalysisResult(id=uuid.uuid4(), file_id=other_file.id, failed_at=None, error_message=None),
        ],
    )
    await session.flush()

    result = await session.execute(select(AnalysisResult.file_id).where(_analysis_file_ids_scope([matched_file.id], "ids")))
    assert result.scalars().all() == [matched_file.id]


@pytest.mark.asyncio
async def test_ledger_keys_scope_survives_past_bind_param_cap(session: AsyncSession) -> None:
    """The SchedulingLedger key-list companion of the same fix, over a ``text[]`` array bind."""
    ledger_keys = [f"process_file:{uuid.uuid4()}" for _ in range(_BIND_LIMIT_BREAK_IDS)]

    result = await session.execute(select(SchedulingLedger.key).where(_ledger_keys_scope(ledger_keys, "keys")))

    assert result.scalars().all() == []
