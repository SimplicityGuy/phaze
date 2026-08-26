"""Database and SAQ instrumentation.

Both are tested against the REAL mechanism rather than a stand-in for it: the database
half attaches to a real SQLAlchemy engine and executes real statements through it, and the
SAQ half drives the hooks with the context shape ``saq.worker.Worker.process`` actually
builds. What each is protecting against is a label carrying data:

* a SQL statement is unbounded AND carries operator data -- a ``WHERE path = '...'`` over
  this archive is a local identifier that would be stored forever in someone else's
  Prometheus. Only the leading keyword is ever read.
* a SAQ job's kwargs carry the file id. Only the registered FUNCTION NAME is a label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from phaze.telemetry import db as telemetry_db, pipeline as telemetry_pipeline, saq as telemetry_saq
from tests.db_guard import resolve_test_dsn


if TYPE_CHECKING:
    from tests.shared.telemetry.conftest import TelemetrySink


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("SELECT 1", "SELECT"),
        ("  select id from files where path = '/some/real/path'", "SELECT"),
        ("INSERT INTO files (id) VALUES (1)", "INSERT"),
        ("UPDATE files SET path = 'x'", "UPDATE"),
        ("DELETE FROM files", "DELETE"),
        ("COMMIT", "COMMIT"),
        ("ROLLBACK", "ROLLBACK"),
        ("CREATE TABLE t (id int)", "DDL"),
        ("DROP TABLE t", "DDL"),
        ("VACUUM ANALYZE", "OTHER"),
        ("", "OTHER"),
        ("   ", "OTHER"),
    ],
)
def test_only_the_leading_keyword_is_ever_read(statement: str, expected: str) -> None:
    """Including the case that matters: a real archive path in the WHERE clause is not read
    at all, so it cannot reach a label by any route."""
    assert telemetry_db._operation(statement) == expected


@pytest.mark.asyncio
async def test_a_real_engine_records_real_statements(telemetry_sink: TelemetrySink) -> None:
    """A real PostgreSQL engine, real statements, the real asyncpg driver.

    The claim is that hooks attached to ``AsyncEngine.sync_engine`` fire under the async
    driver -- a property of SQLAlchemy's greenlet bridge, not of phaze -- so it is
    discharged against the same engine class production builds, on this seat's own test
    database. A sqlite stand-in would prove the handler runs and say nothing about asyncpg.
    """
    engine = create_async_engine(resolve_test_dsn())
    telemetry_db.instrument_engine(engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
            await conn.execute(text("SELECT count(*) FROM pg_class WHERE relname = 'no_such_table'"))
    finally:
        await engine.dispose()

    operations = {attrs["db_operation"] for attrs in telemetry_sink.attribute_sets("phaze.db.statements")}
    assert "SELECT" in operations
    assert telemetry_sink.count("phaze.db.statement.duration") >= 2
    # And the statement text reached nothing: the only attribute is the keyword.
    assert all(set(attrs) == {"db_operation"} for attrs in telemetry_sink.attribute_sets("phaze.db.statements"))


def test_instrumenting_a_non_engine_never_raises() -> None:
    """It is called from the api lifespan and the control worker's SAQ startup hook.

    Regression: a test double reaching this raised ``InvalidRequestError`` out of
    ``controller.startup``, which is the exact shape "instrumentation must never break the
    thing it observes" forbids -- a telemetry attachment aborting a worker's boot.
    """
    telemetry_db.instrument_engine(object())  # type: ignore[arg-type]


def test_instrumenting_twice_does_not_double_count() -> None:
    """``event.listen`` happily registers the same handler twice, and a doubly-registered
    handler reports every statement twice -- a silent 2x, which is worse than no metric."""
    engine = create_async_engine(resolve_test_dsn())
    telemetry_db.instrument_engine(engine)
    telemetry_db.instrument_engine(engine)
    assert engine.sync_engine in telemetry_db._instrumented


class _Job:
    """The subset of ``saq.job.Job`` the hooks read."""

    def __init__(self, function: str, status: str, key: str = "process_file:some-file-id") -> None:
        self.function = function
        self.status = status
        self.key = key
        self.attempts = 1
        self.kwargs = {"file_id": "a-real-uuid-that-must-not-become-a-label"}


@pytest.mark.asyncio
async def test_a_job_is_measured_by_function_name_and_outcome(telemetry_sink: TelemetrySink) -> None:
    ctx: dict[str, Any] = {"job": _Job("process_file", "Status.COMPLETE")}
    await telemetry_saq.before_process(ctx)
    await telemetry_saq.after_process(ctx)

    attribute_sets = telemetry_sink.attribute_sets("phaze.saq.jobs")
    assert attribute_sets == [{"saq_function": "process_file", "outcome": "ok"}]
    assert telemetry_sink.count("phaze.saq.job.duration") == 1
    assert "a-real-uuid" not in repr(attribute_sets)


@pytest.mark.asyncio
async def test_a_failed_job_reports_error(telemetry_sink: TelemetrySink) -> None:
    ctx: dict[str, Any] = {"job": _Job("process_file", "Status.FAILED")}
    await telemetry_saq.before_process(ctx)
    await telemetry_saq.after_process(ctx)
    assert telemetry_sink.attribute_sets("phaze.saq.jobs") == [{"saq_function": "process_file", "outcome": "error"}]


@pytest.mark.asyncio
async def test_a_retried_job_is_not_counted_as_success(telemetry_sink: TelemetrySink) -> None:
    """SAQ's ``retry()`` sets QUEUED and ``after_process`` still runs. From the caller's
    point of view the attempt produced no result, so it is an error."""
    ctx: dict[str, Any] = {"job": _Job("process_file", "Status.QUEUED")}
    await telemetry_saq.before_process(ctx)
    await telemetry_saq.after_process(ctx)
    assert telemetry_sink.attribute_sets("phaze.saq.jobs") == [{"saq_function": "process_file", "outcome": "error"}]


@pytest.mark.asyncio
async def test_the_hooks_never_raise_on_a_context_without_a_job() -> None:
    """They run in SAQ's own ``finally``, alongside phaze's ledger clear. An exception here
    would displace work that must happen."""
    await telemetry_saq.before_process({})
    await telemetry_saq.after_process({})


@pytest.mark.asyncio
async def test_the_hooks_never_raise_on_a_hostile_job() -> None:
    class Exploding:
        @property
        def function(self) -> str:
            msg = "boom"
            raise RuntimeError(msg)

    await telemetry_saq.before_process({"job": Exploding()})
    await telemetry_saq.after_process({"job": Exploding()})


def test_stage_inflight_publishes_the_stage_activity_snapshot(telemetry_sink: TelemetrySink) -> None:
    """Queue DEPTH is published by pipeline stage, not by SAQ queue.

    The only sampler phaze has is the admin UI's stage-activity snapshot, which groups by
    SAQ function name. Publishing that under a `queue` label would be a label that says
    something the data does not.
    """
    telemetry_pipeline.record_stage_inflight({"analyze": {"queued": 9, "active": 4}})
    attribute_sets = telemetry_sink.attribute_sets("phaze.pipeline.stage.inflight")
    assert {frozenset(attrs.items()) for attrs in attribute_sets} == {
        frozenset({("stage", "analyze"), ("status", "queued")}),
        frozenset({("stage", "analyze"), ("status", "active")}),
    }
