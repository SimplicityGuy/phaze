"""Tests for arq WorkerSettings configuration."""

from phaze.config import settings
from phaze.tasks.functions import process_file
from phaze.tasks.worker import WorkerSettings, shutdown, startup


def test_worker_functions_contains_process_file() -> None:
    """WorkerSettings.functions contains process_file."""
    assert process_file in WorkerSettings.functions


def test_worker_max_jobs_matches_settings() -> None:
    """WorkerSettings.max_jobs equals settings.worker_max_jobs."""
    assert WorkerSettings.max_jobs == settings.worker_max_jobs


def test_worker_max_tries_matches_settings() -> None:
    """WorkerSettings.max_tries equals settings.worker_max_retries."""
    assert WorkerSettings.max_tries == settings.worker_max_retries


def test_worker_job_timeout_matches_settings() -> None:
    """WorkerSettings.job_timeout equals settings.worker_job_timeout."""
    assert WorkerSettings.job_timeout == settings.worker_job_timeout


def test_worker_on_startup_is_startup() -> None:
    """WorkerSettings.on_startup is the startup function."""
    assert WorkerSettings.on_startup is startup


def test_worker_on_shutdown_is_shutdown() -> None:
    """WorkerSettings.on_shutdown is the shutdown function."""
    assert WorkerSettings.on_shutdown is shutdown
