"""Unit tests for phaze.agent_watcher.__main__'s sweep-loop decomposition (phaze-bk9el.13).

Filename mirrors the codebase's own ``test_<stem>.py`` convention applied to the
``__main__`` stem, so this module (rather than the broader ``test_main.py`` in
this same directory, which covers ``main()``'s bootstrap sequence) is the
dedicated test file for ``__main__.py``.

phaze-bk9el.13 flattened ``_sweep_loop`` (was nesting 5 levels deep) by
extracting three helpers:

    - ``_post_ready_paths``     -- POST each settled path; one failure must
                                    not stop the rest (inner ``except``).
    - ``_log_evicted_paths``    -- log each path the debouncer dropped.
    - ``_run_sweep_iteration``  -- one sweep tick: call the two above, wrapped
                                    in the outer ``except`` that keeps the
                                    unattended loop alive across an
                                    unanticipated failure.

Each is unit-tested directly below, then ``_sweep_loop`` itself is tested at
the integration level (it owns only the ``while`` / wait-for-shutdown shell
now). Behavior is unchanged from the pre-refactor ``_sweep_loop`` -- these
tests assert the exact same log messages and survival contract as before.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
from typing import TYPE_CHECKING, Any
import unicodedata
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

import phaze.agent_watcher.__main__ as wmain
from phaze.agent_watcher.debouncer import Debouncer


if TYPE_CHECKING:
    from pathlib import Path


# _post_ready_paths: posts each ready path; a single post_one failure is
# logged and does NOT stop the remaining paths from being attempted
# (Pitfall 1 -- the entry has already been removed from the debouncer).
async def test_post_ready_paths_posts_every_path_in_order() -> None:
    fake_poster = MagicMock()
    fake_poster.post_one = AsyncMock()

    await wmain._post_ready_paths(fake_poster, ["/data/music/a.mp3", "/data/music/b.mp3"])

    assert fake_poster.post_one.await_count == 2
    fake_poster.post_one.assert_any_await("/data/music/a.mp3")
    fake_poster.post_one.assert_any_await("/data/music/b.mp3")


async def test_post_ready_paths_survives_a_single_post_failure(caplog: pytest.LogCaptureFixture) -> None:
    """One path's post_one raising must not stop the others from being posted."""
    call_state = {"n": 0}

    async def _post_one(path: str) -> None:
        call_state["n"] += 1
        if path.endswith("b.mp3"):
            raise RuntimeError("simulated post failure (sweep loop survival contract)")

    fake_poster = MagicMock()
    fake_poster.post_one = _post_one

    with caplog.at_level(logging.WARNING, logger="phaze.agent_watcher.__main__"):
        await wmain._post_ready_paths(fake_poster, ["/data/music/a.mp3", "/data/music/b.mp3", "/data/music/c.mp3"])

    # All three were attempted despite the middle one raising.
    assert call_state["n"] == 3
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "post failed" in text and "/data/music/b.mp3" in text, f"expected post-failure log; got: {text!r}"


async def test_post_ready_paths_posts_a_moved_path_with_its_lineage_then_forgets_it() -> None:
    """phaze-oxn2m: lineage is read before the post and released only after it -- also on failure."""
    debouncer = Debouncer()
    debouncer.move("/data/music/incomplete/a.mp3", "/data/music/a.mp3")
    debouncer.move("/data/music/incomplete/b.mp3", "/data/music/b.mp3")
    fake_poster = MagicMock()
    fake_poster.post_one = AsyncMock(side_effect=[None, RuntimeError("simulated post failure")])

    await wmain._post_ready_paths(fake_poster, ["/data/music/a.mp3", "/data/music/b.mp3", "/data/music/c.mp3"], debouncer)

    fake_poster.post_one.assert_any_await("/data/music/a.mp3", previous_paths=["/data/music/incomplete/a.mp3"])
    fake_poster.post_one.assert_any_await("/data/music/b.mp3", previous_paths=["/data/music/incomplete/b.mp3"])
    fake_poster.post_one.assert_any_await("/data/music/c.mp3")
    assert debouncer.lineage("/data/music/a.mp3") == []
    assert debouncer.lineage("/data/music/b.mp3") == []


# check_paths: the agent-side existence step of the phaze-oxn2m stale-row cleanup.
def test_check_paths_marks_present_absent_and_unjudgeable_members(tmp_path: Path) -> None:
    root = tmp_path / "incoming"
    (root / "rel").mkdir(parents=True)
    (root / "rel" / "a.mp3").write_bytes(b"x")
    nfd_name = unicodedata.normalize("NFD", "Bjo\u0308rk.mp3")
    (root / "rel" / nfd_name).write_bytes(b"y")
    document = {
        "agent_id": "agent-a",
        "scan_roots": [str(root), str(tmp_path / "not-mounted")],
        "groups": [
            [
                {"id": "1", "path": f"{root}/incomplete/rel/a.mp3"},
                {"id": "2", "path": f"{root}/rel/a.mp3"},
                {"id": "3", "path": f"{root}/rel/{unicodedata.normalize('NFC', nfd_name)}"},
                {"id": "4", "path": f"{tmp_path}/not-mounted/rel/a.mp3"},
            ]
        ],
    }

    checked = wmain.check_paths(document)

    assert [member["exists"] for member in checked["groups"][0]] == [False, True, True, None]


