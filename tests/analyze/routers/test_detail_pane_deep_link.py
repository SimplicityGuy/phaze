"""phaze-m662: a deep-linked / reloaded detail pane must never render "open" over an empty body.

The Phase-88 shared ``_detail_pane.html`` shell is hosted on BOTH the Analyze workspace
(``?lane=``) and ``/admin/agents`` (``?agent=``). Its body is innerHTML-swapped into
``#detail-pane`` by a trigger's ``hx-get`` — so a bookmark / reload / shared deep link of
``/s/analyze?lane=X`` was a plain full server render with an EMPTY swap target, while the
shell's ``x-init`` flipped ``open = true`` off the query param alone. That suppressed the
``x-show="!open"`` resting empty state and showed the ✕ Close over a blank, never-refreshing
pane.

The fix moves ownership of ``open`` onto exactly ONE path — ``onLoaded()``, fired by a real
after-swap — and gives the deep link a body to swap: the route already resolves the ``?param``
against the known set for the selection ring, and the shell now spends that resolved id on a
one-shot ``hx-trigger="load"`` self-fetch of the wave-2 body.

Consequence for a11y (phaze-am7c interaction, asserted below): because ``x-init`` no longer
pre-sets ``open``, the deep-link swap reaches ``onLoaded()`` with ``wasOpen === false``, so the
heading focus park fires exactly ONCE — identical to the card-click open, and still guarded
against the 5s own-tick.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import re
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.routers import admin_agents


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


_DETAIL_PANE = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates" / "pipeline" / "partials" / "_detail_pane.html"

AGENT_ID = "deep-link-agent"

# The rendered #detail-pane swap-target tag, from `<div id="detail-pane"` to its closing `>`.
# No attribute value on the tag contains a `>`, so `[^>]*` bounds it cleanly.
_PANE_TAG = re.compile(r"<div\b[^>]*\bid=\"detail-pane\"[^>]*>")


def _pane_tag(body: str) -> str:
    match = _PANE_TAG.search(body)
    assert match, "expected the #detail-pane swap target in the rendered page"
    return match.group(0)


async def _first_lane_id(session: AsyncSession) -> str:
    """Return a real lane id from the degrade-safe backend snapshot."""
    from phaze.services.backends import get_backend_lane_snapshot

    lanes = await get_backend_lane_snapshot(session)
    if not lanes:
        pytest.skip("no backend lanes resolved in this environment")
    return str(lanes[0]["id"])


async def _seed_file(session: AsyncSession) -> None:
    """Insert one FileRecord so /s/analyze renders the dashboard, not the first-run empty-state guide."""
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=uuid.uuid4(),
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path="/test/music/deep-link.mp3",
            original_filename="deep-link.mp3",
            current_path="/test/music/deep-link.mp3",
            file_type="mp3",
            file_size=1024,
        ),
    )
    await session.commit()


@pytest_asyncio.fixture
async def smoke(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Smoke client over admin_agents.router alone, seeding one agent with a known id."""
    session.add(Agent(id=AGENT_ID, name="DeepLinkBox", scan_roots=["/data/music"], last_seen_at=datetime.now(UTC), kind="compute"))
    await session.commit()

    app = FastAPI(title="detail-pane-deep-link-smoke", version="test")
    app.include_router(admin_agents.router)
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# --- The regression: a deep link must fetch a body, not fake an open pane ------------


@pytest.mark.asyncio
async def test_lane_deep_link_loads_the_detail_body(client: AsyncClient, session: AsyncSession) -> None:
    """A reloaded /s/analyze?lane={known} self-fetches the lane body into #detail-pane on load.

    RED before phaze-m662: the swap target rendered bare (`<div id="detail-pane" class="mt-3"
    hx-on::after-swap=...>`), so the pane had no body and no way to get one.
    """
    await _seed_file(session)
    lane_id = await _first_lane_id(session)

    response = await client.get("/s/analyze", params={"lane": lane_id})
    assert response.status_code == 200, response.text
    tag = _pane_tag(response.text)

    assert f'hx-get="/pipeline/lanes/{lane_id}"' in tag, "the deep-linked pane must fetch the lane body it claims to be showing"
    assert 'hx-trigger="load"' in tag, "the deep-link body fetch must fire once on load"
    assert 'hx-swap="innerHTML"' in tag


@pytest.mark.asyncio
async def test_lane_deep_link_does_not_force_open_without_a_body(client: AsyncClient, session: AsyncSession) -> None:
    """x-init must record the selection WITHOUT flipping `open` — `open` is owned by onLoaded() alone.

    RED before phaze-m662: `x-init` contained `open = true`, which hid the `x-show="!open"` resting
    empty state and showed the ✕ Close over a blank swap target.
    """
    await _seed_file(session)
    lane_id = await _first_lane_id(session)

    response = await client.get("/s/analyze", params={"lane": lane_id})
    assert response.status_code == 200, response.text

    init = re.search(r'x-init="([^"]*)"', response.text)
    assert init, "expected the detail-pane x-init"
    assert "open = true" not in init.group(1), (
        "x-init flips the pane open off the ?lane param alone — on a reload nothing has swapped a "
        "body in yet, so this renders a pane that claims to be open over an empty target"
    )
    # The resting empty state must still be present in the markup to cover the pre-swap window.
    assert "No lane selected" in response.text


