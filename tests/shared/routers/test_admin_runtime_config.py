"""Tests for the DB-override admin API (phaze-mvq8z.6): GET/POST/DELETE /admin/runtime-config/*.

The process-wide ``get_runtime_config_store()`` singleton is what the router reads/writes through
(mirroring the real api process), so ``_wire_runtime_config_store`` rebuilds it fresh per test and
wires its ``override_provider`` straight to THIS test's own ``session`` fixture (not a new one) --
the same session ``client``'s ``get_session`` override hands the router -- so a POST's write and a
following GET's read agree, and the singleton is torn down after every test so no override leaks
into an unrelated test module (this repo's own "never share a writable path between concurrent
seats" principle, generalized to an in-process cache shared by the whole pytest run).

Real Postgres NOTIFY delivery is NOT exercised here -- this suite's ``session`` fixture wraps every
test in a rolled-back SAVEPOINT (tests/conftest.py), so a "commit" here never reaches a real
top-level COMMIT. That is covered by ``tests/shared/config/test_runtime_config_notify.py``. What
IS exercised here is the endpoint's own in-process ``store.reload("api")`` call, which makes a
successful write visible to the very next request regardless of NOTIFY.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from phaze.models.runtime_config_override import RuntimeConfigOverride
from phaze.runtime_config import get_runtime_config_store
from phaze.services.runtime_config_overrides import get_runtime_config_overrides


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _wire_runtime_config_store(session: AsyncSession):  # type: ignore[no-untyped-def]
    """Fresh global store per test, its override provider bound to THIS test's own session."""
    get_runtime_config_store.cache_clear()
    store = get_runtime_config_store()
    store.set_override_provider(lambda: get_runtime_config_overrides(session))
    yield store
    get_runtime_config_store.cache_clear()


@pytest.mark.asyncio
async def test_table_lists_reloadable_keys_with_default_source(client: AsyncClient) -> None:
    response = await client.get("/admin/runtime-config/_table")

    assert response.status_code == 200
    assert "worker_max_jobs" in response.text
    assert "default" in response.text
    # A restart-only key is listed read-only, labeled, and gets no form.
    assert "database_url" in response.text
    assert "requires restart" in response.text


@pytest.mark.asyncio
async def test_post_valid_override_persists_and_is_reflected_as_the_override_layer(client: AsyncClient, session: AsyncSession) -> None:
    with capture_logs() as logs:
        response = await client.post("/admin/runtime-config/worker_max_jobs", data={"value": "12"})

    assert response.status_code == 200
    assert "12" in response.text
    row = await session.get(RuntimeConfigOverride, "worker_max_jobs")
    assert row is not None
    assert row.value == 12
    assert get_runtime_config_store().current().worker_max_jobs == 12
    assert get_runtime_config_store().snapshot().sources["worker_max_jobs"] == "override"
    reload_lines = [entry for entry in logs if entry.get("event") == "phaze.runtime_config reload"]
    assert reload_lines, "a successful set must emit the core's audit log line"
    assert reload_lines[-1]["outcome"] == "applied"
    assert reload_lines[-1]["changes"]["worker_max_jobs"] == {"old": 8, "new": 12}


@pytest.mark.asyncio
async def test_post_overwrites_an_existing_override(client: AsyncClient, session: AsyncSession) -> None:
    await client.post("/admin/runtime-config/worker_max_jobs", data={"value": "5"})

    response = await client.post("/admin/runtime-config/worker_max_jobs", data={"value": "9"})

    assert response.status_code == 200
    rows = (await session.execute(select(RuntimeConfigOverride))).scalars().all()
    assert len(rows) == 1
    assert rows[0].value == 9


@pytest.mark.asyncio
async def test_post_log_level_select_value(client: AsyncClient, session: AsyncSession) -> None:
    response = await client.post("/admin/runtime-config/log_level", data={"value": "DEBUG"})

    assert response.status_code == 200
    row = await session.get(RuntimeConfigOverride, "log_level")
    assert row is not None
    assert row.value == "DEBUG"


@pytest.mark.asyncio
async def test_post_non_integer_value_for_an_int_key_is_rejected_with_nothing_stored(client: AsyncClient, session: AsyncSession) -> None:
    with capture_logs() as logs:
        response = await client.post("/admin/runtime-config/worker_max_jobs", data={"value": "not-a-number"})

    assert response.status_code == 400
    assert await session.get(RuntimeConfigOverride, "worker_max_jobs") is None
    rejected = [entry for entry in logs if entry.get("event") == "phaze.runtime_config_admin override rejected"]
    assert rejected


@pytest.mark.asyncio
async def test_post_unknown_key_is_rejected_with_nothing_stored(client: AsyncClient, session: AsyncSession) -> None:
    response = await client.post("/admin/runtime-config/not_a_real_key", data={"value": "1"})

    assert response.status_code == 400
    assert (await session.execute(select(RuntimeConfigOverride))).scalars().all() == []


@pytest.mark.asyncio
async def test_post_a_restart_only_key_is_rejected_with_nothing_stored(client: AsyncClient, session: AsyncSession) -> None:
    response = await client.post("/admin/runtime-config/database_url", data={"value": "postgresql://x"})

    assert response.status_code == 400
    assert "requires restart" in response.text or "requires restart" in response.json().get("detail", "")
    assert (await session.execute(select(RuntimeConfigOverride))).scalars().all() == []


@pytest.mark.asyncio
async def test_delete_clears_an_override_and_falls_back_to_the_next_layer(client: AsyncClient, session: AsyncSession) -> None:
    await client.post("/admin/runtime-config/worker_max_jobs", data={"value": "12"})
    default_value = 8  # ControlSettings' own default for worker_max_jobs.

    response = await client.delete("/admin/runtime-config/worker_max_jobs")

    assert response.status_code == 200
    assert await session.get(RuntimeConfigOverride, "worker_max_jobs") is None
    assert get_runtime_config_store().current().worker_max_jobs == default_value
    assert get_runtime_config_store().snapshot().sources["worker_max_jobs"] != "override"


@pytest.mark.asyncio
async def test_delete_an_absent_override_is_a_no_op(client: AsyncClient) -> None:
    response = await client.delete("/admin/runtime-config/worker_max_jobs")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_delete_a_restart_only_key_is_rejected(client: AsyncClient) -> None:
    response = await client.delete("/admin/runtime-config/database_url")

    assert response.status_code == 400
