"""Recovery proof for transient SAQ Postgres upkeep failures."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from psycopg_pool import PoolTimeout
import pytest
from saq.queue.postgres import PostgresQueue

from phaze.tasks._shared.resilient_queue import ResilientPostgresQueue, upkeep_metric_key


class _MetricRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    async def incr(self, key: str) -> None:
        self.values[key] = self.values.get(key, 0) + 1


class _FailingMetricRedis:
    async def incr(self, key: str) -> None:
        raise ConnectionError(key)


def _queue() -> tuple[ResilientPostgresQueue, _MetricRedis]:
    queue = ResilientPostgresQueue.from_url("postgresql://u:p@h:5432/d", name="analyze")
    metrics = _MetricRedis()
    queue.cache_redis = metrics  # type: ignore[attr-defined]
    return queue, metrics


@pytest.mark.asyncio
async def test_sweep_recovers_after_transient_pool_timeout() -> None:
    """A brief outage retries in the same tick, then reconciliation resumes."""
    queue, metrics = _queue()
    base_sweep = AsyncMock(side_effect=[PoolTimeout("brief outage"), ["reconciled-job"]])

    with patch.object(PostgresQueue, "sweep", base_sweep), patch("phaze.tasks._shared.resilient_queue.asyncio.sleep", new=AsyncMock()) as sleep:
        assert await queue.sweep() == ["reconciled-job"]

    assert base_sweep.await_count == 2
    sleep.assert_awaited_once()
    assert metrics.values == {upkeep_metric_key("retries_total", "analyze"): 1}


@pytest.mark.asyncio
async def test_sweep_gives_up_after_bounded_attempts() -> None:
    """A persistent outage remains an error and exports retry/give-up counters."""
    queue, metrics = _queue()
    base_sweep = AsyncMock(side_effect=PoolTimeout("persistent outage"))

    with (
        patch.object(PostgresQueue, "sweep", base_sweep),
        patch("phaze.tasks._shared.resilient_queue.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(PoolTimeout, match="persistent outage"),
    ):
        await queue.sweep()

    assert base_sweep.await_count == 3
    assert sleep.await_count == 2
    assert metrics.values == {
        upkeep_metric_key("retries_total", "analyze"): 2,
        upkeep_metric_key("failures_total", "analyze"): 1,
    }


@pytest.mark.asyncio
async def test_sweep_does_not_retry_programming_errors() -> None:
    """Only connection-acquisition failures consume the retry budget."""
    queue, metrics = _queue()
    base_sweep = AsyncMock(side_effect=ValueError("bad sweep logic"))

    with patch.object(PostgresQueue, "sweep", base_sweep), pytest.raises(ValueError, match="bad sweep logic"):
        await queue.sweep()

    base_sweep.assert_awaited_once()
    assert metrics.values == {}


@pytest.mark.asyncio
async def test_metric_increment_is_optional() -> None:
    """Queues without the attached cache client still preserve the database failure."""
    queue = ResilientPostgresQueue.from_url("postgresql://u:p@h:5432/d", name="analyze")

    await queue._increment_upkeep_metric("retries_total")


@pytest.mark.asyncio
async def test_metric_failure_is_logged_without_replacing_upkeep_error() -> None:
    """A Redis outage cannot mask the Postgres failure that exhausted the retry budget."""
    queue = ResilientPostgresQueue.from_url("postgresql://u:p@h:5432/d", name="analyze")
    queue.cache_redis = _FailingMetricRedis()  # type: ignore[attr-defined]
    base_sweep = AsyncMock(side_effect=PoolTimeout("persistent outage"))

    with (
        patch.object(PostgresQueue, "sweep", base_sweep),
        patch("phaze.tasks._shared.resilient_queue.asyncio.sleep", new=AsyncMock()),
        patch("phaze.tasks._shared.resilient_queue.logger.warning") as warning,
        pytest.raises(PoolTimeout, match="persistent outage"),
    ):
        await queue.sweep()

    assert warning.call_count == 5
    assert [call.args[0] for call in warning.call_args_list].count("saq_upkeep_metric_write_failed") == 3


@pytest.mark.asyncio
async def test_zero_attempt_configuration_hits_defensive_guard() -> None:
    """The impossible fallthrough remains loud if the retry constant is misconfigured."""
    queue, _ = _queue()

    with patch("phaze.tasks._shared.resilient_queue.UPKEEP_RETRY_ATTEMPTS", 0), pytest.raises(AssertionError, match="unreachable"):
        await queue.sweep()
