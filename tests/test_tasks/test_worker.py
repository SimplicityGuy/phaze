"""Tests for SAQ worker settings configuration."""

from phaze.config import settings as app_settings
from phaze.tasks.functions import process_file
from phaze.tasks.worker import settings, shutdown, startup


def test_worker_functions_contains_process_file() -> None:
    """settings["functions"] contains process_file."""
    assert process_file in settings["functions"]


def test_worker_concurrency_matches_settings() -> None:
    """settings["concurrency"] equals app_settings.worker_max_jobs."""
    assert settings["concurrency"] == app_settings.worker_max_jobs


def test_worker_startup_is_startup() -> None:
    """settings["startup"] is the startup function."""
    assert settings["startup"] is startup


def test_worker_shutdown_is_shutdown() -> None:
    """settings["shutdown"] is the shutdown function."""
    assert settings["shutdown"] is shutdown
