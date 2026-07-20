"""Controller-side contract tests for Phase 29 plan 07: /admin/agents router.

Covers:
- GET /admin/agents — full page render (extends base.html, contains nav + table).
- GET /admin/agents/_table — partial-only render (HTMX poll target).
- HX-Request: true on /admin/agents — returns the partial only.
- Status-pill rendering for the 4 states that reach the panel (alive/stale/dead/never).
- Revoked agents are filtered out of the panel entirely (revoked_at IS NULL).
- Empty state (UI-SPEC §Empty State LOCKED copy).
- Sort order: alive → stale → dead → never (revoked agents are filtered out of the panel).
- BLOCKER-2 failure-tolerant footer (htmx event listener + localStorage red banner).

Uses a self-contained smoke-app fixture (mirrors test_pipeline_scans.py:46-78)
that installs the admin_agents router on a bare FastAPI app and overrides
get_session to use the project-wide session fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import TYPE_CHECKING

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers import admin_agents
from phaze.services.agent_liveness import sort_key


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
async def test_status_pills_render_4_visible_states(smoke: AsyncClient) -> None:
    """Status pill rendering for the 4 states that reach the panel, LOCKED Tailwind classes.

    Revoked agents are filtered out of the panel (revoked_at IS NULL), so the REVOKED
    pill no longer renders here — see ``test_revoked_agent_absent``.
    """
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
    # NEVER — gray-100/800 surface (neutral "no signal").
    assert "NEVER" in body
    assert "bg-gray-100 dark:bg-gray-800" in body


@pytest.mark.asyncio
async def test_revoked_agent_absent(smoke: AsyncClient) -> None:
    """Revoked agents (revoked_at IS NOT NULL) never render in the panel or its poll partial.

    Core regression guard for the leak: the ``smoke`` fixture seeds an explicitly-revoked
    ``RevokedBox`` (id ``revoked-agent``). It must be absent from BOTH render paths while a
    non-revoked control (``AliveBox``) is present — proving the filter drops only revoked
    rows, not the whole table.
    """
    for path in ("/admin/agents/_table", "/admin/agents"):
        response = await smoke.get(path)
        assert response.status_code == 200, response.text
        body = response.text
        assert "RevokedBox" not in body, f"revoked agent leaked into {path}"
        assert 'aria-label="Status: revoked"' not in body, f"revoked status pill leaked into {path}"
        # Non-revoked control still renders — the filter is not nuking the table.
        assert "AliveBox" in body, f"non-revoked agent missing from {path}"


# ---------------------------------------------------------------------------
# COMPUTE-01 — Section 2 renders ONE tile per compute lane (per-cluster identity)
# ---------------------------------------------------------------------------


_TWO_CLUSTER_REGISTRY = """
    [[backends]]
    kind = "compute"
    id = "vox"
    rank = 10
    cap = 2
    agent_ref = "vox-node"
    scratch_dir = "/scratch/vox"
    push_host = "vox.push"

    [[backends]]
    kind = "compute"
    id = "xenolab"
    rank = 20
    cap = 2
    agent_ref = "xenolab-node"
    scratch_dir = "/scratch/xenolab"
    push_host = "xenolab.push"
    """


async def _seed_cloud_job(session: AsyncSession, make_file, *, backend_id: str) -> None:  # type: ignore[no-untyped-def]
    """Seed ONE RUNNING CloudJob attributed to ``backend_id`` (its own unique-FK FileRecord)."""
    import uuid

    from phaze.models.cloud_job import CloudJob, CloudJobStatus

    file = await make_file(original_filename=f"{backend_id}-run.mp3")
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            s3_key=f"staging/{file.id}",
            status=CloudJobStatus.RUNNING.value,
            backend_id=backend_id,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_section2_renders_two_cluster_tiles(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """COMPUTE-01: two stamped clusters (vox ACTIVE, xenolab IDLE) → two labeled tiles, never DEAD.

    A 2-compute registry (vox + xenolab) with a RUNNING CloudJob stamped only on vox: Section 2 must
    render BOTH per-cluster tiles labeled by backend_id — vox ACTIVE while xenolab stays a visible IDLE
    lane (registry-composed, no reachability probe) — and NEVER a perpetual DEAD state (KDEPLOY-04).
    """
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    await _seed_cloud_job(session, make_file, backend_id="vox")

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        for path in ("/admin/agents", "/admin/agents/_table"):
            response = await ac.get(path)
            assert response.status_code == 200, response.text
            body = response.text
            compute_section = body.split('id="compute-lanes"', 1)[1]
            # Both clusters render as labeled tiles.
            assert "vox" in compute_section, f"vox tile missing from {path}"
            assert "xenolab" in compute_section, f"xenolab tile missing from {path}"
            # vox is doing work → ACTIVE; xenolab is configured-but-quiet → IDLE (still listed).
            assert "ACTIVE" in compute_section, f"vox ACTIVE pill missing from {path}"
            assert "IDLE" in compute_section, f"xenolab IDLE pill missing from {path}"
            # Never a perpetual DEAD/rose state in Section 2.
            assert "DEAD" not in compute_section, f"DEAD leaked into Section 2 of {path}"


@pytest.mark.asyncio
async def test_section2_poll_partial_matches_full_page(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """COMPUTE-01: the /_table poll partial's Section 2 is byte-identical to the full page's.

    The compute-lane tiles are a single include site, so the first-load full page and the 5s poll
    partial must render the SAME Section-2 markup (no Pitfall-5 flicker between first-load and poll).
    """
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    await _seed_cloud_job(session, make_file, backend_id="vox")

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        full = (await ac.get("/admin/agents")).text
        partial = (await ac.get("/admin/agents/_table")).text

    def _section2(body: str) -> str:
        # From the compute-lanes root up to (and including) its first </section> close — the
        # compute_lanes.html partial nests no <section>, so this isolates Section 2 from the
        # surrounding page chrome (which differs between full page and poll partial by design).
        start = body.index('id="compute-lanes"')
        end = body.index("</section>", start) + len("</section>")
        return body[start:end]

    assert _section2(full) == _section2(partial)


@pytest.mark.asyncio
async def test_section2_empty_registry_renders_idle_card(smoke: AsyncClient) -> None:
    """COMPUTE-01: a pure-local registry (no non-local backends) renders a friendly IDLE/empty card.

    The default smoke app has no cloud backends configured, so ``derive_compute_lane_identities``
    returns no lanes — Section 2 must still render (never blank/error) as an IDLE empty-state card.
    """
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    compute_section = body.split('id="compute-lanes"', 1)[1]
    assert "No compute lanes" in compute_section
    assert "IDLE" in compute_section
    assert "DEAD" not in compute_section


# ---------------------------------------------------------------------------
# COMPUTE-01 (dedupe): suppress the registry-shadowed never-heartbeating compute row
# ---------------------------------------------------------------------------


def _sections(body: str) -> tuple[str, str]:
    """Split the rendered page into (Section 1, Section 2) at the compute-lanes root.

    Section 1 (heartbeating agents) is everything BEFORE ``id="compute-lanes"``; Section 2 (the
    compute-lane tiles) is that marker onward. compute_lanes.html nests no ``id="compute-lanes"``, so
    the single split cleanly separates the two panels.
    """
    section1, _marker, section2 = body.partition('id="compute-lanes"')
    return section1, section2


@pytest_asyncio.fixture
async def shadow_smoke(session: AsyncSession, backends_toml_env) -> AsyncGenerator[AsyncClient]:  # type: ignore[no-untyped-def]
    """Smoke client with a 2-cluster registry (vox, xenolab) + three NEVER Agent rows.

    Seeds the three dedupe cases at once:
      * ``vox``  — kind=compute, id matches a registry backend → the registry-shadowed row to suppress.
      * ``orphan-compute`` — kind=compute, NOT in the registry → a genuine NEVER compute row, kept.
      * ``fs-never`` — kind=fileserver → an ordinary NEVER fileserver row, untouched.
    The conftest legacy revoked row is filtered by the existing revoked-row guard, so it never shows.
    """
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    session.add_all(
        [
            Agent(id="vox", name="vox", scan_roots=[], kind="compute"),  # last_seen_at=None → NEVER
            Agent(id="orphan-compute", name="OrphanCompute", scan_roots=[], kind="compute"),
            Agent(id="fs-never", name="FsNever", scan_roots=[], kind="fileserver"),
        ]
    )
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_dedupe_registry_shadow_compute_row_suppressed_from_section1(shadow_smoke: AsyncClient) -> None:
    """COMPUTE-01: a never-seen 'vox'/kind=compute row is absent from Section 1 while its tile is in Section 2.

    The exact "shown twice" defect: the vox cluster must render as a live tile in Section 2 (registry-
    composed) but NOT sit as a perpetual-NEVER agent row in Section 1.
    """
    for path in ("/admin/agents", "/admin/agents/_table"):
        response = await shadow_smoke.get(path)
        assert response.status_code == 200, response.text
        section1, section2 = _sections(response.text)
        # Suppressed from Section 1 (no shadow agent-row).
        assert "agent-trigger-vox" not in section1, f"vox shadow row leaked into Section 1 of {path}"
        # Still surfaced as a live lane in Section 2.
        assert "vox" in section2, f"vox tile missing from Section 2 of {path}"


@pytest.mark.asyncio
async def test_dedupe_non_registry_compute_row_still_renders_never(shadow_smoke: AsyncClient) -> None:
    """COMPUTE-01: a never-seen compute Agent NOT matching any registry id still renders NEVER in Section 1.

    The suppression predicate is narrow: only registry-shadowed compute rows are dropped. A compute
    agent whose id is not a backend key is a genuine orphan and must keep its NEVER row so the operator
    can see (and clean up) it.
    """
    response = await shadow_smoke.get("/admin/agents/_table")
    section1, _section2 = _sections(response.text)
    assert "agent-trigger-orphan-compute" in section1, "non-registry compute NEVER row was wrongly suppressed"
    assert "OrphanCompute" in section1


@pytest.mark.asyncio
async def test_dedupe_fileserver_never_row_unaffected(shadow_smoke: AsyncClient) -> None:
    """COMPUTE-01: an ordinary fileserver NEVER row is untouched by the compute-only suppression."""
    response = await shadow_smoke.get("/admin/agents/_table")
    section1, _section2 = _sections(response.text)
    assert "agent-trigger-fs-never" in section1, "fileserver NEVER row was wrongly suppressed"
    assert "FsNever" in section1
    # NEVER pill still rendered for the surviving fileserver row.
    assert "NEVER" in section1


@pytest.mark.asyncio
async def test_dedupe_cluster_id_appears_exactly_once(shadow_smoke: AsyncClient) -> None:
    """COMPUTE-01 invariant: the shadowed cluster id 'vox' is represented in exactly ONE section.

    Before the fix, 'vox' appeared in BOTH sections (a NEVER agent row in Section 1 AND a live tile in
    Section 2). After suppression it lives ONLY in Section 2 — proving the "shown twice" duplication is
    gone while the lane identity is preserved.
    """
    response = await shadow_smoke.get("/admin/agents")
    section1, section2 = _sections(response.text)
    assert "vox" not in section1, "vox still duplicated into Section 1 (shown twice)"
    assert "vox" in section2, "vox lane identity lost from Section 2"


@pytest.mark.asyncio
async def test_dedupe_heartbeating_compute_agent_row_kept(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """COMPUTE-01: a genuinely-heartbeating registry compute agent keeps its Section 1 row (never suppressed).

    Suppression is gated on ``_status=='never'`` — a compute agent that IS heartbeating (recent
    last_seen_at → 'alive') is a real process and must stay visible even when its id matches a backend.
    """
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    now = datetime.now(UTC)
    session.add(Agent(id="vox", name="vox", scan_roots=[], kind="compute", last_seen_at=now))
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/admin/agents/_table")

    section1, _section2 = _sections(response.text)
    assert "agent-trigger-vox" in section1, "a heartbeating (alive) compute agent must keep its row"
    assert "ALIVE" in section1


# ---------------------------------------------------------------------------
# phaze-ifcr — COMPUTE-01 shadow-row dedup via structural agent_ref binding
#
# The pre-existing dedupe above only catches the shadow when the operator names the callback agent
# THE SAME as the backend id (agent "vox" == backend "vox"). In production the operator's free choice
# at ``phaze agents add --kind compute`` is typically "k8s-<backend id>" (docs/k8s-burst.md), which the
# id/name string-equality predicate never matches — the filter silently never fires. These tests use a
# backend id != agent id/name fixture (the bead's acceptance criterion) to prove the structural
# ``agent_ref`` binding closes that gap.
# ---------------------------------------------------------------------------

_KUEUE_REGISTRY_WITH_AGENT_REF = """
    [[backends]]
    kind = "kueue"
    id = "vox"
    rank = 10
    cap = 4
    agent_ref = "k8s-vox"
    buckets = ["vox-bucket"]

    [backends.kube]
    api_url = "https://kube.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq"

    [[buckets]]
    id = "vox-bucket"
    scope = "cluster-specific"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-vox"
    """


@pytest_asyncio.fixture
async def kueue_shadow_smoke(session: AsyncSession, backends_toml_env) -> AsyncGenerator[AsyncClient]:  # type: ignore[no-untyped-def]
    """Smoke client with a kueue backend (id="vox") whose callback agent is named "k8s-vox" (id != backend id).

    Mirrors the bead's acceptance fixture verbatim: a kueue backend id "vox" whose callback agent row is
    named "k8s-vox".
    """
    backends_toml_env(_KUEUE_REGISTRY_WITH_AGENT_REF)
    session.add(Agent(id="k8s-vox", name="k8s-vox", scan_roots=[], kind="compute"))  # last_seen_at=None → NEVER
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_dedupe_kueue_agent_ref_shadow_row_suppressed_from_section1(kueue_shadow_smoke: AsyncClient) -> None:
    """phaze-ifcr acceptance: the k8s-vox NEVER row is absent from Section 1 while vox's tile is in Section 2.

    Before the fix: id/name string equality ("k8s-vox" in {"vox"}) is False, so the shadow row leaked
    into Section 1 permanently as NEVER while Section 2 showed the same cluster's lane ACTIVE/IDLE —
    exactly the "2 active workloads but the agent never checked in" operator-confusion the bead reports.
    """
    for path in ("/admin/agents", "/admin/agents/_table"):
        response = await kueue_shadow_smoke.get(path)
        assert response.status_code == 200, response.text
        section1, section2 = _sections(response.text)
        assert "agent-trigger-k8s-vox" not in section1, f"k8s-vox kueue shadow row leaked into Section 1 of {path}"
        assert "vox" in section2, f"vox tile missing from Section 2 of {path}"


@pytest.mark.asyncio
async def test_dedupe_kueue_agent_ref_heartbeating_row_kept(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A genuinely-heartbeating agent_ref-bound compute agent keeps its Section 1 row (never suppressed).

    Suppression stays gated on ``_status=='never'`` even for the new agent_ref binding path — a real,
    heartbeating process must remain visible regardless of which registry key matched it.
    """
    backends_toml_env(_KUEUE_REGISTRY_WITH_AGENT_REF)
    now = datetime.now(UTC)
    session.add(Agent(id="k8s-vox", name="k8s-vox", scan_roots=[], kind="compute", last_seen_at=now))
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/admin/agents/_table")

    section1, _section2 = _sections(response.text)
    assert "agent-trigger-k8s-vox" in section1, "a heartbeating agent_ref-bound compute agent must keep its row"
    assert "ALIVE" in section1


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
    """A "Kind" column header sits AFTER "Agent" and BEFORE "Status" (UI-SPEC §Placement).

    Scoped to the <thead> rather than the whole body: the section heading "Agents · heartbeating"
    also contains the substring "Agent", so an unscoped search can satisfy this assertion off the
    heading while the actual column order is wrong. Within the <thead> these capitalised labels
    appear only as header text, so matching them bare is unambiguous AND shape-agnostic —
    phaze-a6hm.4 wraps a SORTABLE header's label in a <button> (so the old ">Agent<" no longer
    matches), and the invariant under test is the column ORDER, not the markup around it.
    """
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    thead = body[body.find("<thead") : body.find("</thead>")]
    pos_agent = thead.find("Agent")
    pos_kind = thead.find("Kind")
    pos_status = thead.find("Status")
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
    """Sort order: alive → stale → dead → never (revoked agents are filtered out of the panel)."""
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    # Names appear in the LOCKED sort order. We rely on substring positions.
    pos = {
        "alive": body.find("AliveBox"),
        "stale": body.find("StaleBox"),
        "dead": body.find("DeadBox"),
        "never": body.find("NeverBox"),
    }
    assert all(v > 0 for v in pos.values()), f"missing agent name in body: {pos}"
    assert pos["alive"] < pos["stale"] < pos["dead"] < pos["never"], f"sort order violated: {pos}"


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


