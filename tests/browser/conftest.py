"""phaze-tzy6s.14: the real-browser harness.

Boots the ACTUAL application -- uvicorn, the real lifespan, real Alembic migrations, real Postgres
and Redis -- and drives it with Playwright. Nothing here is mocked, and that is the entire point:
this suite exists to cover the half of the contract server-side template assertions cannot see.

Why a separate harness rather than more httpx tests
====================================================

The unit suite asserts ``hx-*`` attributes as strings across 40-odd test files. It can prove the
markup says ``hx-target="#stage-workspace"``; it cannot prove htmx swapped anything, that Back
restored the right response shape, that focus survived the swap, that a dialog trapped focus, or
that a theme choice persisted across a reload. Every bug family in the phaze-8p1uq retrospective
lived in exactly that gap.

Scope and cost
==============

Playwright is NOT a project dependency (only Patchright is, because the 1001Tracklists render path
needs a browser at runtime, in production). This suite runs under the ephemeral
``uv run --with playwright`` idiom, the same one ``scripts/analyze_browser_soak.py`` uses, and is
excluded from the default pytest run by the ``browser`` marker. ``just test-browser`` is the entry
point; CI runs it as a separate non-blocking job.

Fixture scoping -- why the server fixture is SYNCHRONOUS
========================================================

``live_server`` is a plain sync fixture even though everything it does is I/O. Under
``asyncio_mode = "auto"`` pytest-asyncio gives each test a FUNCTION-scoped event loop, so a
session-scoped *async* fixture is created on the first test's loop and then awaited from a loop that
no longer exists on every subsequent test. The observed symptom is not an error but a HANG, which
cost this harness two full runs before the cause was found. Keeping the session-scoped work sync
sidesteps the loop-scope problem entirely rather than papering over it with ``loop_scope`` markers
that then have to be repeated on every test.

Playwright is launched per test for the same reason. It costs roughly a second per test and buys
complete isolation: no shared browser state, no cross-test storage leakage, no loop reuse.

Database isolation
==================

The app gets its OWN database, derived from this worktree's ``TEST_DATABASE_URL`` seat by appending
``_browser``. It is dropped and recreated at session start so a run is deterministic, and the app's
own ``run_migrations()`` builds the schema on boot -- the real startup path, not ``create_all``.

It must be a separate database from the unit suite's: ``tests/conftest.py`` drops the schema at
session teardown, and a live uvicorn holding connections to that database would both corrupt the
unit run and be corrupted by it. This is the same one-database-one-process rule the repo already
documents, applied across suites rather than across processes.

Redis is NOT separated the same way (phaze-8p1uq)
=================================================

The app inherits this worktree's ``PHAZE_REDIS_URL`` verbatim, so the browser suite and the unit
suite share one logical Redis database within a worktree. That was harmless while this suite only
rendered empty states; it is not any more, because the execute-dispatch tests below write real
``exec:*`` keys and ``tests/review/routers/test_execution_dispatch.py`` sweeps ``exec:*`` in fixture
setup AND teardown (CLAUDE.md, "Why Redis matters"). **Do not run the browser bucket and the unit
suite concurrently in one worktree.** Across worktrees they are already isolated, because
``test-db-for`` allocates a distinct logical database per seat. A per-suite index is not carved here
deliberately: the allocation registry is keyed by seat, and minting a second index per seat would
double every worktree's consumption of the 64 available for a hazard that sequencing removes.

Seeding
=======

``seed`` (see ``tests/browser/seed.py``) writes application rows to that same database over this
process's own connection. Read that module's docstring before adding a test that needs state -- in
particular, seed BEFORE navigating, because nothing re-reads a page that has already rendered.

CI diagnosability -- Playwright tracing on failure (phaze-17ni3)
================================================================

Before this, a failing browser test in CI left nothing behind but the pytest assertion text: no
trace, no screenshot, no artifact upload. The state that produced the failure was gone with the
runner the moment the job ended -- and this suite is exactly the kind that produces hard-to-
reproduce failures (an htmx settle race that vanished under instrumentation, a documented
``reset()`` deadlock seen once in 124 runs; see ``FLAKE_RECORD.md``).

Every context created by ``_browser_pages`` now starts a `Playwright trace
<https://playwright.dev/python/docs/trace-viewer>`_ (``screenshots=True, snapshots=True,
sources=True``) unconditionally, and the trace is written to ``test-results/traces/`` ONLY when the
test that owns it failed -- retain-on-failure, not always-on. This is Playwright's own recommended
default: starting a trace is cheap (it records into memory as the test runs), the expensive part is
exporting it, and a passing test discards it via ``tracing.stop()`` with no ``path``. The trace zip
is the high-value artifact: it embeds a per-action timeline, the DOM snapshot before/after each
action (so a screenshot never has to be captured separately), the network log, console output and
source locations -- opened with ``playwright show-trace`` or trace.playwright.dev.

Naming is per-test AND per-context: contexts are created per test (not per session -- see "Fixture
scoping" above), so a fixed filename would overwrite and a test that opens more than one context
(``page_at`` inside a loop, e.g. ``test_responsive_matrix.py``) would collide on the last one.
``_trace_path`` encodes the pytest node id (parametrization included), a per-node call counter
(``_browser_pages`` can be entered more than once per test) and the context's index within that
call.

Failure detection has to cover two different call shapes this harness exposes, and neither alone is
reliable:

- ``page`` / ``tablet_page`` / ``phone_page`` / ``open_page`` -- the ``_browser_pages`` generator is
  owned by a FIXTURE and its ``finally`` runs during pytest's teardown phase, after the test body has
  already returned or raised. An exception raised inside the test body never reaches this generator
  at all, so the only signal available is the recorded pytest report -- ``_test_failed`` reads it
  back off ``item.stash``, populated by the ``pytest_runtest_makereport`` hook below.
- ``page_at`` / ``open_page_cm`` -- used directly as ``async with page_at(...) as page:`` INSIDE the
  test body, so an exception raised in that block propagates through this generator's own ``yield``
  via ``athrow`` before the test has "returned" from pytest's point of view, and the stash lookup
  above would still see no report. The ``except BaseException`` clause below catches that case
  directly -- excluding ``pytest.skip()`` / ``pytest.xfail()``, which are ``BaseException`` too but
  are not failures; see ``_NON_FAILURE_OUTCOMES``.

Both paths funnel into one boolean so the save-or-discard decision at ``finally`` is a single check
regardless of which fixture a test used.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator


_BROWSER_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BROWSER_DIR.parent.parent
_BOOT_TIMEOUT_SEC = 180.0

# --- CI artifacts: Playwright tracing on failure (phaze-17ni3) ----------------------------------

_ARTIFACT_DIR = _REPO_ROOT / "test-results"
_TRACE_DIR = _ARTIFACT_DIR / "traces"

# Stashes the per-phase pytest report on the test item, so a fixture torn down AFTER the test body
# has already run/raised (see the module docstring) can still ask "did the test I was serving fail?"
_PHASE_REPORT_KEY = pytest.StashKey[dict[str, pytest.TestReport]]()

# A monotonic per-test counter for how many times `_browser_pages` has been entered -- `page_at`
# opens a fresh browser+context on every call, and a single test can call it more than once (e.g. a
# viewport/theme sweep in a loop), so the test's node id alone is not a unique trace filename.
_CALL_COUNTER_KEY = pytest.StashKey[int]()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Record each phase's report on the item so fixture teardown can read the outcome back.

    Standard hookwrapper pattern (the same one pytest-playwright uses): pytest calls this once per
    phase (setup/call/teardown); we let the real implementation run, then stash its result.
    """
    outcome = yield
    report = outcome.get_result()
    item.stash.setdefault(_PHASE_REPORT_KEY, {})[report.when] = report


