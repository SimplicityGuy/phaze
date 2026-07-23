"""Tests for the control-only scheduling-ledger service (Phase 45 Plan 01, Task 2).

Covers the five service helpers:

- ``upsert_ledger_entry``     -- idempotent ON CONFLICT DO UPDATE (the WRITE hook primitive)
- ``insert_ledger_if_absent`` -- ON CONFLICT DO NOTHING (the Plan-04 backfill primitive)
- ``clear_ledger_entry``      -- DELETE by key, GUARDED against a same-key re-enqueue race
  (phaze-3yln) -- no-op if absent OR currently owned by a live re-enqueue
- ``get_ledger_rows``         -- read all rows for recovery
- ``routing_for_function``    -- agent/controller classifier (raises on unknown)

The DB-touching cases use the real PostgreSQL ``session`` fixture from
``tests/conftest.py`` (auto-marked ``integration``); ``routing_for_function`` is a pure
function tested without a DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

import pytest
from sqlalchemy import select, text, update

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
async def test_upsert_bumps_updated_at_not_created_at(session) -> None:  # type: ignore[no-untyped-def]
    """phaze-7634: a re-enqueue (conflicting upsert) bumps SchedulingLedger.updated_at; created_at pinned.

    SchedulingLedger also carries TimestampMixin's updated_at (distinct from the business-facing
    enqueued_at already covered by ``test_upsert_inserts_then_updates_idempotently``). Same defect
    class as phaze-c8nz: `on_conflict_do_update`'s `set_` clause used to omit `updated_at`, and
    `TimestampMixin.updated_at`'s ORM `onupdate` hook never fires for a Core upsert. Backdate both
    columns, re-upsert, and assert updated_at moves forward while created_at is untouched.
    """
    fid = uuid.uuid4()
    key = f"process_file:{fid}"
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs={"file_id": str(fid), "v": 1})
    await session.commit()

    # Backdate created_at/updated_at directly (bypassing the ORM/onupdate hook) to a fixed point
    # well in the past. scheduling_ledger.created_at/updated_at are TIMESTAMP WITHOUT TIME ZONE
    # columns -- use a naive UTC value so asyncpg doesn't reject the aware/naive mismatch.
    outage_time = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=12)
    await session.execute(update(SchedulingLedger).where(SchedulingLedger.key == key).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    before_reupsert = datetime.now(UTC).replace(tzinfo=None)

    await upsert_ledger_entry(session, key=key, function="process_file", kwargs={"file_id": str(fid), "v": 2})
    await session.commit()

    session.expire_all()
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one()
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_reupsert - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


@pytest.mark.asyncio
async def test_upsert_preserves_redrive_attempt_across_the_crash_window(session) -> None:  # type: ignore[no-untyped-def]
    """The WRITE-hook upsert must NEVER clobber the dedicated ``redrive_attempt`` column (phaze-2jl1 / phaze-y0j0).

    This is the invariant that makes the push/S3 re-drive budget crash-safe: the ``before_enqueue``
    hook rewrites ``payload`` wholesale from its OWN session and commits BEFORE the re-drive handler
    stamps the incremented counter. If the counter lived in ``payload`` a crash in that window would
    reset it to 0. Because it lives in ``redrive_attempt`` (absent from the hook's ON CONFLICT DO
    UPDATE set-list), the hook's upsert leaves it at its prior value -- so a crash between the two
    commits un-increments the budget at worst, never zeroes it.
    """
    fid = uuid.uuid4()
    key = f"s3_upload:{fid}"
    # A prior re-drive has advanced the bounded counter to 2 (in the dedicated column, not payload).
    await upsert_ledger_entry(session, key=key, function="s3_upload", kwargs={"file_id": str(fid), "part_urls": ["stale"]})
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one()
    row.redrive_attempt = 2
    await session.commit()

    # The enqueue hook fires again and rewrites payload WHOLESALE (fresh part_urls, no counter key) --
    # exactly what apply_deterministic_key does from its own session on a re-drive enqueue.
    await upsert_ledger_entry(session, key=key, function="s3_upload", kwargs={"file_id": str(fid), "part_urls": ["fresh"]})
    await session.commit()

    session.expire_all()
    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one()
    assert row.payload == {"file_id": str(fid), "part_urls": ["fresh"]}, "hook rewrites payload wholesale"
    assert row.redrive_attempt == 2, "the bounded re-drive counter must survive the payload-clobbering upsert (crash window)"


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


# ---------------------------------------------------------------------------
# clear_ledger_entry ownership guard (phaze-3yln): a same-key re-enqueue race must win
# ---------------------------------------------------------------------------


async def _seed_saq_jobs_table(session) -> None:  # type: ignore[no-untyped-def]
    """Ensure ``saq_jobs`` exists with SAQ's canonical postgres schema (mirrors
    ``test_ledger_backfill.py``'s ``_seed_saq_jobs`` -- MUST be the full schema, not a minimal
    stand-in: another integration test elsewhere in this suite's run may already have created the
    REAL ``saq_jobs`` via SAQ's own ``init_db()`` before this test runs, and a narrower
    ``CREATE TABLE IF NOT EXISTS`` would silently no-op against it, leaving ``job``/``queue`` NOT
    NULL and rejecting a 2-column INSERT).

    Safe either way: if ``saq_jobs`` does not yet exist, this ``CREATE TABLE`` runs inside this
    suite's per-test outer transaction (D-07) and is fully undone at teardown; if it already exists
    (a real, durably-committed SAQ-managed table from an earlier test), this is a no-op and every
    row THIS test inserts still rolls back with the rest of the per-test transaction.
    """
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS saq_jobs (
                key TEXT PRIMARY KEY,
                lock_key SERIAL NOT NULL,
                job BYTEA NOT NULL,
                queue TEXT NOT NULL,
                status TEXT NOT NULL,
                priority SMALLINT NOT NULL DEFAULT 0,
                group_key TEXT,
                scheduled BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
                expire_at BIGINT
            )
            """
        )
    )


