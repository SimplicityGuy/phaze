"""The profiling tool must not touch live databases, leak inputs or mix concurrent tabs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from urllib.parse import urlunsplit

import pytest

from scripts.perf_dashboard import _CURRENT, MeasuredSemaphore, Sample, fingerprint, measured_async, validate_targets


def _parser_only_redis_url(
    *, scheme: str = "redis", host: str = "localhost", port: int = 6380, index: str = "18", query: str = "", fragment: str = ""
) -> str:
    """Build deterministic policy inputs; these URLs are never passed to a Redis client.

    Connection tests below use the environment's isolated allocation. This parser-only
    fixture must exercise the profiler's exact local port policy independently of it.
    """
    return urlunsplit((scheme, f"{host}:{port}", f"/{index}", query, fragment))


def test_only_isolated_local_targets_are_allowed() -> None:
    redis_url = _parser_only_redis_url()
    validate_targets("postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_profile_test", redis_url)
    validate_targets("postgresql://phaze:phaze@localhost:5545/phaze_benchmark", redis_url)
    for dsn in (
        "postgresql://phaze:phaze@localhost:5432/phaze_profile_test",
        "postgresql://phaze:phaze@host-prod:5433/phaze_profile_test",
        "postgresql://phaze:phaze@localhost:5433/phaze_test",
        "postgresql://phaze:phaze@localhost:5433/phaze",
        "postgresql://phaze:phaze@localhost:5433/phaze_profile_test?host=host-prod",
        "postgresql://phaze:phaze@localhost:5433/phaze_profile_test?port=5432",
        "postgresql://phaze:phaze@localhost:5433/phaze_profile_test#override",
        "mysql://phaze:phaze@localhost:5433/phaze_profile_test",
    ):
        with pytest.raises(ValueError, match="isolated local"):
            validate_targets(dsn, redis_url)
    for redis_url in (
        _parser_only_redis_url(index="0"),
        _parser_only_redis_url(port=6379),
        _parser_only_redis_url(host="host-prod"),
        _parser_only_redis_url(index=""),
        _parser_only_redis_url(query="db=0"),
        _parser_only_redis_url(query="host=host-prod"),
        _parser_only_redis_url(query="port=6379"),
        _parser_only_redis_url(fragment="override"),
        _parser_only_redis_url(scheme="unix"),
        _parser_only_redis_url(scheme="rediss"),
    ):
        with pytest.raises(ValueError, match="dedicated local"):
            validate_targets("postgresql://phaze:phaze@localhost:5433/phaze_profile_test", redis_url)


def test_fingerprint_never_emits_query_or_private_values() -> None:
    query = "SELECT * FROM files WHERE filename = '<track-01>'"
    digest = fingerprint(query)
    assert len(digest) == 16
    assert set(digest) <= set("0123456789abcdef")
    assert fingerprint("SELECT   * FROM files WHERE filename = '<track-01>'") == digest
    assert fingerprint(query.replace("01", "02")) != digest


async def test_concurrent_requests_keep_separate_measurements_on_error() -> None:
    entered = asyncio.Event()
    second_started = asyncio.Event()

    async def fail() -> None:
        entered.set()
        await second_started.wait()
        raise ValueError("synthetic")

    async def first() -> Sample:
        sample = Sample("/")
        token = _CURRENT.set(sample)
        try:
            with pytest.raises(ValueError, match="synthetic"):
                await measured_async("first", fail)()
            return sample
        finally:
            _CURRENT.reset(token)

    async def second() -> Sample:
        await entered.wait()
        sample = Sample("/pipeline/stats")
        token = _CURRENT.set(sample)
        try:
            second_started.set()
            await measured_async("second", asyncio.sleep)(0)
            return sample
        finally:
            _CURRENT.reset(token)

    one, two = await asyncio.gather(first(), second())
    assert set(one.events) == {"first"}
    assert set(two.events) == {"second"}
    assert _CURRENT.get() is None


async def test_semaphore_retains_the_original_global_bound_and_releases_on_failure() -> None:
    original = asyncio.Semaphore(1)
    instrumented = MeasuredSemaphore(original)
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        with pytest.raises(ValueError, match="synthetic"):
            async with instrumented:
                order.append("first")
                entered.set()
                await release.wait()
                raise ValueError("synthetic")

    async def second() -> None:
        await entered.wait()
        order.append("waiting")
        release.set()
        async with instrumented:
            order.append("second")

    await asyncio.gather(first(), second())
    assert order == ["first", "waiting", "second"]
    assert not original.locked()


async def test_real_database_drivers_count_success_and_failure_without_sql_leakage() -> None:
    """Both actual database drivers are used by the poll; degraded SQL must not disappear."""
    from contextlib import ExitStack
    import os

    from psycopg import errors
    from psycopg_pool import AsyncConnectionPool
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    from scripts.perf_dashboard import MeasuredPool, instrument

    dsn = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(dsn, poolclass=MeasuredPool)
    pool = AsyncConnectionPool(dsn.replace("postgresql+asyncpg://", "postgresql://"), open=False, min_size=1, max_size=1)
    sample = Sample("/synthetic-driver-check")
    token = _CURRENT.set(sample)
    try:
        await pool.open()
        with ExitStack() as stack:
            instrument(stack, engine)
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT 1")) == 1
                with pytest.raises(DBAPIError):
                    await connection.execute(text("SELECT 1 / 0"))
            async with pool.connection() as connection, connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                assert await cursor.fetchone() == (1,)
                with pytest.raises(errors.DivisionByZero):
                    await cursor.execute("SELECT 1 / 0")
        for driver in ("sqlalchemy", "psycopg"):
            assert sum(len(values) for key, values in sample.events.items() if key.startswith(f"sql.{driver}.")) == 2
            assert sum(value for key, value in sample.errors.items() if key.startswith(f"sql.{driver}.")) == 1
            assert any(key.startswith(f"pool.{driver}.acquire") for key in sample.events)
        assert "SELECT" not in repr(sample)
        decoded = json.loads(json.dumps(asdict(sample)))
        assert decoded["events"] == sample.events
        assert decoded["errors"] == sample.errors
    finally:
        _CURRENT.reset(token)
        await pool.close()
        await engine.dispose()


def test_summary_reports_counts_median_and_max_without_small_sample_percentiles() -> None:
    from scripts.perf_dashboard import summarize

    one = Sample("/", wall_ms=3, events={"template.render": [2]})
    assert summarize([one])["/"]["wall"] == {"n": 1, "median_ms": 3, "max_ms": 3}
    two = Sample("/", wall_ms=7, events={"template.render": [4]})
    report = summarize([one, two])["/"]
    assert report["wall"] == {"n": 2, "median_ms": 5, "max_ms": 7}
    assert report["events"]["template.render"] == {"n": 2, "median_ms": 3, "max_ms": 4}
    assert "p95" not in json.dumps(report)


async def test_profile_refuses_real_pytest_session_before_queue_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pytest already owns this seat on another connection; no endpoint or queue runs."""
    from argparse import Namespace
    import os
    from unittest.mock import Mock

    from scripts import perf_dashboard
    from tests.db_guard import TEST_DB_SESSION_LOCK_KEY

    assert perf_dashboard._DATABASE_SESSION_LOCK_KEY == TEST_DB_SESSION_LOCK_KEY
    queue_factory = Mock(side_effect=AssertionError("queue setup must not happen without ownership"))
    monkeypatch.setattr(perf_dashboard, "build_pipeline_queue", queue_factory)
    # This harness never adopts pytest's opt-out: the connection actually holding its lock wins.
    monkeypatch.setenv("PHAZE_TEST_DB_ALLOW_SHARED", "1")
    args = Namespace(dsn=os.environ["TEST_DATABASE_URL"], redis_url=os.environ["PHAZE_REDIS_URL"])
    # CI's real seat uses different ports; target policy is tested independently above.
    # Isolate only that policy seam while exercising the actual held database lock.
    target_validator = Mock()
    monkeypatch.setattr(perf_dashboard, "validate_targets", target_validator)
    with pytest.raises(RuntimeError, match="owned by a pytest session"):
        await perf_dashboard.run(args)
    target_validator.assert_called_once_with(args.dsn, args.redis_url)
    queue_factory.assert_not_called()


