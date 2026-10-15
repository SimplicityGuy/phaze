"""The profiling tool must not touch live databases, leak inputs or mix concurrent tabs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json

import pytest

from scripts.perf_dashboard import _CURRENT, MeasuredSemaphore, Sample, fingerprint, measured_async, validate_targets


def test_only_isolated_local_targets_are_allowed() -> None:
    redis_url = "redis://localhost:6380/18"
    validate_targets("postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_profile_test", redis_url)
    validate_targets("postgresql://phaze:phaze@localhost:5545/phaze_benchmark", redis_url)
    for dsn in (
        "postgresql://phaze:phaze@localhost:5432/phaze_profile_test",
        "postgresql://phaze:phaze@host-prod:5433/phaze_profile_test",
        "postgresql://phaze:phaze@localhost:5433/phaze_test",
        "postgresql://phaze:phaze@localhost:5433/phaze",
    ):
        with pytest.raises(ValueError, match="isolated local"):
            validate_targets(dsn, redis_url)
    for redis_url in ("redis://localhost:6380/0", "redis://localhost:6379/18", "redis://host-prod:6380/18", "redis://localhost:6380/"):
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