@pytest.mark.asyncio
async def test_lane_deep_link_body_endpoint_is_reachable(client: AsyncClient, session: AsyncSession) -> None:
    """The URL the pane self-fetches actually serves the wave-2 body (the load trigger is not a dead link)."""
    await _seed_file(session)
    lane_id = await _first_lane_id(session)

    body_response = await client.get(f"/pipeline/lanes/{lane_id}")
    assert body_response.status_code == 200, body_response.text
    assert body_response.text.strip(), "the lane-detail body fragment must not be empty"


@pytest.mark.asyncio
async def test_no_lane_param_emits_no_load_fetch(client: AsyncClient, session: AsyncSession) -> None:
    """Without ?lane the pane must NOT self-fetch — the resting empty state is the correct render."""
    await _seed_file(session)
    response = await client.get("/s/analyze")
    assert response.status_code == 200, response.text
    tag = _pane_tag(response.text)

    assert "hx-get=" not in tag, "an unselected pane must not fetch a detail body"
    assert 'hx-trigger="load"' not in tag
    assert "No lane selected" in response.text


@pytest.mark.asyncio
async def test_unknown_lane_param_emits_no_load_fetch(client: AsyncClient, session: AsyncSession) -> None:
    """An unknown ?lane is a lookup-miss (T-88-01): no fetch, no highlight, never a 500."""
    await _seed_file(session)
    response = await client.get("/s/analyze", params={"lane": "no-such-lane"})
    assert response.status_code == 200, response.text
    tag = _pane_tag(response.text)

    assert "hx-get=" not in tag, "an unresolvable ?lane must not emit a fetch for a nonexistent lane body"
    assert "No lane selected" in response.text


# --- The same shell, the same defect, on /admin/agents -------------------------------


@pytest.mark.asyncio
async def test_agent_deep_link_loads_the_activity_body(smoke: AsyncClient) -> None:
    """/admin/agents?agent={known} self-fetches the agent-activity body into #detail-pane on load."""
    response = await smoke.get("/admin/agents", params={"agent": AGENT_ID})
    assert response.status_code == 200, response.text
    tag = _pane_tag(response.text)

    assert f'hx-get="/admin/agents/{AGENT_ID}/_activity"' in tag
    assert 'hx-trigger="load"' in tag


@pytest.mark.asyncio
async def test_agent_deep_link_does_not_force_open_without_a_body(smoke: AsyncClient) -> None:
    """The agent pane variant must also leave `open` to onLoaded()."""
    response = await smoke.get("/admin/agents", params={"agent": AGENT_ID})
    assert response.status_code == 200, response.text

    init = re.search(r'x-init="([^"]*)"', response.text)
    assert init, "expected the detail-pane x-init"
    assert "open = true" not in init.group(1)
    assert "No agent selected" in response.text


@pytest.mark.asyncio
async def test_unknown_agent_param_emits_no_load_fetch(smoke: AsyncClient) -> None:
    """An unknown ?agent highlights nothing and fetches nothing."""
    response = await smoke.get("/admin/agents", params={"agent": "no-such-agent"})
    assert response.status_code == 200, response.text
    assert "hx-get=" not in _pane_tag(response.text)


# --- phaze-am7c interaction: the deep link must still move focus exactly once ---------


def test_deep_link_open_transition_still_triggers_heading_focus() -> None:
    """The deep-link path must reach onLoaded() with a FALSE pre-swap open state.

    phaze-am7c guards the heading focus on the closed->open transition (``const wasOpen =
    this.open``). If ``x-init`` pre-set ``open = true``, the deep-link swap would arrive with
    ``wasOpen`` already true and the focus park would be SKIPPED — a deep-linked pane opening
    without ever moving focus, silently diverging from the click path. Asserting on the template
    source keeps that coupling explicit: nothing outside ``onLoaded()`` may set ``open`` true.
    """
    html = _DETAIL_PANE.read_text()
    html = re.sub(r"\{#.*?#\}", "", html, flags=re.DOTALL)  # strip Jinja comments (they discuss `open`)

    init = re.search(r'x-init="([^"]*)"', html)
    assert init, "expected the detail-pane x-init"
    assert not re.search(r"\bopen\s*=\s*true", init.group(1)), (
        "x-init must not pre-open the pane: it would make onLoaded()'s pre-swap open state true on "
        "the deep-link swap, skipping the am7c heading focus that the card-click path performs"
    )

    component = re.search(r'x-data="([^"]*)"', html, re.DOTALL)
    assert component, "expected the shell <section x-data> component"
    # `open = true` may appear exactly once in the whole component — inside onLoaded().
    assert len(re.findall(r"\bopen\s*=\s*true", component.group(1))) == 1, "`open` must be flipped true in exactly one place (onLoaded)"
    assert "open = true" in component.group(1)[component.group(1).find("onLoaded()") :], "the single `open = true` must live in onLoaded()"