def _test_failed(request: pytest.FixtureRequest) -> bool:
    """Whether the test currently owning ``request`` has already been recorded as failed.

    Only meaningful from fixture teardown, after the "call" phase report exists -- see the module
    docstring for why ``page_at``/``open_page_cm`` cannot rely on this alone.
    """
    reports = request.node.stash.get(_PHASE_REPORT_KEY, {})
    return any(report.failed for report in reports.values())


# Raised by `pytest.skip()` / `pytest.xfail()` -- both are `BaseException`, NOT `Exception`,
# specifically so plugins can tell "the test opted out" apart from "the test failed". Excluded from
# the `except BaseException` catch in `_browser_pages` below: `test_a_wide_table_scrolls_inside_...`
# (test_keyboard_screen_reader.py) calls `pytest.skip()` from INSIDE a `page_at` block for a
# viewport with no visible table, and without this exclusion that skip was caught, misread as a
# failure, and given a trace it did not earn -- found by running the real suite while building this,
# not by inspection.
_NON_FAILURE_OUTCOMES = (pytest.skip.Exception, pytest.xfail.Exception)


def _sanitize_for_filename(value: str) -> str:
    """Collapse a pytest node id (or any string) into a safe, readable filename component."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def _trace_path(request: pytest.FixtureRequest, *, call_index: int, context_index: int) -> Path:
    """Where to save a failed context's trace: unique per test, per call, per context."""
    node = _sanitize_for_filename(request.node.nodeid)
    return _TRACE_DIR / f"{node}__call{call_index}__ctx{context_index}.zip"


