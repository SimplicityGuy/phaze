"""Tests for the Phase-71 force-local routing override (BEUI-02).

Two behaviors are proven here:

* ``get_route_control`` (Task 2) -- the degrade-safe reader: True iff the seeded ``'global'`` row has
  ``force_local`` True; absent row -> False; any DB exception -> SAVEPOINT rollback -> False, never
  raises (the reader is on the hot 5s poll + the routing gate, so a raise would 500 them -- T-71-03).
* the duration router (Task 3) -- with force-local engaged a new long file routes LOCAL and is NOT held
  in ``AWAITING_CLOUD``, behaving exactly like the all-local (``cloud_enabled=False``) path (D-08).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze.config import get_settings
from phaze.config_backends import ComputeBackend
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.route_control import RouteControl
from phaze.services.route_control import get_route_control
from tests._queue_fakes import make_agent_live, wire_fakes


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# A single compute backend -> cloud_enabled True + active_cloud_kind 'compute'; force-local then
# overrides it to the all-local path. Mirrors test_pipeline's _COMPUTE_BACKEND registry fixture.
_COMPUTE_BACKEND = ComputeBackend(kind="compute", id="a1", rank=10, cap=2, agent_ref="cloud-1", scratch_dir="/scratch", push_host="a1.push")

_LONG = 6000.0  # >= cloud_route_threshold_sec default (5400)


async def _seed_route_control(session: AsyncSession, *, force_local: bool) -> None:
    """Seed (or update) the single ``'global'`` route_control row to ``force_local``."""
    row = await session.get(RouteControl, "global")
    if row is None:
        session.add(RouteControl(id="global", force_local=force_local))
    else:
        row.force_local = force_local
    await session.commit()


@pytest.mark.asyncio
async def test_route_control_degrades_on_absent_row(session: AsyncSession) -> None:
    """No ``'global'`` row (pre-migration / empty table) -> False (cloud-enabled), never raises."""
    assert await get_route_control(session) is False


@pytest.mark.asyncio
async def test_route_control_reads_seeded_false(session: AsyncSession) -> None:
    """A seeded ``force_local=false`` row reads as False (cloud-enabled)."""
    await _seed_route_control(session, force_local=False)
    assert await get_route_control(session) is False


@pytest.mark.asyncio
async def test_route_control_reads_forced_true(session: AsyncSession) -> None:
    """A ``force_local=true`` row reads as True (force-local engaged)."""
    await _seed_route_control(session, force_local=True)
    assert await get_route_control(session) is True


class _NullSavepoint:
    """Async-context-manager stand-in for ``session.begin_nested()`` in the fake-session tests.

    ``__aexit__`` returns ``False`` so an exception raised inside the ``async with`` block (the
    ``session.get`` read) propagates out to ``get_route_control``'s degrade ``except`` -- exactly as
    a real SAVEPOINT does after ``ROLLBACK TO SAVEPOINT``.
    """

    async def __aenter__(self) -> _NullSavepoint:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_route_control_degrades_on_db_error() -> None:
    """Any DB exception degrades to False -- the reader NEVER raises (T-71-03).

    The read runs inside a SAVEPOINT (``begin_nested``); the exception propagates out of the nested
    scope and is caught by the degrade ``except`` (CR-01 -- the caller's shared session is never
    touched with a full ``session.rollback()``).
    """

    class _BoomSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def get(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("boom")

    assert await get_route_control(_BoomSession()) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_route_control_degrades_when_begin_nested_itself_raises() -> None:
    """Even if the session is so broken ``begin_nested()`` itself raises, the reader still degrades to
    False rather than propagating (T-71-03)."""

    class _DoubleBoomSession:
        def begin_nested(self) -> object:
            raise RuntimeError("begin_nested boom")

    assert await get_route_control(_DoubleBoomSession()) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_route_control_degrade_preserves_caller_loaded_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01: the degrade must NOT expire ORM rows the caller already loaded on this same session.

    ``routers.pipeline`` loads ``files_with_duration`` (ORM ``FileRecord`` rows) BEFORE calling
    ``get_route_control(session)`` on the SAME session. A plain ``session.rollback()`` in the degrade
    branch would expire those already-loaded rows, 500-ing the render/enqueue path on the next lazy
    load (MissingGreenlet from a sync context).

    Distinguishing signal (fixture never commits, so ``inspect().expired`` cannot tell a SAVEPOINT
    rollback apart from a plain one -- a plain rollback expunges the pending flush to *transient*,
    not *expired*): flush a FileRecord row, force ONLY the route_control read to fail, then assert
    ``session.get`` still finds the file afterwards -- proving the outer transaction survived.
    """
    from unittest.mock import AsyncMock

    from phaze.models.agent import Agent

    session.add(Agent(id="cr01-route-control-agent", name="Cr01RouteBox", scan_roots=[], kind="fileserver"))
    file_id = uuid.uuid4()
    rec = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex,
        original_path=f"/media/{file_id}.mp3",
        original_filename=f"{file_id}.mp3",
        current_path=f"/media/{file_id}.mp3",
        file_type="mp3",
        file_size=1234,
        agent_id="cr01-route-control-agent",
    )
    session.add(rec)
    await session.flush()

    real_get = session.get
    monkeypatch.setattr(session, "get", AsyncMock(side_effect=RuntimeError("boom")))
    result = await get_route_control(session)
    monkeypatch.setattr(session, "get", real_get)  # restore for the assertion query

    assert result is False
    assert await session.get(FileRecord, file_id) is not None


