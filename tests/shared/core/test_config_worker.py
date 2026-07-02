"""Tests for worker configuration settings defaults."""

from phaze.config import Settings


def test_worker_max_jobs_default() -> None:
    """Worker max_jobs defaults to 8."""
    s = Settings()
    assert s.worker_max_jobs == 8


def test_worker_job_timeout_default() -> None:
    """Worker job_timeout defaults to 600 seconds."""
    s = Settings()
    assert s.worker_job_timeout == 600


def test_worker_max_retries_default() -> None:
    """Worker max_retries defaults to 4."""
    s = Settings()
    assert s.worker_max_retries == 4


def test_worker_process_pool_size_default() -> None:
    """Worker process_pool_size defaults to 4."""
    s = Settings()
    assert s.worker_process_pool_size == 4


def test_worker_health_check_interval_default() -> None:
    """Worker health_check_interval defaults to 60 seconds."""
    s = Settings()
    assert s.worker_health_check_interval == 60


def test_worker_keep_result_default() -> None:
    """Worker keep_result defaults to 3600 seconds."""
    s = Settings()
    assert s.worker_keep_result == 3600


def test_models_path_default() -> None:
    """models_path defaults to /models."""
    s = Settings()
    assert s.models_path == "/models"


def test_scan_stall_seconds_default() -> None:
    """scan_stall_seconds defaults to 86400 (24h).

    scan_directory runs with no SAQ wall-clock timeout (timeout=0 -> unbounded),
    so the progress-based stall reaper is the sole liveness guard. A 24h window
    ensures a healthy, slow-but-progressing bulk archive walk (e.g. hashing a
    multi-GB file on a network mount) is never falsely reaped.
    """
    s = Settings()
    assert s.scan_stall_seconds == 86400
