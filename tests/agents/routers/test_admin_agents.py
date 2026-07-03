"""Controller-side contract tests for Phase 29 plan 07: /admin/agents router.

Covers:
- GET /admin/agents — full page render (extends base.html, contains nav + table).
- GET /admin/agents/_table — partial-only render (HTMX poll target).
- HX-Request: true on /admin/agents — returns the partial only.
- 5-state status-pill rendering (alive/stale/dead/revoked/never).
- Empty state (UI-SPEC §Empty State LOCKED copy).
- Sort order: alive → stale → dead → never → revoked (UI-SPEC LOCKED).
- BLOCKER-2 failure-tolerant footer (htmx event listener + localStorage red banner).

Uses a self-contained smoke-app fixture (mirrors test_pipeline_scans.py:46-78)
that installs the admin_agents router on a bare FastAPI app and overrides
get_session to use the project-wide session fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers import admin_agents


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a smoke FastAPI app mounting only admin_agents.router."""
    app = FastAPI(title="admin-agents-smoke", version="test")
    app.include_router(admin_agents.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest_asyncio.fixture
async def smoke(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Smoke client seeding one agent per status (5 rows)."""
    now = datetime.now(UTC)
    session.add_all(
        [
            # alive-agent is the seeded kind='compute' row (Phase 48 kind-badge contract).
            Agent(id="alive-agent", name="AliveBox", scan_roots=["/data/music"], last_seen_at=now, kind="compute"),
            Agent(
                id="stale-agent",
                name="StaleBox",
                scan_roots=["/data/music"],
                last_seen_at=now - timedelta(seconds=120),
                kind="fileserver",
            ),
            Agent(
                id="dead-agent",
                name="DeadBox",
                scan_roots=["/data/music"],
                last_seen_at=now - timedelta(seconds=600),
                kind="fileserver",
            ),
            Agent(
                id="revoked-agent",
                name="RevokedBox",
                scan_roots=["/data/music"],
                last_seen_at=now,
                revoked_at=now,
                kind="fileserver",
            ),
            Agent(id="never-agent", name="NeverBox", scan_roots=["/data/music"], kind="fileserver"),
        ]
    )
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def empty_smoke(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Smoke client with NO seeded agents beyond the conftest legacy row.

    The conftest legacy `legacy-application-server` agent is automatically
    seeded by `async_engine`; we do NOT want it visible on the /admin/agents
    page for the empty-state test, so this fixture deletes it.
    """
    from sqlalchemy import delete

    await session.execute(delete(Agent))
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# 6 core tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_renders_full_html(smoke: AsyncClient) -> None:
    """GET /admin/agents returns the full page with base.html chrome."""
    response = await smoke.get("/admin/agents")
    assert response.status_code == 200, response.text
    body = response.text
    # Full-page chrome from base.html.
    assert "<html" in body
    assert "<nav" in body
    # Page title from agents.html block.
    assert "Agents - Phaze" in body or "Agents" in body
    # The polling section is rendered.
    assert 'id="agents-table-section"' in body
    # The polling cadence + endpoint are wired correctly.
    assert 'hx-get="/admin/agents/_table"' in body
    assert 'hx-trigger="every 5s"' in body
    assert 'hx-swap="outerHTML"' in body


@pytest.mark.asyncio
async def test_htmx_request_returns_partial_only(smoke: AsyncClient) -> None:
    """HX-Request: true on /admin/agents returns the partial, not the full page."""
    response = await smoke.get("/admin/agents", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    # Partial has no <html> chrome.
    assert "<html" not in body
    assert "<nav" not in body
    # But the polling section IS present.
    assert 'id="agents-table-section"' in body


@pytest.mark.asyncio
async def test_dedicated_table_route_returns_partial(smoke: AsyncClient) -> None:
    """GET /admin/agents/_table returns the partial unconditionally (UI-SPEC LOCKED)."""
    response = await smoke.get("/admin/agents/_table")
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body
    assert 'id="agents-table-section"' in body
    # Re-emits its own hx-trigger (NEVER halts polling per UI-SPEC).
    assert 'hx-trigger="every 5s"' in body
    assert 'hx-get="/admin/agents/_table"' in body


@pytest.mark.asyncio
async def test_status_pills_render_all_5_states(smoke: AsyncClient) -> None:
    """5-state status pill rendering with LOCKED Tailwind classes per UI-SPEC."""
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    # ALIVE — green-100/950 surface.
    assert "ALIVE" in body
    assert "bg-green-100 dark:bg-green-950" in body
    assert 'aria-label="Status: alive"' in body
    # STALE — amber-100/950 surface.
    assert "STALE" in body
    assert "bg-amber-100 dark:bg-amber-950" in body
    assert 'aria-label="Status: stale"' in body
    # DEAD — red-100/950 surface.
    assert "DEAD" in body
    assert "bg-red-100 dark:bg-red-950" in body
    assert 'aria-label="Status: dead"' in body
    # REVOKED — gray-100/800 surface (neutral).
    assert "REVOKED" in body
    # NEVER — same gray-100/800 surface (visually unified "no signal").
    assert "NEVER" in body
    assert "bg-gray-100 dark:bg-gray-800" in body


# ---------------------------------------------------------------------------
# Phase 48 — Kind badge (CLOUDAGENT-03), UI-SPEC §Component Contract LOCKED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kind_badge_compute_renders(smoke: AsyncClient) -> None:
    """Full-page GET /admin/agents renders the COMPUTE badge for a kind='compute' row.

    Palette + label + aria-label are LOCKED by 48-UI-SPEC §Component Contract.
    """
    response = await smoke.get("/admin/agents")
    body = response.text
    assert "COMPUTE" in body
    assert "bg-indigo-100 dark:bg-indigo-950" in body
    assert "text-indigo-700 dark:text-indigo-400" in body
    assert 'aria-label="Kind: compute"' in body
    # LOCKED geometry copied verbatim from _status_pill.html.
    assert "text-xs font-semibold px-2 py-0.5 rounded-full" in body


@pytest.mark.asyncio
async def test_kind_badge_fileserver_renders(smoke: AsyncClient) -> None:
    """Full-page GET /admin/agents renders the FILE SERVER badge for a kind='fileserver' row."""
    response = await smoke.get("/admin/agents")
    body = response.text
    assert "FILE SERVER" in body
    assert "bg-slate-100" in body
    assert "dark:bg-slate-800" in body
    assert "text-slate-700 dark:text-slate-300" in body
    assert 'aria-label="Kind: file server"' in body


@pytest.mark.asyncio
async def test_kind_badge_in_poll_partial(smoke: AsyncClient) -> None:
    """The HTMX poll partial GET /admin/agents/_table renders the same kind badges.

    Avoids the Pitfall-5 first-load-vs-poll flicker: one include site covers both
    the full-page and the 5s poll render paths.
    """
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    assert "COMPUTE" in body
    assert "bg-indigo-100 dark:bg-indigo-950" in body
    assert 'aria-label="Kind: compute"' in body
    assert "FILE SERVER" in body
    assert "bg-slate-100" in body
    assert 'aria-label="Kind: file server"' in body


@pytest.mark.asyncio
async def test_kind_column_header_present(smoke: AsyncClient) -> None:
    """A "Kind" column header sits AFTER "Agent" and BEFORE "Status" (UI-SPEC §Placement)."""
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    pos_agent = body.find(">Agent<")
    pos_kind = body.find(">Kind<")
    pos_status = body.find(">Status<")
    assert pos_agent > 0, "Agent header missing"
    assert pos_kind > 0, "Kind header missing"
    assert pos_status > 0, "Status header missing"
    assert pos_agent < pos_kind < pos_status, f"Kind column not between Agent and Status: {pos_agent=} {pos_kind=} {pos_status=}"


@pytest.mark.asyncio
async def test_empty_state(empty_smoke: AsyncClient) -> None:
    """Empty agents table renders the UI-SPEC §Empty State LOCKED copy."""
    response = await empty_smoke.get("/admin/agents/_table")
    assert response.status_code == 200
    body = response.text
    assert "No agents registered yet" in body
    assert "just up-agent" in body
    # The polling cadence is still emitted on the empty-state section.
    assert 'hx-trigger="every 5s"' in body


@pytest.mark.asyncio
async def test_sort_order(smoke: AsyncClient) -> None:
    """Sort order: alive → stale → dead → never → revoked (UI-SPEC LOCKED)."""
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    # Names appear in the LOCKED sort order. We rely on substring positions.
    pos = {
        "alive": body.find("AliveBox"),
        "stale": body.find("StaleBox"),
        "dead": body.find("DeadBox"),
        "never": body.find("NeverBox"),
        "revoked": body.find("RevokedBox"),
    }
    assert all(v > 0 for v in pos.values()), f"missing agent name in body: {pos}"
    assert pos["alive"] < pos["stale"] < pos["dead"] < pos["never"] < pos["revoked"], f"sort order violated: {pos}"


# ---------------------------------------------------------------------------
# 3 BLOCKER-2 tests — UI-SPEC §Error / Failure-Tolerant Refresh LOCKED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_includes_htmx_error_listener(smoke: AsyncClient) -> None:
    """BLOCKER-2: UI-SPEC §Error / Failure-Tolerant Refresh LOCKED — the full
    page must include the htmx:responseError + htmx:sendError listener that
    writes localStorage `phaze:agents:lastError`."""
    response = await smoke.get("/admin/agents")
    body = response.text
    assert "htmx:responseError" in body, "Missing htmx:responseError listener (BLOCKER-2)"
    assert "htmx:sendError" in body, "Missing htmx:sendError listener (BLOCKER-2)"
    assert "htmx:afterSwap" in body, "Missing htmx:afterSwap recovery handler (BLOCKER-2)"
    assert "phaze:agents:lastError" in body, "Missing localStorage key (BLOCKER-2)"
    assert "localStorage.setItem" in body, "Listener must write to localStorage (BLOCKER-2)"
    assert "localStorage.removeItem" in body, "Recovery handler must clear localStorage (BLOCKER-2)"


@pytest.mark.asyncio
async def test_partial_includes_failure_tolerant_footer(smoke: AsyncClient) -> None:
    """BLOCKER-2: agents_table partial must render the red 'Refresh failed'
    footer driven by localStorage `phaze:agents:lastError`."""
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    assert "localStorage.getItem" in body, "Partial must read from localStorage (BLOCKER-2)"
    assert "phaze:agents:lastError" in body, "Partial must reference the localStorage key (BLOCKER-2)"
    assert "Refresh failed" in body, "Partial must include the red 'Refresh failed' copy (BLOCKER-2)"


@pytest.mark.asyncio
async def test_partial_failure_footer_uses_role_alert(smoke: AsyncClient) -> None:
    """BLOCKER-2 + accessibility: red failure banner uses role=alert so
    screen readers announce it when it becomes visible."""
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    assert 'role="alert"' in body, "Failure banner must have role=alert (a11y + BLOCKER-2)"


# ---------------------------------------------------------------------------
# Production-wiring smoke test (router registered in main.create_app)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 66 — discreet flag-gated /saq footer link (CLEAN-01), D-09/D-10/D-11
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saq_link_present_when_enable_saq_ui_true(smoke: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full-page GET /admin/agents renders the discreet /saq footer link when enable_saq_ui is true.

    The handler reads the flag via the ``get_settings()`` call-site, so we toggle it through the
    env var + lru_cache-clear idiom (the conftest autouse fixture also clears the cache per test).
    The link must open in a new tab with ``rel="noopener"`` (T-66-05 reverse-tabnabbing guard, D-11).
    """
    from phaze.config import get_settings

    monkeypatch.setenv("PHAZE_ENABLE_SAQ_UI", "true")
    get_settings.cache_clear()

    response = await smoke.get("/admin/agents")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'href="/saq"' in body, "flag-gated /saq footer link must be present when enable_saq_ui is true"
    assert 'target="_blank"' in body, "the /saq link must open in a new tab (D-11)"
    assert 'rel="noopener"' in body, "the /saq link must carry rel=noopener (reverse-tabnabbing guard, T-66-05)"


@pytest.mark.asyncio
async def test_saq_link_absent_when_enable_saq_ui_false(smoke: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full-page GET /admin/agents omits the /saq link when enable_saq_ui is false.

    When the flag is off, the ``/saq`` sub-app is not mounted (main.py), so the link must NOT
    render — otherwise it would dangle as a dead 404 (D-09 / T-66-07).
    """
    from phaze.config import get_settings

    monkeypatch.setenv("PHAZE_ENABLE_SAQ_UI", "false")
    get_settings.cache_clear()

    response = await smoke.get("/admin/agents")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'href="/saq"' not in body, "the /saq link must be absent when enable_saq_ui is false (never a dead 404)"


@pytest.mark.asyncio
async def test_saq_link_absent_from_poll_partial(smoke: AsyncClient) -> None:
    """The polled /_table partial never carries the /saq link — it lives only in the page shell."""
    response = await smoke.get("/admin/agents/_table")
    assert response.status_code == 200
    assert 'href="/saq"' not in response.text, "the /saq link must not leak into the polled partial"


@pytest.mark.asyncio
async def test_router_registered_in_main_app() -> None:
    """admin_agents.router is registered in main.create_app() (production wiring)."""
    from phaze.main import create_app
    from tests._route_introspection import effective_route_paths

    app = create_app()
    paths = effective_route_paths(app)
    # Both handlers must be reachable on the production app.
    assert "/admin/agents" in paths
    assert "/admin/agents/_table" in paths