def test_check_paths_cli_reads_stdin_and_writes_the_annotated_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.mp3").write_bytes(b"x")
    document = {"agent_id": "agent-a", "scan_roots": [str(tmp_path)], "groups": [[{"id": "1", "path": str(tmp_path / "a.mp3")}]]}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(document)))
    monkeypatch.setattr(sys, "stdout", out)

    real_resolve = wmain.resolve_media_path

    def _logging_resolve(path: str) -> str:
        structlog.get_logger("phaze.services.media_path_resolve").info("resolve_media_path: NFC/NFD on-disk name mismatch resolved")
        return real_resolve(path)

    monkeypatch.setattr(wmain, "resolve_media_path", _logging_resolve)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)

    assert wmain._main_check_paths() == 0  # the autouse logging fixture resets structlog afterwards

    assert json.loads(out.getvalue())["groups"][0][0]["exists"] is True, "stdout must be the JSON document alone"
    assert "mismatch resolved" in err.getvalue()


def test_entrypoint_dispatches_check_paths_or_the_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    check = MagicMock(return_value=0)
    run = MagicMock()
    monkeypatch.setattr(wmain, "_main_check_paths", check)
    monkeypatch.setattr(wmain.asyncio, "run", run)
    monkeypatch.setattr(wmain, "main", MagicMock(return_value="watcher-coroutine"))

    with pytest.raises(SystemExit) as exited:
        wmain._entrypoint(["check-paths"])
    assert exited.value.code == 0
    run.assert_not_called()

    wmain._entrypoint([])
    run.assert_called_once_with("watcher-coroutine")


# _log_evicted_paths: logs a WARNING per evicted path, no side effects beyond
# logging.
def test_log_evicted_paths_logs_each_path(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="phaze.agent_watcher.__main__"):
        wmain._log_evicted_paths(["/data/music/stuck.mp3", "/data/music/also-stuck.mp3"])

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "stuck.mp3" in text and "max_pending" in text
    assert "also-stuck.mp3" in text


def test_log_evicted_paths_no_op_on_empty_list(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="phaze.agent_watcher.__main__"):
        wmain._log_evicted_paths([])

    assert caplog.records == []


# _run_sweep_iteration: one sweep tick -- happy path, and the outer `except`
# that keeps a single bad tick from ever propagating out of the loop.
async def test_run_sweep_iteration_happy_path_posts_and_logs() -> None:
    fake_debouncer = MagicMock()
    fake_debouncer.sweep = MagicMock(return_value=(["/data/music/a.mp3"], ["/data/music/stuck.mp3"]))
    fake_debouncer.lineage = MagicMock(return_value=[])
    fake_poster = MagicMock()
    fake_poster.post_one = AsyncMock()

    await wmain._run_sweep_iteration(
        debouncer=fake_debouncer,
        poster=fake_poster,
        settle_period=5.0,
        max_pending=3600.0,
    )

    fake_debouncer.sweep.assert_called_once_with(settle_period=5.0, max_pending=3600.0)
    fake_poster.post_one.assert_awaited_once_with("/data/music/a.mp3")


async def test_run_sweep_iteration_outer_except_swallows_sweep_failure(caplog: pytest.LogCaptureFixture) -> None:
    """If debouncer.sweep itself raises, the iteration logs and returns -- never raises.

    This is the case the inner ``_post_ready_paths`` try/except cannot cover:
    the failure happens before there is anything to post.
    """
    fake_debouncer = MagicMock()
    fake_debouncer.sweep = MagicMock(side_effect=RuntimeError("debouncer.sweep exploded"))
    fake_poster = MagicMock()

    with caplog.at_level(logging.ERROR, logger="phaze.agent_watcher.__main__"):
        await wmain._run_sweep_iteration(
            debouncer=fake_debouncer,
            poster=fake_poster,
            settle_period=5.0,
            max_pending=3600.0,
        )

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "sweep iteration failed" in text, f"expected outer-except log; got: {text!r}"


