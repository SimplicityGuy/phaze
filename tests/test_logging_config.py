"""Unit tests for the central structlog configuration (PR3 observability).

These tests assert wiring + levels, not formatting cosmetics: they parse JSON
lines and check which records are emitted/suppressed, that the root logger holds
exactly one handler after repeated calls, that foreign stdlib records flow
through the configured pipeline, and that noisy libraries are tamed. They never
assert exact full log strings.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest
import structlog

from phaze.logging_config import configure_logging


if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def reset_logging() -> Iterator[None]:
    """Reset structlog + the stdlib root logger after each test.

    Logging config is process-global; without this teardown one test's
    configure_logging() would leak its handler/level into the next.
    """
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    for name in ("httpx", "httpcore", "asyncio", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def _last_json_line(text: str) -> dict[str, object]:
    """Parse the last non-empty captured line as a JSON object."""
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, "expected at least one captured log line"
    return json.loads(lines[-1])


@pytest.mark.usefixtures("reset_logging")
def test_json_logs_emit_parseable_object(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_logs=True)
    structlog.get_logger("test.json").info("hello world", key="value")

    payload = _last_json_line(capsys.readouterr().out)
    assert payload["event"] == "hello world"
    assert payload["level"] == "info"
    assert payload["logger"] == "test.json"
    assert "timestamp" in payload
    assert payload["key"] == "value"


@pytest.mark.usefixtures("reset_logging")
def test_console_logs_are_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_logs=False)
    structlog.get_logger("test.console").info("human readable")

    out = capsys.readouterr().out
    assert "human readable" in out
    last = next(line for line in reversed(out.splitlines()) if line.strip())
    with pytest.raises(json.JSONDecodeError):
        json.loads(last)


@pytest.mark.usefixtures("reset_logging")
def test_debug_level_emits_debug_record(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="DEBUG", json_logs=True)
    structlog.get_logger("test.debug").debug("debug detail")

    assert _last_json_line(capsys.readouterr().out)["event"] == "debug detail"


@pytest.mark.usefixtures("reset_logging")
def test_info_level_suppresses_debug_record(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_logs=True)
    structlog.get_logger("test.suppress").debug("should be dropped")

    assert "should be dropped" not in capsys.readouterr().out


@pytest.mark.usefixtures("reset_logging")
def test_positional_args_still_interpolate(capsys: pytest.CaptureFixture[str]) -> None:
    """Legacy ``logger.info("text %s", value)`` must still %-format after the swap."""
    configure_logging(level="INFO", json_logs=True)
    structlog.get_logger("test.posargs").info("count is %s", 42)

    assert _last_json_line(capsys.readouterr().out)["event"] == "count is 42"


@pytest.mark.usefixtures("reset_logging")
def test_idempotent_single_handler() -> None:
    configure_logging(level="INFO", json_logs=True)
    configure_logging(level="INFO", json_logs=True)
    configure_logging(level="DEBUG", json_logs=False)

    assert len(logging.getLogger().handlers) == 1


@pytest.mark.usefixtures("reset_logging")
def test_foreign_stdlib_log_flows_through(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_logs=True)
    logging.getLogger("uvicorn.error").info("foreign record")

    payload = _last_json_line(capsys.readouterr().out)
    assert payload["event"] == "foreign record"
    assert payload["level"] == "info"
    assert "timestamp" in payload


@pytest.mark.usefixtures("reset_logging")
def test_noisy_libs_tamed_at_info() -> None:
    configure_logging(level="INFO", json_logs=True)
    assert logging.getLogger("httpx").level == logging.WARNING


@pytest.mark.usefixtures("reset_logging")
def test_noisy_libs_follow_debug() -> None:
    configure_logging(level="DEBUG", json_logs=True)
    assert logging.getLogger("httpx").level == logging.DEBUG


@pytest.mark.usefixtures("reset_logging")
def test_env_level_fallback(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PHAZE_LOG_LEVEL", "DEBUG")
    configure_logging(json_logs=True)
    structlog.get_logger("test.env").debug("env driven debug")

    assert _last_json_line(capsys.readouterr().out)["event"] == "env driven debug"


@pytest.mark.usefixtures("reset_logging")
def test_env_json_fallback(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PHAZE_LOG_JSON", "true")
    configure_logging(level="INFO")
    structlog.get_logger("test.envjson").info("env json")

    assert _last_json_line(capsys.readouterr().out)["event"] == "env json"


@pytest.mark.usefixtures("reset_logging")
def test_reconfigure_changes_level_for_already_used_logger(capsys: pytest.CaptureFixture[str]) -> None:
    """A second configure_logging() with a new level applies to an already-used logger.

    Regression guard for cache_logger_on_first_use: with caching the proxy would
    freeze at the first level and silently ignore the reconfigure.
    """
    log = structlog.get_logger("test.reconfig")

    configure_logging(level="INFO", json_logs=True)
    log.debug("dropped at info")
    assert "dropped at info" not in capsys.readouterr().out

    configure_logging(level="DEBUG", json_logs=True)
    log.debug("kept at debug")
    assert _last_json_line(capsys.readouterr().out)["event"] == "kept at debug"


@pytest.mark.usefixtures("reset_logging")
def test_unknown_level_falls_back_to_info(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="NONSENSE", json_logs=True)
    log = structlog.get_logger("test.fallback")
    log.info("info kept")
    log.debug("debug dropped")

    out = capsys.readouterr().out
    assert "info kept" in out
    assert "debug dropped" not in out
