"""Reload triggers (phaze-mvq8z.5): SIGHUP + the watched-directory debounce/content-hash pipeline.

``PollingObserver`` is used throughout rather than the native backend: phaze-mvq8z.3 measured,
inside a real container, that the native backend sees ZERO host edits on a Colima/virtiofs bind
mount, while PollingObserver catches both edit patterns reliably -- and using it here removes any
dependency on which native filesystem-event backend this test happens to run under (FSEvents on a
dev Mac, inotify in CI). Poll interval and debounce settle are shortened (tens of ms, not the
~1s the measurement used) purely for test speed; the mechanism under test is unaffected by the
interval's absolute size.

The real-process SIGHUP delivery test (a separate OS process, not this interpreter) lives in
``test_runtime_config_sighup_process.py`` -- this file covers the SIGHUP handler's *installation
and wiring* in-process (fast, no subprocess), and the directory-watch debounce/hash pipeline.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from phaze.config import ControlSettings
from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore
from phaze.runtime_config_triggers import RuntimeConfigWatcher, build_watcher, install_sighup_handler


if TYPE_CHECKING:
    from pathlib import Path


_POLL_INTERVAL = 0.05
_SETTLE_SECONDS = 0.2
# Generous relative to (_POLL_INTERVAL + _SETTLE_SECONDS) so a slow CI runner doesn't flake;
# the mechanism itself settles in well under 0.5s per the module docstring's measurement.
_WAIT_TIMEOUT = 5.0


def _store(tmp_path: Path, *, cores: int = 64) -> RuntimeConfigStore:
    return RuntimeConfigStore(ControlSettings(), runtime_toml=tmp_path / RUNTIME_TOML_NAME, env={}, physical_cores=lambda: cores)


def _spy_reload(store: RuntimeConfigStore, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Wrap ``store.reload`` so calls are counted/inspected while still running for real."""
    spy = AsyncMock(wraps=store.reload)
    monkeypatch.setattr(store, "reload", spy)
    return spy


async def _wait_for_call_count(spy: AsyncMock, count: int, *, timeout: float = _WAIT_TIMEOUT) -> None:
    elapsed = 0.0
    step = 0.02
    while spy.await_count < count and elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step
    assert spy.await_count >= count, f"expected >= {count} reload call(s) within {timeout}s, got {spy.await_count}"


