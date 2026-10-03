"""Every runtime-config reload attempt emits the audit log line and the ``phaze.config.*`` metrics.

The metrics are read back from a real SDK ``MeterProvider`` (the package's ``telemetry_sink``),
under ``PHAZE_TELEMETRY_STRICT`` -- so a label the catalogue does not declare, or a value
outside its declared domain, fails here rather than being dropped in production. The log line is
ADR-0019 (runtime config hot-reload) §10's compensating control for an unauthenticated admin
write surface, so what it carries is asserted field by field, not merely that it appeared.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from phaze.config import ControlSettings
from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore


if TYPE_CHECKING:
    from pathlib import Path

    from tests.shared.telemetry.conftest import TelemetrySink


@pytest.fixture(autouse=True)
def _no_ambient_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_MAX_JOBS", raising=False)
    monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", "/nonexistent/phaze-runtime-config/backends.toml")


def _store(tmp_path: Path) -> RuntimeConfigStore:
    return RuntimeConfigStore(ControlSettings(), runtime_toml=tmp_path / RUNTIME_TOML_NAME, env={}, physical_cores=lambda: 64)


def _collect(sink: TelemetrySink) -> tuple[list[dict[str, Any]], float, dict[str, float]]:
    """Counter attribute sets, counter total, and gauge values -- from ONE collection.

    Read all instruments together so counters and the gauges' retained last values come from
    the same snapshot (``TelemetrySink.collect``).
    """
    points = sink.collect()
    reloads = points.get("phaze.config.reloads", [])
    gauges = {name: float(found[-1].value) for name, found in points.items() if name.startswith("phaze.config.last_reload.")}
    return [dict(point.attributes or {}) for point in reloads], float(sum(point.value for point in reloads)), gauges


def _reload_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == "phaze.runtime_config reload"]


def test_a_successful_reload_logs_its_changes_and_sets_the_success_metrics(tmp_path: Path, telemetry_sink: TelemetrySink) -> None:
    store = _store(tmp_path)
    start = store.current().worker_max_jobs
    (tmp_path / RUNTIME_TOML_NAME).write_text("worker_max_jobs = 3\n", encoding="utf-8")

    with capture_logs() as logs:
        result = asyncio.run(store.reload("sighup"))

    [event] = _reload_events(logs)
    assert event["log_level"] == "info"
    assert (event["source"], event["outcome"]) == ("sighup", "applied")
    assert event["changes"] == {"worker_max_jobs": {"old": start, "new": 3}}
    assert event["digest"] == store.snapshot().digest
    assert "error" not in event

    attribute_sets, total, gauges = _collect(telemetry_sink)
    assert attribute_sets == [{"source": "sighup", "outcome": "applied"}]
    assert total == 1
    assert gauges == {"phaze.config.last_reload.successful": 1.0, "phaze.config.last_reload.success_timestamp": pytest.approx(result.at)}


def test_a_rejected_reload_logs_the_error_and_clears_the_success_gauge(tmp_path: Path, telemetry_sink: TelemetrySink) -> None:
    store = _store(tmp_path)
    startup = asyncio.run(store.reload("startup"))
    assert _collect(telemetry_sink)[2]["phaze.config.last_reload.success_timestamp"] == pytest.approx(startup.at)
    (tmp_path / RUNTIME_TOML_NAME).write_text('worker_max_jobs = 0\nredis_url = "redis://:secret@r:6379/0"\n', encoding="utf-8")

    with capture_logs() as logs:
        result = asyncio.run(store.reload("file"))

    [event] = _reload_events(logs)
    assert event["log_level"] == "warning"
    assert (event["source"], event["outcome"], event["changes"]) == ("file", "rejected", {})
    assert event["error"] == result.error
    assert event["restart_required"] == ["redis_url"]
    assert "secret" not in repr(event)

    attribute_sets, _total, gauges = _collect(telemetry_sink)
    assert {"source": "file", "outcome": "rejected"} in attribute_sets
    # The success gauge drops to 0, and the success timestamp is not set: it keeps the startup value.
    assert gauges == {"phaze.config.last_reload.successful": 0.0, "phaze.config.last_reload.success_timestamp": pytest.approx(startup.at)}


def test_a_failing_applier_logs_it_and_reports_partial(tmp_path: Path, telemetry_sink: TelemetrySink) -> None:
    store = _store(tmp_path)

    def broken(_old: object, _new: object) -> None:
        raise RuntimeError("semaphore gone")

    store.register_applier("broken", broken)
    (tmp_path / RUNTIME_TOML_NAME).write_text("lane_io_concurrency = 9\n", encoding="utf-8")

    with capture_logs() as logs:
        asyncio.run(store.reload("api"))

    [event] = _reload_events(logs)
    assert (event["outcome"], event["applier_errors"]) == ("partial", {"broken": "RuntimeError: semaphore gone"})
    assert any(entry["event"] == "phaze.runtime_config applier failed" and entry["applier"] == "broken" for entry in logs)
    attribute_sets, _total, gauges = _collect(telemetry_sink)
    assert attribute_sets == [{"source": "api", "outcome": "partial"}]
    assert gauges == {"phaze.config.last_reload.successful": 0.0}


def test_a_no_op_reload_is_still_counted_and_successful(tmp_path: Path, telemetry_sink: TelemetrySink) -> None:
    store = _store(tmp_path)
    with capture_logs() as logs:
        asyncio.run(store.reload("poll"))
    [event] = _reload_events(logs)
    assert (event["outcome"], event["changes"]) == ("unchanged", {})
    attribute_sets, _total, gauges = _collect(telemetry_sink)
    assert attribute_sets == [{"source": "poll", "outcome": "unchanged"}]
    assert gauges["phaze.config.last_reload.successful"] == 1.0
