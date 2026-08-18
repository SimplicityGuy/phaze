"""Guard: the suite's two "wait for it to settle" helpers, and the shape neither may go back to (phaze-5lq8a).

This module covers ``tests/_background_drain`` (awaits a known set of router background TASKS) and
``tests/_async_settle`` (polls a PREDICATE nothing hands you a handle to). They wait for different
things and share one failure mode: a bounded loop that gives up SILENTLY and lets the caller carry
on. ``_background_drain``'s docstring has the savepoint-stack damage that made it a P0; the
properties pinned here are what stop either coming back:

1. **One definition each.** ``_background_tasks`` is reached through ``tests._background_drain``
   and nowhere else under ``tests/``; ``wait_until`` lives in ``tests._async_settle`` rather than
   being re-typed per module. Both were copy-paste before this, and in both cases EVERY copy
   carried the identical bug: six of the drain (the fix that landed for five of them,
   ``fix(tests): make _drain_background actually await background tasks``, missed the sixth in
   ``tests/metadata/test_retry_affordances.py``, still spinning months later) and three of
   ``_wait_until``. The third of those was found by this very guard, after a hand grep had reported
   two -- which is the argument for the guard in one line. Same reasoning as
   ``tests/db_guard.BLOCKED_WAITER_SQL``: one definition means the next correction lands everywhere
   at once.

2. **Complete or raise.** Neither helper may return with the thing it waited for unfinished. The
   drain's predecessor, ``for _ in range(500): await asyncio.sleep(0)``, did exactly that --
   ``sleep(0)`` yields to the event loop but never waits for I/O, so under load the yields ran out
   while a task was still mid round-trip and it returned as if drained. The caller then asserted
   against half-finished work and handed its fixtures a savepoint stack another session was still
   mutating, surfacing as ``InvalidSavepointSpecificationError`` at teardown of a test that PASSED.
   ``_wait_until``'s version of the same mistake returned quietly on its deadline.

   Pinned behaviourally rather than by a source scan for ``sleep(0)``: a single yield is a
   legitimate idiom elsewhere in the suite (letting a just-created task reach its first await), and
   it is the RIGHT primitive inside ``wait_until``, whose predicate is driven by a task on this same
   loop. What matters is what the helpers DO on timeout, not which primitive they use.

Property 1's scan is a source check rather than a runtime one for the reason
``tests/shared/test_cluster_wide_catalog_scoping.py`` gives: the defect is dormant on a small
isolated run, so a runtime assertion would go green in exactly the conditions where the bug cannot
show itself.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
import re
import tokenize

import pytest

from tests._async_settle import wait_until
from tests._background_drain import drain_router_background_tasks, pending_router_background_tasks


REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

# The modules allowed to reach into the router's task registry: the drain itself, and this guard,
# which has to plant a task in the registry to prove the drain refuses to return without it.
REGISTRY_OWNERS = frozenset({TESTS_ROOT / "_background_drain.py", Path(__file__).resolve()})

# Executable use, not prose: `pipeline_mod._background_tasks`, `from ... import _background_tasks`,
# `_background_tasks.discard`. A docstring that merely names the registry is fine and common.
_REGISTRY_USE_RE = re.compile(r"(?<![\w`])_background_tasks(?![\w`])")

# A private re-roll of the settle helper. Written with `\s+` so this line does not match ITSELF --
# the same self-reference dodge `_REGISTRY_USE_RE` uses. `tests/browser/conftest.py`'s
# `_wait_until_serving` is deliberately not matched: it already raises on its deadline.
_SETTLE_HELPER_RE = re.compile(r"def\s+_wait_until\s*\(")


def _test_sources() -> list[Path]:
    return sorted(TESTS_ROOT.rglob("*.py"))


def _executable_lines(path: Path) -> list[str]:
    """Lines with every comment and string literal blanked out -- a prose mention is not a use.

    Tokenised rather than scanned for ``\"\"\"`` fences. The hand-rolled fence counter this replaces
    got the state machine wrong on a ONE-LINE docstring (two fences on a line, even count, so it
    neither entered nor left the block) and desynchronised for the rest of the file -- which made
    this very module's own prose read as code and flagged it as an offender. Modules under
    ``tests/`` document these helpers heavily, so the stripper has to be right rather than brief.
    """
    source = path.read_text(encoding="utf-8")
    rows = source.splitlines()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError):  # pragma: no cover -- every file under tests/ parses
        return rows
    blanked = [list(row) for row in rows]
    for token in tokens:
        if token.type not in (tokenize.COMMENT, tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row_index in range(start_row - 1, min(end_row, len(blanked))):
            row = blanked[row_index]
            first = start_col if row_index == start_row - 1 else 0
            last = end_col if row_index == end_row - 1 else len(row)
            row[first:last] = " " * (min(last, len(row)) - min(first, len(row)))
    return [text for text in ("".join(row) for row in blanked) if text.strip()]


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


# --------------------------------------------------------------------------------------------------
# The same two properties for `tests/_async_settle.wait_until`, the predicate-shaped sibling.
# --------------------------------------------------------------------------------------------------
def test_no_test_module_defines_its_own_settle_helper() -> None:
    """``_wait_until`` was THREE byte-identical copies, all carrying the silent give-up.

    Matched on ``def _wait_until(`` rather than a prefix: ``tests/browser/conftest.py`` has a
    ``_wait_until_serving`` boot poller that already RAISES on its deadline, which is the correct
    shape and not an offender.
    """
    offenders = sorted(
        str(path.relative_to(REPO_ROOT)) for path in _test_sources() if any(_SETTLE_HELPER_RE.search(line) for line in _executable_lines(path))
    )
    assert offenders == [], f"these modules re-roll a private settle helper instead of importing `tests._async_settle.wait_until`: {offenders}"


@pytest.mark.asyncio
async def test_wait_until_raises_rather_than_returning_on_an_unsettled_predicate() -> None:
    """A predicate that never comes true is an ERROR, not a quiet return into the assertions below."""
    with pytest.raises(AssertionError, match="still false after"):
        await wait_until(lambda: False, timeout=0.01, description="a condition that never holds")


@pytest.mark.asyncio
async def test_wait_until_returns_as_soon_as_the_predicate_holds() -> None:
    """And it must not be trivially strict: it returns on the first true reading, including immediately."""
    ticks = 0

    def _true_on_third_look() -> bool:
        nonlocal ticks
        ticks += 1
        return ticks >= 3

    await wait_until(_true_on_third_look, timeout=2.0, description="true on the third look")

    assert ticks == 3, "wait_until kept polling after the predicate went true"
    await wait_until(lambda: True, timeout=0.0, description="already true")  # a zero budget is fine when nothing must change