def _seat_dsn() -> str:
    """The worktree's isolated test DSN, defaulting to the documented single-agent target."""
    return os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test")


# Postgres NAMEDATALEN is 64, so an identifier is capped at 63 bytes and anything longer is
# TRUNCATED SILENTLY -- no error, no warning (phaze-tzy6s.17).
_PG_MAX_IDENTIFIER_BYTES = 63


def _browser_dsn() -> str:
    """A dedicated database for the live app, derived from this seat's DSN.

    The length check is not defensive padding. ``just test-db-for`` builds seat names as
    ``phaze_<derived>_test`` where ``<derived>`` carries the worktree name plus a hash, so appending
    ``_browser`` can push a long seat past Postgres's 63-byte identifier limit -- and Postgres
    truncates rather than failing. Two seats whose names differ only past byte 63 would then resolve
    to ONE database, which is the precise failure ``test-db-for``'s hash exists to prevent and which
    CLAUDE.md calls out as producing failures indistinguishable from real regressions. Better to
    refuse loudly here than to let two browser runs quietly share a database and recreate it under
    each other -- ``_recreate_database`` below does a DROP + CREATE, so the collision is destructive.
    """
    base, _, name = _seat_dsn().rpartition("/")
    browser_name = f"{name}_browser"
    if len(browser_name.encode()) > _PG_MAX_IDENTIFIER_BYTES:
        raise RuntimeError(
            f"browser database name {browser_name!r} is {len(browser_name.encode())} bytes, over Postgres's "
            f"{_PG_MAX_IDENTIFIER_BYTES}-byte identifier limit. Postgres would truncate it silently, so two seats "
            "could collide on one database and DROP each other's. Use a shorter worktree/seat name for "
            "`just test-db-for`."
        )
    return f"{base}/{browser_name}"


def _sync_dsn(dsn: str) -> str:
    """The same DSN without the ``+asyncpg`` driver suffix, which psycopg/Alembic cannot parse."""
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _recreate_database(dsn: str) -> None:
    """DROP + CREATE the browser database so every run starts from a known state."""
    import asyncpg

    base, _, name = dsn.rpartition("/")

    async def _run() -> None:
        conn = await asyncpg.connect(f"{_sync_dsn(base)}/postgres")
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    asyncio.run(_run())