# ---------------------------------------------------------------------------
# GET /admin/agents history-restore response shape (phaze-64uy)
#
# admin/partials/agents_table.html sets hx-push-url="/admin/agents?agent=<id>" on each drill-in row
# (DRILL-03 / D-02), so that URL enters browser history. A Back with the snapshot evicted from
# htmx's 10-entry cache re-fetches it as a RESTORE carrying BOTH HX-Request: true and
# HX-History-Restore-Request: true -- and on a restore htmx IGNORES hx-target and swaps the
# response into <body>.
#
# This handler used to ask the question through a LOCAL ``_is_htmx`` helper that re-derived the
# decision from the raw header, which routers/response_shape.py rule 1 bans outright for exactly
# this reason: it answered the restore with the chrome-less agents_table partial, replacing the
# whole admin page with a bare table. The helper is deleted; the shared ``wants_fragment`` predicate
# is the only sanctioned way to ask.
# ---------------------------------------------------------------------------


_RESTORE_HEADERS = {"HX-Request": "true", "HX-History-Restore-Request": "true"}


@pytest.mark.asyncio
async def test_history_restore_returns_full_page_not_partial(smoke: AsyncClient) -> None:
    """A history-restore GET /admin/agents returns the FULL page, chrome included.

    Asserts the CHROME, not merely a 200 -- the buggy handler returned 200 with the partial, so a
    status-only assertion passes against the bug.
    """
    response = await smoke.get("/admin/agents?agent=alive-agent", headers=_RESTORE_HEADERS)
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must return a full document, not the table partial"
    assert 'aria-label="Main navigation"' in body, "the app nav must survive a history restore"
    assert 'id="agents-table-section"' in body, "the polling section must still be present inside the page"


