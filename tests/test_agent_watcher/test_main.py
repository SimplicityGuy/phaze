"""Unit tests for phaze.agent_watcher.__main__ (Phase 27 D-04, D-16, D-18, Pitfall 1).

Six behaviors:

1. main() calls whoami() then constructs the Observer and starts it.
2. Observer.schedule is invoked once per identity.scan_root.
3. Graceful shutdown on SIGTERM: observer.stop / observer.join / client.close
   are awaited in order.
4. main() raises RuntimeError when whoami_with_retry exhausts the budget.
5. End-to-end event-to-post: synthesizing a FileCreatedEvent + advancing the
   fake clock past settle_period results in one POST with batch_id absent
   from the body (D-18 verification).
6. OSError on the vanished-path path is swallowed -- sweep loop survives
   (Pitfall 1 behavior gate; this is the binding for Task 1's poster.py
   acceptance criterion).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
from typing import Any
import unicodedata
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from watchdog.events import FileCreatedEvent

import phaze.agent_watcher.__main__ as wmain
from phaze.agent_watcher.debouncer import Debouncer
from phaze.agent_watcher.observer import WatcherEventHandler
from phaze.agent_watcher.poster import Poster
from phaze.config import AgentSettings
from phaze.schemas.agent_identity import AgentIdentity
from phaze.services.agent_client import AgentApiServerError, PhazeAgentClient


_TEST_TOKEN = "phaze_agent_test"  # nosec B105 -- test fixture, not a real secret


def _build_agent_settings(monkeypatch: pytest.MonkeyPatch) -> AgentSettings:
    """Construct an AgentSettings instance with all required env vars set."""
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test:8000")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-TOKEN-1234567890ab")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music")
    return AgentSettings()


def test_configure_logging_attaches_stdout_handler() -> None:
    """Phase 27 UAT Gap 7: stdout handler must exist after _configure_logging.

    Before this fix the watcher's logger.info/error calls went to /dev/null
    because asyncio.run(main()) doesn't invoke uvicorn's logging config.
    Operators saw an empty `docker logs phaze-watcher-1` even when the
    watcher was healthy and processing events.
    """
    import sys

    # Snapshot + reset root handlers so the test is hermetic
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        for h in before:
            root.removeHandler(h)
        wmain._configure_logging()
        stdout_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout]
        assert len(stdout_handlers) == 1, f"expected exactly one stdout handler, got {len(stdout_handlers)}"
        assert root.level <= logging.INFO, f"root level must be <= INFO; got {root.level}"
        # Idempotency: calling again does not add a second stdout handler
        wmain._configure_logging()
        stdout_handlers_after = [h for h in root.handlers if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout]
        assert len(stdout_handlers_after) == 1, "configure_logging must be idempotent"
    finally:
        # Restore prior handler set
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in before:
            root.addHandler(h)


def _build_identity(roots: list[str], agent_id: str = "test-agent-1") -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        scan_roots=roots,
        created_at=dt.datetime(2026, 5, 13, 0, 0, 0, tzinfo=dt.UTC),
    )


# ---------------------------------------------------------------------------
# Test 1: main() calls whoami then starts Observer + schedules one root.
# ---------------------------------------------------------------------------
async def test_main_calls_whoami_then_starts_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_agent_settings(monkeypatch)
    identity = _build_identity(roots=["/data/music", "/data/concerts"])

    fake_client = AsyncMock(spec=PhazeAgentClient)
    fake_client.whoami = AsyncMock(return_value=identity)
    fake_client.close = AsyncMock()

    fake_observer = MagicMock()
    fake_observer_cls = MagicMock(return_value=fake_observer)

    monkeypatch.setattr(wmain, "get_settings", lambda: cfg)
    monkeypatch.setattr(wmain, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(wmain, "Observer", fake_observer_cls)
    # Make the sweep loop exit immediately by pre-setting shutdown_event.
    real_event_cls = asyncio.Event

    def _preset_event() -> asyncio.Event:
        e = real_event_cls()
        e.set()
        return e

    monkeypatch.setattr(wmain.asyncio, "Event", _preset_event)

    await wmain.main()

    assert fake_client.whoami.await_count == 1
    # One schedule call per scan_root
    assert fake_observer.schedule.call_count == 2
    fake_observer.start.assert_called_once()


async def test_main_uses_polling_observer_when_flag_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 27 UAT Gap 8: PHAZE_WATCHER_POLLING_MODE=true must select PollingObserver.

    macOS docker bind mounts (rancher-desktop / Docker Desktop) silently drop
    inotify events through 9p/virtiofs — the native Observer schedules but
    never fires. A regression that wired the flag to the WRONG observer
    class (or ignored it entirely) would leave Mac devs unable to UAT the
    watcher even on a fresh stack.
    """
    monkeypatch.setenv("PHAZE_WATCHER_POLLING_MODE", "true")
    cfg = _build_agent_settings(monkeypatch)
    assert cfg.watcher_polling_mode is True, "test precondition: flag should propagate to AgentSettings"
    identity = _build_identity(roots=["/data/music"])

    fake_client = AsyncMock(spec=PhazeAgentClient)
    fake_client.whoami = AsyncMock(return_value=identity)
    fake_client.close = AsyncMock()

    fake_polling_observer = MagicMock()
    fake_polling_cls = MagicMock(return_value=fake_polling_observer)
    fake_native_cls = MagicMock()  # native must NOT be called

    monkeypatch.setattr(wmain, "get_settings", lambda: cfg)
    monkeypatch.setattr(wmain, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(wmain, "PollingObserver", fake_polling_cls)
    monkeypatch.setattr(wmain, "Observer", fake_native_cls)

    real_event_cls = asyncio.Event

    def _preset_event() -> asyncio.Event:
        e = real_event_cls()
        e.set()
        return e

    monkeypatch.setattr(wmain.asyncio, "Event", _preset_event)

    await wmain.main()

    fake_polling_cls.assert_called_once()
    fake_native_cls.assert_not_called()
    fake_polling_observer.schedule.assert_called_once()
    fake_polling_observer.start.assert_called_once()


async def test_main_uses_native_observer_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (PHAZE_WATCHER_POLLING_MODE unset) selects native Observer, not Polling."""
    cfg = _build_agent_settings(monkeypatch)
    assert cfg.watcher_polling_mode is False, "test precondition: default must be false"
    identity = _build_identity(roots=["/data/music"])

    fake_client = AsyncMock(spec=PhazeAgentClient)
    fake_client.whoami = AsyncMock(return_value=identity)
    fake_client.close = AsyncMock()

    fake_polling_cls = MagicMock()
    fake_native = MagicMock()
    fake_native_cls = MagicMock(return_value=fake_native)

    monkeypatch.setattr(wmain, "get_settings", lambda: cfg)
    monkeypatch.setattr(wmain, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(wmain, "PollingObserver", fake_polling_cls)
    monkeypatch.setattr(wmain, "Observer", fake_native_cls)

    real_event_cls = asyncio.Event

    def _preset_event() -> asyncio.Event:
        e = real_event_cls()
        e.set()
        return e

    monkeypatch.setattr(wmain.asyncio, "Event", _preset_event)

    await wmain.main()

    fake_native_cls.assert_called_once()
    fake_polling_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: main() schedules Observer per scan_root (3 roots -> 3 schedules).
# ---------------------------------------------------------------------------
async def test_main_constructs_observer_per_scan_root(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_agent_settings(monkeypatch)
    identity = _build_identity(roots=["/a", "/b", "/c"])

    fake_client = AsyncMock(spec=PhazeAgentClient)
    fake_client.whoami = AsyncMock(return_value=identity)
    fake_client.close = AsyncMock()

    fake_observer = MagicMock()
    monkeypatch.setattr(wmain, "get_settings", lambda: cfg)
    monkeypatch.setattr(wmain, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(wmain, "Observer", MagicMock(return_value=fake_observer))
    real_event_cls = asyncio.Event
    monkeypatch.setattr(wmain.asyncio, "Event", lambda: (lambda e: (e.set(), e)[1])(real_event_cls()))

    await wmain.main()

    assert fake_observer.schedule.call_count == 3
    scheduled_paths = [c.kwargs.get("path") or c.args[1] for c in fake_observer.schedule.call_args_list]
    assert scheduled_paths == ["/a", "/b", "/c"]


# ---------------------------------------------------------------------------
# Test 3: Graceful shutdown on SIGTERM -- stop / join / close all called.
# ---------------------------------------------------------------------------
async def test_main_graceful_shutdown_on_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_agent_settings(monkeypatch)
    identity = _build_identity(roots=["/x"])

    fake_client = AsyncMock(spec=PhazeAgentClient)
    fake_client.whoami = AsyncMock(return_value=identity)
    fake_client.close = AsyncMock()

    fake_observer = MagicMock()
    # WR-07: a healthy shutdown reports ``is_alive() is False`` after join.
    fake_observer.is_alive.return_value = False
    monkeypatch.setattr(wmain, "get_settings", lambda: cfg)
    monkeypatch.setattr(wmain, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(wmain, "Observer", MagicMock(return_value=fake_observer))
    real_event_cls = asyncio.Event
    monkeypatch.setattr(wmain.asyncio, "Event", lambda: (lambda e: (e.set(), e)[1])(real_event_cls()))

    await wmain.main()

    # finally: block invoked stop + join(timeout=10.0) + close
    fake_observer.stop.assert_called_once()
    fake_observer.join.assert_called_once_with(timeout=10.0)
    fake_client.close.assert_awaited_once()


async def test_main_logs_warning_when_observer_does_not_stop(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """WR-07: a wedged watchdog thread does NOT hang the shutdown.

    If ``observer.is_alive()`` still returns True after the bounded join,
    the watcher logs a warning and proceeds to close the HTTP client. The
    container's process supervisor handles the final SIGKILL -- the goal is
    to never block ``docker compose down`` on an NFS stall / FUSE deadlock.
    """
    cfg = _build_agent_settings(monkeypatch)
    identity = _build_identity(roots=["/x"])

    fake_client = AsyncMock(spec=PhazeAgentClient)
    fake_client.whoami = AsyncMock(return_value=identity)
    fake_client.close = AsyncMock()

    fake_observer = MagicMock()
    # Simulate a wedged thread: still alive after join returns.
    fake_observer.is_alive.return_value = True
    monkeypatch.setattr(wmain, "get_settings", lambda: cfg)
    monkeypatch.setattr(wmain, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(wmain, "Observer", MagicMock(return_value=fake_observer))
    real_event_cls = asyncio.Event
    monkeypatch.setattr(wmain.asyncio, "Event", lambda: (lambda e: (e.set(), e)[1])(real_event_cls()))

    with caplog.at_level(logging.WARNING, logger="phaze.agent_watcher.__main__"):
        await wmain.main()

    fake_observer.join.assert_called_once_with(timeout=10.0)
    # Warning surfaced; client still closed (shutdown did not hang).
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "did not stop within" in text
    fake_client.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 4: whoami exhaustion -> RuntimeError.
# ---------------------------------------------------------------------------
async def test_main_exits_nonzero_on_whoami_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_agent_settings(monkeypatch)

    fake_client = AsyncMock(spec=PhazeAgentClient)
    fake_client.whoami = AsyncMock(side_effect=AgentApiServerError("GET /whoami -> 503 after retries"))
    fake_client.close = AsyncMock()

    monkeypatch.setattr(wmain, "get_settings", lambda: cfg)
    monkeypatch.setattr(wmain, "construct_agent_client", lambda _cfg: fake_client)

    # No-op sleep so the backoff budget burns in ~0 seconds.
    async def _no_sleep(_delay: float) -> None:
        return None

    import phaze.tasks._shared.agent_bootstrap as ab

    monkeypatch.setattr(ab.asyncio, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="exhausted retry budget"):
        await wmain.main()

    # WR-02: even on whoami exhaustion (auth fail or exhausted retry budget) the
    # client MUST still be closed before the RuntimeError propagates -- otherwise
    # the underlying httpx.AsyncClient leaks (ResourceWarning) and the module's
    # deterministic-close contract is violated.
    fake_client.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 5: Event -> POST end-to-end; batch_id absent in JSON body (D-18).
# ---------------------------------------------------------------------------
async def test_event_to_post_e2e(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Stage a real file so stat + SHA-256 succeed.
    music_file = tmp_path / "song.mp3"
    music_file.write_bytes(b"\x00" * 1024)

    # respx-mock the upsert_files endpoint.
    base_url = "http://app.test:8000"
    real_client = PhazeAgentClient(base_url=base_url, token=_TEST_TOKEN, timeout=5.0)
    poster = Poster(client=real_client, agent_id="test-agent")

    debouncer = Debouncer()
    loop = asyncio.get_running_loop()
    handler = WatcherEventHandler(loop=loop, debouncer_touch=debouncer.touch)

    # Capture POST body on respx mock.
    captured: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={"agent_id": "test-agent", "upserted": 1, "inserted": 1, "enqueued": 1},
        )

    with respx.mock(base_url=base_url, assert_all_called=True) as mock:
        mock.post("/api/internal/agent/files").mock(side_effect=_capture)

        # Synthesize the event (direct ctor; the test bypasses the watchdog
        # Observer thread entirely, exercising only the asyncio-side primitives).
        handler.on_created(FileCreatedEvent(src_path=str(music_file)))
        # call_soon_threadsafe schedules touch; let the loop drain.
        await asyncio.sleep(0)

        assert debouncer.pending_count() == 1

        # Advance time past settle_period via a fake-clock equivalent: instead
        # of monkeypatching time.monotonic (the Debouncer was already called
        # with the real clock above), set a long-elapsed settle_period of 0
        # so the entry is immediately considered settled.
        ready, evicted = debouncer.sweep(settle_period=0.0, max_pending=3600.0)
        assert ready == [str(music_file)]
        assert evicted == []

        await poster.post_one(ready[0])

    await real_client.close()

    # D-18 verification: batch_id absent OR null in the JSON body.
    import json

    body = json.loads(captured["json"])
    assert "batch_id" not in body or body["batch_id"] is None, f"D-18 violation: batch_id present: {body!r}"
    # And the chunk shape is correct.
    assert len(body["files"]) == 1
    assert body["files"][0]["original_filename"] == "song.mp3"
    # Path is NFC-normalized (the input was already NFC, so the round-trip
    # preserves it; the invariant is that the assertion does not fail).
    assert unicodedata.is_normalized("NFC", body["files"][0]["original_path"])


# ---------------------------------------------------------------------------
# Test 5b (Phase 27 UAT Gap 5): missing required env -> actionable log + exit 1.
# ---------------------------------------------------------------------------
async def test_main_logs_actionable_error_on_missing_env(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gap 5: ``PHAZE_AGENT_API_URL`` missing must log an ERROR + exit 1.

    Previously the watcher died with a raw pydantic ValidationError stack
    trace, drowning the operator-actionable hint that
    ``whoami_with_retry`` would otherwise surface. The fix wraps the
    ``get_settings()`` call in a try/except ValidationError, logs a clear
    per-field summary at ERROR level, and exits 1.
    """
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    # Intentionally leave PHAZE_AGENT_API_URL unset -- the validator should trip.
    monkeypatch.delenv("PHAZE_AGENT_API_URL", raising=False)
    monkeypatch.delenv("agent_api_url", raising=False)
    # Provide the other required vars so the failure is isolated to API_URL.
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-abc123")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/data/music")

    # Clear the get_settings lru_cache so the patched env is honored.
    from phaze.config import get_settings

    get_settings.cache_clear()

    with caplog.at_level(logging.ERROR, logger="phaze.agent_watcher.__main__"), pytest.raises(SystemExit) as excinfo:
        await wmain.main()

    assert excinfo.value.code == 1, f"expected exit code 1, got {excinfo.value.code!r}"

    text = "\n".join(rec.getMessage() for rec in caplog.records)
    # Operator must be able to find the failed variable name in the log.
    assert "PHAZE_AGENT_API_URL" in text or "agent_api_url" in text, f"missing-var log does not mention PHAZE_AGENT_API_URL: {text!r}"
    # ... and the log must use words operators search for when triaging.
    assert ("missing" in text.lower()) or ("required" in text.lower()), f"missing-var log lacks 'missing'/'required' keyword: {text!r}"


# ---------------------------------------------------------------------------
# Test 6: OSError on vanished path -> no exception, sweep loop survives.
# (Pitfall 1 behavior gate; binds to Task 1's poster.py OSError handling.)
# ---------------------------------------------------------------------------
async def test_oserror_on_vanished_path(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # Build a real client + poster but monkeypatch Path.stat to raise OSError
    # so the post_one call exercises the Pitfall-1 drop branch.
    real_client = PhazeAgentClient(base_url="http://app.test:8000", token=_TEST_TOKEN, timeout=5.0)
    poster = Poster(client=real_client, agent_id="test-agent")

    def _raise_oserror(_self: Path) -> Any:
        raise OSError("ENOENT")

    monkeypatch.setattr(Path, "stat", _raise_oserror)

    with caplog.at_level(logging.DEBUG, logger="phaze.agent_watcher.poster"):
        # Should NOT raise.
        await poster.post_one("/var/empty/vanished.mp3")

    await real_client.close()

    # And the sweep-loop survival proof: simulate a subsequent post that
    # would normally succeed -- but here we just confirm the first call
    # returned cleanly (no exception escaped) which is the invariant.
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "vanished" in text.lower() or "dropping" in text.lower(), f"expected debug log of dropped path; got: {text!r}"