def _wait_until_serving(base_url: str, process: subprocess.Popen[bytes]) -> None:
    """Poll until the app answers, failing loudly with its own output if it dies during boot."""
    import httpx

    deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
    with httpx.Client(timeout=5.0) as probe:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = (process.stdout.read() if process.stdout else b"").decode(errors="replace")
                raise RuntimeError(f"the app exited during boot (rc={process.returncode}):\n{output[-4000:]}")
            with contextlib.suppress(Exception):
                if probe.get(f"{base_url}/health").status_code == 200:
                    return
            time.sleep(0.5)
    raise RuntimeError(f"the app did not serve {base_url}/health within {_BOOT_TIMEOUT_SEC:.0f}s")


@pytest.fixture(scope="session")
def live_server() -> Iterator[str]:
    """A real uvicorn serving the real app against its own migrated database."""
    dsn = _browser_dsn()
    _recreate_database(dsn)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PHAZE_DATABASE_URL": dsn,
        "PHAZE_MIGRATIONS_DATABASE_URL": _sync_dsn(dsn),
        "PHAZE_QUEUE_URL": _sync_dsn(dsn),
        "PHAZE_REDIS_URL": os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0"),
        "PHAZE_ENABLE_SAQ_UI": "false",
    }
    process = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell, test harness
        [sys.executable, "-m", "uvicorn", "phaze.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_until_serving(base_url, process)
        yield base_url
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=15)
        if process.poll() is None:
            process.kill()


# --- Viewports and themes (phaze-fk1ww + phaze-8p1uq, reconciled) -------------------------------
#
# NAMED, not hardcoded per fixture. The suite shipped with TWO inline page fixtures, 1440x900 and
# 390x844: the two ends of the responsive contract with its middle missing. ADR-0009's breakpoint
# table forks at `lg` (1024px), so every width from `md` (768) up to 1023px takes the SAME drawer
# branch as the phone while having none of the phone's other properties -- a two-column workspace
# grid, a wide table, and enough room that a layout bug there is invisible at 390px. That band was
# never validated, and phaze-mrg1c was found in it.
#
# The tablet width is 768 DELIBERATELY. Two independent passes proposed 768 and 820; 768 wins
# because it is the width the RECORDED evidence was measured at -- ADR-0009's 2026-08-17 validation
# entry and every row of phaze-mrg1c's overflow table cite "tablet 768". Renaming the constant to
# 820 would silently invalidate both records. It is also the `md` boundary itself, which is where
# breakpoint bugs concentrate.
#
# Flat {width, height} rather than full context kwargs: tests index VIEWPORTS[name]["width"] to
# decide which side of a breakpoint they are on. Context-only properties (touch, is_mobile) are
# derived by _viewport_context_kwargs below, so a width stays a width.
VIEWPORTS: dict[str, dict[str, int]] = {
    # >= lg: the static expanded 280px rail branch.
    "desktop": {"width": 1440, "height": 900},
    # md (768) through lg-1: the drawer branch at a width the phone fixture cannot represent.
    "tablet": {"width": 768, "height": 1024},
    # Below md, and the exact width at which the pre-.13 icon-only rail was unusable -- therefore
    # the width the drawer contract must be proven at.
    "phone": {"width": 390, "height": 844},
}

# Both branches of `_applyTheme` (shell.html). "auto" is deliberately NOT a member: it resolves to
# one of these two via prefers-color-scheme, so it is a resolution mechanism to test once rather
# than a third rendered appearance to sweep every workspace in. Asserting dark-mode contrast under
# "auto" would assert the CI runner's preference, not the product's.
THEMES: tuple[str, ...] = ("light", "dark")


