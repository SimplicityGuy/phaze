"""``cloud_route_threshold_sec`` is read from the LIVE runtime-config snapshot, not a static
settings value (``phaze-mvq8z.8``).

The real consumer per CLAUDE.md rule 3 is ``POST /api/v1/analyze`` (``routers/pipeline/analysis.py``),
not a bare ``current().cloud_route_threshold_sec`` read -- proving the SNAPSHOT changed says
nothing about whether the call site actually consults it. This test drives the threshold up via
``runtime.toml`` + ``store.reload(...)`` (no env/settings change visible to the caller, no process
restart) and shows a file that used to be HELD in AWAITING_CLOUD now routes LOCAL, and vice versa
moving it back down.

Both ``get_settings`` and ``get_runtime_config_store`` are process-wide ``lru_cache(maxsize=1)``
singletons (``tests/conftest.py`` clears both once per test), and the autouse
``_cloud_compute_registry`` fixture (``_shared.py``) calls ``get_settings()`` -- caching an
instance built BEFORE this test's own ``PHAZE_RUNTIME_CONFIG_DIR`` override -- as part of every
test in this package, before the test body runs at all. Clearing both caches again and
re-applying the compute-backend registry is what lets ``get_runtime_config_store()`` actually
pick up ``tmp_path`` as ``runtime_config_dir`` for this test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.config import get_settings
from phaze.runtime_config import RUNTIME_TOML_NAME, get_runtime_config_store
from tests.shared.routers.pipeline._shared import (
    _COMPUTE_BACKEND,
    _LONG,
    _cloud_compute_registry,  # noqa: F401 -- autouse fixture, never referenced by name
    _is_awaiting_cloud,
    _persist_files_with_duration,
    drain_router_background_tasks,
    make_agent_live,
    seed_active_agent,
    wire_fakes,
)


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.runtime_config import RuntimeConfigStore


def _rebuild_store_watching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeConfigStore:
    """Point a FRESH ``get_runtime_config_store()`` at ``tmp_path``, cloud backend re-applied.

    See the module docstring: the autouse ``_cloud_compute_registry`` fixture already cached a
    ``get_settings()`` built without this override, so both singletons must be cleared and
    rebuilt here, in the test body, after that fixture has already run.
    """
    monkeypatch.setenv("PHAZE_RUNTIME_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_runtime_config_store.cache_clear()
    monkeypatch.setattr(get_settings(), "backends", [_COMPUTE_BACKEND])
    return get_runtime_config_store()


@pytest.mark.asyncio
async def test_raising_the_threshold_at_runtime_reroutes_a_previously_long_file_local(
    client: AsyncClient, session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_LONG`` (6000s) starts >= the 5400s default -- held. Reload a HIGHER threshold live -> local."""
    store = _rebuild_store_watching(tmp_path, monkeypatch)
    assert store.current().cloud_route_threshold_sec == 5400  # the unmodified default

    (long_file,) = await _persist_files_with_duration(session, [_LONG])
    await make_agent_live(session)  # phaze-c9w9: the OWNING agent must be live for local routing
    capture = wire_fakes(client)

    # Raise the threshold live -- above 6000s -- with no env change and no restart.
    (tmp_path / RUNTIME_TOML_NAME).write_text("cloud_route_threshold_sec = 7000\n", encoding="utf-8")
    result = await store.reload("file")
    assert result.outcome == "applied"
    assert store.current().cloud_route_threshold_sec == 7000

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    # No longer "long" against the new threshold: routes LOCAL, not held.
    assert data["local"] == 1
    assert data["awaiting_cloud"] == 0

    await drain_router_background_tasks()
    assert [kwargs["file_id"] for _q, _t, kwargs in capture] == [str(long_file.id)]


@pytest.mark.asyncio
async def test_lowering_the_threshold_back_at_runtime_holds_the_same_duration_again(
    client: AsyncClient, session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse: reload the threshold back down below ``_LONG`` -- the file holds again.

    Also exercises the SIGHUP trigger source specifically (the raising test above used "file"),
    covering the bead acceptance criterion via both real triggers.
    """
    store = _rebuild_store_watching(tmp_path, monkeypatch)
    (tmp_path / RUNTIME_TOML_NAME).write_text("cloud_route_threshold_sec = 7000\n", encoding="utf-8")
    assert (await store.reload("file")).outcome == "applied"

    (long_file,) = await _persist_files_with_duration(session, [_LONG])
    await seed_active_agent(session, "cloud", kind="compute")
    await seed_active_agent(session, "nox", kind="fileserver")
    wire_fakes(client)

    (tmp_path / RUNTIME_TOML_NAME).write_text("cloud_route_threshold_sec = 5400\n", encoding="utf-8")
    result = await store.reload("sighup")
    assert result.outcome == "applied"
    assert store.current().cloud_route_threshold_sec == 5400

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["local"] == 0
    assert data["cloud"] == 0
    assert data["awaiting_cloud"] == 1

    await drain_router_background_tasks()
    assert await _is_awaiting_cloud(session, long_file.id)
