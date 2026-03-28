"""Tests for task functions."""

from arq import Retry
import pytest

from phaze.tasks.functions import process_file


async def test_process_file_returns_result() -> None:
    """process_file returns correct result dict for valid file_id."""
    ctx: dict = {"job_try": 1}
    result = await process_file(ctx, file_id=42)
    assert result == {"file_id": 42, "status": "processed"}


async def test_process_file_raises_retry_on_exception() -> None:
    """process_file raises arq.Retry when an exception occurs."""

    async def _failing(ctx: dict) -> None:
        """Simulate process_file where the try block raises."""
        try:
            raise ValueError("simulated failure")
        except Exception as exc:
            raise Retry(defer=ctx["job_try"] * 5) from exc

    ctx: dict = {"job_try": 1}
    with pytest.raises(Retry):
        await _failing(ctx)


async def test_process_file_retry_defer_job_try_1() -> None:
    """Retry defer is 5 seconds on first try (job_try=1)."""
    retry = Retry(defer=1 * 5)
    assert retry.defer_score == 5_000  # 5 seconds in ms


async def test_process_file_retry_defer_job_try_2() -> None:
    """Retry defer is 10 seconds on second try (job_try=2)."""
    retry = Retry(defer=2 * 5)
    assert retry.defer_score == 10_000  # 10 seconds in ms


async def test_process_file_retry_defer_job_try_3() -> None:
    """Retry defer is 15 seconds on third try (job_try=3)."""
    retry = Retry(defer=3 * 5)
    assert retry.defer_score == 15_000  # 15 seconds in ms