def _viewport_context_kwargs(viewport: str | dict[str, Any], theme: str | None = None) -> dict[str, Any]:
    """Playwright context kwargs for a named viewport, touch included where the device has it."""
    if isinstance(viewport, dict):
        return dict(viewport)
    kwargs: dict[str, Any] = {"viewport": VIEWPORTS[viewport]}
    if viewport == "phone":
        kwargs |= {"has_touch": True, "is_mobile": True}
    elif viewport == "tablet":
        # Touch without `is_mobile`: a tablet reports a real pointer-coarse input but not a mobile
        # viewport meta override, and `is_mobile` would silently rescale the layout under test.
        kwargs |= {"has_touch": True}
    if theme is not None:
        # Keep prefers-color-scheme in agreement with the pinned theme so shell.html's `auto`
        # branch cannot disagree with the explicit one.
        kwargs.setdefault("color_scheme", theme)
    return kwargs


async def _pin_theme(context: Any, theme: str | None) -> None:
    """Seed localStorage['phaze-theme'] via an INIT script, before the first paint.

    Not an evaluate() after load: shell.html applies the root class from an inline pre-flash IIFE
    that reads that key BEFORE Alpine loads, so a theme written after navigation is one repaint too
    late and the test would assert against the DEFAULT theme's computed styles -- the exact flake
    this indirection exists to avoid.
    """
    if theme is not None:
        await context.add_init_script(f"try {{ localStorage.setItem('phaze-theme', {json.dumps(theme)}); }} catch (e) {{}}")


@contextlib.asynccontextmanager
async def _browser_pages(live_server: str, request: pytest.FixtureRequest) -> AsyncGenerator[Any]:
    """Yield a factory that opens pages on ONE browser, closing every context on exit.

    One Chromium per test, N contexts within it -- the launch is the expensive part (~1s) and the
    context is what actually carries the isolation, so a test comparing two viewports or two themes
    pays for one browser rather than two.

    Every context this factory opens carries a Playwright trace from the moment it is created. On
    exit, a context's trace is exported to ``test-results/traces/`` if the test that owns it failed,
    and discarded otherwise -- see the module docstring, "CI diagnosability", for why the failure
    check needs both an exception-based path and a stashed-report-based path.
    """
    from playwright.async_api import async_playwright

    # `_browser_pages` can be entered more than once per test (`page_at` opens a fresh browser per
    # call); this disambiguates their trace filenames.
    call_index = request.node.stash.get(_CALL_COUNTER_KEY, 0)
    request.node.stash[_CALL_COUNTER_KEY] = call_index + 1

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        contexts: list[Any] = []
        raised = False
        try:

            async def _open(viewport: str | dict[str, Any] = "desktop", *, theme: str | None = None, **context_kwargs: Any) -> Any:
                kwargs = _viewport_context_kwargs(viewport, theme) | context_kwargs
                context = await browser.new_context(base_url=live_server, **kwargs)
                contexts.append(context)
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                await _pin_theme(context, theme)
                return await context.new_page()

            yield _open
        except _NON_FAILURE_OUTCOMES:
            # `pytest.skip()` / `pytest.xfail()` called from inside a `page_at` block: not a
            # failure, so fall through without setting `raised` -- see `_NON_FAILURE_OUTCOMES`.
            raise
        except BaseException:
            # `page_at`/`open_page_cm` wrap the test body directly, so a test failure propagates
            # into this generator via `athrow` -- catch it here rather than relying solely on the
            # stashed report, which is not populated yet at this point for that call shape.
            raised = True
            raise
        finally:
            failed = raised or _test_failed(request)
            if failed and contexts:
                _TRACE_DIR.mkdir(parents=True, exist_ok=True)
            for context_index, context in enumerate(contexts):
                with contextlib.suppress(Exception):
                    if failed:
                        trace_path = _trace_path(request, call_index=call_index, context_index=context_index)
                        await context.tracing.stop(path=str(trace_path))
                    else:
                        await context.tracing.stop()
                with contextlib.suppress(Exception):
                    await context.close()
            await browser.close()


