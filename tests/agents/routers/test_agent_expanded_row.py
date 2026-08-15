"""phaze-2u8v.6: the Agents-table detail is an EXPANDED ROW, not a side panel.

Two presentation defects this bead fixes, one per surface:
  * the compute-lane cards' shared #detail-pane used to be a right-side GRID COLUMN
    (analyze_workspace.html), so opening it grew that row's height and pushed everything below
    it down the page (covered by test_lane_pane_layout.py, the lane half of this bead).
  * the agents TABLE's detail used the SAME shared side panel even though the surface is tabular
    data -- this file locks the replacement: an EXPANDED ROW (admin/partials/_agent_detail_row.html)
    rendered as a `<tr>` immediately beneath the selected agent's trigger row, full table width
    (colspan, never a narrow column that clips a column like the old panel clipped SKIPPED), with
    its own dismiss (✕ / Esc) and `hx-preserve` so the table's own 5s self-poll cannot destroy and
    recreate it out from under an operator who has it open.

Uses the self-contained smoke-app fixture pattern shared with test_admin_agents.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import re
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


AGENT_ID = "expand-row-agent"
OTHER_AGENT_ID = "expand-row-other"

_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_AGENTS_TABLE = _TEMPLATES / "admin" / "agents.html"
_DETAIL_ROW = _TEMPLATES / "admin" / "partials" / "_agent_detail_row.html"


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a smoke FastAPI app mounting only admin_agents.router (mirrors test_admin_agents)."""
    app = FastAPI(title="agent-expanded-row-smoke", version="test")
    app.include_router(admin_agents.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest_asyncio.fixture
async def smoke(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Smoke client seeding two live agents so selection can be proven exclusive."""
    now = datetime.now(UTC)
    session.add_all(
        [
            Agent(id=AGENT_ID, name="ExpandRowBox", scan_roots=["/data/music"], last_seen_at=now, kind="compute"),
            Agent(id=OTHER_AGENT_ID, name="OtherBox", scan_roots=[], last_seen_at=now, kind="fileserver"),
        ],
    )
    await session.commit()

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_selected_agent_renders_an_expanded_row(smoke: AsyncClient) -> None:
    """?agent={known} renders a <tr id="agent-detail-row-{id}"> immediately as a sibling row."""
    response = await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID})
    assert response.status_code == 200, response.text
    body = response.text

    assert f'id="agent-detail-row-{AGENT_ID}"' in body
    # It is a genuine <tr>, not a floating panel -- it belongs to the SAME table as the trigger row.
    trigger_at = body.find(f'id="agent-trigger-{AGENT_ID}"')
    detail_at = body.find(f'id="agent-detail-row-{AGENT_ID}"')
    assert trigger_at != -1 and detail_at != -1
    assert trigger_at < detail_at, "the expanded row must follow its trigger row, not precede it"


@pytest.mark.asyncio
async def test_expanded_row_spans_the_full_table_width(smoke: AsyncClient) -> None:
    """The expanded row's <td> uses colspan to span every column -- never a narrow clipped column.

    The old side panel's narrow 1/3-width column is exactly what clipped the SKIPPED column in the
    6-stage matrix; the expanded row must not reintroduce a narrow container.
    """
    body = (await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID})).text
    start = body.find(f'id="agent-detail-row-{AGENT_ID}"')
    assert start != -1
    row = body[start : body.find("</tr>", start)]
    match = re.search(r'colspan="(\d+)"', row)
    assert match, "expected the expanded row's <td> to carry a colspan"
    # Agent / Kind / Status / Queue / Last seen / Scan roots / Actions -- 7 columns.
    assert int(match.group(1)) == 7


@pytest.mark.asyncio
async def test_expanded_row_carries_hx_preserve(smoke: AsyncClient) -> None:
    """hx-preserve keeps the row (and its in-flight own-tick/content) alive across the table's own poll.

    Without it, the SAME #agents-table-section 5s self-poll that already re-renders every agent row
    would also destroy and recreate this one -- tearing out its fetched content, own-tick, and focus --
    every 5 seconds regardless of whether the operator ever touched it.
    """
    body = (await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID})).text
    start = body.find(f'id="agent-detail-row-{AGENT_ID}"')
    assert start != -1
    row = body[start : body.find("</tr>", start)]
    assert "hx-preserve" in row


@pytest.mark.asyncio
async def test_expanded_row_has_a_close_control(smoke: AsyncClient) -> None:
    """The expanded row carries a visible ✕ Close control, consistent with phaze-nawk's dismiss contract."""
    body = (await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID})).text
    start = body.find(f'id="agent-detail-row-{AGENT_ID}"')
    assert start != -1
    row = body[start : body.find("</tr>", start) + len("</tr>")]
    assert 'aria-label="Close detail"' in row
    assert "hide()" in row
    # Esc dismisses too (matches the lane pane's keydown.escape contract).
    assert "@keydown.escape.window" in row


