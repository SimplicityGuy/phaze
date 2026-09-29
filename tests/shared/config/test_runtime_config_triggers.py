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
import contextlib
import gc
import os
from pathlib import Path
import signal
import threading
from unittest.mock import AsyncMock, MagicMock
import weakref

import pytest

from phaze.config import ControlSettings
from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore
from phaze.runtime_config_triggers import RuntimeConfigWatcher, build_backends_watcher, build_watcher, install_sighup_handler


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
    """Several rapid touches inside one settle window still produce exactly one reload, of the
    LAST content written.

    Drives the debounce timer directly via ``watcher._on_touch()`` -- what the watchdog event
    handler calls through ``call_soon_threadsafe``, the exact code path under test -- rather than
    through a real observer thread polling real filesystem events. The earlier version wrote to
    disk and waited a fixed multiple of ``_POLL_INTERVAL`` between writes, relying on the real
    PollingObserver's background thread noticing each write inside that window; under host load
    (this gate runs alongside other full suites sharing the machine) that OS thread's own poll
    cadence could slip past the settle window, so a touch some measured evidence expected to
    coalesce instead landed as its own separate reload -- a real flake, not a bug in the trigger.
    No real observer is started here, so there is no cross-thread OS scheduling for load to
    perturb; the only remaining wall-clock dependency is the settle wait below, which -- as in
    every other test in this file -- only needs to be reached EVENTUALLY, not on a tight
    inter-event schedule.
    """
    target = tmp_path / RUNTIME_TOML_NAME
    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    watcher = RuntimeConfigWatcher(store, tmp_path, polling=True, poll_interval=_POLL_INTERVAL, settle_seconds=_SETTLE_SECONDS)
    # Deliberately no watcher.start(): see the docstring above.
    try:
        for level in ("DEBUG", "WARNING", "ERROR"):
            target.write_text(f'log_level = "{level}"\n', encoding="utf-8")
            watcher._on_touch()  # what on_created/on_modified/on_deleted schedule via call_soon_threadsafe

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


def test_the_default_is_polling_not_native() -> None:
    """Dispatcher decision (phaze-mvq8z dispatch, reviewing this bead before submit): default to
    PollingObserver, not native -- phaze-mvq8z.3 measured native silently seeing ZERO host edits
    through a Colima/virtiofs bind mount (no error, just no reload), and neither Docker Desktop nor
    a native Linux host mount (the production target) is verified either way. A silently-inert
    trigger is strictly worse than polling one small directory every second."""
    assert ControlSettings().runtime_config_watch_polling is True
    assert build_watcher(MagicMock())._polling is True  # asserting the resolved value, not just the settings field


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


@pytest.mark.asyncio
async def test_the_sighup_reload_task_is_held_strongly_until_it_finishes() -> None:
    """phaze-mvq8z.20 finding 3: asyncio holds tasks WEAKLY, so a fire-and-forget reload task whose
    only other referent is the future it awaits is a garbage cycle -- ``gc.collect()`` destroys it
    mid-flight ("Task was destroyed but it is pending!"), losing the reload, possibly while it holds
    the store lock. The reload here parks on a future nothing outside the task references, which is
    exactly that cycle; the handler must keep the task alive until it finishes, then let it go.
    """
    started = asyncio.Event()

    class _ParkedStore:
        async def reload(self, _source: str) -> None:
            started.set()
            await asyncio.get_running_loop().create_future()

    loop = asyncio.get_running_loop()
    assert install_sighup_handler(loop, _ParkedStore()) is True  # type: ignore[arg-type]
    try:
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.wait_for(started.wait(), timeout=_WAIT_TIMEOUT)
        task_ref = weakref.ref(next(task for task in asyncio.all_tasks() if task.get_name() == "runtime-config-sighup-reload"))

        gc.collect()
        task = task_ref()
        assert task is not None, "the in-flight SIGHUP reload task was garbage-collected"
        assert not task.done()

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        del task
        await asyncio.sleep(0)  # let the done-callbacks the finished task scheduled run
        gc.collect()
        assert task_ref() is None, "a FINISHED reload task must be released, not accumulated"
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


# --- backends.toml's own watcher (phaze-mvq8z.8): a SECOND RuntimeConfigWatcher instance. ---