@contextlib.asynccontextmanager
async def open_page_cm(
    live_server: str, request: pytest.FixtureRequest, *, viewport: str = "desktop", theme: str | None = None, **context_kwargs: Any
) -> AsyncGenerator[Any]:
    """A single page at a named viewport, optionally pinned to a theme before the first paint."""
    async with _browser_pages(live_server, request) as factory:
        yield await factory(viewport, theme=theme, **context_kwargs)


@pytest_asyncio.fixture
def page_at(live_server: str, request: pytest.FixtureRequest) -> Any:
    """Factory fixture: ``async with page_at(viewport=..., theme=...) as page``.

    A factory rather than a matrix of named fixtures because the caller decides how many pages one
    test needs. The viewport x theme sweep opens one browser per cell and walks fourteen workspaces
    inside it; a fixture-per-cell shape would launch fourteen browsers to do the same work.
    """

    def _factory(*, viewport: str = "desktop", theme: str | None = None, **context_kwargs: Any) -> Any:
        return open_page_cm(live_server, request, viewport=viewport, theme=theme, **context_kwargs)

    return _factory


@pytest_asyncio.fixture
async def open_page(live_server: str, request: pytest.FixtureRequest) -> AsyncGenerator[Any]:
    """Factory fixture: ``await open_page("tablet", theme="dark")`` -> a Playwright page.

    The general form the named fixtures below are built from. Use it directly when a test needs a
    width or theme that has no dedicated fixture, or needs two pages at once.
    """
    async with _browser_pages(live_server, request) as factory:
        yield factory


@pytest_asyncio.fixture
async def page(open_page: Any) -> Any:
    """A desktop-width page."""
    return await open_page("desktop")


@pytest_asyncio.fixture
async def tablet_page(open_page: Any) -> Any:
    """A tablet-width page (between ``md`` and ``lg``) with touch enabled."""
    return await open_page("tablet")


@pytest_asyncio.fixture
async def phone_page(open_page: Any) -> Any:
    """A phone-width page with touch enabled.

    390x844 is below the ``lg`` breakpoint -- the exact width at which the pre-.13 icon-only rail
    was unusable, and therefore the width the drawer contract must be proven at.
    """
    return await open_page("phone")


@pytest.fixture
def browser_dsn() -> str:
    """The DSN of the database the live app is serving, for tests that seed a state into it."""
    return _browser_dsn()


# --- Seeded application state --------------------------------------------------------------------


# The Redis key families the execute-dispatch path owns. Swept per test alongside the database --
# see `_reset_dispatch_keys` for why this is not optional.
_DISPATCH_KEY_PATTERNS = ("exec:*", "execdispatch:*", "exec_progress_req:*")