@pytest.mark.asyncio
async def test_dismiss_clears_the_fetched_content_and_the_url(smoke: AsyncClient) -> None:
    """hide() must wipe the swapped activity body AND clear the ?agent= URL param (phaze-nawk CONSOLE-03).

    Flipping `open` alone (hiding via x-show) without also clearing the fetched innerHTML and the own-tick
    loop it carries would leave content live behind the scenes -- the same "✕-only-hides-the-icon" trap
    phaze-nawk closed for the shared shell. Source-level guard: assert the wiring exists in the template.
    """
    html = _DETAIL_ROW.read_text()
    html = re.sub(r"\{#.*?#\}", "", html, flags=re.DOTALL)
    m = re.search(r'x-data="([^"]*)"', html, re.DOTALL)
    assert m, "expected the expanded row's x-data component"
    component = m.group(1)
    assert re.search(r"getElementById\('agent-activity-", component), "hide() must reach the activity swap target to clear it"
    assert re.search(r"innerHTML\s*=\s*''", component), "hide() must wipe the swap target's innerHTML, not just hide the row"
    assert "history.replaceState" in component, "hide() must clear the ?agent= URL param so the next poll stops rendering this row"


@pytest.mark.asyncio
async def test_only_one_agent_expands_at_a_time(smoke: AsyncClient) -> None:
    """Selecting a different agent renders ONLY that agent's expanded row."""
    body = (await smoke.get("/admin/agents/_table", params={"agent": OTHER_AGENT_ID})).text
    assert f'id="agent-detail-row-{OTHER_AGENT_ID}"' in body
    assert f'id="agent-detail-row-{AGENT_ID}"' not in body


@pytest.mark.asyncio
async def test_no_selection_renders_no_expanded_row(smoke: AsyncClient) -> None:
    """With no ?agent= at all, neither agent's expanded row renders."""
    body = (await smoke.get("/admin/agents/_table")).text
    assert "agent-detail-row-" not in body


@pytest.mark.asyncio
async def test_agents_page_no_longer_hosts_the_shared_side_panel() -> None:
    """admin/agents.html must not INCLUDE the shared _detail_pane.html side-panel shell any more.

    Comment prose is allowed to still name the old shell for context (see the Jinja comment at the
    top of the file); what must be gone is the actual `{% include %}`/`{% with pane_kind = ... %}`
    wiring that rendered it.
    """
    html = re.sub(r"\{#.*?#\}", "", _AGENTS_TABLE.read_text(), flags=re.DOTALL)
    assert "_detail_pane.html" not in html
    assert "pane_kind" not in html


_COMPUTE_LANE_PANE = _TEMPLATES / "admin" / "partials" / "_compute_lane_pane.html"


@pytest.mark.asyncio
async def test_compute_lane_pane_partial_deleted_and_not_wired_back() -> None:
    """phaze-rdxfu (acceptance rule 6): the dedicated side-pane shell is GONE, not just unincluded.

    Pins the deletion itself (not just the include site) so a future edit cannot resurrect the file
    and quietly wire it back into agents.html/agents_table.html -- both the file's absence and the
    absence of any reference to it or its swap target are asserted.
    """
    assert not _COMPUTE_LANE_PANE.exists(), "_compute_lane_pane.html must be deleted (acceptance rule 6)"

    for template in (_AGENTS_TABLE, _TEMPLATES / "admin" / "partials" / "agents_table.html"):
        html = re.sub(r"\{#.*?#\}", "", template.read_text(), flags=re.DOTALL)
        assert "_compute_lane_pane.html" not in html, f"{template} still references the deleted pane partial"
        assert "#compute-lane-pane" not in html, f"{template} still references the deleted pane's swap target"

    # And the retired card-grid partial the pane used to be included alongside is gone too.
    assert not (_TEMPLATES / "admin" / "partials" / "compute_lanes.html").exists()


_COMPUTE_LANE_DETAIL_ROW = _TEMPLATES / "admin" / "partials" / "_compute_lane_detail_row.html"


def _dismiss_component(source: Path) -> str:
    """Return the `hide()`-carrying Alpine `x-data` blob from a detail-row partial, comments stripped."""
    html = re.sub(r"\{#.*?#\}", "", source.read_text(), flags=re.DOTALL)
    m = re.search(r'x-data="([^"]*)"', html, re.DOTALL)
    assert m, f"expected {source} to carry an x-data component"
    return m.group(1)