@pytest.mark.asyncio
async def test_route_forced_local_no_hold(client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force-local engaged: a new long file routes LOCAL (enqueued), NOT held in AWAITING_CLOUD (D-08).

    With a compute backend in the registry (cloud_enabled True) a long file would normally be HELD in
    AWAITING_CLOUD. Engaging force-local makes the effective flag ``cloud_enabled AND NOT force_local``
    False, so the duration router treats nothing as "long" -- the file routes to the fileserver queue
    exactly like the all-local path, and is never parked in AWAITING_CLOUD.
    """
    monkeypatch.setattr(get_settings(), "backends", [_COMPUTE_BACKEND])
    await _seed_route_control(session, force_local=True)

    uid = uuid.uuid4()
    long_file = FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )
    session.add(long_file)
    await session.flush()
    session.add(FileMetadata(file_id=uid, duration=_LONG))
    await session.commit()

    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
    wire_fakes(client)

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    # Routed LOCAL, nothing held for the cloud path.
    assert data["local"] == 1
    assert data["awaiting_cloud"] == 0

    await session.refresh(long_file)


@pytest.mark.asyncio
async def test_force_local_toggle_roundtrip(client: AsyncClient, session: AsyncSession) -> None:
    """POST engage=true flips the persisted row and the returned pill shows FORCED LOCAL; false reverts (D-08/D-10).

    The thin endpoint (mirroring the pause/resume thin-endpoint pattern) writes the durable
    ``route_control`` 'global' row and returns the ``_force_local_pill.html`` partial reflecting the
    JUST-COMMITTED state (authoritative, never optimistic -- T-71-10) plus the OOB confirmation toast.
    """
    # Engage -> row true; pill FORCED LOCAL (aria-checked=true) + engage toast.
    engaged = await client.post("/pipeline/routing/force-local", data={"engage": "true"})
    assert engaged.status_code == 200
    assert "FORCED" in engaged.text
    assert 'aria-checked="true"' in engaged.text
    assert "forced to LOCAL" in engaged.text  # OOB engage toast copy
    assert 'id="routing-override-warning"' in engaged.text
    assert 'hx-swap-oob="true"' in engaged.text
    assert 'id="routing-operations-warning" hx-swap-oob="true"' in engaged.text
    assert "Override active: all analysis is routed locally" in engaged.text
    assert 'aria-label="Routing override active: forced local"' in engaged.text
    assert await get_route_control(session) is True

    # Revert -> row false; pill CLOUD ROUTING (aria-checked=false) + revert toast.
    reverted = await client.post("/pipeline/routing/force-local", data={"engage": "false"})
    assert reverted.status_code == 200
    assert "CLOUD" in reverted.text
    assert 'aria-checked="false"' in reverted.text
    assert "Cloud routing restored" in reverted.text  # OOB revert toast copy
    assert 'id="routing-override-warning"' in reverted.text
    assert 'hx-swap-oob="true"' in reverted.text
    assert 'id="routing-operations-warning" hx-swap-oob="true"' in reverted.text
    assert "Override active: all analysis is routed locally" not in reverted.text
    assert 'aria-label="Routing override active: forced local"' not in reverted.text
    assert await get_route_control(session) is False


@pytest.mark.asyncio
async def test_force_local_control_lives_in_config_and_header_only_warns(client: AsyncClient, session: AsyncSession) -> None:
    """Config owns the mutation while the persistent header only warns when it is active (phaze-6hd58).

    The seed reads ``get_route_control`` in ``shell.py`` ``_render_stage`` (base shell context),
    NOT the Analyze-only dashboard context -- so the global control shows correct state everywhere.
    The whole control is exercised end to end from the Config page: the page renders the pill with the
    engage confirmation, the POST flips the persisted row, and both the pill and the warning update.
    """
    # Normal routing: the Config page carries the control, with the engage confirmation and the restore copy.
    config0 = await client.get("/s/runtime-config")
    assert config0.status_code == 200
    assert 'id="force-local-pill"' in config0.text
    assert 'aria-checked="false"' in config0.text
    assert 'hx-confirm="Force all analysis routing to local?' in config0.text
    assert "Restoring normal routing is immediate" in config0.text
    assert 'id="routing-operations-warning"' in config0.text
    assert "Override active: all analysis is routed locally" not in config0.text

    # Engage from the Config page's own control (the pill's hx-post target), then the page reflects it.
    assert 'hx-post="/pipeline/routing/force-local"' in config0.text
    engaged = await client.post("/pipeline/routing/force-local", data={"engage": "true"})
    assert engaged.status_code == 200
    assert 'aria-checked="true"' in engaged.text
    assert await get_route_control(session) is True

    # Engaged: a non-Config page's header shows the read-only warning, which links to Config and cannot mutate.
    page = await client.get("/s/discover")
    assert page.status_code == 200
    assert 'aria-label="Routing override active: forced local"' in page.text
    header = page.text.split("</header>", maxsplit=1)[0]
    assert 'hx-post="/pipeline/routing/force-local"' not in header
    assert 'href="/s/runtime-config"' in header
    assert "FORCED" in page.text

    config = await client.get("/s/runtime-config")
    assert config.status_code == 200
    assert 'id="force-local-pill"' in config.text
    assert 'aria-checked="true"' in config.text
    assert "Override active: all analysis is routed locally" in config.text
    assert "hx-confirm=" not in config.text  # restoring is immediate

    # Revert, then reload: normal routing is quiet in the header and remains controllable in Config.
    await client.post("/pipeline/routing/force-local", data={"engage": "false"})
    assert await get_route_control(session) is False
    page2 = await client.get("/s/discover")
    assert 'aria-label="Routing override active: forced local"' not in page2.text
    config2 = await client.get("/s/runtime-config")
    assert 'aria-checked="false"' in config2.text
    assert "CLOUD" in config2.text
    assert 'hx-confirm="Force all analysis routing to local?' in config2.text


@pytest.mark.asyncio
async def test_operations_url_redirects_to_config_and_routing_is_gone_from_the_rail(client: AsyncClient) -> None:
    """The removed Routing page 307-redirects to Config; the rail has no Routing or Config entry (phaze-6hd58)."""
    redirect = await client.get("/s/operations", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/s/runtime-config"

    followed = await client.get("/s/operations", follow_redirects=True)
    assert followed.status_code == 200
    assert 'id="force-local-pill"' in followed.text

    page = await client.get("/s/runtime-config")
    rail = page.text.split("<aside", maxsplit=1)[1].split("</aside>", maxsplit=1)[0]
    assert 'data-rail-stage="operations"' not in rail
    assert 'data-rail-stage="runtime-config"' not in rail
    assert "Routing" not in rail
    assert "Runtime config" not in page.text


@pytest.mark.asyncio
async def test_config_gear_in_header_is_named_and_marks_the_current_page(client: AsyncClient) -> None:
    """The header gear is a named link to Config with aria-current only on the Config page (phaze-6hd58)."""
    from bs4 import BeautifulSoup

    on_config = BeautifulSoup((await client.get("/s/runtime-config")).text, "html.parser")
    gear = on_config.select_one("header #config-trigger")
    assert gear is not None
    assert gear["href"] == "/s/runtime-config"
    assert gear["aria-label"] == "Config"
    assert gear["aria-current"] == "page"
    assert "focus-visible:ring-2" in gear["class"]
    assert on_config.title is not None
    assert on_config.title.get_text() == "Config | Phaze"

    elsewhere = BeautifulSoup((await client.get("/s/discover")).text, "html.parser")
    other = elsewhere.select_one("header #config-trigger")
    assert other is not None
    assert not other.has_attr("aria-current")


def test_no_template_links_to_the_removed_routing_page_or_the_old_config_name() -> None:
    """No template or router source still points at /s/operations or names the page "Runtime config" (phaze-6hd58)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "src" / "phaze"
    offenders = []
    for path in [*root.joinpath("templates").rglob("*.html"), *root.joinpath("routers").rglob("*.py")]:
        text = path.read_text(encoding="utf-8")
        for needle in ('"/s/operations', "'/s/operations", 'href="/s/operations', 'Runtime config"', 'RUNTIME CONFIG"'):
            if needle in text and path.name != "__init__.py":
                offenders.append(f"{path.name}: {needle}")
    assert not offenders
