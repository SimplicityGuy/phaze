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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
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


# --- Viewports and themes -----------------------------------------------------------------------
#
# NAMED, not hardcoded per fixture (phaze-8p1uq). The suite previously defined desktop and phone
# inline in two fixture bodies, so proving anything at a third width meant copying a third fixture
# and its context kwargs. The named specs below are the axis ADR-0009's multi-viewport requirement
# actually needs: a test parametrizes over ``VIEWPORTS`` (or asks ``open_page`` for one by name)
# instead of the suite growing one fixture per size.
#
# The three widths are chosen against Tailwind's breakpoints, which is what the layout switches on:
#   phone   390  -- below ``md`` (768). The drawer contract's width; the pre-.13 icon-rail defect.
#   tablet  820  -- between ``md`` (768) and ``lg`` (1024). The band NOTHING covered before: ``md:``
#                   styles apply while the rail is still off-canvas, so a layout that assumes
#                   "drawer implies phone" (or "md implies rail") is only wrong here.
#   desktop 1440 -- above ``lg``. The permanently-mounted rail.
VIEWPORTS: dict[str, dict[str, Any]] = {
    "phone": {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True},
    "tablet": {"viewport": {"width": 820, "height": 1180}, "has_touch": True},
    "desktop": {"viewport": {"width": 1440, "height": 900}},
}

# The values the shell's theme store persists under ``phaze-theme`` (shell.html). "auto" defers to
# the OS, which is why a test that wants a KNOWN theme must pick "light" or "dark" explicitly --
# asserting dark-mode contrast under "auto" asserts the CI runner's preference, not the product's.
#
# Exported for `@pytest.mark.parametrize("theme", THEMES)`, so it is deliberately not referenced in
# this module. Do not delete it as unused; it is the counterpart to VIEWPORTS above and exists so a
# theme sweep names one list rather than re-spelling the pair at each call site.
THEMES = ("light", "dark")


@contextlib.asynccontextmanager
async def _browser_pages(live_server: str) -> AsyncGenerator[Any]:
    """Yield a factory that opens pages on one browser, closing everything on exit.

    One Chromium per test, N contexts within it -- the launch is the expensive part (~1s) and the
    context is what actually carries the isolation, so a test comparing two viewports or two themes
    pays for one browser rather than two.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        contexts: list[Any] = []
        try:

            async def _open(viewport: str | dict[str, Any] = "desktop", *, theme: str | None = None, **context_kwargs: Any) -> Any:
                spec = VIEWPORTS[viewport] if isinstance(viewport, str) else viewport
                context = await browser.new_context(base_url=live_server, **{**spec, **context_kwargs})
                contexts.append(context)
                if theme is not None:
                    # An INIT script, not an evaluate() after load: shell.html reads
                    # localStorage['phaze-theme'] in a head <script> to set the root class before
                    # first paint, so a theme written after navigation is one repaint too late and
                    # the test would assert against the default theme's computed styles.
                    await context.add_init_script(f"try {{ localStorage.setItem('phaze-theme', {json.dumps(theme)}); }} catch (e) {{}}")
                return await context.new_page()

            yield _open
        finally:
            for context in contexts:
                with contextlib.suppress(Exception):
                    await context.close()
            await browser.close()


@pytest_asyncio.fixture
async def open_page(live_server: str) -> AsyncGenerator[Any]:
    """Factory fixture: ``await open_page("tablet", theme="dark")`` -> a Playwright page.

    The general form the named fixtures below are built from. Use it directly when a test needs a
    width or theme that has no dedicated fixture, or needs two pages at once.
    """
    async with _browser_pages(live_server) as factory:
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
