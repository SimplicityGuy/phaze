"""Controller-side contract tests for Phase 29 plan 07: /admin/agents router.

Covers:
- GET /admin/agents — 301 redirect to /s/agents, preserving the query string (phaze-uvmcr.4).
- GET /admin/agents/_table — partial-only render (HTMX poll target), UNCHANGED.
- Status-pill rendering for the 4 states that reach the panel (alive/stale/dead/never).
- Revoked agents are filtered out of the panel entirely (revoked_at IS NULL).
- Empty state (UI-SPEC §Empty State LOCKED copy).
- Sort order: alive → stale → dead → never (revoked agents are filtered out of the panel).
- BLOCKER-2 failure-tolerant footer (htmx event listener + localStorage red banner) -- the
  listener now lives in shell.html (see tests/shared/core/test_shell_agents_pane.py for the
  H2 relocation coverage); this module still pins the banner MARKUP in agents_table.html.

phaze-uvmcr.4: the full-page render moved into the shell (UTILITY_PANES["agents"], reachable at
GET /s/agents). Every assertion that used to hit bare GET /admin/agents for FULL-PAGE content now
hits GET /s/agents instead (the smoke app mounts shell.router alongside admin_agents.router for
exactly this); assertions against the unconditional fragment endpoints (/_table,
/{id}/_activity, /compute-lanes/{id}) are unaffected and unchanged.

Uses a self-contained smoke-app fixture (mirrors test_pipeline_scans.py:46-78)
that installs the admin_agents + shell routers on a bare FastAPI app and overrides
get_session to use the project-wide session fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import html
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import select

from phaze.constants import AGENT_LIVENESS_STALE_SECONDS
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers import admin_agents, shell
from phaze.services.agent_liveness import sort_key


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a smoke FastAPI app mounting admin_agents.router + shell.router.

    phaze-uvmcr.4: shell.router is now REQUIRED here, not optional -- GET /admin/agents redirects
    to /s/agents (shell.shell_stage), and the full-page content assertions throughout this module
    hit /s/agents directly to exercise the SAME build_agents_pane_context() admin_agents.page()'s
    redirect target used to build inline before this bead.
    """
    app = FastAPI(title="admin-agents-smoke", version="test")
    app.include_router(admin_agents.router)
    app.include_router(shell.router)
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
async def sort_smoke(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Smoke client seeded so name-ascending is a FULL DERANGEMENT of the default last-seen order.

    The shared ``smoke`` fixture cannot prove poll survival on its own: its most-recently-seen agent
    is also its alphabetically-first, so it leads BOTH orders and a template that silently reset to
    the default would still look right at the top of the table. Here the four agents' names run
    opposite to their last-seen recency, so the two orders differ at EVERY position and any reset is
    detectable wherever it happens.

    Pre-existing rows are cleared for the same reason ``empty_smoke`` clears them: the conftest
    ``test-fileserver`` row has a NULL last_seen, so it would pin the tail of both orders and
    reintroduce the coincidence this fixture exists to remove.
    """
    from sqlalchemy import delete

    await session.execute(delete(Agent))
    now = datetime.now(UTC)
    session.add_all(
        [
            Agent(id="agent-1", name="Delta", scan_roots=[], last_seen_at=now - timedelta(seconds=1), kind="fileserver"),
            Agent(id="agent-2", name="Charlie", scan_roots=[], last_seen_at=now - timedelta(seconds=2), kind="fileserver"),
            Agent(id="agent-3", name="Bravo", scan_roots=[], last_seen_at=now - timedelta(seconds=3), kind="fileserver"),
            Agent(id="agent-4", name="Alpha", scan_roots=[], last_seen_at=now - timedelta(seconds=4), kind="fileserver"),
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
async def test_page_redirects_to_shell_pane(smoke: AsyncClient) -> None:
    """GET /admin/agents 301s to /s/agents (phaze-uvmcr.4), unconditionally -- no query string here."""
    response = await smoke.get("/admin/agents", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/s/agents"


@pytest.mark.asyncio
async def test_shell_pane_renders_full_shell_with_agents_table(smoke: AsyncClient) -> None:
    """GET /s/agents renders the full shell (rail/header/status strip) with the agents table inside."""
    response = await smoke.get("/s/agents")
    assert response.status_code == 200, response.text
    body = response.text
    # Full-shell chrome (not the retired base.html nav -- the shell's own rail/header/skip-link).
    assert "<html" in body
    assert 'id="stage-workspace"' in body
    assert 'data-stage="agents"' in body
    # The polling section is rendered.
    assert 'id="agents-table-section"' in body
    # The polling cadence + endpoint are wired correctly.
    # phaze-a6hm.4: the poll now arms sort.poll_url(), so the endpoint carries the active order as a
    # query string rather than standing alone. Asserted as a prefix -- the invariant is that the poll
    # re-requests THIS endpoint, not that it does so without parameters.
    assert _poll_url(body).startswith("/admin/agents/_table")
    assert 'hx-trigger="every 5s"' in body
    assert 'hx-swap="outerHTML"' in body


@pytest.mark.asyncio
async def test_htmx_request_to_shell_pane_returns_bare_fragment(smoke: AsyncClient) -> None:
    """HX-Request: true on /s/agents (a rail swap) returns the bare fragment, not the full shell."""
    response = await smoke.get("/s/agents", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    # Bare fragment: no shell chrome.
    assert "<html" not in body
    assert "<head" not in body
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
    # phaze-a6hm.4: the poll now arms sort.poll_url(), so the endpoint carries the active order as a
    # query string rather than standing alone. Asserted as a prefix -- the invariant is that the poll
    # re-requests THIS endpoint, not that it does so without parameters.
    assert _poll_url(body).startswith("/admin/agents/_table")


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
    for path in ("/admin/agents/_table", "/s/agents"):
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
async def test_lane_rows_render_for_both_clusters_never_dead(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """phaze-rdxfu: two stamped clusters (vox ACTIVE, xenolab IDLE) → two lane rows, never DEAD.

    A 2-compute registry (vox + xenolab) with a RUNNING CloudJob stamped only on vox: the merged
    table must render BOTH per-cluster rows labeled by backend_id — vox ACTIVE while xenolab stays a
    visible IDLE lane (registry-composed, no reachability probe) — and NEVER a perpetual DEAD state
    (KDEPLOY-04), which is structurally impossible for a lane row (TableRow.status_kind == "lane").
    """
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    await _seed_cloud_job(session, make_file, backend_id="vox")

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        for path in ("/s/agents", "/admin/agents/_table"):
            response = await ac.get(path)
            assert response.status_code == 200, response.text
            body = response.text
            # Both clusters render as their own rows.
            assert 'id="compute-lane-trigger-vox"' in body, f"vox row missing from {path}"
            assert 'id="compute-lane-trigger-xenolab"' in body, f"xenolab row missing from {path}"
            # vox is doing work → ACTIVE; xenolab is configured-but-quiet → IDLE (still listed).
            assert "ACTIVE" in body, f"vox ACTIVE pill missing from {path}"
            assert "IDLE" in body, f"xenolab IDLE pill missing from {path}"
            # Never a perpetual DEAD/rose state anywhere on the page (no dead agent is seeded either).
            assert "DEAD" not in body, f"DEAD leaked into {path}"


@pytest.mark.asyncio
def _merged_row_order(body: str) -> list[str]:
    """Return every row's trigger id (agent OR lane) in on-page order, prefix included.

    Used for the poll/full-page parity check below instead of a raw byte comparison of the whole
    section: `data-refreshed-at` and the "Last refreshed Ns ago" Alpine seed are real timestamps that
    legitimately differ by a few milliseconds between two SEPARATE requests, so a byte-for-byte
    comparison of the full section would be flaky on wall-clock timing rather than testing the
    property acceptance rule 7 actually asks for -- ROW ORDERING agreement.
    """
    return re.findall(r'<tr id="((?:agent|compute-lane)-trigger-[^"]+)"', body)


@pytest.mark.asyncio
async def test_poll_partial_matches_full_page_for_the_merged_table(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """phaze-rdxfu (acceptance rule 7): the /_table poll partial's row ordering matches the full page's.

    Agent rows and lane rows are a single include site, so the first-load full page and the 5s poll
    partial must render rows in the SAME order — no Pitfall-5 flicker between first-load and poll, and
    no ordering drift between the two render paths for the same query.
    """
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    await _seed_cloud_job(session, make_file, backend_id="vox")

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        full = (await ac.get("/s/agents")).text
        partial = (await ac.get("/admin/agents/_table")).text

    full_order = _merged_row_order(full)
    assert full_order, "the fixture must seed at least one row for this comparison to mean anything"
    assert full_order == _merged_row_order(partial)


@pytest.mark.asyncio
async def test_empty_registry_renders_normally_with_no_lane_rows(smoke: AsyncClient) -> None:
    """phaze-rdxfu (acceptance rule 7): a pure-local registry (no non-local backends) is not broken.

    The default smoke app has no cloud backends configured, so ``derive_compute_lane_identities``
    returns no lanes — the merged table must still render normally (agent rows only, never
    blank/error), and no leftover lane-only chrome (the retired Section-2 card grid) survives it.
    """
    response = await smoke.get("/admin/agents/_table")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="agents-table-section"' in body
    assert "compute-lane-trigger-" not in body, "no lane rows should render for an empty registry"
    assert "AliveBox" in body, "agent rows still render normally alongside an empty lane registry"


# ---------------------------------------------------------------------------
# COMPUTE-01 (dedupe): suppress the registry-shadowed never-heartbeating compute row
# ---------------------------------------------------------------------------


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
async def test_dedupe_registry_shadow_compute_row_suppressed_from_agent_row(shadow_smoke: AsyncClient) -> None:
    """COMPUTE-01: a never-seen 'vox'/kind=compute row never renders its OWN agent row; its lane row does.

    The exact "shown twice" defect: the vox cluster must render as a live LANE row (registry-composed)
    but NOT ALSO sit as a perpetual-NEVER AGENT row — one identity, one row.
    """
    for path in ("/s/agents", "/admin/agents/_table"):
        response = await shadow_smoke.get(path)
        assert response.status_code == 200, response.text
        body = response.text
        # Suppressed as an agent row (no shadow agent-trigger for vox).
        assert "agent-trigger-vox" not in body, f"vox shadow row leaked as an agent row in {path}"
        # Still surfaced as a live lane row.
        assert 'id="compute-lane-trigger-vox"' in body, f"vox lane row missing from {path}"


@pytest.mark.asyncio
async def test_dedupe_non_registry_compute_row_also_suppressed(shadow_smoke: AsyncClient) -> None:
    """phaze-2u8v.4: a never-seen compute Agent matching NO registry id is suppressed from its own row too.

    This inverts the original COMPUTE-01 expectation, which kept such a row visible "so the operator can
    see (and clean up) it". The affordance was implemented by rendering a claim that is false by
    construction: a kind='compute' row is a bearer-token callback identity, nothing behind it heartbeats,
    so its NEVER/—/never/0 columns describe the schema rather than the cluster. Registry membership does
    not change that, and keying on it is what let the deployed k8s rows sit perpetually-dead — a kueue
    backend binds no agent_ref, and a lane whose [[backends]] block is commented out has no registry key
    at all. The heartbeating table renders only identities a persistent process actually beats for;
    identities that cannot heartbeat get a lane row instead (none here — orphan-compute is registry-less).
    """
    response = await shadow_smoke.get("/admin/agents/_table")
    body = response.text
    assert "agent-trigger-orphan-compute" not in body, "non-registry compute NEVER row leaked into the heartbeating table"
    assert "OrphanCompute" not in body


@pytest.mark.asyncio
async def test_dedupe_compute_row_that_stopped_heartbeating_still_renders_dead(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """The suppression gate stays ``_status == 'never'`` — a compute agent that WENT dead stays visible.

    The counterweight to the widened predicate: suppressing by kind alone would hide a real compute node
    that was heartbeating and then stopped, which is exactly the failure an operator needs this table to
    report. Only rows that have NEVER checked in are structurally unable to heartbeat.
    """
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    stale = datetime.now(UTC) - timedelta(seconds=AGENT_LIVENESS_STALE_SECONDS + 60)
    session.add(Agent(id="compute-went-down", name="ComputeWentDown", scan_roots=[], kind="compute", last_seen_at=stale))
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/admin/agents/_table")

    body = response.text
    assert "agent-trigger-compute-went-down" in body, "a compute agent that stopped heartbeating must stay visible"
    assert "DEAD" in body


@pytest.mark.asyncio
async def test_dedupe_fileserver_never_row_unaffected(shadow_smoke: AsyncClient) -> None:
    """COMPUTE-01: an ordinary fileserver NEVER row is untouched by the compute-only suppression."""
    response = await shadow_smoke.get("/admin/agents/_table")
    body = response.text
    assert "agent-trigger-fs-never" in body, "fileserver NEVER row was wrongly suppressed"
    assert "FsNever" in body
    # NEVER pill still rendered for the surviving fileserver row.
    assert "NEVER" in body


@pytest.mark.asyncio
async def test_dedupe_cluster_id_represented_by_exactly_one_row_kind(shadow_smoke: AsyncClient) -> None:
    """COMPUTE-01 invariant: the shadowed cluster id 'vox' is represented by exactly ONE row kind.

    Before the fix, 'vox' appeared as BOTH a NEVER agent row AND a live lane row. After suppression it
    renders ONLY as a lane row — proving the "shown twice" duplication is gone while the lane identity
    is preserved.
    """
    response = await shadow_smoke.get("/s/agents")
    body = response.text
    assert "agent-trigger-vox" not in body, "vox still duplicated into an agent row (shown twice)"
    assert 'id="compute-lane-trigger-vox"' in body, "vox lane identity lost"


@pytest.mark.asyncio
async def test_dedupe_heartbeating_compute_agent_row_kept(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """COMPUTE-01: a genuinely-heartbeating registry compute agent keeps its own agent row (never suppressed).

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

    body = response.text
    assert "agent-trigger-vox" in body, "a heartbeating (alive) compute agent must keep its row"
    assert "ALIVE" in body


# ---------------------------------------------------------------------------
# phaze-2u8v.4 — the deployed shape: k8s rows perpetually-dead as agent rows
#
# The two dedupe generations above both keyed on REGISTRY MEMBERSHIP: the row was dropped when its
# id/name equalled a non-local backend id (phaze-zlv), or later when it equalled a backend's bound
# ``agent_ref`` (phaze-ifcr). The deployed registry satisfies NEITHER key, so in production the filter
# never once fired:
#
#   * the kueue backend is ``id = "vox"`` and binds no agent_ref (the binding was optional), while the
#     callback agent the operator provisioned is ``k8s-vox`` — no key matches;
#   * the second cluster's ``[[backends]]`` block is commented out (the lane was disabled), so its
#     ``k8s-xenolab`` callback row has no registry entry that COULD match, ever.
#
# Both therefore rendered STATUS "NEVER" / QUEUE "—" / LAST SEEN "never" / SCAN ROOTS 0 as an agent
# row while the SAME cluster's lane row reported ACTIVE with 3 running (pre-merge: two DIFFERENT
# panels disagreeing; post-merge: two DIFFERENT rows disagreeing, same underlying defect shape).
# This fixture reproduces that registry verbatim (minus the disabled block) and pins the fix.
# ---------------------------------------------------------------------------

_DEPLOYED_KUEUE_REGISTRY = """
    [[backends]]
    kind = "kueue"
    id = "vox"
    rank = 10
    cap = 3
    buckets = ["vox-bucket"]

    [backends.kube]
    api_url = "https://kube.example.com"
    namespace = "phaze"
    local_queue = "phaze-burst"

    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1

    [[buckets]]
    id = "vox-bucket"
    scope = "cluster-specific"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-burst"
    """


@pytest_asyncio.fixture
async def deployed_shape_smoke(session: AsyncSession, backends_toml_env) -> AsyncGenerator[AsyncClient]:  # type: ignore[no-untyped-def]
    """Smoke client mirroring the deployed registry + the three Agent rows it actually carries.

    ``k8s-vox`` shadows a registered backend under a different name; ``k8s-xenolab`` shadows a lane that
    is no longer in the registry at all; ``nox`` is the real file-server agent that must be unaffected.
    """
    backends_toml_env(_DEPLOYED_KUEUE_REGISTRY)
    session.add_all(
        [
            Agent(id="k8s-vox", name="k8s vox", scan_roots=[], kind="compute"),  # last_seen_at=None → NEVER
            Agent(id="k8s-xenolab", name="k8s xenolab", scan_roots=[], kind="compute"),
            Agent(id="nox", name="nox", scan_roots=["/srv/a", "/srv/b"], kind="fileserver", last_seen_at=datetime.now(UTC)),
        ]
    )
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_deployed_k8s_rows_never_render_as_dead_agents(deployed_shape_smoke: AsyncClient) -> None:
    """Neither k8s callback row reaches the heartbeating table — under a registry that matches neither by name.

    ``k8s-vox`` diverges from its backend id and binds no agent_ref; ``k8s-xenolab``'s backend is absent
    from the registry entirely. Registry-keyed suppression misses both; kind-keyed suppression cannot.
    """
    for path in ("/s/agents", "/admin/agents/_table"):
        response = await deployed_shape_smoke.get(path)
        assert response.status_code == 200, response.text
        body = response.text
        assert "agent-trigger-k8s-vox" not in body, f"k8s-vox rendered as a heartbeating agent in {path}"
        assert "agent-trigger-k8s-xenolab" not in body, f"k8s-xenolab rendered as a heartbeating agent in {path}"


@pytest.mark.asyncio
async def test_deployed_shape_agent_and_lane_rows_do_not_contradict_each_other(deployed_shape_smoke: AsyncClient) -> None:
    """The vox cluster is claimed live by exactly one row kind — the lane row — and dead by none.

    The reported defect was two DIFFERENT panels disagreeing about the SAME lane: "NEVER" in one,
    "ACTIVE · 3 workloads" in the other. Post-merge that is two DIFFERENT ROWS disagreeing — the lane
    row keeps the lane; no agent row for it exists to contradict it.
    """
    response = await deployed_shape_smoke.get("/s/agents")
    body = response.text
    assert "agent-trigger-k8s-vox" not in body, "the vox cluster is still represented by an agent row"
    assert 'id="compute-lane-trigger-vox"' in body, "the vox lane identity was lost"
    assert "DEAD" not in body


@pytest.mark.asyncio
async def test_deployed_shape_fileserver_agent_keeps_its_liveness(deployed_shape_smoke: AsyncClient) -> None:
    """The file-server agent still reports real heartbeat liveness — the no-regression half of the bead.

    A never-seen FILESERVER row (the shared conftest's ``test-fileserver``) keeps its NEVER pill: a file
    server is a persistent process, so "has not checked in" is a real and actionable statement about it.
    Only the k8s rows, which have no process to check in, never render their own agent row.
    """
    response = await deployed_shape_smoke.get("/admin/agents/_table")
    body = response.text
    assert "agent-trigger-nox" in body, "the file-server agent must stay in the heartbeating table"
    assert "ALIVE" in body
    assert "agent-trigger-k8s" not in body, "a k8s callback identity is still being rendered as a heartbeating agent"


# ---------------------------------------------------------------------------
# Phase 48 — Kind badge (CLOUDAGENT-03), UI-SPEC §Component Contract LOCKED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kind_badge_compute_renders(smoke: AsyncClient) -> None:
    """Full shell-hosted GET /s/agents renders the COMPUTE badge for a kind='compute' row.

    Palette + label + aria-label are LOCKED by 48-UI-SPEC §Component Contract.
    """
    response = await smoke.get("/s/agents")
    body = response.text
    assert "COMPUTE" in body
    assert "bg-indigo-100 dark:bg-indigo-950" in body
    assert "text-indigo-700 dark:text-indigo-400" in body
    assert 'aria-label="Kind: compute"' in body
    # LOCKED geometry copied verbatim from _status_pill.html.
    assert "text-xs font-semibold px-2 py-0.5 rounded-full" in body


@pytest.mark.asyncio
async def test_kind_badge_fileserver_renders(smoke: AsyncClient) -> None:
    """Full shell-hosted GET /s/agents renders the FILE SERVER badge for a kind='fileserver' row."""
    response = await smoke.get("/s/agents")
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
# BLOCKER-2 tests — UI-SPEC §Error / Failure-Tolerant Refresh LOCKED
#
# phaze-uvmcr.4 (H2): the htmx:responseError/htmx:sendError/htmx:afterSwap LISTENER moved out of
# admin/agents.html (retired) into shell.html's own persistent bottom <script> -- outside
# #stage-workspace, so a rail swap can never re-run it and stack a duplicate. The banner MARKUP
# itself (role=alert, localStorage-driven Alpine footer) is untouched in agents_table.html. The
# tests below pin BOTH halves of that split, plus the structural proof that the listener-attach
# code is physically absent from every response shape that could otherwise re-run it (a rail-swap
# fragment, a poll tick) -- which is what makes "exactly one attach, ever" true without a browser.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_shell_load_includes_htmx_error_listener(smoke: AsyncClient) -> None:
    """BLOCKER-2 (H2): a full shell load of /s/agents includes the listener, exactly once."""
    response = await smoke.get("/s/agents")
    body = response.text
    assert "htmx:responseError" in body, "Missing htmx:responseError listener (BLOCKER-2)"
    assert "htmx:sendError" in body, "Missing htmx:sendError listener (BLOCKER-2)"
    assert "htmx:afterSwap" in body, "Missing htmx:afterSwap recovery handler (BLOCKER-2)"
    assert "phaze:agents:lastError" in body, "Missing localStorage key (BLOCKER-2)"
    assert "localStorage.setItem" in body, "Listener must write to localStorage (BLOCKER-2)"
    assert "localStorage.removeItem" in body, "Recovery handler must clear localStorage (BLOCKER-2)"
    # Exactly one registration per full load -- not stacked, not duplicated.
    assert body.count("document.body.addEventListener('htmx:responseError'") == 1


@pytest.mark.asyncio
async def test_error_listener_is_shell_chrome_not_agents_pane_content(smoke: AsyncClient) -> None:
    """H2: the listener lives OUTSIDE #stage-workspace -- it renders on every stage, not just agents.

    Proves the relocation actually landed in shell chrome (shell.html) rather than merely moving
    within the pane content: a full load of a COMPLETELY DIFFERENT stage (the Summary landing) must
    carry the identical listener, because it is part of the shared shell every stage's full-document
    response includes -- agents-specific only in the id it checks against, never in whether it is
    present at all.
    """
    other_stage = await smoke.get("/")
    assert "htmx:responseError" in other_stage.text
    assert "phaze:agents:lastError" in other_stage.text


@pytest.mark.asyncio
async def test_rail_swap_fragment_never_reattaches_the_error_listener(smoke: AsyncClient) -> None:
    """H2 (the core fix): a rail-swap fragment for /s/agents never carries the listener-attach code.

    This is the structural guarantee behind "N navigations to the pane never produce N listeners":
    the attach code is simply ABSENT from every response an htmx rail swap could ever receive, so
    repeated navigation cannot stack a duplicate no matter how many times it happens. The banner
    MARKUP itself must still be present -- only the ATTACH code is chrome-only.
    """
    response = await smoke.get("/s/agents", headers={"HX-Request": "true"})
    body = response.text
    assert "document.body.addEventListener" not in body, "the listener-attach script leaked into the rail-swap fragment (H2 regression)"
    # The banner it drives is untouched and still renders inside the fragment.
    assert 'role="alert"' in body
    assert "Refresh failed" in body


@pytest.mark.asyncio
async def test_poll_partial_never_carries_the_error_listener(smoke: AsyncClient) -> None:
    """The 5s poll partial (/_table) never re-attaches the listener either -- same H2 guarantee."""
    response = await smoke.get("/admin/agents/_table")
    assert "document.body.addEventListener" not in response.text


@pytest.mark.asyncio
async def test_banner_still_works_after_arriving_via_a_rail_swap(smoke: AsyncClient) -> None:
    """Acceptance rule 5: the BLOCKER-2 banner still appears on a genuine poll failure reached via a
    rail swap. The banner is a self-contained Alpine component (agents_table.html) that reads
    localStorage on its own 2s poll, independent of how the surrounding pane was reached -- this
    pins that its markup (and the localStorage read it needs) is present in the rail-swap fragment
    shape, not only the full-page shape.
    """
    response = await smoke.get("/s/agents", headers={"HX-Request": "true"})
    body = response.text
    assert "localStorage.getItem" in body, "banner must read localStorage even inside a rail-swap fragment"
    assert "phaze:agents:lastError" in body
    assert 'role="alert"' in body


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
    """Full shell-hosted GET /s/agents renders the discreet /saq footer link when enable_saq_ui is true.

    The handler reads the flag via the ``get_settings()`` call-site, so we toggle it through the
    env var + lru_cache-clear idiom (the conftest autouse fixture also clears the cache per test).
    The link must open in a new tab with ``rel="noopener"`` (T-66-05 reverse-tabnabbing guard, D-11).
    """
    from phaze.config import get_settings

    monkeypatch.setenv("PHAZE_ENABLE_SAQ_UI", "true")
    get_settings.cache_clear()

    response = await smoke.get("/s/agents")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'href="/saq"' in body, "flag-gated /saq footer link must be present when enable_saq_ui is true"
    assert 'target="_blank"' in body, "the /saq link must open in a new tab (D-11)"
    assert 'rel="noopener"' in body, "the /saq link must carry rel=noopener (reverse-tabnabbing guard, T-66-05)"


@pytest.mark.asyncio
async def test_saq_link_absent_when_enable_saq_ui_false(smoke: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full shell-hosted GET /s/agents omits the /saq link when enable_saq_ui is false.

    When the flag is off, the ``/saq`` sub-app is not mounted (main.py), so the link must NOT
    render — otherwise it would dangle as a dead 404 (D-09 / T-66-07).
    """
    from phaze.config import get_settings

    monkeypatch.setenv("PHAZE_ENABLE_SAQ_UI", "false")
    get_settings.cache_clear()

    response = await smoke.get("/s/agents")
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
# GET /admin/agents history-restore response shape (phaze-64uy, superseded by phaze-uvmcr.4)
#
# admin/partials/agents_table.html sets hx-push-url="/admin/agents?agent=<id>" on each drill-in row
# (DRILL-03 / D-02), so that URL enters browser history. A Back with the snapshot evicted from
# htmx's 10-entry cache re-fetches it as a RESTORE carrying BOTH HX-Request: true and
# HX-History-Restore-Request: true -- and on a restore htmx IGNORES hx-target and swaps the
# response into <body>.
#
# phaze-uvmcr.4: GET /admin/agents is now an UNCONDITIONAL 301 redirect to /s/agents (no shape
# branching left here at all -- see admin_agents.page()'s docstring for why no in-tree caller ever
# issues a live htmx swap against this bare path). A restore request redirects exactly like a plain
# one; the interesting assertion is that FOLLOWING that redirect lands on shell.shell_stage's real
# full document, which is what response_shape.py rule 2 actually requires for a restore.
# ---------------------------------------------------------------------------


_RESTORE_HEADERS = {"HX-Request": "true", "HX-History-Restore-Request": "true"}


@pytest.mark.asyncio
async def test_history_restore_also_redirects_to_shell(smoke: AsyncClient) -> None:
    """A history-restore GET /admin/agents redirects too -- not just a plain request."""
    response = await smoke.get("/admin/agents?agent=alive-agent", headers=_RESTORE_HEADERS, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/s/agents?agent=alive-agent"


@pytest.mark.asyncio
async def test_restore_header_alone_also_redirects(smoke: AsyncClient) -> None:
    """The restore header dominates even without ``HX-Request`` (response_shape rule 2) -- still redirects."""
    response = await smoke.get("/admin/agents", headers={"HX-History-Restore-Request": "true"}, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/s/agents"


@pytest.mark.asyncio
async def test_history_restore_redirect_lands_on_the_full_shell(smoke: AsyncClient) -> None:
    """Following the restore redirect lands on the real full document, chrome included.

    Asserts the CHROME, not merely a 200 -- a handler that answered with the bare table partial
    would still pass a status-only assertion, which is exactly the phaze-64uy defect class.
    """
    response = await smoke.get("/admin/agents?agent=alive-agent", headers=_RESTORE_HEADERS, follow_redirects=True)
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "a history restore must ultimately land on a full document, not a fragment"
    assert 'id="stage-workspace"' in body, "the shell chrome must survive a history restore"
    assert 'id="agents-table-section"' in body, "the polling section must still be present inside the page"


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


def _poll_url(body: str) -> str:
    """Return the URL the rendered self-poll has ARMED for its next 5s tick.

    Read off the live ``hx-get`` of ``#agents-table-section`` (the element that re-requests itself)
    rather than being assumed, so a change that stops threading the sort into the poll fails these
    tests instead of passing against a hardcoded guess. Scoped to that opening tag specifically:
    rows and sort buttons carry their own ``hx-get``, and any of them could satisfy a loose match.
    """
    start = body.find('<section id="agents-table-section"')
    assert start != -1, "the polled section is missing"
    tag = body[start : body.index(">", start)]
    match = re.search(r'hx-get="([^"]+)"', tag)
    assert match is not None, "the polled section must arm an hx-get"
    return html.unescape(match.group(1))


def _poll_vals(body: str) -> dict[str, str]:
    """Return the ``sort``/``order`` the self-poll will re-request on its next tick."""
    query = urlparse(_poll_url(body)).query
    return {name: values[0] for name, values in parse_qs(query).items()}


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
async def test_queue_sort_survives_a_heartbeat_above_int32_max(smoke: AsyncClient, session: AsyncSession) -> None:
    """phaze-5yyh: an agent-reported ``queue_depth`` above INT32_MAX must not 500 the queue-sorted
    view. ``queue_depth`` is committed as an int8 domain on both the heartbeat writer
    (``schemas/agent_heartbeat.py``'s ``QUEUE_DEPTH_MAX = 10**12``) and the storage side
    (``_LANE_MERGE_SQL``'s ``::bigint`` SUM) -- casting the ORDER BY expression to int4 made
    Postgres raise ``DataError`` on the whole statement for any real value above 2147483647, and
    because the never-halting 5s self-poll re-sends ``sort=queue``, the break was permanent for
    every operator once a single agent reported one.
    """
    agent = (await session.execute(select(Agent).where(Agent.id == "alive-agent"))).scalar_one()
    agent.last_status = {"queue_depth": 3_000_000_000}  # > INT32_MAX (2147483647), valid int8
    await session.commit()

    response = await smoke.get("/admin/agents/_table", params={"sort": "queue", "order": "asc"})
    assert response.status_code == 200, response.text
    assert "alive-agent" in _row_order(response.text)


# ---------------------------------------------------------------------------
# phaze-rdxfu (acceptance rule 8 / SCOPE rule 3): lane placement is deterministic and PINNED under
# every sortable column and both directions -- including the -infinity/-1 no-value folding for
# last_seen/scan_roots. Agent-relative order is never re-derived (that stays SQL's job, pinned above
# by test_default_matches_locked_sort_key / test_sort_is_server_side_across_the_whole_set); each test
# below crafts values chosen so the expected merged order is unambiguous, then asserts it exactly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lane_placement_pinned_for_name_sort(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A lane sorts into the AGENT column by plain string comparison on its backend_id, both directions."""
    from sqlalchemy import delete

    await session.execute(delete(Agent))
    now = datetime.now(UTC)
    session.add_all(
        [
            Agent(id="agent-beta", name="Beta", scan_roots=[], kind="fileserver", last_seen_at=now),
            Agent(id="agent-delta", name="Delta", scan_roots=[], kind="fileserver", last_seen_at=now),
        ]
    )
    await session.commit()
    backends_toml_env("""
    [[backends]]
    kind = "compute"
    id = "Charlie"
    rank = 10
    cap = 2
    agent_ref = "charlie-node"
    scratch_dir = "/scratch/charlie"
    push_host = "charlie.push"
    """)

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        asc = (await ac.get("/admin/agents/_table", params={"sort": "name", "order": "asc"})).text
        desc = (await ac.get("/admin/agents/_table", params={"sort": "name", "order": "desc"})).text

    assert _merged_row_order(asc) == ["agent-trigger-agent-beta", "compute-lane-trigger-Charlie", "agent-trigger-agent-delta"]
    assert _merged_row_order(desc) == ["agent-trigger-agent-delta", "compute-lane-trigger-Charlie", "agent-trigger-agent-beta"]


@pytest.mark.asyncio
async def test_lane_placement_pinned_for_kind_sort(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A lane sorts into the KIND column by the same string comparison an agent's kind would use.

    'compute' < 'fileserver' < 'kueue' (ASCII), so a single kueue lane sorts strictly after every
    real agent kind and a single compute agent sorts strictly before every real agent kind -- one
    row per bucket is enough to pin the ordering unambiguously.
    """
    from sqlalchemy import delete

    await session.execute(delete(Agent))
    now = datetime.now(UTC)
    session.add_all(
        [
            Agent(id="agent-compute", name="AgentCompute", scan_roots=[], kind="compute", last_seen_at=now),
            Agent(id="agent-fs", name="AgentFs", scan_roots=[], kind="fileserver", last_seen_at=now),
        ]
    )
    await session.commit()
    backends_toml_env(_DEPLOYED_KUEUE_REGISTRY)  # a single kind='kueue' backend, id='vox'

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        asc = (await ac.get("/admin/agents/_table", params={"sort": "kind", "order": "asc"})).text
        desc = (await ac.get("/admin/agents/_table", params={"sort": "kind", "order": "desc"})).text

    assert _merged_row_order(asc) == ["agent-trigger-agent-compute", "agent-trigger-agent-fs", "compute-lane-trigger-vox"]
    assert _merged_row_order(desc) == ["compute-lane-trigger-vox", "agent-trigger-agent-fs", "agent-trigger-agent-compute"]


@pytest.mark.asyncio
async def test_lane_placement_pinned_for_queue_sort(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """A lane's ``waiting`` count competes DIRECTLY with an agent's queue depth (real values, no fold).

    An agent with no reported queue_depth still folds to -1 (the existing _QUEUE_DEPTH_ORDER fold,
    unrelated to lanes) and sorts below the lane's real 1-waiting count; an agent reporting a REAL
    depth of 5 sorts above it.
    """
    import uuid

    from phaze.models.cloud_job import CloudJob, CloudJobStatus

    # make_file's default agent_id="test-fileserver" FK-targets the conftest seed row, so it is left
    # in place here (unlike the other pinning tests, which wipe the Agent table) -- the assertions
    # below filter the rendered order down to just the 3 rows under test, so its own NEVER row (whose
    # own -1 queue fold could tie with agent-noqueue) never has to be reasoned about.
    file = await make_file(original_filename="vox-waiting.mp3")
    now = datetime.now(UTC)
    session.add_all(
        [
            Agent(id="agent-noqueue", name="AgentNoQueue", scan_roots=[], kind="fileserver", last_seen_at=now),
            Agent(id="agent-q5", name="AgentQ5", scan_roots=[], kind="fileserver", last_seen_at=now, last_status={"queue_depth": 5}),
        ]
    )
    backends_toml_env(_DEPLOYED_KUEUE_REGISTRY)  # a single kind='kueue' backend, id='vox'
    session.add(
        CloudJob(
            id=uuid.uuid4(), file_id=file.id, s3_key=f"staging/{file.id}", status=CloudJobStatus.SUBMITTED.value, backend_id="vox", inadmissible=True
        )
    )
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        asc = (await ac.get("/admin/agents/_table", params={"sort": "queue", "order": "asc"})).text
        desc = (await ac.get("/admin/agents/_table", params={"sort": "queue", "order": "desc"})).text

    _under_test = {"agent-trigger-agent-noqueue", "agent-trigger-agent-q5", "compute-lane-trigger-vox"}
    assert [r for r in _merged_row_order(asc) if r in _under_test] == [
        "agent-trigger-agent-noqueue",
        "compute-lane-trigger-vox",
        "agent-trigger-agent-q5",
    ]
    assert [r for r in _merged_row_order(desc) if r in _under_test] == [
        "agent-trigger-agent-q5",
        "compute-lane-trigger-vox",
        "agent-trigger-agent-noqueue",
    ]


@pytest.mark.asyncio
async def test_lane_placement_pinned_for_last_seen_sort_no_value_fold(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A lane's last_seen folds to -infinity -- it sorts EXACTLY where a never-seen agent would.

    Mirrors ``_LAST_SEEN_ORDER``'s NULL fold precisely (acceptance rule 3): a lane is never a "most
    recent" row under either direction, and a tie against a genuinely never-seen agent always
    resolves agent-first (deterministic, never a coin flip that could reshuffle between polls).
    """
    from sqlalchemy import delete

    await session.execute(delete(Agent))
    now = datetime.now(UTC)
    session.add_all(
        [
            Agent(id="agent-recent", name="AgentRecent", scan_roots=[], kind="fileserver", last_seen_at=now - timedelta(seconds=5)),
            Agent(id="agent-old", name="AgentOld", scan_roots=[], kind="fileserver", last_seen_at=now - timedelta(seconds=500)),
            Agent(id="agent-never", name="AgentNever", scan_roots=[], kind="fileserver"),  # last_seen_at=None
        ]
    )
    await session.commit()
    backends_toml_env(_DEPLOYED_KUEUE_REGISTRY)  # a single kind='kueue' backend, id='vox'

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        desc = (await ac.get("/admin/agents/_table", params={"sort": "last_seen", "order": "desc"})).text
        asc = (await ac.get("/admin/agents/_table", params={"sort": "last_seen", "order": "asc"})).text

    assert _merged_row_order(desc) == [
        "agent-trigger-agent-recent",
        "agent-trigger-agent-old",
        "agent-trigger-agent-never",
        "compute-lane-trigger-vox",
    ]
    assert _merged_row_order(asc) == [
        "agent-trigger-agent-never",
        "compute-lane-trigger-vox",
        "agent-trigger-agent-old",
        "agent-trigger-agent-recent",
    ]


@pytest.mark.asyncio
async def test_lane_placement_pinned_for_scan_roots_sort_no_value_fold(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A lane's scan_roots folds to -1 -- mirrors ``_QUEUE_DEPTH_ORDER``'s absent-value fold exactly.

    An agent with a genuine EMPTY scan_roots list still reports a real ``0``, which is NOT the same
    fold value and must sort strictly above a lane's -1 under both directions.
    """
    from sqlalchemy import delete

    await session.execute(delete(Agent))
    now = datetime.now(UTC)
    session.add_all(
        [
            Agent(id="agent-noroots", name="AgentNoRoots", scan_roots=[], kind="fileserver", last_seen_at=now),
            Agent(id="agent-3roots", name="Agent3Roots", scan_roots=["/a", "/b", "/c"], kind="fileserver", last_seen_at=now),
        ]
    )
    await session.commit()
    backends_toml_env(_DEPLOYED_KUEUE_REGISTRY)  # a single kind='kueue' backend, id='vox'

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        asc = (await ac.get("/admin/agents/_table", params={"sort": "scan_roots", "order": "asc"})).text
        desc = (await ac.get("/admin/agents/_table", params={"sort": "scan_roots", "order": "desc"})).text

    assert _merged_row_order(asc) == ["compute-lane-trigger-vox", "agent-trigger-agent-noroots", "agent-trigger-agent-3roots"]
    assert _merged_row_order(desc) == ["agent-trigger-agent-3roots", "agent-trigger-agent-noroots", "compute-lane-trigger-vox"]


@pytest.mark.asyncio
async def test_poll_tick_preserves_operator_sort(sort_smoke: AsyncClient) -> None:
    """THE bead: the 5s self-poll carries the chosen sort forward instead of resetting it.

    #agents-table-section re-swaps ITSELF every 5s with hx-swap="outerHTML" and is spec'd never to
    halt. So it is not enough that the click renders sorted — the NEXT tick must re-send the sort,
    or the table snaps back to the default order 5 seconds after the operator clicked, silently, on
    a fuse too long for any manual check to catch.

    This replays the tick rather than asserting the first render: it reads the URL the response
    ARMED via ``sort.poll_url()`` (contract rule 4a) and issues exactly that request, the way htmx
    would, then asserts the follow-up render is still in the operator's order — and still arms the
    same URL, so tick N+2 survives too.

    The sort chosen here ("name" ascending) is one whose order differs from the default at EVERY
    position (asserted below). A sort that merely agreed with the default on the first row would let
    a completely broken template pass this test.
    """
    default = _row_order((await sort_smoke.get("/admin/agents/_table")).text)
    first = await sort_smoke.get("/admin/agents/_table", params={"sort": "name", "order": "asc"})
    chosen = _row_order(first.text)

    # Without this the test could not fail: if the two orders agreed, a poll that reset to the
    # default would still "preserve" the visible order and the regression would sail through.
    assert set(chosen) == set(default)
    assert all(a != b for a, b in zip(chosen, default, strict=True)), (
        f"seed data too weak to detect a reset: sorted and default orders coincide somewhere.\nsorted={chosen}\ndefault={default}"
    )

    # What the rendered section will actually request on its next tick — read from the markup.
    armed = _poll_url(first.text)
    assert _poll_vals(first.text) == {"sort": "name", "order": "asc"}, f"the poll did not arm the operator's sort: {armed}"

    # Tick N+1: fire the armed URL verbatim.
    tick = await sort_smoke.get(armed)
    assert tick.status_code == 200
    assert _row_order(tick.text) == chosen, "the 5s poll reset the operator's chosen sort to the default"

    # ...and the loop is self-sustaining: tick N+1 arms the same URL for tick N+2.
    assert _poll_url(tick.text) == armed


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
    """A full shell-hosted load carries the sort too, so a reload/bookmark reproduces the chosen order."""
    body = (await smoke.get("/s/agents", params={"sort": "name", "order": "desc"})).text
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

    ``?agent=`` is this table's other view state — it drives the selected-row ring and the detail
    pane — but it travels differently from the sort, and the split is the point of this test.

    It is carried in the section's ``hx-vals``, read LIVE from ``location.search``, which htmx
    inherits to descendant elements — so the sort buttons send it without naming it. It is
    deliberately absent from ``poll_url()``: a row click swaps only ``#detail-pane``, so this
    section's rendered markup still holds the PREVIOUS selection, and a baked-in ``?agent=`` would
    re-assert that stale id on every 5s tick and erase the ring the operator just clicked.

    The two channels must also stay disjoint, because htmx APPENDS ``hx-vals`` to the ``hx-get``
    query string — a key in both would be transmitted twice.
    """
    body = (await smoke.get("/admin/agents/_table", params={"agent": "alive-agent", "sort": "name", "order": "asc"})).text

    # The live selection rides in hx-vals on the polled section, inherited by the sort buttons.
    # phaze-2u8v.5 threads the SAME channel for ?clane= (the burst-lane drill-down selection).
    # phaze-w92dg appends `|| ""`: a null from an absent param would serialize as the literal
    # string "null", which the unresolved-selection carrier logic would treat as a real id.
    assert (
        'hx-vals=\'js:{agent: new URLSearchParams(location.search).get("agent") || "", clane: new URLSearchParams(location.search).get("clane") || ""}\''
    ) in body
    # ...and is NOT frozen into the armed poll URL, where it would go stale.
    assert "agent" not in _poll_vals(body), "a stale ?agent= was baked into the poll and will erase the selection"
    # The selection genuinely survived this render.
    assert 'aria-current="true"' in body


# ---------------------------------------------------------------------------
# phaze-rdxfu — every lane is a row, indistinguishable in interaction shape from an agent row; the
# per-lane state colors are never the sole signal; a lane row is a drill-in trigger into an
# EXPANDED ROW whose body fetches GET /admin/agents/compute-lanes/{id}.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merged_table_carries_no_unconditional_amber_alarm_styling(smoke: AsyncClient) -> None:
    """The retired Section-2 container/heading amber never reappears in the merged table.

    That amber never varied with data (every lane could be IDLE and the shell was still amber) -- it
    read as an always-on alarm. The merged table shares Section 1's ALWAYS-neutral chrome, so this
    guard pins that the retired classes cannot be reintroduced by a future edit.
    """
    response = await smoke.get("/admin/agents/_table")
    body = response.text
    assert "border-amber-500/25" not in body
    assert "bg-amber-500/[0.06]" not in body
    assert "text-amber-700 dark:text-amber-300" not in body  # the old Section-2 heading color


@pytest.mark.asyncio
async def test_lane_status_pill_carries_word_and_aria_label_not_hue_only(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """A lane's STATUS cell spells out ACTIVE/WAITING/IDLE by word + aria-label, never hue alone (WCAG 1.4.1).

    The retired Section-2 legend explained the SAME three colors in a separate paragraph next to the
    card grid; the merged table's per-row STATUS pill is now the self-explanatory unit (mirrors
    _kind_badge.html's "glyph/word + aria-label, never hue-only" contract) so no standalone legend is
    needed to understand ANY row -- consistent with how the agent 5-state pill already worked.
    """
    _seeded_backends = """
    [[backends]]
    kind = "kueue"
    id = "burst-a"
    rank = 10
    cap = 3
    buckets = ["burst-a-bucket"]

    [backends.kube]
    api_url = "https://kube.example.com"
    namespace = "phaze"
    local_queue = "phaze-burst"

    [[buckets]]
    id = "burst-a-bucket"
    scope = "cluster-specific"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-burst"
    """
    backends_toml_env(_seeded_backends)
    await _seed_cloud_job(session, make_file, backend_id="burst-a")

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/admin/agents/_table")
    body = response.text
    assert "ACTIVE" in body
    assert 'aria-label="Status: active"' in body


@pytest.mark.asyncio
async def test_lane_row_is_a_drill_in_trigger_matching_the_agent_row_pattern(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """Each real lane row carries the SAME keyboard-accessible drill-in wiring shape an agent row does.

    phaze-rdxfu: a lane row's click re-fetches the WHOLE merged table (hx-target=#agents-table-section)
    naming its OWN selection via hx-vals, exactly like an agent row -- no more direct
    hx-get=/admin/agents/compute-lanes/{id} on the trigger itself (that endpoint is now only fetched by
    the expanded row's body slot, once the row is open).
    """
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    await _seed_cloud_job(session, make_file, backend_id="vox")

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/admin/agents/_table")
    body = response.text

    assert 'id="compute-lane-trigger-vox"' in body
    assert 'role="button"' in body
    assert """hx-vals='{"clane": "vox"}'""" in body
    assert 'hx-target="#agents-table-section"' in body
    assert 'hx-push-url="/admin/agents?clane=vox' in body


@pytest.mark.asyncio
async def test_compute_lane_detail_active_lane_lists_running_files(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """The drill-down endpoint identifies the FILE being processed, not just a count (the bead's core ask)."""
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    await _seed_cloud_job(session, make_file, backend_id="vox")

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/admin/agents/compute-lanes/vox")

    assert response.status_code == 200, response.text
    body = response.text
    assert "vox-run.mp3" in body  # the seeded file's display filename, not a bare count
    assert "Running workloads" in body
    assert 'hx-get="/admin/agents/compute-lanes/vox"' in body  # own 5s tick
    assert 'hx-trigger="every 5s"' in body
    assert 'hx-target="#compute-lane-activity-vox"' in body  # phaze-rdxfu: the expanded row's per-lane slot
    assert "ephemeral · no heartbeat" in body


@pytest.mark.asyncio
async def test_compute_lane_detail_idle_lane_renders_empty_state(
    session: AsyncSession,
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """An IDLE lane (0 running) renders the friendly empty state, never a blank list (idle acceptance)."""
    backends_toml_env(_TWO_CLUSTER_REGISTRY)  # xenolab has no in-flight rows -> IDLE

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/admin/agents/compute-lanes/xenolab")

    assert response.status_code == 200, response.text
    assert "No workloads currently running." in response.text
    assert "IDLE" in response.text


@pytest.mark.asyncio
async def test_compute_lane_detail_unknown_backend_is_friendly_offline(smoke: AsyncClient) -> None:
    """An unknown/removed backend_id renders "Compute lane offline" at 200 -- never a 404/500."""
    response = await smoke.get("/admin/agents/compute-lanes/__nope__")
    assert response.status_code == 200, response.text
    assert "Compute lane offline" in response.text
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_selected_compute_lane_opens_its_expanded_row(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """?clane=<id> on /s/agents opens that lane's EXPANDED ROW (deep-link/reload), agent-row shape."""
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    await _seed_cloud_job(session, make_file, backend_id="vox")

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/s/agents", params={"clane": "vox"})

    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="compute-lane-trigger-vox"' in body
    assert 'aria-current="true"' in body
    assert 'id="compute-lane-detail-row-vox"' in body
    # The expanded row's body slot self-fetches on insertion, exactly like an agent's.
    assert 'hx-get="/admin/agents/compute-lanes/vox"' in body
    assert 'hx-trigger="load"' in body


@pytest.mark.asyncio
async def test_unknown_clane_query_param_highlights_nothing(smoke: AsyncClient) -> None:
    """An unresolvable ?clane= (T-88-01 lookup-in-known-set) highlights no lane and opens nothing."""
    response = await smoke.get("/s/agents", params={"clane": "__hostile__"})
    assert response.status_code == 200, response.text
    assert 'aria-current="true"' not in response.text


# ---------------------------------------------------------------------------
# phaze-w92dg: never auto-collapse — unresolved-selection hx-preserve carriers
# ---------------------------------------------------------------------------
#
# The expanded detail row survives the section's 5s outerHTML self-poll only via hx-preserve,
# which matches by id against the INCOMING response. A selection param that failed
# lookup-in-known-set for one tick (degraded derive_compute_lane_identities read -> [], an agent
# leaving the non-revoked set) used to omit the detail row from that response entirely, so htmx
# tore the operator's live open row down. The operator rule is that the detail only ever closes
# on Esc/✕ — so every response must keep carrying the detail-row id for as long as the request
# still asks for it, via a minimal empty carrier <tr> when the selection cannot be resolved.


@pytest.mark.asyncio
async def test_unresolved_agent_selection_still_carries_detail_row_id(smoke: AsyncClient) -> None:
    """?agent= that fails lookup still emits an hx-preserve carrier under the SAME detail-row id."""
    body = (await smoke.get("/admin/agents/_table", params={"agent": "gone-agent"})).text
    assert '<tr id="agent-detail-row-gone-agent" hx-preserve></tr>' in body
    # Unresolved stays unresolved: no ring, no real expanded row body.
    assert 'aria-current="true"' not in body
    assert 'id="agent-activity-gone-agent"' not in body


@pytest.mark.asyncio
async def test_unresolved_lane_selection_still_carries_detail_row_id(smoke: AsyncClient) -> None:
    """?clane= that fails lookup (e.g. a degraded registry tick) still carries its detail-row id."""
    body = (await smoke.get("/admin/agents/_table", params={"clane": "gone-lane"})).text
    assert '<tr id="compute-lane-detail-row-gone-lane" hx-preserve></tr>' in body
    assert 'aria-current="true"' not in body


@pytest.mark.asyncio
async def test_resolved_agent_selection_renders_real_detail_row_without_carrier(smoke: AsyncClient) -> None:
    """A resolvable ?agent= renders the REAL expanded row exactly once — never a duplicate carrier id."""
    body = (await smoke.get("/admin/agents/_table", params={"agent": "alive-agent"})).text
    assert body.count('id="agent-detail-row-alive-agent"') == 1
    # The real row, not the empty carrier: it hosts the self-fetching body slot.
    assert 'id="agent-activity-alive-agent"' in body


@pytest.mark.asyncio
async def test_empty_selection_params_emit_no_carrier(smoke: AsyncClient) -> None:
    """The idle poll's `agent=&clane=` (hx-vals null-coercion to "") must not emit phantom carriers."""
    body = (await smoke.get("/admin/agents/_table", params={"agent": "", "clane": ""})).text
    assert "agent-detail-row-" not in body
    assert "compute-lane-detail-row-" not in body


@pytest.mark.asyncio
async def test_no_rows_branch_still_carries_detail_row_id(empty_smoke: AsyncClient) -> None:
    """Even the empty-state render (no <table> at all) keeps an unresolved selection's id alive.

    One all-degraded tick (no agents loaded, lane registry read failed) must not destroy an open
    detail row — the carrier rides in the hidden fallback table the no-rows branch emits.
    """
    body = (await empty_smoke.get("/admin/agents/_table", params={"clane": "vanished-lane"})).text
    assert "No agents registered yet" in body
    assert '<tr id="compute-lane-detail-row-vanished-lane" hx-preserve></tr>' in body
