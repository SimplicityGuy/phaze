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


async def test_a_pool_size_reload_with_a_slot_held_still_resizes_the_telemetry_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A held telemetry slot must not make the telemetry pool's applier a silent no-op
    (phaze-mvq8z.19 finding 3). Slots are held whenever analysis is running, which is exactly when
    an operator retunes the pool: if the limiter admits 5 while the slot pool stays at 3, children 4
    and 5 get no slot and export under the shared identity. Both must reach the new size, and the
    held slot must still never be reissued."""
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
    pool = telemetry_slots.default_pool()
    held = pool.acquire()
    assert held is not None

    runtime_toml.write_text("worker_process_pool_size = 5\n", encoding="utf-8")
    result = await store.reload("file")

    assert result.outcome == "applied", result.error
    assert limiter.total_tokens == 5, "the limiter has no in-flight-holding concept of its own; it always resizes"
    assert telemetry_slots.default_pool().size == 5, "a held slot made the telemetry pool decline the resize"
    issued = [telemetry_slots.default_pool().acquire() for _ in range(4)]
    assert held not in issued, "the held slot was reissued"
    assert sorted(slot for slot in issued if slot is not None) == [1, 2, 3, 4]
