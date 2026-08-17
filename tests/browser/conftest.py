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
"""

from __future__ import annotations

import asyncio
import contextlib
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


# --- Parameterizable viewports and themes (phaze-fk1ww) ----------------------------------------
#
# The suite shipped with TWO hardcoded page fixtures, 1440x900 and 390x844. That covers the two ends
# of the responsive contract and misses its middle: ADR-0009's breakpoint table forks at ``lg``
# (1024px), so every width from ``md`` up to 1023px takes the SAME drawer branch as the phone while
# having none of the phone's other properties -- a two-column workspace grid, a wide table, and
# enough room that a layout bug there is invisible at 390px. It was never validated (phaze-fk1ww).
#
# Named rather than free-form so a viewport is written down once and every test that claims to have
# checked "tablet" checked the same width. Add to the map, do not inline a size at a call site.
VIEWPORTS: dict[str, dict[str, int]] = {
    # >= lg: the static expanded 280px rail branch.
    "desktop": {"width": 1440, "height": 900},
    # md (768) and below lg (1024): the drawer branch at a width the phone fixture cannot represent.
    # 768x1024 is the portrait tablet the `md` column rules were written for.
    "tablet": {"width": 768, "height": 1024},
    # Below lg, and the exact width at which the pre-.13 icon-only rail was unusable -- therefore the
    # width the drawer contract must be proven at.
    "phone": {"width": 390, "height": 844},
}

# Both branches of `_applyTheme` (shell.html). "auto" is deliberately NOT a member: it resolves to
# one of these two via prefers-color-scheme, so it is a resolution mechanism to test once rather
# than a third rendered appearance to sweep every workspace in.
THEMES: tuple[str, ...] = ("light", "dark")


def _viewport_context_kwargs(viewport: str) -> dict[str, Any]:
    """Playwright context kwargs for a named viewport, touch included where the device has it."""
    kwargs: dict[str, Any] = {"viewport": VIEWPORTS[viewport]}
    if viewport == "phone":
        kwargs |= {"has_touch": True, "is_mobile": True}
    elif viewport == "tablet":
        # Touch without ``is_mobile``: a tablet reports a real pointer-coarse input but not a mobile
        # viewport meta override, and ``is_mobile`` would silently rescale the layout under test.
        kwargs |= {"has_touch": True}
    return kwargs


@contextlib.asynccontextmanager
async def open_page(live_server: str, *, viewport: str = "desktop", theme: str | None = None, **context_kwargs: Any) -> AsyncGenerator[Any]:
    """A page at a named viewport, optionally pinned to a theme before the first paint.

    The theme is pinned by seeding ``localStorage['phaze-theme']`` from an init script rather than
    by clicking the header toggle. shell.html applies the ``.dark`` class from an inline pre-flash
    IIFE that reads that key BEFORE Alpine loads, so seeding it is the only way to have the very
    first rendered frame already be in the theme under test -- clicking gets there a paint later,
    and any assertion racing that paint is the flake this fixture exists to avoid. ``color_scheme``
    is set alongside it so ``prefers-color-scheme`` agrees with the pinned mode and the ``auto``
    branch cannot disagree with the explicit one.
    """
    from playwright.async_api import async_playwright

    kwargs = _viewport_context_kwargs(viewport) | context_kwargs
    if theme is not None:
        kwargs.setdefault("color_scheme", theme)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(base_url=live_server, **kwargs)
            try:
                if theme is not None:
                    await context.add_init_script(f"try {{ localStorage.setItem('phaze-theme', {theme!r}); }} catch (e) {{}}")
                yield await context.new_page()
            finally:
                await context.close()
        finally:
            await browser.close()


@pytest_asyncio.fixture
def page_at(live_server: str) -> Any:
    """Factory fixture: ``async with page_at(viewport=..., theme=...) as page``.

    A factory rather than a matrix of named fixtures because the caller decides how many pages one
    test needs. The viewport x theme sweep opens ONE browser per cell and walks fourteen workspaces
    inside it; a fixture-per-cell shape would launch fourteen browsers to do the same work.
    """

    def _factory(*, viewport: str = "desktop", theme: str | None = None, **context_kwargs: Any) -> Any:
        return open_page(live_server, viewport=viewport, theme=theme, **context_kwargs)

    return _factory


@pytest_asyncio.fixture
async def page(live_server: str) -> AsyncGenerator[Any]:
    """A desktop-width page."""
    async with open_page(live_server, viewport="desktop") as value:
        yield value


@pytest_asyncio.fixture
async def phone_page(live_server: str) -> AsyncGenerator[Any]:
    """A phone-width page with touch enabled.

    390x844 is below the ``lg`` breakpoint -- the exact width at which the pre-.13 icon-only rail
    was unusable, and therefore the width the drawer contract must be proven at.
    """
    async with open_page(live_server, viewport="phone") as value:
        yield value


@pytest_asyncio.fixture
async def tablet_page(live_server: str) -> AsyncGenerator[Any]:
    """A tablet-width (``md``) page -- below ``lg``, so the drawer branch, but not a phone."""
    async with open_page(live_server, viewport="tablet") as value:
        yield value


@pytest.fixture
def browser_dsn() -> str:
    """The DSN of the database the live app is serving, for tests that seed a state into it."""
    return _browser_dsn()


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