@pytest.mark.asyncio
async def test_a_rename_into_place_edit_triggers_one_debounced_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The safe-write pattern (write a temp name, then atomically replace) -- phaze-mvq8z.3's
    measured DELETE+CREATE pair for a rename must coalesce into exactly one reload("file")."""
    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    watcher = RuntimeConfigWatcher(store, tmp_path, polling=True, poll_interval=_POLL_INTERVAL, settle_seconds=_SETTLE_SECONDS)
    watcher.start()
    try:
        tmp_file = tmp_path / f"{RUNTIME_TOML_NAME}.tmp"
        tmp_file.write_text('log_level = "DEBUG"\n', encoding="utf-8")
        tmp_file.replace(tmp_path / RUNTIME_TOML_NAME)

        await _wait_for_call_count(spy, 1)
        # Give one more full settle window to prove the delete+create pair collapsed into ONE
        # reload, not two.
        await asyncio.sleep(_SETTLE_SECONDS * 2)
        assert spy.await_count == 1
        spy.assert_awaited_once_with("file")
        assert store.last_result is not None
        assert store.last_result.outcome == "applied"
        assert store.current().log_level == "DEBUG"
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_an_in_place_edit_triggers_one_debounced_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The OTHER edit pattern phaze-mvq8z.3 measured: same inode, ordinary buffered write."""
    target = tmp_path / RUNTIME_TOML_NAME
    target.write_text('log_level = "WARNING"\n', encoding="utf-8")
    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    watcher = RuntimeConfigWatcher(store, tmp_path, polling=True, poll_interval=_POLL_INTERVAL, settle_seconds=_SETTLE_SECONDS)
    watcher.start()
    try:
        with target.open("w", encoding="utf-8") as handle:
            handle.write('log_level = "ERROR"\n')

        await _wait_for_call_count(spy, 1)
        await asyncio.sleep(_SETTLE_SECONDS * 2)
        assert spy.await_count == 1
        spy.assert_awaited_once_with("file")
        assert store.current().log_level == "ERROR"
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_a_same_content_rewrite_triggers_no_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-op rewrite (identical bytes) must not even ATTEMPT reload("file") -- the content-hash
    compare happens in the trigger itself, before ``store.reload`` is ever called."""
    target = tmp_path / RUNTIME_TOML_NAME
    body = 'log_level = "WARNING"\n'
    target.write_text(body, encoding="utf-8")
    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    watcher = RuntimeConfigWatcher(store, tmp_path, polling=True, poll_interval=_POLL_INTERVAL, settle_seconds=_SETTLE_SECONDS)
    watcher.start()
    try:
        # Same bytes, written a different way (a real editor "save" with no changes, or a
        # redeployed identical file) -- an in-place rewrite AND a rename-into-place, both with
        # the SAME content the watcher was seeded with.
        with target.open("w", encoding="utf-8") as handle:
            handle.write(body)
        await asyncio.sleep(_POLL_INTERVAL * 4)
        tmp_file = tmp_path / f"{RUNTIME_TOML_NAME}.tmp"
        tmp_file.write_text(body, encoding="utf-8")
        tmp_file.replace(target)

        # Wait well past a full settle window from the LAST touch; no call should ever land.
        await asyncio.sleep(_SETTLE_SECONDS * 3)
        spy.assert_not_awaited()
        # reload("file") was never called, so the store never even looked at the file layer --
        # it still reports the default it was constructed with, not the file's WARNING.
        assert store.current().log_level == "INFO"
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_an_edit_to_a_different_file_in_the_directory_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The watch is filtered to the ONE target filename -- a sibling file's edit (e.g. a future
    bead's backends.toml sharing the directory-mount requirement) must never fire a reload."""
    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    watcher = RuntimeConfigWatcher(store, tmp_path, polling=True, poll_interval=_POLL_INTERVAL, settle_seconds=_SETTLE_SECONDS)
    watcher.start()
    try:
        (tmp_path / "backends.toml").write_text("[[backends]]\n", encoding="utf-8")
        await asyncio.sleep(_SETTLE_SECONDS * 3)
        spy.assert_not_awaited()
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_a_burst_of_touches_within_the_settle_window_coalesces_to_one_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Several rapid edits inside one settle window still produce exactly one reload, of the
    LAST content written."""
    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    watcher = RuntimeConfigWatcher(store, tmp_path, polling=True, poll_interval=_POLL_INTERVAL, settle_seconds=_SETTLE_SECONDS)
    watcher.start()
    try:
        target = tmp_path / RUNTIME_TOML_NAME
        for level in ("DEBUG", "WARNING", "ERROR"):
            target.write_text(f'log_level = "{level}"\n', encoding="utf-8")
            await asyncio.sleep(_POLL_INTERVAL * 2)  # inside the settle window: each touch resets it

        await _wait_for_call_count(spy, 1)
        await asyncio.sleep(_SETTLE_SECONDS * 2)
        assert spy.await_count == 1
        assert store.current().log_level == "ERROR"
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_stop_stops_the_observer_thread_and_cancels_the_pending_debounce(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After stop(), the observer thread is joined and a subsequent edit is never observed."""
    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    watcher = RuntimeConfigWatcher(store, tmp_path, polling=True, poll_interval=_POLL_INTERVAL, settle_seconds=_SETTLE_SECONDS)
    watcher.start()
    observer = watcher._observer  # asserting real thread teardown, not just a mock call
    assert observer is not None
    assert observer.is_alive()

    await watcher.stop()
    assert not observer.is_alive()

    # A post-stop edit must never reach store.reload -- the debounce timer is gone AND the
    # observer thread that would have touched it is gone.
    (tmp_path / RUNTIME_TOML_NAME).write_text('log_level = "DEBUG"\n', encoding="utf-8")
    await asyncio.sleep(_SETTLE_SECONDS * 3)
    spy.assert_not_awaited()

    # Idempotent: a second stop() must not raise.
    await watcher.stop()