async def _seed_saq_job_row(session, *, key: str, status: str) -> None:  # type: ignore[no-untyped-def]
    """Insert one minimal-but-schema-valid ``saq_jobs`` row for ``key`` at ``status``."""
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status) VALUES (:key, :job, :queue, :status)"),
        {"key": key, "job": b"{}", "queue": "phaze-3yln-test-queue", "status": status},
    )


@pytest.mark.asyncio
async def test_clear_entry_survives_a_racing_re_enqueue_with_a_live_saq_job(session) -> None:  # type: ignore[no-untyped-def]
    """The core phaze-3yln regression: reproduces [old job goes terminal] -> [same-key re-enqueue
    upserts a FRESH ledger row + a LIVE saq_jobs row] -> [old job's finally-block clear runs].

    Before the fix this was an unconditional ``DELETE ... WHERE key = :key``, so the old job's clear
    deleted the NEW job's just-upserted ledger row -- leaving a live queued job with no ledger row,
    invisible to ``recover_orphaned_work`` after a genuine queue loss. The guard must make the clear
    a no-op whenever a live (queued/active) saq_jobs row for the SAME key exists.
    """
    key = "process_file:racer"
    await _seed_saq_jobs_table(session)

    # The re-enqueue's WRITE hook already ran: a fresh ledger row for K is upserted...
    await upsert_ledger_entry(session, key=key, function="process_file", kwargs={"file_id": "racer", "generation": "new"})
    # ...and its saq_jobs row is already live (queued) -- the re-enqueue's INSERT ON CONFLICT DO
    # UPDATE landed before the old job's clear below.
    await _seed_saq_job_row(session, key=key, status="queued")
    await session.commit()

    # The OLD job's after_process finally-block clear races in NOW, still holding only the stale key.
    await clear_ledger_entry(session, key)
    await session.commit()

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()
    assert row is not None, "a live re-enqueue's ledger row must survive the old job's racing clear"
    assert row.payload["generation"] == "new"