# _sweep_loop: the thin while/wait-for-shutdown shell around one iteration.
async def test_sweep_loop_delegates_to_run_sweep_iteration_each_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """_sweep_loop's own job is just the while-guard + inter-tick wait; verify

    it calls ``_run_sweep_iteration`` with the loop's own args on every tick
    until shutdown, and stops calling it once shutdown_event is set.
    """
    shutdown = asyncio.Event()
    call_args: list[tuple[Any, Any, float, float]] = []

    async def _fake_iteration(debouncer: Any, poster: Any, settle_period: float, max_pending: float) -> None:
        call_args.append((debouncer, poster, settle_period, max_pending))
        if len(call_args) >= 3:
            shutdown.set()

    monkeypatch.setattr(wmain, "_run_sweep_iteration", _fake_iteration)

    fake_debouncer = MagicMock()
    fake_poster = MagicMock()
    await wmain._sweep_loop(
        debouncer=fake_debouncer,
        poster=fake_poster,
        sweep_interval=0.001,
        settle_period=5.0,
        max_pending=3600.0,
        shutdown_event=shutdown,
    )

    assert len(call_args) == 3
    assert all(args == (fake_debouncer, fake_poster, 5.0, 3600.0) for args in call_args)


async def test_sweep_loop_exits_immediately_when_shutdown_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-set shutdown_event means the loop body never runs at all."""
    shutdown = asyncio.Event()
    shutdown.set()
    call_count = {"n": 0}

    async def _fake_iteration(*_args: Any, **_kwargs: Any) -> None:
        call_count["n"] += 1

    monkeypatch.setattr(wmain, "_run_sweep_iteration", _fake_iteration)

    await wmain._sweep_loop(
        debouncer=MagicMock(),
        poster=MagicMock(),
        sweep_interval=0.001,
        settle_period=5.0,
        max_pending=3600.0,
        shutdown_event=shutdown,
    )

    assert call_count["n"] == 0


async def test_sweep_loop_posts_ready_logs_evicted_then_exits(caplog: pytest.LogCaptureFixture) -> None:
    """End-to-end through the real (non-monkeypatched) helper chain.

    Covers the full loop body: sweep call, post_one on ready entries,
    warning log on evicted entries, swallow Exception raised by post_one,
    and the wait_for(timeout=sweep_interval) tick -- unchanged behavior
    from the pre-refactor single-function ``_sweep_loop``.
    """
    shutdown = asyncio.Event()
    fake_debouncer = MagicMock()
    # Two ready (one will succeed, one will raise) and one evicted.
    fake_debouncer.sweep = MagicMock(return_value=(["/data/music/a.mp3", "/data/music/b.mp3"], ["/data/music/stuck.mp3"]))
    fake_debouncer.lineage = MagicMock(return_value=[])

    fake_poster = MagicMock()
    call_state = {"n": 0}

    async def _post_one(path: str) -> None:
        call_state["n"] += 1
        if path.endswith("b.mp3"):
            raise RuntimeError("simulated post failure (sweep loop survival contract)")
        # On the second successful post, set shutdown so the loop exits cleanly
        # after the wait_for tick.
        shutdown.set()

    fake_poster.post_one = _post_one

    with caplog.at_level(logging.WARNING, logger="phaze.agent_watcher.__main__"):
        await wmain._sweep_loop(
            debouncer=fake_debouncer,
            poster=fake_poster,
            sweep_interval=0.01,
            settle_period=0.0,
            max_pending=3600.0,
            shutdown_event=shutdown,
        )

    # Both ready paths were attempted (the failing one did not abort the loop).
    assert call_state["n"] >= 2
    text = "\n".join(r.getMessage() for r in caplog.records)
    # The evicted path produced a WARNING.
    assert "stuck.mp3" in text and "max_pending" in text, f"expected eviction warning; got: {text!r}"
    # The exception from post_one was captured (not propagated).
    assert "post failed" in text, f"expected post-failure log; got: {text!r}"


async def test_sweep_loop_outer_except_swallows_sweep_failure(caplog: pytest.LogCaptureFixture) -> None:
    """End-to-end: debouncer.sweep raising does not crash the loop.

    If debouncer.sweep ITSELF raises (not just an individual post_one -- those
    are already wrapped by an inner try/except), the loop must log and
    continue rather than crashing the watcher. The shutdown_event check on
    the next iteration takes the loop down cleanly.
    """
    shutdown = asyncio.Event()
    fake_debouncer = MagicMock()
    state = {"sweeps": 0}

    def _sweep_then_set_shutdown(**_kwargs: Any) -> tuple[list[str], list[str]]:
        state["sweeps"] += 1
        # First sweep raises; set shutdown so the loop exits on the next
        # iteration via the while-guard.
        shutdown.set()
        raise RuntimeError("debouncer.sweep exploded")

    fake_debouncer.sweep = _sweep_then_set_shutdown

    fake_poster = MagicMock()

    with caplog.at_level(logging.ERROR, logger="phaze.agent_watcher.__main__"):
        await wmain._sweep_loop(
            debouncer=fake_debouncer,
            poster=fake_poster,
            sweep_interval=0.01,
            settle_period=0.0,
            max_pending=3600.0,
            shutdown_event=shutdown,
        )

    assert state["sweeps"] == 1
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "sweep iteration failed" in text, f"expected outer-except log; got: {text!r}"
