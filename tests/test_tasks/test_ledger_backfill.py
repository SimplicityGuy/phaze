"""Tests for the Phase-45 Plan-04 one-time startup ledger backfill (locked decision #3).

``backfill_ledger_from_saq_jobs(session)`` seeds the durable ``scheduling_ledger`` from the
live ``saq_jobs`` queued/active rows at first boot after migration 022, so in-flight work
(and any residual incident cohort still present) stays recoverable immediately -- there is no
blind window between the migration landing and the ``before_enqueue`` WRITE hook populating
the ledger. It is a CONTROL-SIDE runtime reconcile, NEVER an Alembic data step.

Contract (must_haves):

  - deserializes each queued/active ``saq_jobs`` blob (the SAQ default ``json.dumps`` serializer,
    so the blob is a JSON object with top-level ``function`` / ``kwargs`` / ``key``), mirroring
    ``pipeline._job_started_ms`` -- NO ``saq.Job`` construction,
  - only KEYED rows (``function`` in ``_KEY_BUILDERS``) are seeded; a random-key / non-pipeline
    row is SKIPPED,
  - idempotent (``insert_ledger_if_absent`` == ON CONFLICT (key) DO NOTHING): a second run over
    the same broker state inserts 0,
  - a row already written by the WRITE hook is NOT overwritten (DO NOTHING preserves the fresher
    row),
  - degrades safely: a missing ``saq_jobs`` table (pre-migration env) or an unparseable blob is
    tolerated (SAVEPOINT / skip-the-row) and NEVER raises -- boot must not abort.

The two pure-parse unit tests below need no DB (they exercise the blob-deserialize tolerance).
The end-to-end seed/idempotency/no-overwrite cases run against the real ``saq_jobs`` broker under
``just integration-test`` (``@pytest.mark.integration``; skips when Postgres is unavailable).
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.scheduling_ledger import upsert_ledger_entry
from phaze.tasks.reenqueue import _parse_job_blob, backfill_ledger_from_saq_jobs


# Raw libpq broker DSN (NOT the +asyncpg dialect form psycopg3 cannot parse) + the SQLAlchemy
# +asyncpg form for the AsyncSession the backfill reads/writes through.
_RAW_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
_SA_DSN = (os.environ.get("TEST_DATABASE_URL") or _RAW_DSN).replace("postgresql://", "postgresql+asyncpg://")


# --- Pure blob-parse tolerance (no DB) -------------------------------------------------


class _RaisingSession:
    """Minimal AsyncSession stand-in whose SAVEPOINT read raises (a missing saq_jobs table).

    ``begin_nested()`` returns an async-context-manager whose body raises on the SELECT, so the
    backfill's degrade path (return empty tally, never re-raise) is exercised WITHOUT a real DB.
    """

    def begin_nested(self) -> Any:
        session = self

        class _Nested:
            async def __aenter__(self_inner) -> Any:
                return self_inner

            async def __aexit__(self_inner, *_exc: object) -> bool:
                return False

        _ = session
        return _Nested()

    async def execute(self, *_a: Any, **_kw: Any) -> Any:
        raise RuntimeError('relation "saq_jobs" does not exist')


@pytest.mark.asyncio
async def test_missing_saq_jobs_table_degrades_to_no_op() -> None:
    """A session whose ``saq_jobs`` read raises yields an empty tally, never raises (T-45-14).

    Models a pre-migration env where ``saq_jobs`` does not exist: the SELECT inside the backfill's
    SAVEPOINT errors, the nested scope rolls back alone, and the function returns
    ``{"inserted": 0, "skipped": 0}`` so a pre-migration boot cannot abort.
    """
    result = await backfill_ledger_from_saq_jobs(_RaisingSession())  # type: ignore[arg-type]
    assert result == {"inserted": 0, "skipped": 0}


@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        (b'{"function": "process_file", "key": "process_file:1"}', {"function": "process_file", "key": "process_file:1"}),
        ('{"function": "scan_live_set"}', {"function": "scan_live_set"}),
        ({"function": "already_a_dict"}, {"function": "already_a_dict"}),  # pre-decoded passthrough
        (b"\xff\xfenot json", None),  # not JSON -> None
        (b'"a json string but not a dict"', None),  # JSON, not a dict -> None
        (b"[1, 2, 3]", None),  # JSON array, not a dict -> None
        (None, None),  # not str/bytes/dict -> None
    ],
)
def test_parse_job_blob_tolerates_garbage(blob: object, expected: dict[str, Any] | None) -> None:
    """``_parse_job_blob`` mirrors ``pipeline._job_started_ms``: JSON dict in, dict or None out."""
    assert _parse_job_blob(blob) == expected


# --- Loop classification (fake session, no DB) -----------------------------------------


class _SeededSession:
    """AsyncSession stand-in whose SAVEPOINT read returns canned ``(job, key)`` rows.

    The SELECT (a ``TextClause``) yields the seeded rows; any other execute (the
    ``insert_ledger_if_absent`` INSERT) records the row's key into ``inserted_keys`` and is a no-op.
    Lets the backfill loop's keyed/skip/bad-blob/missing-function branches run with NO real DB.
    """

    def __init__(self, rows: list[tuple[object, object]]) -> None:
        self._rows = rows
        self.inserted_keys: list[str] = []
        self.inserted_params: list[dict[str, Any]] = []

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
            rows = self._rows

            class _Result:
                def all(self_inner) -> list[tuple[object, object]]:
                    return rows

            return _Result()
        # The insert_ledger_if_absent INSERT: capture the bound key, no-op the write.
        with contextlib.suppress(Exception):
            params = statement.compile().params
            self.inserted_keys.append(params["key_m0"])
            self.inserted_params.append(params)
        return None


@pytest.mark.asyncio
async def test_backfill_loop_seeds_keyed_skips_everything_else() -> None:
    """The loop seeds keyed rows and skips random-key / bad-blob / missing-function rows (no DB).

    Drives ``backfill_ledger_from_saq_jobs`` over a fake session returning a mix of rows and asserts
    the tally + which keys reached the ledger INSERT, covering each classification branch:

      - keyed (function in _KEY_BUILDERS, blob carries it) -> inserted,
      - keyed via key-prefix fallback (blob lacks ``function``) -> inserted,
      - random-key (function not keyed) -> skipped,
      - unparseable blob -> skipped,
      - blob with no function AND a non-keyed key prefix -> skipped.
    """
    fid_a, fid_b, fid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key_a = f"process_file:{fid_a}"
    key_b = f"fingerprint_file:{fid_b}"  # blob omits "function" -> key-prefix fallback
    key_random = f"some_unkeyed:{uuid.uuid4().hex}"

    rows: list[tuple[object, object]] = [
        (json.dumps({"function": "process_file", "key": key_a, "kwargs": {"file_id": str(fid_a)}}).encode(), key_a),
        (json.dumps({"key": key_b, "kwargs": {"file_id": str(fid_b)}}).encode(), key_b),  # no function -> fallback
        (json.dumps({"function": "some_unkeyed", "key": key_random, "kwargs": {}}).encode(), key_random),  # not keyed
        (b"\xff\xfegarbage", f"process_file:{fid_c}"),  # unparseable blob
        (json.dumps({"kwargs": {}}).encode(), "no_colon_prefix"),  # no function + non-keyed key
    ]
    session = _SeededSession(rows)

    tally = await backfill_ledger_from_saq_jobs(session)  # type: ignore[arg-type]

    assert tally == {"inserted": 2, "skipped": 3}, tally
    assert set(session.inserted_keys) == {key_a, key_b}


@pytest.mark.asyncio
async def test_backfill_carries_timeout_and_retries_from_blob() -> None:
    """The SAQ job blob serializes top-level ``timeout`` / ``retries`` (Job dataclass fields), so
    the backfill seeds them into the ledger -- giving even the in-flight transition cohort the
    correct replay policy (the live backlog was enqueued with timeout=7200 already in the blob)."""
    fid = uuid.uuid4()
    key = f"process_file:{fid}"
    rows: list[tuple[object, object]] = [
        (json.dumps({"function": "process_file", "key": key, "kwargs": {"file_id": str(fid)}, "timeout": 7200, "retries": 2}).encode(), key),
    ]
    session = _SeededSession(rows)

    tally = await backfill_ledger_from_saq_jobs(session)  # type: ignore[arg-type]

    assert tally == {"inserted": 1, "skipped": 0}, tally
    params = session.inserted_params[0]
    assert params["timeout_m0"] == 7200
    assert params["retries_m0"] == 2


@pytest.mark.asyncio
async def test_backfill_loop_tolerates_non_dict_kwargs() -> None:
    """A keyed row whose ``kwargs`` is not a dict is seeded with empty kwargs (defensive)."""
    fid = uuid.uuid4()
    key = f"scan_live_set:{fid}"
    rows: list[tuple[object, object]] = [(json.dumps({"function": "scan_live_set", "key": key, "kwargs": "oops"}).encode(), key)]
    session = _SeededSession(rows)

    tally = await backfill_ledger_from_saq_jobs(session)  # type: ignore[arg-type]

    assert tally == {"inserted": 1, "skipped": 0}, tally
    assert session.inserted_keys == [key]


# --- Integration: real saq_jobs broker -------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_seeds_keyed_skips_random_is_idempotent_and_no_overwrite() -> None:
    """End-to-end: seed keyed + random-key + bad blobs; backfill twice; assert the full contract.

    Self-contained (no shared integration fixtures): probe Postgres, create a fresh
    ``scheduling_ledger`` (the ORM table) + seed ``saq_jobs`` rows directly via libpq, then drive
    the backfill through an AsyncSession on the SAME DB. Asserts:

      - a KEYED queued row (``process_file:<id>``) is seeded once,
      - a KEYED active row (``extract_file_metadata:<id>``) is seeded once,
      - a RANDOM-key row (function not in ``_KEY_BUILDERS``) is SKIPPED (no ledger row),
      - an UNPARSEABLE blob skips only that row (does not abort the batch),
      - a TERMINAL (complete) keyed row is NOT seeded (only queued/active are live),
      - a second run inserts 0 (idempotent),
      - a row already present from the WRITE hook is NOT overwritten (DO NOTHING).

    Skips when Postgres is unavailable; cleans up its own rows.
    """
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(_RAW_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    engine = create_async_engine(_SA_DSN)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Unique queue name so we only ever touch + clean up our own saq_jobs rows.
    queue_name = f"itest-backfill-{uuid.uuid4().hex[:8]}"

    keyed_queued_id = uuid.uuid4()
    keyed_active_id = uuid.uuid4()
    keyed_complete_id = uuid.uuid4()
    keyed_queued_key = f"process_file:{keyed_queued_id}"
    keyed_active_key = f"extract_file_metadata:{keyed_active_id}"
    keyed_complete_key = f"fingerprint_file:{keyed_complete_id}"
    random_key = f"some_unkeyed_task:{uuid.uuid4().hex}"
    bad_blob_key = f"process_file:{uuid.uuid4()}"

    def _blob(function: str, key: str, file_id: uuid.UUID) -> str:
        """A minimal SAQ-shaped JSON job blob (default json.dumps serializer)."""
        return json.dumps(
            {
                "function": function,
                "key": key,
                "kwargs": {"file_id": str(file_id), "agent_id": "nox"},
                "queue": queue_name,
            }
        )

    insert_sql = "INSERT INTO saq_jobs (key, job, queue, status) VALUES (%s, %s, %s, %s)"

    async def _seed_saq_jobs() -> None:
        async with await psycopg.AsyncConnection.connect(_RAW_DSN) as conn:
            # Create saq_jobs with SAQ's CANONICAL postgres schema (saq.queue.postgres_migrations),
            # not a minimal stand-in. The table is shared across the ephemeral DB, so a 4-column
            # stub here would poison SAQ's own `init_db()` ("column scheduled does not exist") in any
            # other integration test that builds a real PostgresQueue. lock_key/priority/scheduled
            # all carry defaults, so the 4-column INSERT below still works against the full schema.
            await conn.execute(
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
            # ``backfill_ledger_from_saq_jobs`` scans saq_jobs GLOBALLY (prod semantics), and the
            # table is shared across the ephemeral DB. Other broker integration tests can leave
            # keyed queued/active rows behind under a random suite order, which would inflate the
            # ``inserted`` tally below. Truncating makes the seeded set the only live content, so the
            # exact-count assertions are deterministic regardless of test order. Safe because pytest
            # runs serially and every broker test is self-contained (creates + cleans its own rows).
            await conn.execute("TRUNCATE saq_jobs")
            rows = [
                (keyed_queued_key, _blob("process_file", keyed_queued_key, keyed_queued_id).encode(), queue_name, "queued"),
                (keyed_active_key, _blob("extract_file_metadata", keyed_active_key, keyed_active_id).encode(), queue_name, "active"),
                (keyed_complete_key, _blob("fingerprint_file", keyed_complete_key, keyed_complete_id).encode(), queue_name, "complete"),
                (random_key, _blob("some_unkeyed_task", random_key, uuid.uuid4()).encode(), queue_name, "queued"),
                (bad_blob_key, b"\xff\xfenot json at all", queue_name, "queued"),
            ]
            for row in rows:
                await conn.execute(insert_sql, row)
            await conn.commit()

    async def _cleanup_saq_jobs() -> None:
        with contextlib.suppress(Exception):
            async with await psycopg.AsyncConnection.connect(_RAW_DSN) as conn:
                await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue_name,))
                await conn.commit()

    try:
        # Ensure the ORM ledger table exists (migration may not have run on this ephemeral DB).
        async with engine.begin() as conn:
            await conn.run_sync(SchedulingLedger.__table__.create, checkfirst=True)

        # Pre-existing hook-written row for the queued keyed key, with a DISTINCT payload so we
        # can prove the backfill did NOT overwrite it (DO NOTHING preserves the fresher row).
        sentinel_payload = {"file_id": str(keyed_queued_id), "agent_id": "nox", "sentinel": "hook-written"}
        async with sm() as session:
            await upsert_ledger_entry(session, key=keyed_queued_key, function="process_file", kwargs=sentinel_payload)
            await session.commit()

        await _seed_saq_jobs()

        async with sm() as session:
            first = await backfill_ledger_from_saq_jobs(session)
            await session.commit()

        # ``inserted`` counts INSERT-if-absent CALLS for keyed rows (an UPPER bound -- the queued
        # keyed row's call is a DO-NOTHING no-op against the pre-existing hook row), so both keyed
        # rows are counted; the random-key + bad-blob + complete (terminal) rows are skipped.
        assert first["inserted"] == 2, first
        assert first["skipped"] >= 2, first

        async with sm() as session:
            rows = (await session.execute(select(SchedulingLedger))).scalars().all()
            keys = {r.key for r in rows}
            assert keyed_queued_key in keys
            assert keyed_active_key in keys
            assert keyed_complete_key not in keys  # terminal: not live, not backfilled
            assert random_key not in keys  # not a keyed function
            assert bad_blob_key not in keys  # unparseable blob skipped

            # No-overwrite: the hook-written sentinel payload survives (DO NOTHING preserved it).
            preserved = next(r for r in rows if r.key == keyed_queued_key)
            assert preserved.payload.get("sentinel") == "hook-written"
            rows_after_first = len(rows)

        # Idempotent: a second run over the same broker state changes NO ledger rows. The tally's
        # ``inserted`` re-counts the keyed CALLS (still queued/active), but every one is a DO-NOTHING
        # no-op, so the durable row COUNT is unchanged -- the true idempotency signal.
        async with sm() as session:
            await backfill_ledger_from_saq_jobs(session)
            await session.commit()
        async with sm() as session:
            rows_after_second = len((await session.execute(select(SchedulingLedger))).scalars().all())
        assert rows_after_second == rows_after_first, (rows_after_first, rows_after_second)
    finally:
        await _cleanup_saq_jobs()
        with contextlib.suppress(Exception):
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM scheduling_ledger WHERE key = ANY(:keys)"), {"keys": [keyed_queued_key, keyed_active_key]})
        await engine.dispose()