@pytest.mark.asyncio
async def test_clear_entry_survives_when_saq_job_is_active(session) -> None:  # type: ignore[no-untyped-def]
    """``active`` (not just ``queued``) is also a LIVE status recovery would treat as owning the key."""
    key = "fingerprint_file:racer-active"
    await _seed_saq_jobs_table(session)
    await upsert_ledger_entry(session, key=key, function="fingerprint_file", kwargs={"file_id": "racer-active"})
    await _seed_saq_job_row(session, key=key, status="active")
    await session.commit()

    await clear_ledger_entry(session, key)
    await session.commit()

    assert (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_clear_entry_clears_when_saq_job_is_terminal(session) -> None:  # type: ignore[no-untyped-def]
    """The normal (no race) case: the finishing job's OWN saq_jobs row is already terminal
    (``complete``), so no live row exists for the key and the clear proceeds exactly as before."""
    key = "extract_file_metadata:normal"
    await _seed_saq_jobs_table(session)
    await upsert_ledger_entry(session, key=key, function="extract_file_metadata", kwargs={"file_id": "normal"})
    await _seed_saq_job_row(session, key=key, status="complete")
    await session.commit()

    await clear_ledger_entry(session, key)
    await session.commit()

    assert (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_clear_entry_clears_when_saq_jobs_table_present_but_key_absent(session) -> None:  # type: ignore[no-untyped-def]
    """A ``saq_jobs`` table exists (unlike the degrade-path test above) but has NO row for this
    key at all -- also not live, so the guarded clear still proceeds."""
    key = "s3_upload:no-broker-row"
    await _seed_saq_jobs_table(session)
    await upsert_ledger_entry(session, key=key, function="s3_upload", kwargs={"file_id": "no-broker-row"})
    await session.commit()

    await clear_ledger_entry(session, key)
    await session.commit()

    assert (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none() is None


class _RaisingLivenessProbeSession:
    """AsyncSession stand-in whose guarded-clear SAVEPOINT read always raises -- models a missing
    ``saq_jobs`` table (a pre-migration env) DETERMINISTICALLY, independent of the ambient test
    database's history. Once ANY real integration test in this suite's run has created the REAL
    ``saq_jobs`` table (a durably-committed, non-ORM table that outlives any single test's rolled-
    back transaction -- see ``test_ledger_backfill.py``), the ``session``-fixture-based absent-table
    case above stops exercising this branch. Mirrors ``test_ledger_backfill.py``'s ``_RaisingSession``.
    """

    def __init__(self) -> None:
        self.fallback_deletes: list[Any] = []

    def begin_nested(self) -> Any:
        class _Nested:
            async def __aenter__(self_inner) -> Any:
                return self_inner

            async def __aexit__(self_inner, *_exc: object) -> bool:
                return False

        return _Nested()

    async def execute(self, statement: Any, *_a: Any, **_kw: Any) -> Any:
        from sqlalchemy.sql.elements import TextClause

        if isinstance(statement, TextClause):
            raise RuntimeError('relation "saq_jobs" does not exist')
        self.fallback_deletes.append(statement)
        return None


@pytest.mark.asyncio
async def test_clear_entry_falls_back_to_unconditional_delete_when_liveness_probe_raises() -> None:
    """Deterministic degrade-path coverage (phaze-3yln): the guarded SAVEPOINT read raises, so the
    nested scope rolls back alone and the pre-fix unconditional delete-by-key fires as a fallback --
    never leaving a row permanently un-clearable just because the liveness probe itself failed."""
    session = _RaisingLivenessProbeSession()

    await clear_ledger_entry(session, "process_file:degrade-path")  # type: ignore[arg-type]

    assert len(session.fallback_deletes) == 1


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
