"""``phaze.tasks.agent_worker`` startup wiring for the live-resizable analysis pool (phaze-mvq8z.7).

Proves the WIRING agent_worker's ``startup()`` owns: ``ctx["analysis_semaphore"]`` is a
:class:`~phaze.services.resizable_limiter.ResizableLimiter` sized from the runtime-config
snapshot (not the static settings), and BOTH it and the telemetry slot pool are registered as
reload appliers, so a live ``worker_process_pool_size`` reload (docs/design/0019-runtime-config-hot-reload.md §5/§7) reaches both
without a restart. The limiter's own grow/shrink/FIFO semantics are unit-tested in
``tests/shared/services/test_resizable_limiter.py``; the real-subprocess proof that a shrink
never kills an in-flight child lives in
``tests/analyze/services/pipeline/test_analysis_exec.py``. This file is the glue in between:
does a live reload actually reach the objects agent_worker built.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _set_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minimum env ``AgentSettings()`` needs to validate -- set BEFORE constructing one,
    including the store's own settings, which every test below builds by hand."""
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-000000000000")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test-id")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/var/empty")
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@app-server.example:5432/phaze")


async def _start(monkeypatch: pytest.MonkeyPatch, store: Any) -> dict[str, Any]:
    """Boot ``agent_worker.startup()`` against ``store``, with every heavy external mocked out --
    the same shape ``test_agent_startup_banner.py`` uses, minus the banner assertions this file
    has no interest in. Callers set the env (:func:`_set_agent_env`) before this, since they
    also construct an ``AgentSettings`` of their own for the store."""
    from phaze.config import AgentSettings
    import phaze.tasks.agent_worker as aw

    fake_cfg = AgentSettings()
    monkeypatch.setattr(aw, "get_settings", lambda: fake_cfg)
    monkeypatch.setattr(aw, "get_runtime_config_store", lambda: store)

    fake_identity = MagicMock(agent_id="test-id")
    fake_client = AsyncMock()
    fake_client.whoami = AsyncMock(return_value=fake_identity)
    fake_client.close = AsyncMock()
    monkeypatch.setattr(aw, "construct_agent_client", lambda _cfg: fake_client)
    monkeypatch.setattr(pathlib.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(aw, "ensure_models_present", lambda _models_dir: None)
    monkeypatch.setattr(aw, "_wait_for_queue_ready", AsyncMock())

    ctx: dict[str, Any] = {}
    await aw.startup(ctx)
    return ctx


async def test_startup_builds_a_resizable_limiter_sized_from_the_runtime_config_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``analysis_semaphore`` is a live-resizable limiter, sized from the RELOADABLE snapshot
    (already reloaded from ``startup`` before the limiter is built), not a bare ``asyncio.Semaphore``
    off the static settings."""
    from phaze.config import AgentSettings
    from phaze.runtime_config import RuntimeConfigStore
    from phaze.services.resizable_limiter import ResizableLimiter
    from phaze.telemetry import slots as telemetry_slots

    _set_agent_env(monkeypatch)
    telemetry_slots._reset_for_tests()
    store = RuntimeConfigStore(
        AgentSettings(worker_process_pool_size=3),
        runtime_toml=tmp_path / "runtime.toml",
        physical_cores=lambda: 64,  # never let this test's assertions depend on the host's core count
    )

    ctx = await _start(monkeypatch, store)

    limiter = ctx["analysis_semaphore"]
    assert isinstance(limiter, ResizableLimiter)
    assert limiter.total_tokens == 3
    assert telemetry_slots.default_pool().size == 3


async def test_a_live_pool_size_reload_resizes_both_the_limiter_and_the_telemetry_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per docs/design/0019-runtime-config-hot-reload.md §5/§7, the wiring this bead adds: a ``worker_process_pool_size`` reload must
    reach the SAME limiter object ``ctx["analysis_semaphore"]`` holds, and
    ``telemetry_slots.default_pool()``, without a restart -- growing in this case, which the
    limiter's own unit tests already prove admits waiting work immediately; here the question
    is only whether agent_worker's registration actually delivers the new value to both."""
    from phaze.config import AgentSettings
    from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore
    from phaze.telemetry import slots as telemetry_slots

    _set_agent_env(monkeypatch)
    telemetry_slots._reset_for_tests()
    runtime_toml = tmp_path / RUNTIME_TOML_NAME
    store = RuntimeConfigStore(
        AgentSettings(worker_process_pool_size=3),
        runtime_toml=runtime_toml,
        physical_cores=lambda: 64,
    )

    ctx = await _start(monkeypatch, store)
    limiter = ctx["analysis_semaphore"]
    assert limiter.total_tokens == 3
    assert telemetry_slots.default_pool().size == 3

    runtime_toml.write_text("worker_process_pool_size = 5\n", encoding="utf-8")
    result = await store.reload("file")

    assert result.outcome == "applied", result.error
    assert limiter.total_tokens == 5, "the reload never reached the SAME limiter object startup() built"
    assert telemetry_slots.default_pool().size == 5


async def test_a_pool_size_reload_that_declines_leaves_both_where_they_were(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A held telemetry slot makes ``set_default_pool_size`` decline (its own pre-existing
    safety net, phaze.telemetry.slots): the reload still succeeds overall (`outcome="applied"`,
    since the limiter's OWN applier ran and changed something), but the pool stays exactly as it
    was rather than silently reissuing a slot a child still holds."""
    from phaze.config import AgentSettings
    from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore
    from phaze.telemetry import slots as telemetry_slots

    _set_agent_env(monkeypatch)
    telemetry_slots._reset_for_tests()
    runtime_toml = tmp_path / RUNTIME_TOML_NAME
    store = RuntimeConfigStore(
        AgentSettings(worker_process_pool_size=3),
        runtime_toml=runtime_toml,
        physical_cores=lambda: 64,
    )

    ctx = await _start(monkeypatch, store)
    limiter = ctx["analysis_semaphore"]
    held = telemetry_slots.default_pool().acquire()
    assert held is not None

    runtime_toml.write_text("worker_process_pool_size = 5\n", encoding="utf-8")
    result = await store.reload("file")

    assert result.outcome == "applied", result.error
    assert limiter.total_tokens == 5, "the limiter has no in-flight-holding concept of its own; it always resizes"
    assert telemetry_slots.default_pool().size == 3, "a pool with a slot held must decline the resize, not reissue it"