async def _reset_dispatch_keys(url: str) -> None:
    """Drop the execute-dispatch keys so a test cannot inherit a previous run's live batch.

    Truncating the database is NOT sufficient isolation for this suite, and the gap is not
    theoretical -- it was found by a test asserting "Dispatched 2 proposals" and being told 3, from
    a batch dispatched by an earlier pytest invocation entirely.

    ``execdispatch:active`` is a single-dispatch sentinel with a **24-hour** safety TTL (phaze-fa2p
    / phaze-0t2c), and ``exec:{batch_id}`` hashes carry the same. Neither is in Postgres, so
    ``TRUNCATE`` cannot see them. On page load ``_reattach_active_progress`` reads that sentinel and
    re-renders the referenced batch's progress card into the very target this suite asserts on --
    so a stale key does not merely linger, it actively injects another run's dispatch into the
    current test's DOM. Worse, a held sentinel makes ``/execution/start`` REFUSE, which reads as
    "the Execute button is broken" rather than as leaked state.

    Scoped to explicit patterns rather than ``FLUSHDB``: this logical database is shared with the
    worktree's unit suite (see the module docstring), and these three families are exactly the ones
    ``tests/review/routers/test_execution_dispatch.py`` already sweeps. Flushing would additionally
    destroy cache entries the app's lifespan owns.
    """
    from redis.asyncio import Redis

    client = Redis.from_url(url, decode_responses=True)
    try:
        for pattern in _DISPATCH_KEY_PATTERNS:
            keys = [key async for key in client.scan_iter(pattern)]
            if keys:
                await client.delete(*keys)
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def seed(live_server: str, redis_url: str) -> AsyncGenerator[Any]:
    """A :class:`tests.browser.seed.Seeder` on the live app's database, on an empty corpus.

    Depends on ``live_server`` so the schema exists (the app migrates on boot) and so ordering is
    explicit rather than incidental. Resets before yielding, not after: a failed test's rows are
    then still there for inspection, and the next test is protected regardless of how the previous
    one exited -- an ``after`` teardown protects nobody from a killed run.
    """
    from tests.browser.seed import Seeder, reset, sessionmaker_for

    await _reset_dispatch_keys(redis_url)
    engine, make_session = sessionmaker_for(_browser_dsn())
    try:
        async with make_session() as session:
            await reset(session)
            yield Seeder(session)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def redis_url() -> str:
    """The Redis URL the live app was booted against -- see the module docstring's sharing caveat."""
    return os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Mark everything under tests/browser, so a new file here cannot forget the marker.

    Scoped by path deliberately. pytest calls this hook with the WHOLE session's item list even
    though it is defined in a subdirectory conftest, so marking unconditionally tags every test in
    the repo as ``browser`` -- which, combined with the ``-m 'not browser'`` default, silently
    deselects the entire suite and reports success. That is a green run that tested nothing, so the
    path check below is load-bearing, not defensive styling.
    """
    for item in items:
        if _BROWSER_DIR in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.browser)


# --- Stray scratch-test guard (phaze-o7e3e) -------------------------------------------------------
#
# pytest's collection has no concept of "committed" -- every ``test_*.py`` dropped into this
# directory joins the suite, including one written mid-investigation as throwaway scratch. Measured,
# 2026-08-20: a 40-test scratch file (``tests/browser/test_zz_investigate_39eiy.py``, `test_zz_*`
# chosen by its author specifically to signal "ignore me") collected into three consecutive
# ``just test-browser`` runs before a reviewer noticed the pass count did not match the baseline
# (162 -> 202 -> 202 -> 202; three of four runs had to be discarded). That is worse than an ordinary
# failure: a contaminated run LOOKS healthier than a clean one, and this suite is the only source of
# the 10-consecutive-clean-run evidence phaze-8p1uq's promotion gate needs.
#
# AC1 choice: loud failure, not silent exclusion (option (a), preferred explicitly by the bead). A
# naming convention was rejected on the evidence above -- the author already tried exactly that
# (`test_zz_*`) and it did nothing, because nothing in pytest's default collection reads a filename
# as a signal. An explicit opt-in marker was rejected too: it would force every real committed test
# to carry ceremony it does not need today (AC2), just to prove a negative for files that do not
# exist yet.
#
# AC5 scope: the same exposure -- any stray test_*.py anywhere under tests/ collects silently, since
# ``testpaths = ["tests"]`` in pyproject.toml has no tracked-file check either -- is real tree-wide,
# not unique to this directory. This guard is scoped to tests/browser/ only, deliberately: this is
# the one directory currently backing load-bearing evidence for a promotion gate, and a tree-wide
# guard is a materially bigger blast radius (every test directory, every contributor) that this bead
# did not ask for and that was not evaluated here. Widening it is a separate decision.
#
# Cost: this hook runs ``git ls-files`` ONCE per pytest session (cached, not per collected item), and
# only when at least one browser test is actually SELECTED to run -- see ``trylast=True`` below, which
# makes this the last ``pytest_collection_modifyitems`` hookimpl to run, after the ``-m`` mark plugin
# has already removed deselected items from ``items``. That means a bare ``pytest`` / ``just check``
# run (default ``-m 'not browser'``) collects tests/browser/ (so the marker above still applies) but
# never reaches this check at all, because no browser item survives deselection -- the guard costs
# nothing on the suite everyone runs constantly, and only pays its one subprocess call on
# ``just test-browser``, the "counted form" this bead's design section anticipated.
#
# Escape hatch (AC3) for deliberate local debugging: set PHAZE_TEST_BROWSER_ALLOW_UNTRACKED=1 to run
# an untracked file anyway. It is a per-invocation env var, not a per-file marker or a directory
# carve-out, on purpose -- it has to be typed again every time, so it never becomes muscle memory the
# way a forgotten marker or an established "junk" folder would.
_ALLOW_UNTRACKED_ENV = "PHAZE_TEST_BROWSER_ALLOW_UNTRACKED"


@functools.lru_cache(maxsize=1)
def _git_tracked_browser_files() -> frozenset[Path] | None:
    """Absolute paths ``git`` tracks under tests/browser/, or ``None`` if the lookup itself failed.

    One subprocess call per session (cached) -- see the guard comment above for why a per-item
    shell-out was rejected on cost grounds. Failing open (``None``, which skips the check further
    down) rather than closed: a git lookup failure is a tooling problem, not evidence of a stray
    file, and should not turn into a false collection failure for every browser-suite run on a
    machine where it occurs.
    """
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only collection guard
            ["git", "ls-files", "--", str(_BROWSER_DIR)],  # noqa: S607 -- resolved via PATH deliberately
            cwd=_REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"tests/browser/conftest.py: could not list git-tracked files under tests/browser/ ({exc}); "
            "skipping the untracked-scratch-test guard for this run",
            file=sys.stderr,
        )
        return None
    return frozenset((_REPO_ROOT / line).resolve() for line in result.stdout.splitlines() if line)


@pytest.hookimpl(specname="pytest_collection_modifyitems", trylast=True)
def pytest_collection_modifyitems_untracked_browser_guard(items: list[Any]) -> None:
    """Fail the whole session, loudly, if a SELECTED tests/browser/ item is not tracked by git.

    ``trylast=True`` is load-bearing: it is what makes this hookimpl run after the ``-m`` mark
    plugin's own ``pytest_collection_modifyitems`` has already dropped deselected items from
    ``items``, so this only looks at tests that are actually about to execute.

    The function name is load-bearing too, not just descriptive: pytest's own plugin manager
    (``_pytest.config.PytestPluginManager.parse_hookimpl_opts``) discards any attribute whose name
    does not start with ``pytest_`` before it ever looks at the ``specname=`` marker below -- unlike
    plain pluggy, which honors ``specname`` regardless of the Python-level name. A name like
    ``_guard_untracked_browser_tests`` is silently never registered as a hookimpl at all (verified:
    it neither prints nor runs), which is exactly the kind of failure this guard exists to avoid
    elsewhere -- so it is not one to reproduce here by accident.
    """
    if os.environ.get(_ALLOW_UNTRACKED_ENV) == "1":
        return

    selected_browser_files = {Path(str(item.fspath)).resolve() for item in items if _BROWSER_DIR in Path(str(item.fspath)).resolve().parents}
    if not selected_browser_files:
        return

    tracked = _git_tracked_browser_files()
    if tracked is None:
        return

    untracked = sorted(path.relative_to(_REPO_ROOT) for path in selected_browser_files if path not in tracked)
    if not untracked:
        return

    names = "\n  ".join(str(path) for path in untracked)
    pytest.exit(
        f"tests/browser/ collection guard (phaze-o7e3e): untracked test file(s) about to run:\n  {names}\n"
        f"Commit the file if it's a real test, delete it if it was scratch, or set "
        f"{_ALLOW_UNTRACKED_ENV}=1 to run it deliberately anyway.",
        returncode=2,
    )