@pytest.mark.asyncio
async def test_stop_bounds_the_join_and_warns_on_a_wedged_observer_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged observer thread does not hang shutdown -- the join is bounded and logs a warning."""
    store = _store(tmp_path)
    watcher = RuntimeConfigWatcher(store, tmp_path, polling=True, poll_interval=_POLL_INTERVAL, settle_seconds=_SETTLE_SECONDS)

    fake_observer = MagicMock()
    fake_observer.is_alive.return_value = True  # still alive after join(timeout=10.0): wedged
    monkeypatch.setattr("phaze.runtime_config_triggers.PollingObserver", MagicMock(return_value=fake_observer))

    watcher.start()
    await watcher.stop()
    fake_observer.stop.assert_called_once()
    fake_observer.join.assert_called_once_with(timeout=10.0)


def test_start_is_best_effort_on_an_unwatchable_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory that cannot be watched (missing mount, exhausted watch limit, ...) logs a
    warning and leaves the process running -- SIGHUP/API/DB reload layers are unaffected."""
    store = _store(tmp_path)
    watcher = RuntimeConfigWatcher(store, tmp_path / "does-not-exist", polling=True, poll_interval=_POLL_INTERVAL, settle_seconds=_SETTLE_SECONDS)

    fake_observer = MagicMock()
    fake_observer.schedule.side_effect = OSError("no such directory")
    monkeypatch.setattr("phaze.runtime_config_triggers.PollingObserver", MagicMock(return_value=fake_observer))

    async def _run() -> None:
        watcher.start()  # must not raise

    asyncio.run(_run())
    assert watcher._observer is None


def test_build_watcher_uses_get_settings_directly_not_a_caller_local_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: ``build_watcher`` must resolve settings via its OWN ``get_settings`` import,
    never a parameter -- several startup-hook tests patch e.g.
    ``phaze.tasks.controller.get_settings`` with a bare ``MagicMock()`` that only stands in for
    the handful of attributes THAT test cares about; a ``build_watcher(store, settings)`` that
    accepted the local (mocked) settings would crash on ``Path(MagicMock())``."""
    store = MagicMock()
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: MagicMock())  # the trap this regresses
    watcher = build_watcher(store)
    assert watcher._directory.name == "runtime"  # the real default, /etc/phaze/runtime


# --- SIGHUP: in-process wiring tests. The real-subprocess delivery test lives in its own file. ---


@pytest.mark.asyncio
async def test_install_sighup_handler_triggers_exactly_one_reload_and_the_loop_keeps_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    loop = asyncio.get_running_loop()
    assert install_sighup_handler(loop, store) is True
    try:
        os.kill(os.getpid(), signal.SIGHUP)
        await _wait_for_call_count(spy, 1)
        await asyncio.sleep(_POLL_INTERVAL * 2)
        assert spy.await_count == 1
        spy.assert_awaited_once_with("sighup")

        # The loop is still alive and the handler is still installed -- SIGHUP never stops the
        # worker, and a SECOND signal produces a SECOND, independent reload.
        assert asyncio.get_running_loop() is loop
        os.kill(os.getpid(), signal.SIGHUP)
        await _wait_for_call_count(spy, 2)
        assert spy.await_count == 2
    finally:
        loop.remove_signal_handler(signal.SIGHUP)


def test_install_sighup_handler_off_the_main_thread_is_a_graceful_no_op() -> None:
    """``TestClient`` drives the api lifespan on an anyio portal thread with its own loop --
    ``add_signal_handler`` raises RuntimeError there, and it must not propagate."""
    store = MagicMock()
    outcome: dict[str, object] = {}

    def _run() -> None:
        async def _main() -> None:
            loop = asyncio.get_running_loop()
            outcome["installed"] = install_sighup_handler(loop, store)

        asyncio.run(_main())

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(timeout=5)
    assert outcome["installed"] is False