@pytest.mark.asyncio
async def test_restore_header_alone_returns_full_page(smoke: AsyncClient) -> None:
    """The restore header dominates even without ``HX-Request`` (response_shape rule 2)."""
    response = await smoke.get("/admin/agents", headers={"HX-History-Restore-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower()
    assert 'aria-label="Main navigation"' in body


@pytest.mark.asyncio
async def test_local_is_htmx_helper_is_gone(smoke: AsyncClient) -> None:
    """``admin_agents`` must not carry its own shape predicate (response_shape rule 1).

    The module-level guard in tests/shared/routers/test_response_shape.py already forbids the raw
    header read; this pins the specific helper by NAME so it cannot be reintroduced under its old
    identity with a different implementation.
    """
    assert not hasattr(admin_agents, "_is_htmx"), "re-deriving the shape decision locally is banned -- use response_shape.wants_fragment"


# ---------------------------------------------------------------------------
# phaze-a6hm.4 — sortable columns via the shared column_sort contract
# ---------------------------------------------------------------------------


def _row_order(body: str) -> list[str]:
    """Return the agent ids in the order they appear as rows in ``body``.

    Reads the rendered row anchors rather than any header state, so these tests observe the order
    the operator actually SEES rather than the order the handler claims to have asked for.
    """
    return re.findall(r'id="agent-trigger-([^"]+)"', body)


def _poll_vals(body: str) -> dict[str, str]:
    """Return the ``sort``/``order`` the rendered self-poll will send on its NEXT 5s tick.

    Parsed out of the live ``hx-vals`` attribute instead of being assumed, so a change that stops
    threading the sort into the poll fails these tests rather than passing on a hardcoded guess.
    """
    match = re.search(r"hx-vals='js:\{(.*?)\}'", body)
    assert match is not None, "the polled section must carry hx-vals"
    return dict(re.findall(r'(\w+):\s*"([^"]*)"', match.group(1)))


@pytest.mark.asyncio
async def test_sortable_headers_use_the_shared_contract(smoke: AsyncClient) -> None:
    """Whitelisted headers render as sort buttons pointed at this table's own endpoint."""
    body = (await smoke.get("/admin/agents/_table")).text
    thead = body[body.find("<thead") : body.find("</thead>")]
    assert 'hx-get="/admin/agents/_table?sort=name&amp;order=asc"' in thead
    # The target is the EXISTING self-replacing section, swapped outerHTML. An innerHTML swap here
    # would nest a second #agents-table-section (duplicate id + duplicate 5s trigger) on every click.
    assert 'hx-target="#agents-table-section"' in thead
    assert 'hx-swap="outerHTML"' in thead


@pytest.mark.asyncio
async def test_sort_is_server_side_across_the_whole_set(smoke: AsyncClient) -> None:
    """A sort click reorders rows in SQL, and the reverse direction is the exact mirror."""
    ascending = _row_order((await smoke.get("/admin/agents/_table", params={"sort": "name", "order": "asc"})).text)
    descending = _row_order((await smoke.get("/admin/agents/_table", params={"sort": "name", "order": "desc"})).text)
    assert ascending == sorted(ascending, key=str.lower) or ascending == sorted(ascending)
    assert descending == list(reversed(ascending))
    # Revoked agents stay filtered out no matter how the table is ordered.
    assert "revoked-agent" not in ascending


@pytest.mark.asyncio
async def test_default_order_matches_the_locked_sort_key(smoke: AsyncClient, session: AsyncSession) -> None:
    """The SQL default reproduces the UI-SPEC LOCKED ``sort_key`` order exactly.

    phaze-a6hm.4 moved this table's ORDER BY out of a Python ``rows.sort(key=sort_key)`` and into
    SQL. That is only safe while the two agree, so the equivalence is pinned against ``sort_key``
    ITSELF rather than a hand-copied expected list: if a future threshold change makes the status
    tiers stop being a pure function of last-seen recency, this fails instead of silently reordering
    the operator's default view.

    Stated as "the rendered order is non-decreasing under ``sort_key``" rather than "equals
    ``sorted(rows, key=sort_key)``", because ``sort_key`` genuinely TIES on never-seen agents (they
    all share the +inf tiebreaker). Python's stable sort resolves those ties by whatever order the
    unordered SELECT happened to return, so an equality assertion would pin a database accident. The
    SQL path is strictly MORE determined here — it breaks the tie on ``Agent.id`` — and
    ``test_ties_break_deterministically`` covers that half.
    """
    rendered = _row_order((await smoke.get("/admin/agents/_table")).text)

    now = datetime.now(UTC)
    rows = (await session.execute(select(Agent).where(Agent.revoked_at.is_(None)))).scalars().all()
    by_id = {a.id: a for a in rows}
    keys = [sort_key(by_id[agent_id], now) for agent_id in rendered]
    assert keys == sorted(keys), f"rendered order disagrees with the LOCKED sort_key order: {rendered}"


@pytest.mark.asyncio
async def test_ties_break_deterministically_across_polls(smoke: AsyncClient) -> None:
    """Rows that tie on the sort key hold a stable position between ticks.

    Never-seen agents all tie on last-seen, and an operator-chosen key ties far harder (every
    fileserver agent shares a kind). Without the unique ``Agent.id`` tail on the ORDER BY, Postgres
    is free to return tied rows in a different order each time — so rows would visibly swap places
    every 5 seconds under a cursor that never moved.
    """
    for params in ({}, {"sort": "kind", "order": "asc"}):
        first = _row_order((await smoke.get("/admin/agents/_table", params=params)).text)
        again = _row_order((await smoke.get("/admin/agents/_table", params=params)).text)
        assert first == again, f"tied rows re-shuffled between polls for {params}"


@pytest.mark.asyncio
async def test_never_seen_agents_sort_last_by_default(smoke: AsyncClient, session: AsyncSession) -> None:
    """Never-seen agents occupy the BOTTOM of the default view, not the top.

    Regression for the NULL-ordering trap: Postgres orders NULLS FIRST under DESC, so the obvious
    ``Agent.last_seen_at.desc()`` puts every never-heartbeated agent ABOVE the live ones — inverting
    the LOCKED alive→stale→dead→never order on the very first render, with no error to notice.

    Asserts the whole never-seen BLOCK sits in the tail rather than naming one row, so the guarantee
    still reads correctly however many never-seen agents the fixtures happen to seed.
    """
    order = _row_order((await smoke.get("/admin/agents/_table")).text)
    rows = (await session.execute(select(Agent).where(Agent.revoked_at.is_(None)))).scalars().all()
    never = {a.id for a in rows if a.last_seen_at is None}

    assert order[0] == "alive-agent", "the most recently seen agent must lead"
    assert never, "fixture must seed at least one never-seen agent for this to mean anything"
    tail = set(order[-len(never & set(order)) :])
    assert tail == never & set(order), f"never-seen agents are not in the tail: {order}"


@pytest.mark.asyncio
async def test_poll_tick_preserves_operator_sort(smoke: AsyncClient) -> None:
    """THE bead: the 5s self-poll carries the chosen sort forward instead of resetting it.

    #agents-table-section re-swaps ITSELF every 5s with hx-swap="outerHTML" and is spec'd never to
    halt. So it is not enough that the click renders sorted — the NEXT tick must re-send the sort,
    or the table snaps back to the default order 5 seconds after the operator clicked, silently, on
    a fuse too long for any manual check to catch.

    This replays the tick rather than asserting the first render: it takes the sort the rendered
    hx-vals will actually send, issues THAT request the way htmx would, and asserts the follow-up
    render is still in the operator's order — and still says so, so tick N+2 survives too.
    """
    chosen = _row_order((first := await smoke.get("/admin/agents/_table", params={"sort": "name", "order": "desc"})).text)

    # What the rendered section will send on its next tick — read from the markup, not assumed.
    vals = _poll_vals(first.text)
    assert vals["sort"] == "name"
    assert vals["order"] == "desc"

    # Tick N+1: the poll fires with exactly those params.
    tick = await smoke.get("/admin/agents/_table", params={"sort": vals["sort"], "order": vals["order"], "agent": ""})
    assert tick.status_code == 200
    assert _row_order(tick.text) == chosen, "the 5s poll reset the operator's chosen sort to the default"

    # ...and the loop is self-sustaining: tick N+1 re-emits the same instruction for tick N+2.
    assert _poll_vals(tick.text) == vals


@pytest.mark.asyncio
async def test_poll_default_tick_does_not_reset_to_a_different_order(smoke: AsyncClient) -> None:
    """The unsorted case is stable across ticks too (no click, no drift)."""
    first = await smoke.get("/admin/agents/_table")
    second = await smoke.get("/admin/agents/_table", params=_poll_vals(first.text))
    assert _row_order(second.text) == _row_order(first.text)


@pytest.mark.asyncio
async def test_drill_in_push_url_keeps_the_sort(smoke: AsyncClient) -> None:
    """Opening a row must not rewrite the URL to a sort-less one.

    The row pushes "/admin/agents?agent=<id>". Without the sort appended, a reload after drilling in
    drops the operator back into the default order — the same reset as the poll bug, via a different
    door.
    """
    body = (await smoke.get("/admin/agents/_table", params={"sort": "kind", "order": "desc"})).text
    assert 'hx-push-url="/admin/agents?agent=alive-agent&amp;sort=kind&amp;order=desc"' in body


@pytest.mark.asyncio
async def test_page_route_honours_and_survives_sort(smoke: AsyncClient) -> None:
    """A full-page load carries the sort too, so a reload/bookmark reproduces the chosen order."""
    body = (await smoke.get("/admin/agents", params={"sort": "name", "order": "desc"})).text
    assert "<html" in body.lower()
    assert _poll_vals(body) == {"sort": "name", "order": "desc"}


@pytest.mark.asyncio
async def test_unknown_sort_degrades_to_default_and_never_reaches_a_column(smoke: AsyncClient) -> None:
    """An unwhitelisted sort renders the DEFAULT order at 200 — it does not 422 and does not sort.

    Asserting the status alone would pass against an implementation that happily ``getattr``-ed its
    way to a column, so this pins the ORDER: a hostile key naming a real-but-unoffered attribute
    (``token_hash``, ``revoked_at``) must produce output identical to sending no sort at all, which
    is only possible if the key never selected a column. 422-ing instead would blank the whole
    workspace on a poll to punish a stale bookmark (contract rule 3).

    Only ``sort`` varies: ``order`` is deliberately left off so it degrades to the default too.
    Pinning the direction as well would let a passing/failing result turn on the DIRECTION rather
    than on whether the hostile key reached a column, which is the property under test.
    """
    default_order = _row_order((await smoke.get("/admin/agents/_table")).text)
    for hostile in ("token_hash", "revoked_at", "id; drop table agents", "__class__", "name.desc()"):
        response = await smoke.get("/admin/agents/_table", params={"sort": hostile})
        assert response.status_code == 200, f"{hostile!r} must degrade, not 422"
        assert _row_order(response.text) == default_order, f"{hostile!r} reached a column"
        # The rejected key is discarded, never echoed back into the next poll.
        assert _poll_vals(response.text)["sort"] == "last_seen"


@pytest.mark.asyncio
async def test_unknown_order_degrades_to_default_direction(smoke: AsyncClient) -> None:
    """An unrecognised ``order`` falls back to the contract default direction rather than erroring."""
    response = await smoke.get("/admin/agents/_table", params={"sort": "name", "order": "sideways"})
    assert response.status_code == 200
    # The contract's default_order is DESCENDING (it encodes "most recently seen first").
    assert _poll_vals(response.text)["order"] == admin_agents.AGENTS_SORT.default_order


@pytest.mark.asyncio
async def test_active_column_announces_itself_via_aria_sort(smoke: AsyncClient) -> None:
    """The active header carries aria-sort; other sortable headers say "none" (contract rule 5)."""
    thead = (body := (await smoke.get("/admin/agents/_table", params={"sort": "name", "order": "desc"})).text)[
        body.find("<thead") : body.find("</thead>")
    ]
    assert 'aria-sort="descending"' in thead
    assert thead.count('aria-sort="descending"') == 1
    assert 'aria-sort="none"' in thead
    # "Status"/"Actions" are not sortable, so they must omit the attribute entirely rather than
    # claim aria-sort="none" -- which would announce a sorting affordance that does not exist.
    assert thead.count("aria-sort=") == len(admin_agents.AGENTS_SORT.columns)


@pytest.mark.asyncio
async def test_polling_never_halts_under_any_sort(smoke: AsyncClient) -> None:
    """UI-SPEC §Polling LOCKED: every sorted render still re-emits its own 5s trigger."""
    for params in ({}, {"sort": "name", "order": "asc"}, {"sort": "bogus", "order": "bogus"}):
        body = (await smoke.get("/admin/agents/_table", params=params)).text
        assert 'hx-trigger="every 5s"' in body
        assert 'hx-swap="outerHTML"' in body


@pytest.mark.asyncio
async def test_sort_click_preserves_the_open_detail_pane(smoke: AsyncClient) -> None:
    """Sorting must not close the operator's open agent (contract rule 4: one click changes one thing).

    ``?agent=`` is this table's other piece of view state — it drives the selected-row ring and the
    detail pane. A header link that dropped it would silently deselect the row the operator was
    reading the moment they reordered the table.
    """
    body = (await smoke.get("/admin/agents/_table", params={"agent": "alive-agent", "sort": "name", "order": "asc"})).text
    thead = body[body.find("<thead") : body.find("</thead>")]
    assert "agent=alive-agent&amp;sort=kind" in thead, "a sort click dropped the selected agent"
    # And the selection genuinely survived this render.
    assert 'aria-current="true"' in body