@pytest.mark.asyncio
async def test_dismiss_url_rewrite_keeps_sort_and_order(smoke: AsyncClient) -> None:
    """phaze-trof4: hide()'s history.replaceState must keep ?sort=/?order=, not just clear ?agent=.

    A bare '/s/agents' discards the operator's chosen sort, so a reload/bookmark restore after
    dismissing the detail row silently snaps the table back to the default last_seen-desc order --
    the same invisible reset the poll-carries-the-sort fix (phaze-a6hm.4) and the drill-in push-url
    fix (test_drill_in_push_url_keeps_the_sort) both close for their own paths. The dismiss path is a
    third door to the identical bug and needs the identical fix.
    """
    body = (await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID, "sort": "queue", "order": "desc"})).text
    start = body.find(f'id="agent-detail-row-{AGENT_ID}"')
    assert start != -1
    row = body[start : body.find("</tr>", start)]
    assert "history.replaceState({}, '', '/s/agents?sort=queue&amp;order=desc')" in row
    # And the bare, sort-dropping form the bug produced must be gone.
    assert "history.replaceState({}, '', '/s/agents')" not in row


@pytest.mark.asyncio
async def test_dismiss_url_rewrite_keeps_the_default_sort_too(smoke: AsyncClient) -> None:
    """Same contract with no explicit ?sort=/?order= on the request -- the resolved DEFAULT still rides."""
    body = (await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID})).text
    start = body.find(f'id="agent-detail-row-{AGENT_ID}"')
    assert start != -1
    row = body[start : body.find("</tr>", start)]
    assert "history.replaceState({}, '', '/s/agents?sort=last_seen&amp;order=desc')" in row


@pytest.mark.asyncio
async def test_dismiss_removes_the_row_node_instead_of_only_hiding_it(smoke: AsyncClient) -> None:
    """phaze-fg8vq: hide() must remove this row from the DOM, not just flip x-show off.

    A stale x-show-hidden node left in place under the SAME id wins htmx's hx-preserve id match
    against a fresh, freshly-open row rendered by re-clicking the same agent inside the ~5s poll
    window -- the reopen click's response is discarded in favour of the dead node, wedging the
    expanded row open=false/empty until a different agent is picked or the page is hard-reloaded.
    Removing the node outright means there is nothing left with this id for a later swap to collide
    with, so a reopen inside the window always wins.
    """
    body = (await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID})).text
    start = body.find(f'id="agent-detail-row-{AGENT_ID}"')
    assert start != -1
    row = body[start : body.find("</tr>", start) + len("</tr>")]
    assert "this.$el.remove()" in row, "hide() must remove the row node, not only set open = false"


@pytest.mark.asyncio
async def test_dismiss_focuses_the_trigger_before_removing_the_row(smoke: AsyncClient) -> None:
    """Focus must move to the trigger row BEFORE the node is removed -- removing first drops focus to <body>."""
    body = (await smoke.get("/admin/agents/_table", params={"agent": AGENT_ID})).text
    start = body.find(f'id="agent-detail-row-{AGENT_ID}"')
    assert start != -1
    row = body[start : body.find("</tr>", start) + len("</tr>")]
    focus_at = row.find(f"getElementById('agent-trigger-{AGENT_ID}')?.focus()")
    remove_at = row.find("this.$el.remove()")
    assert focus_at != -1, "hide() must still return focus to the trigger row by stable id"
    assert remove_at != -1
    assert focus_at < remove_at, "focus must be moved before the row removes itself"


@pytest.mark.asyncio
async def test_compute_lane_dismiss_url_rewrite_keeps_sort_and_order(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
) -> None:
    """phaze-trof4's sibling site: _compute_lane_detail_row.html's hide() had the identical bare-path bug."""
    from tests.agents.routers.test_admin_agents import _TWO_CLUSTER_REGISTRY, _seed_cloud_job

    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    await _seed_cloud_job(session, make_file, backend_id="vox")

    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        body = (await ac.get("/admin/agents/_table", params={"clane": "vox", "sort": "name", "order": "asc"})).text

    start = body.find('id="compute-lane-detail-row-vox"')
    assert start != -1
    row = body[start : body.find("</tr>", start) + len("</tr>")]
    assert "history.replaceState({}, '', '/s/agents?sort=name&amp;order=asc')" in row
    assert "history.replaceState({}, '', '/s/agents')" not in row
    assert "this.$el.remove()" in row, "hide() must remove the row node, not only set open = false"


@pytest.mark.asyncio
async def test_agent_detail_row_hide_component_source_contract() -> None:
    """Source-level pin: both fixes hold in _agent_detail_row.html even outside a rendered request."""
    component = _dismiss_component(_DETAIL_ROW)
    assert re.search(r"history\.replaceState\(\{\}, '', '/s/agents\?\{\{\s*sort\.query_params\(\)\s*\}\}'\)", component)
    assert "this.$el.remove()" in component


@pytest.mark.asyncio
async def test_compute_lane_detail_row_hide_component_source_contract() -> None:
    """Source-level pin: both fixes hold in _compute_lane_detail_row.html too (mirrors the agent row)."""
    component = _dismiss_component(_COMPUTE_LANE_DETAIL_ROW)
    assert re.search(r"history\.replaceState\(\{\}, '', '/s/agents\?\{\{\s*sort\.query_params\(\)\s*\}\}'\)", component)
    assert "this.$el.remove()" in component
