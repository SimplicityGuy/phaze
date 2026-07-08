"""Tests for worker configuration settings defaults."""

import pytest

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


# --------------------------------------------------------------------------- lane knobs (dh1)


def test_lane_concurrency_defaults() -> None:
    """Per-lane concurrency defaults: analyze 4, fingerprint 2, meta 2, io 4 (design table)."""
    s = Settings()
    assert s.lane_analyze_concurrency == 4
    assert s.lane_fingerprint_concurrency == 2
    assert s.lane_meta_concurrency == 2
    assert s.lane_io_concurrency == 4


def test_agent_heartbeat_enabled_default() -> None:
    """The heartbeat flag defaults True (all-mode / single-worker back-compat)."""
    s = Settings()
    assert s.agent_heartbeat_enabled is True


def test_lane_knobs_read_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented PHAZE_LANE_*_CONCURRENCY + PHAZE_AGENT_HEARTBEAT env aliases parse."""
    monkeypatch.setenv("PHAZE_LANE_ANALYZE_CONCURRENCY", "6")
    monkeypatch.setenv("PHAZE_LANE_FINGERPRINT_CONCURRENCY", "3")
    monkeypatch.setenv("PHAZE_LANE_META_CONCURRENCY", "5")
    monkeypatch.setenv("PHAZE_LANE_IO_CONCURRENCY", "7")
    monkeypatch.setenv("PHAZE_AGENT_HEARTBEAT", "false")
    s = Settings()
    assert s.lane_analyze_concurrency == 6
    assert s.lane_fingerprint_concurrency == 3
    assert s.lane_meta_concurrency == 5
    assert s.lane_io_concurrency == 7
    assert s.agent_heartbeat_enabled is False


# --------------------------------------------------------------------------- pool hygiene (ryn)


def test_pool_hygiene_defaults() -> None:
    """DB pool + dispatch-queue knobs default to the leaned PgBouncer-footprint values (ryn).

    Session-mode PgBouncer pins one server conn per client conn; the reduced pool sizes +
    hygiene (pre_ping/recycle/bounded timeout) cut phaze's server-connection footprint so the
    shared ~55-cap pool stops deadlocking. Homelab raises the cap to ~80 in parallel (headroom).
    """
    s = Settings()
    assert s.db_pool_size == 5
    assert s.db_max_overflow == 5
    assert s.db_pool_timeout == 10
    assert s.db_pool_recycle == 1800
    assert s.db_pool_pre_ping is True
    assert s.dispatch_queue_min_size == 0
    assert s.dispatch_queue_max_size == 2


def test_pool_hygiene_read_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each PHAZE_DB_* / PHAZE_DISPATCH_QUEUE_* alias overrides its knob (incl. the bool false case)."""
    monkeypatch.setenv("PHAZE_DB_POOL_SIZE", "9")
    monkeypatch.setenv("PHAZE_DB_MAX_OVERFLOW", "11")
    monkeypatch.setenv("PHAZE_DB_POOL_TIMEOUT", "20")
    monkeypatch.setenv("PHAZE_DB_POOL_RECYCLE", "3600")
    monkeypatch.setenv("PHAZE_DB_POOL_PRE_PING", "false")
    monkeypatch.setenv("PHAZE_DISPATCH_QUEUE_MIN_SIZE", "3")
    monkeypatch.setenv("PHAZE_DISPATCH_QUEUE_MAX_SIZE", "6")
    s = Settings()
    assert s.db_pool_size == 9
    assert s.db_max_overflow == 11
    assert s.db_pool_timeout == 20
    assert s.db_pool_recycle == 3600
    assert s.db_pool_pre_ping is False
    assert s.dispatch_queue_min_size == 3
    assert s.dispatch_queue_max_size == 6
