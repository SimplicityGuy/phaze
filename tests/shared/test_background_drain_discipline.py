"""Guard: one strict drain for the router's background tasks, and no test may re-roll it (phaze-5lq8a).

``tests/_background_drain`` explains what an undrained pipeline-router background task does to the
shared per-test connection's savepoint stack. This module pins the two properties that stop the
defect coming back:

1. **One definition.** ``_background_tasks`` is reached through ``tests._background_drain`` and
   nowhere else under ``tests/``. It was six hand-rolled copies of a polling loop; every one of them
   carried the same bug, and the fix that landed for five of them
   (``fix(tests): make _drain_background actually await background tasks``) missed the sixth, in
   ``tests/metadata/test_retry_affordances.py``, which was still spinning on ``asyncio.sleep(0)``
   months later. The same argument ``tests/db_guard.BLOCKED_WAITER_SQL`` makes for the blocked-waiter
   barrier: one definition means the next correction lands everywhere at once.

2. **Complete or raise.** The drain must never return with work in flight. The predecessor's
   ``for _ in range(500): await asyncio.sleep(0)`` did exactly that -- ``sleep(0)`` yields to the
   event loop but never waits for I/O, so under load the yields ran out while a task was still mid
   round-trip, and the helper returned as if it had drained. The caller then asserted against
   half-finished work and handed its fixtures a savepoint stack another session was still mutating,
   which surfaced as ``InvalidSavepointSpecificationError`` at teardown of a test that PASSED. This
   is pinned behaviourally rather than by a source scan for ``sleep(0)``: a single yield is a
   legitimate idiom elsewhere in the suite (letting a just-created task reach its first await), so
   the property worth enforcing is what the drain DOES, not which primitive it avoids.

Property 1's scan is a source check rather than a runtime one for the reason
``tests/shared/test_cluster_wide_catalog_scoping.py`` gives: the defect is dormant on a small
isolated run, so a runtime assertion would go green in exactly the conditions where the bug cannot
show itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re

import pytest

from tests._background_drain import drain_router_background_tasks, pending_router_background_tasks


REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

# The modules allowed to reach into the router's task registry: the drain itself, and this guard,
# which has to plant a task in the registry to prove the drain refuses to return without it.
REGISTRY_OWNERS = frozenset({TESTS_ROOT / "_background_drain.py", Path(__file__).resolve()})

# Executable use, not prose: `pipeline_mod._background_tasks`, `from ... import _background_tasks`,
# `_background_tasks.discard`. A docstring that merely names the registry is fine and common.
_REGISTRY_USE_RE = re.compile(r"(?<![\w`])_background_tasks(?![\w`])")


def _test_sources() -> list[Path]:
    return sorted(TESTS_ROOT.rglob("*.py"))


def _executable_lines(path: Path) -> list[str]:
    """Lines outside docstrings/comments -- a prose mention of the registry is not a use of it.

    Deliberately crude (strip ``#`` comments and any line inside a triple-quoted block) rather
    than an AST walk: the goal is to be obviously right about what it skips, and every module under
    ``tests/`` documents these registries heavily in prose.
    """
    lines: list[str] = []
    in_docstring = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        fence_count = raw.count('"""')
        if in_docstring:
            if fence_count:
                in_docstring = fence_count % 2 == 0
            continue
        if fence_count and fence_count % 2 == 1:
            in_docstring = True
            continue
        code = raw.split("#", 1)[0]
        if code.strip():
            lines.append(code)
    return lines


def test_only_the_shared_helper_reaches_into_the_router_task_registry() -> None:
    """No test module may poll ``_background_tasks`` itself -- it must call the shared drain."""
    offenders = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in _test_sources()
        if path not in REGISTRY_OWNERS and any(_REGISTRY_USE_RE.search(line) for line in _executable_lines(path))
    )
    assert offenders == [], (
        "these test modules reach into `phaze.routers.pipeline._background_tasks` directly instead of "
        f"awaiting `tests._background_drain.drain_router_background_tasks()`: {offenders}. Six hand-rolled "
        "copies of that loop all carried the same savepoint-corrupting bug (phaze-5lq8a)."
    )


def test_the_scan_actually_reaches_the_modules_it_is_meant_to_police() -> None:
    """An over-tight matcher would pass vacuously forever -- pin it against real, current call sites."""
    drain_callers = {
        str(path.relative_to(REPO_ROOT)) for path in _test_sources() if "drain_router_background_tasks()" in path.read_text(encoding="utf-8")
    }
    for expected in (
        "tests/analyze/test_retry_affordances.py",
        "tests/metadata/test_retry_affordances.py",
        "tests/integration/routers/test_pipeline_analysis_retry_clears_marker.py",
        "tests/integration/routers/test_pipeline_retry_group_restore_xor_guard.py",
        "tests/shared/routers/test_pipeline.py",
    ):
        assert expected in drain_callers, f"{expected} triggers a backgrounding endpoint but no longer drains"
    assert _REGISTRY_USE_RE.search("pending = set(pipeline_mod._background_tasks)"), "the registry matcher stopped matching a real use"
    assert not _REGISTRY_USE_RE.search("the ``_background_tasks`` discipline"), "a prose mention must not be flagged"


@pytest.mark.asyncio
async def test_the_drain_raises_rather_than_returning_with_work_in_flight() -> None:
    """The property the whole bead turns on: an undrainable task is an ERROR, never a silent return."""
    import phaze.routers.pipeline as pipeline_mod

    never_finishes = asyncio.create_task(asyncio.Event().wait(), name="phaze-5lq8a-probe")
    pipeline_mod._background_tasks.add(never_finishes)
    try:
        with pytest.raises(AssertionError, match="did not drain"):
            await drain_router_background_tasks(timeout=0.05)
    finally:
        pipeline_mod._background_tasks.discard(never_finishes)
        never_finishes.cancel()


@pytest.mark.asyncio
async def test_the_drain_returns_once_the_tasks_have_actually_completed() -> None:
    """And it must not be trivially strict: real work that finishes drains without complaint."""
    import phaze.routers.pipeline as pipeline_mod

    finished = False

    async def _work() -> None:
        nonlocal finished
        await asyncio.sleep(0.01)
        finished = True

    task = asyncio.create_task(_work(), name="phaze-5lq8a-work")
    pipeline_mod._background_tasks.add(task)
    task.add_done_callback(pipeline_mod._background_tasks.discard)

    await drain_router_background_tasks(timeout=5.0)

    assert finished, "the drain returned before the task it was waiting on had run"
    assert pending_router_background_tasks() == []
