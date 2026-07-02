"""Tests for the control-only scheduling-ledger service (Phase 45 Plan 01, Task 2).

Covers the five service helpers:

- ``upsert_ledger_entry``     -- idempotent ON CONFLICT DO UPDATE (the WRITE hook primitive)
- ``insert_ledger_if_absent`` -- ON CONFLICT DO NOTHING (the Plan-04 backfill primitive)
- ``clear_ledger_entry``      -- DELETE by key (no-op if absent)
- ``get_ledger_rows``         -- read all rows for recovery
- ``routing_for_function``    -- agent/controller classifier (raises on unknown)

The DB-touching cases use the real PostgreSQL ``session`` fixture from
``tests/conftest.py`` (auto-marked ``integration``); ``routing_for_function`` is a pure
function tested without a DB.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.scheduling_ledger import (
    clear_ledger_entry,
    get_ledger_rows,
    insert_ledger_if_absent,
    routing_for_function,
    upsert_ledger_entry,
)


# ---------------------------------------------------------------------------
# routing_for_function (pure, no DB)
# ---------------------------------------------------------------------------


def test_routing_for_agent_function() -> None:
    assert routing_for_function("process_file") == "agent"
    assert routing_for_function("extract_file_metadata") == "agent"
    assert routing_for_function("fingerprint_file") == "agent"
    assert routing_for_function("scan_live_set") == "agent"


def test_routing_for_controller_function() -> None:
    assert routing_for_function("generate_proposals") == "controller"
    assert routing_for_function("search_tracklist") == "controller"
    assert routing_for_function("scrape_and_store_tracklist") == "controller"
    assert routing_for_function("match_tracklist_to_discogs") == "controller"


def test_routing_for_unknown_function_raises() -> None:
    with pytest.raises(ValueError, match="not a routable"):
        routing_for_function("totally_unknown_task")


# ---------------------------------------------------------------------------
# upsert / insert-if-absent / clear / read (DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates_idempotently(session) -> None:  # type: ignore[no-untyped-def]
    fid = uuid.uuid4()
    key = f"process_file:{fid}"
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs={"file_id": str(fid), "v": 1})
    await session.commit()

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one()
    assert row.function == "process_file"
    assert row.routing == "agent"
    assert row.payload == {"file_id": str(fid), "v": 1}
    first_enqueued_at = row.enqueued_at

    # A second upsert with the SAME key refreshes payload/enqueued_at -- never errors on duplicate.
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs={"file_id": str(fid), "v": 2})
    await session.commit()

    # Drop the identity-map cache (the fixture uses expire_on_commit=False) so the re-query
    # reads the freshly-updated DB row, not the stale in-memory instance.
    session.expire_all()
    rows = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalars().all()
    assert len(rows) == 1, "upsert must keep exactly one row per key"
    assert rows[0].payload == {"file_id": str(fid), "v": 2}
    assert rows[0].enqueued_at >= first_enqueued_at


@pytest.mark.asyncio
async def test_insert_if_absent_does_not_overwrite_existing(session) -> None:  # type: ignore[no-untyped-def]
    key = "generate_proposals:batch1"
    await upsert_ledger_entry(session, key=key, function="generate_proposals", kwargs={"file_ids": ["a"], "src": "hook"})
    await session.commit()

    # insert-if-absent must leave the existing (fresher hook-written) row untouched.
    await insert_ledger_if_absent(session, key=key, function="generate_proposals", kwargs={"file_ids": ["a"], "src": "backfill"})
    await session.commit()

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one()
    assert row.payload["src"] == "hook", "insert-if-absent must not overwrite the existing row"
    assert row.routing == "controller"


@pytest.mark.asyncio
async def test_insert_if_absent_inserts_when_missing(session) -> None:  # type: ignore[no-untyped-def]
    key = "search_tracklist:xyz"
    await insert_ledger_if_absent(session, key=key, function="search_tracklist", kwargs={"file_id": "xyz"})
    await session.commit()

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one()
    assert row.function == "search_tracklist"
    assert row.routing == "controller"


@pytest.mark.asyncio
async def test_clear_entry_deletes_and_is_noop_when_absent(session) -> None:  # type: ignore[no-untyped-def]
    key = "fingerprint_file:f1"
    await upsert_ledger_entry(session, key=key, function="fingerprint_file", kwargs={"file_id": "f1"})
    await session.commit()

    await clear_ledger_entry(session, key)
    await session.commit()
    assert (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none() is None

    # A second clear of an already-absent key is a clean no-op (no raise).
    await clear_ledger_entry(session, key)
    await session.commit()


@pytest.mark.asyncio
async def test_upsert_stores_and_refreshes_timeout_and_retries(session) -> None:  # type: ignore[no-untyped-def]
    """The WRITE hook captures the job's effective timeout/retries so recovery can replay the
    SAME policy. Without this, a recovered ``process_file`` (analyze) job loses its 7200s bound
    and falls back to the 600s default -- a 12x reduction that times out every long concert set.
    """
    fid = uuid.uuid4()
    key = f"process_file:{fid}"
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs={"file_id": str(fid)}, timeout=7200, retries=2)
    await session.commit()

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one()
    assert row.timeout == 7200
    assert row.retries == 2

    # A re-enqueue of the same key refreshes the policy too (ON CONFLICT DO UPDATE).
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs={"file_id": str(fid)}, timeout=3600, retries=1)
    await session.commit()
    session.expire_all()
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one()
    assert row.timeout == 3600
    assert row.retries == 1


@pytest.mark.asyncio
async def test_upsert_defaults_timeout_and_retries_to_none(session) -> None:  # type: ignore[no-untyped-def]
    """timeout/retries are OPTIONAL: a caller that omits them stores NULL, and replay then falls
    back to the queue's before_enqueue defaults exactly as before (backward-compatible)."""
    await upsert_ledger_entry(session, key="search_tracklist:s1", function="search_tracklist", kwargs={"file_id": "s1"})
    await session.commit()

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == "search_tracklist:s1"))).scalar_one()
    assert row.timeout is None
    assert row.retries is None


@pytest.mark.asyncio
async def test_insert_if_absent_stores_timeout_and_retries(session) -> None:  # type: ignore[no-untyped-def]
    """The Plan-04 backfill primitive also carries timeout/retries through from the live broker blob."""
    await insert_ledger_if_absent(session, key="process_file:bf1", function="process_file", kwargs={"file_id": "bf1"}, timeout=7200, retries=2)
    await session.commit()

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == "process_file:bf1"))).scalar_one()
    assert row.timeout == 7200
    assert row.retries == 2


@pytest.mark.asyncio
async def test_get_ledger_rows_returns_all(session) -> None:  # type: ignore[no-untyped-def]
    await upsert_ledger_entry(session, key="process_file:1", function="process_file", kwargs={"file_id": "1"})
    await upsert_ledger_entry(session, key="generate_proposals:2", function="generate_proposals", kwargs={"file_ids": ["2"]})
    await session.commit()

    rows = await get_ledger_rows(session)
    keys = {r.key for r in rows}
    assert {"process_file:1", "generate_proposals:2"} <= keys