def test_build_backends_watcher_uses_the_real_backends_config_path_not_a_caller_local_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same regression ``test_build_watcher_uses_get_settings_directly...`` guards for the
    runtime.toml watcher: must resolve via ``ControlSettings.model_config`` / its own
    ``get_settings`` import, never a caller-local mocked binding."""
    store = MagicMock()
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: MagicMock())  # the trap this regresses
    monkeypatch.delenv("PHAZE_BACKENDS_CONFIG_FILE", raising=False)
    watcher = build_backends_watcher(store)
    assert watcher._directory == Path("/etc/phaze")  # the real default backends.toml's parent
    assert watcher._target_name == "backends.toml"


def test_build_backends_watcher_follows_a_configured_backends_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The default paths differ -- ``/etc/phaze/backends.toml`` vs. ``/etc/phaze/runtime/runtime.toml``
    -- so a deployment pointing PHAZE_BACKENDS_CONFIG_FILE elsewhere must watch THAT directory."""
    monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", str(tmp_path / "elsewhere" / "backends.toml"))
    watcher = build_backends_watcher(MagicMock())
    assert watcher._directory == tmp_path / "elsewhere"
    assert watcher._target_name == "backends.toml"


@pytest.mark.asyncio
async def test_a_backends_toml_edit_triggers_one_debounced_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end proof (bead acceptance): a backends-only file edit reloads the registry.

    ``store.reload("file")`` itself doesn't touch backends.toml at all (it lives outside
    RuntimeConfig by design -- see ``phaze.runtime_config_backends``'s module docstring); what
    this test proves is narrower and just as necessary: the SECOND watcher this bead adds
    actually calls ``store.reload("file")`` when ``backends.toml`` -- not ``runtime.toml`` --
    changes, using the real watchdog debounce/hash pipeline, not a bare unit-level ``.reload()``
    call.
    """
    backends_path = tmp_path / "backends.toml"
    monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", str(backends_path))
    monkeypatch.setenv("PHAZE_RUNTIME_CONFIG_WATCH_POLLING", "true")
    monkeypatch.setenv("PHAZE_RUNTIME_CONFIG_WATCH_POLL_INTERVAL_SECONDS", str(_POLL_INTERVAL))
    monkeypatch.setenv("PHAZE_RUNTIME_CONFIG_WATCH_DEBOUNCE_SECONDS", str(_SETTLE_SECONDS))
    backends_path.write_text('[[backends]]\nkind = "local"\nid = "local"\nrank = 99\ncap = 1\n', encoding="utf-8")

    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    watcher = build_backends_watcher(store)
    watcher.start()
    try:
        backends_path.write_text(
            '[[backends]]\nkind = "local"\nid = "local"\nrank = 99\ncap = 2\n',
            encoding="utf-8",
        )
        await _wait_for_call_count(spy, 1)
        spy.assert_awaited_once_with("file")
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_an_edit_to_runtime_toml_does_not_touch_the_backends_watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two watchers are scoped to their own filename -- editing runtime.toml in the SAME
    directory backends.toml happens to live in must not fire the backends watcher (and, by the
    existing directory-watch test above, vice versa)."""
    monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", str(tmp_path / "backends.toml"))
    monkeypatch.setenv("PHAZE_RUNTIME_CONFIG_WATCH_POLLING", "true")
    monkeypatch.setenv("PHAZE_RUNTIME_CONFIG_WATCH_POLL_INTERVAL_SECONDS", str(_POLL_INTERVAL))
    monkeypatch.setenv("PHAZE_RUNTIME_CONFIG_WATCH_DEBOUNCE_SECONDS", str(_SETTLE_SECONDS))
    (tmp_path / "backends.toml").write_text('[[backends]]\nkind = "local"\nid = "local"\nrank = 99\ncap = 1\n', encoding="utf-8")

    store = _store(tmp_path)
    spy = _spy_reload(store, monkeypatch)
    watcher = build_backends_watcher(store)
    watcher.start()
    try:
        (tmp_path / RUNTIME_TOML_NAME).write_text("worker_max_jobs = 3\n", encoding="utf-8")
        await asyncio.sleep(_POLL_INTERVAL * 3 + _SETTLE_SECONDS * 2)
        assert spy.await_count == 0
    finally:
        await watcher.stop()