async def test_profile_lock_blocks_pytest_and_measurement_then_releases_on_error() -> None:
    """Use only this seat's idle migration DB so the outer pytest lock stays untouched."""
    import os

    from scripts.perf_dashboard import exclusive_database
    from tests.db_guard import SharedTestDatabaseError, acquire_exclusive_session_lock, release_exclusive_session_lock

    dsn = os.environ["MIGRATIONS_TEST_DATABASE_URL"]
    with pytest.raises(ValueError, match="synthetic"):
        async with exclusive_database(dsn):
            with pytest.raises(SharedTestDatabaseError, match="phaze-dashboard-profile-lock"):
                acquire_exclusive_session_lock(dsn, explicit=True)
            with pytest.raises(RuntimeError, match="another measurement"):
                async with exclusive_database(dsn):
                    pytest.fail("second measurement acquired an owned database")
            raise ValueError("synthetic")
    # Closing the profiling guard must make the exact same database/key available to pytest.
    connection = acquire_exclusive_session_lock(dsn, explicit=True)
    assert connection is not None
    release_exclusive_session_lock(connection)


async def test_instrumentation_rejects_a_second_sqlalchemy_engine() -> None:
    """Imported factory mistakes must not become a silent default-database profile."""
    from contextlib import ExitStack
    import os

    from sqlalchemy.ext.asyncio import create_async_engine

    from scripts.perf_dashboard import instrument

    selected = create_async_engine(os.environ["TEST_DATABASE_URL"])
    accidental = create_async_engine(os.environ["TEST_DATABASE_URL"])
    sample = Sample("/routing-check")
    token = _CURRENT.set(sample)
    try:
        with ExitStack() as stack:
            instrument(stack, selected)
            async with selected.connect():
                pass
            with pytest.raises(RuntimeError, match="outside the selected database"):
                async with accidental.connect():
                    pytest.fail("an unselected engine was allowed")
        assert sample.errors["routing.unexpected_engine"] == 1
        # Instrumentation is scoped: no global restriction leaks out of the profiling context.
        async with accidental.connect():
            pass
    finally:
        _CURRENT.reset(token)
        await accidental.dispose()
        await selected.dispose()
